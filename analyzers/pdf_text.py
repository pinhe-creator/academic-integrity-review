"""
PDF / 文本 / 图像 analyzer
==========================
对论文 PDF（或代码/文本文件）做几类轻量检测：

1) AI 残留语句：未经编辑就粘贴进来的大模型口头禅。
2) 扭曲术语（tortured phrases）：洗稿工具把标准术语换成怪异近义词的痕迹。
3) 图像疑似重复（v2，修了三类假阳性）：
   - 跳过软掩膜（SMask）等附属对象；
   - 按【内容字节】去重——同一张图被多次嵌入只算一次，根治“把同一张图比成相似两张”；
   - 过滤【近乎纯色】的图（平均哈希对空白图不可靠，是 dist=3/7 噪声的主因）；
   - 用更稳健的 dHash；
   - 把相似的两张图【缩略图直接放进卡片】，并精确到“第几页第几张”，便于人工核对；
   - 区分“同一张图跨页复用”与“相似但不完全相同的两张”。
   说明：仍是【基础启发式】，远不及 ImageTwin/Proofig，仅供人工复核线索。

安全：本工具【绝不执行】上传的代码，只当文本扫描。
依赖：pymupdf(fitz), pillow, numpy
"""

import os
import re
import base64
import hashlib
from io import BytesIO
from collections import OrderedDict

import fitz  # PyMuPDF
import numpy as np
from PIL import Image
from .base import AnalysisResult

# ---- AI 残留语句（大小写不敏感）----
AI_RESIDUE_PATTERNS = [
    r"as an? (?:ai|artificial intelligence) language model",
    r"as a large language model",
    r"i(?:'m| am) (?:sorry,? but )?(?:unable|not able|cannot|can't) (?:to )?(?:fulfill|provide|complete|comply)",
    r"as of my last (?:knowledge )?(?:update|training)",
    r"i (?:do not|don't) have (?:access to|the ability)",
    r"regenerate response",
    r"certainly! (?:here (?:is|are)|below)",
    r"i hope this helps",
    r"please note that as an ai",
]

# ---- 扭曲术语词典：怪异写法 -> 正确术语（可持续扩充）----
TORTURED_PHRASES = {
    "flag to clamor": "signal to noise",
    "counterfeit consciousness": "artificial intelligence",
    "haphazardly woods": "random forest",
    "leftover vitality": "residual energy",
    "feeble counterfeit": "weak artificial",
    "bosom malignant growth": "breast cancer",
    "gigantic information": "big data",
    "convolutional brain organization": "convolutional neural network",
    "profound learning": "deep learning",
    "bolster vector machine": "support vector machine",
    "mean square mistake": "mean square error",
    "irregular get to memory": "random access memory",
    "lung disease": "lung cancer",
}

# ---- 图像参数 ----
MIN_IMG_SIZE = 128        # 边长小于此的忽略（多为 logo/图标）
HASH_SIDE = 16            # dHash：比较相邻像素，16x17 -> 256 bit
MAX_HAMMING = 8           # 海明距离 <= 此值视为“高度相似”（保守）
MIN_STD = 12.0            # 灰度标准差低于此 -> 近乎纯色，哈希不可靠，跳过
THUMB_MAX = 180           # 缩略图最大边长
MAX_PAIRS = 12            # 最多报告多少对相似图
MAX_REUSE = 8             # 最多报告多少条跨页复用

TEXT_EXT = {".py", ".r", ".m", ".cpp", ".c", ".h", ".java", ".js", ".ipynb",
            ".txt", ".tex", ".csv", ".md"}


# ============ 文本类检测 ============
def _scan_text(text: str, where_fn) -> list:
    found = []
    low = text.lower()
    for pat in AI_RESIDUE_PATTERNS:
        for m in re.finditer(pat, low):
            snippet = text[max(0, m.start() - 40): m.end() + 40].replace("\n", " ").strip()
            found.append(dict(
                kind="text_pattern",
                title="疑似未删除的 AI 生成残留语句",
                detail=f"匹配到模型常见用语：…{snippet}… 这类句子若出现在正文，"
                       f"通常意味着文本直接来自大模型且未经编辑。",
                locus=where_fn(m.start()),
            ))
    for bad, good in TORTURED_PHRASES.items():
        idx = low.find(bad)
        if idx != -1:
            found.append(dict(
                kind="text_pattern",
                title="疑似扭曲术语（tortured phrase）",
                detail=f"出现「{bad}」，标准术语应为「{good}」。把通用术语替换成怪异近义词"
                       f"是洗稿/论文工厂的典型痕迹。",
                locus=where_fn(idx),
            ))
    return found


# ============ 图像哈希与工具 ============
def _gray(im: Image.Image, size) -> np.ndarray:
    return np.asarray(im.convert("L").resize(size), dtype=np.float64)


def _dhash(im: Image.Image) -> np.ndarray:
    """差异哈希：比较每行相邻像素，比平均哈希更稳健。"""
    g = _gray(im, (HASH_SIDE + 1, HASH_SIDE))
    return (g[:, 1:] > g[:, :-1]).flatten()


def _entropy_ok(im: Image.Image) -> bool:
    """信息量是否足够（近乎纯色的图哈希不可靠，应排除）。
    在 64x64 尺度上判断：真实图表的低频结构能保留，纯空白/纯色则方差极低。"""
    return float(_gray(im, (64, 64)).std()) >= MIN_STD


def _thumb(im: Image.Image) -> str:
    t = im.convert("RGB")
    t.thumbnail((THUMB_MAX, THUMB_MAX))
    buf = BytesIO()
    t.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _hamming(a, b) -> int:
    return int(np.count_nonzero(a != b))


def _extract_unique_images(doc):
    """
    抽取“去重后”的图片：按内容字节去重，跳过软掩膜，过滤近乎纯色。
    返回 list[{"occ":[(page,idx),...], "im":PIL, "dh":ndarray}]，按首次出现排序。
    occ 记录这张（字节相同的）图片在文档中出现的所有 (页码, 该页第几张) 位置。
    """
    by_content = OrderedDict()
    page_count = {}
    for pno in range(len(doc)):
        infos = doc.get_page_images(pno, full=True)
        smasks = {info[1] for info in infos if info[1]}  # 软掩膜的 xref，跳过
        for info in infos:
            xref = info[0]
            if xref in smasks:
                continue
            try:
                raw = doc.extract_image(xref)["image"]
            except Exception:
                continue
            chash = hashlib.md5(raw).hexdigest()
            if chash in by_content:  # 同一张图再次出现，只记录位置
                page_count[pno + 1] = page_count.get(pno + 1, 0) + 1
                by_content[chash]["occ"].append((pno + 1, page_count[pno + 1]))
                continue
            try:
                im = Image.open(BytesIO(raw))
            except Exception:
                continue
            if min(im.size) < MIN_IMG_SIZE or not _entropy_ok(im):
                continue
            page_count[pno + 1] = page_count.get(pno + 1, 0) + 1
            by_content[chash] = {"occ": [(pno + 1, page_count[pno + 1])],
                                 "im": im.copy(), "dh": _dhash(im)}
    return list(by_content.values())


# ============ 主入口 ============
def analyze_pdf(path: str, filename: str) -> AnalysisResult:
    res = AnalysisResult()
    text_hits, page_count = [], 0
    try:
        doc = fitz.open(path)
        page_count = len(doc)

        # 文本：逐页扫描，定位到页码
        for pno in range(page_count):
            hits = _scan_text(doc[pno].get_text(),
                              where_fn=lambda pos, p=pno: f"{filename} · 第 {p+1} 页")
            text_hits.extend(hits)

        imgs = _extract_unique_images(doc)

        # A) 同一张图片在多页重复出现（同页内的字节级重复视为排版，不报）
        reuse = 0
        for g in imgs:
            pages = sorted({p for p, _ in g["occ"]})
            if len(pages) >= 2 and reuse < MAX_REUSE:
                reuse += 1
                res.add(
                    source="pdf_image", source_label="图像重复检测", kind="image_duplicate",
                    title="同一张图片在多页重复出现",
                    detail=f"完全相同的一张图片出现在第 {', '.join(map(str, pages))} 页。"
                           f"跨页复用同一图片有时是正常排版（如示意图），有时是图表复制，"
                           f"请结合下方缩略图与上下文判断。",
                    status="informational",
                    locus=f"{filename} · 第 {', '.join(map(str, pages))} 页",
                    images=[{"label": f"出现于第 {', '.join(map(str, pages))} 页",
                             "data_uri": _thumb(g["im"])}],
                )

        # B) 相似但不完全相同的两张图（精确到第几页第几张，并附两图缩略）
        reported = 0
        for i in range(len(imgs)):
            for j in range(i + 1, len(imgs)):
                if reported >= MAX_PAIRS:
                    break
                if len(imgs[i]["dh"]) != len(imgs[j]["dh"]):
                    continue
                dist = _hamming(imgs[i]["dh"], imgs[j]["dh"])
                if dist <= MAX_HAMMING:
                    pa, pb = imgs[i]["occ"][0], imgs[j]["occ"][0]
                    same = pa[0] == pb[0]
                    where = ("同一页内，可能是相关子图，也可能是局部复制粘贴"
                             if same else "位于不同页")
                    reported += 1
                    res.add(
                        source="pdf_image", source_label="图像重复检测", kind="image_duplicate",
                        title="发现高度相似（但不完全相同）的两张图片",
                        detail=f"第 {pa[0]} 页第 {pa[1]} 张 与 第 {pb[0]} 页第 {pb[1]} 张，"
                               f"感知哈希海明距离 {dist}（越小越像，≤{MAX_HAMMING} 视为高度相似）。"
                               f"{where}。请对比下方两图确认是否为不当复用/拼接。",
                        status="informational",
                        locus=f"{filename} · 第{pa[0]}页第{pa[1]}张 / 第{pb[0]}页第{pb[1]}张",
                        images=[{"label": f"第{pa[0]}页 第{pa[1]}张", "data_uri": _thumb(imgs[i]["im"])},
                                {"label": f"第{pb[0]}页 第{pb[1]}张", "data_uri": _thumb(imgs[j]["im"])}],
                    )
        doc.close()
    except Exception as e:
        res.add(
            source="pdf_text", source_label="PDF 文本/图像", kind="lookup_skipped",
            title="PDF 读取失败", detail=f"无法解析「{filename}」：{e}",
            status="informational", locus=filename,
        )
        return res

    for kw in text_hits:
        res.add(source="pdf_text", source_label="PDF 文本/图像", status="informational", **kw)

    if not res.items:
        res.add(
            source="pdf_text", source_label="PDF 文本/图像", kind="lookup_clean",
            title="未发现可疑文本模式或重复图像",
            detail=f"已扫描「{filename}」共 {page_count} 页（AI 残留语句、扭曲术语；图像去重后做感知哈希比对，"
                   f"已排除软掩膜与近乎纯色图），未触发预设规则。仅代表这几项自动检测未发现异常。",
            status="clean", locus=filename,
        )
    return res


def analyze_textfile(path: str, filename: str) -> AnalysisResult:
    """代码/文本文件：只扫 AI 残留 + 扭曲术语，绝不执行。"""
    res = AnalysisResult()
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
    except Exception as e:
        res.add(source="pdf_text", source_label="代码/文本", kind="lookup_skipped",
                title="文件读取失败", detail=str(e), status="informational", locus=filename)
        return res

    hits = _scan_text(text, where_fn=lambda pos: f"{filename}（字符位置 {pos}）")
    if not hits:
        res.add(source="pdf_text", source_label="代码/文本", kind="lookup_clean",
                title="未发现可疑文本模式",
                detail=f"已扫描「{filename}」的 AI 残留语句与扭曲术语，未发现异常。"
                       f"注意：本工具不会运行代码；代码与论文结论是否一致，仍需人工尝试复现。",
                status="clean", locus=filename)
    for kw in hits:
        res.add(source="pdf_text", source_label="代码/文本", status="informational", **kw)
    return res


def is_textfile(filename: str) -> bool:
    return os.path.splitext(filename)[1].lower() in TEXT_EXT
