"""
paper-evidence 后端
===================
单进程应用：既提供网页，也提供分析接口。

启动：
    uvicorn app:app --reload
然后浏览器打开 http://127.0.0.1:8000

接口：
    GET  /            返回网页
    POST /analyze     表单：doi(可选) + files(可选，多文件)，返回证据 JSON
"""

import os
import tempfile
from pathlib import Path

# 载入项目根目录的 .env：本地可在其中配置 Upstash 等环境变量；线上仍用平台的环境变量。
# 必须在导入下面读取环境变量的模块（analyzers / usage_stats）之前调用。
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse
from typing import List, Optional

from analyzers import crossref, pubpeer, excel_stats, pdf_text, unpaywall
from analyzers.base import AnalysisResult
import usage_stats

app = FastAPI(title="paper-evidence")

STATIC_DIR = Path(__file__).parent / "static"
MAX_FILE_MB = 50  # 单文件大小上限，防止误传巨大文件


@app.get("/", response_class=HTMLResponse)
def index():
    usage_stats.increment("visits")          # 每次打开首页 +1
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/stats")
def stats():
    """只读返回统计数字（不计数），供页脚展示。"""
    return JSONResponse(usage_stats.get())


def _route_file(path: str, filename: str, result: AnalysisResult) -> None:
    """根据扩展名把文件交给对应 analyzer。"""
    low = filename.lower()
    if low.endswith((".xlsx", ".xls", ".csv")):
        result.extend(excel_stats.analyze_excel(path, filename))
    elif low.endswith(".pdf"):
        result.extend(pdf_text.analyze_pdf(path, filename))
    elif pdf_text.is_textfile(filename):
        result.extend(pdf_text.analyze_textfile(path, filename))
    else:
        result.add(
            source="dispatch", source_label="系统", kind="lookup_skipped",
            title="暂不支持的文件类型",
            detail=f"「{filename}」未被分析。当前支持：.pdf / .xlsx / .xls / .csv "
                   f"以及常见代码与文本文件。",
            status="informational", locus=filename,
        )


@app.post("/analyze")
async def analyze(
    doi: Optional[str] = Form(None),
    files: List[UploadFile] = File(default=[]),
):
    result = AnalysisResult()
    files = files or []
    had_input = bool(doi and doi.strip()) or any((uf.filename or "") for uf in files)
    user_uploaded_pdf = any(
        (uf.filename or "").lower().endswith(".pdf") for uf in files
    )

    # ---- 1. DOI 类来源 ----
    if doi and doi.strip():
        result.extend(crossref.analyze_doi(doi))
        result.extend(pubpeer.analyze_doi(doi))

        # Unpaywall：查开放获取全文；若找到 PDF 且用户没自己传 PDF，则自动下载分析
        oa_result, pdf_url = unpaywall.lookup_oa(doi)
        result.extend(oa_result)
        if pdf_url and not user_uploaded_pdf:
            data, err = unpaywall.download_pdf(pdf_url, MAX_FILE_MB)
            if data:
                with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
                    tmp.write(data)
                    oa_path = tmp.name
                try:
                    oa_res = pdf_text.analyze_pdf(oa_path, "开放获取全文.pdf")
                    # 标注来源，让用户清楚这些发现来自自动抓取的全文
                    for it in oa_res.items:
                        it.source_label = "论文全文（Unpaywall 自动获取）"
                    result.extend(oa_res)
                finally:
                    try:
                        os.unlink(oa_path)
                    except OSError:
                        pass
            else:
                result.add(
                    source="unpaywall", source_label="Unpaywall", kind="lookup_skipped",
                    title="开放获取 PDF 下载失败",
                    detail=f"未能自动获取全文：{err}。你可以打开上面的链接手动下载后上传。",
                    status="informational",
                )

    # ---- 2. 上传文件 ----
    for uf in files or []:
        if not uf.filename:
            continue
        data = await uf.read()
        if len(data) > MAX_FILE_MB * 1024 * 1024:
            result.add(
                source="dispatch", source_label="系统", kind="lookup_skipped",
                title="文件过大，已跳过",
                detail=f"「{uf.filename}」超过 {MAX_FILE_MB} MB 上限。",
                status="informational", locus=uf.filename,
            )
            continue
        # 落到临时文件再分析，分析完即删
        suffix = os.path.splitext(uf.filename)[1]
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(data)
            tmp_path = tmp.name
        try:
            _route_file(tmp_path, uf.filename, result)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    if not result.items:
        result.add(
            source="dispatch", source_label="系统", kind="lookup_skipped",
            title="没有可分析的输入",
            detail="请至少填写一个 DOI，或上传论文 PDF / 数据文件 / 代码。",
            status="informational",
        )

    if had_input:
        usage_stats.increment("analyses")     # 真正提交一次分析才 +1

    # ---- 按来源做一个纯计数的小结（不打分、只计数）----
    by_source = {}
    for it in result.items:
        by_source[it.source_label] = by_source.get(it.source_label, 0) + 1

    return JSONResponse({
        "evidence": [it.to_dict() for it in result.items],
        "summary": {"total": len(result.items), "by_source": by_source},
    })
