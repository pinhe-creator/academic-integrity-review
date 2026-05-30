"""
Unpaywall analyzer
==================
按 DOI 查询是否有【开放获取（OA）全文】，拿到 PDF 直链，从而打通闭环：
用户只给一个 DOI，工具就能自动把论文正文拉下来，喂给文本/图像检测。

重要：Unpaywall【没有 API key】，只需在请求里带上邮箱即可（用于追踪用量、出问题时通知你）。
  端点：https://api.unpaywall.org/v2/{DOI}?email=你的邮箱
  限额：每天约 10 万次调用。

请设置环境变量 UNPAYWALL_EMAIL=你的邮箱（缺省会尝试复用 CROSSREF_MAILTO）。
"""

import os
import httpx
from .base import AnalysisResult, DEFAULT_CONTACT_EMAIL
from .crossref import _normalize_doi  # 复用 DOI 规范化

# 环境变量优先；都没设则用 base.py 里写死的邮箱
EMAIL = (os.environ.get("UNPAYWALL_EMAIL", "")
         or os.environ.get("CROSSREF_MAILTO", "")
         or DEFAULT_CONTACT_EMAIL)
BASE = "https://api.unpaywall.org/v2"
TIMEOUT = 20.0


def lookup_oa(raw_doi: str):
    """
    查询开放获取情况。
    返回 (AnalysisResult, pdf_url 或 None)。pdf_url 非空时调用方可下载分析。
    """
    res = AnalysisResult()
    doi = _normalize_doi(raw_doi)
    if not doi:
        return res, None

    if not EMAIL:
        res.add(
            source="unpaywall", source_label="Unpaywall", kind="lookup_skipped",
            title="Unpaywall 未启用（需填邮箱）",
            detail="请设置环境变量 UNPAYWALL_EMAIL=你的邮箱（或复用 CROSSREF_MAILTO）。"
                   "Unpaywall 不需要 API key，只要邮箱即可。",
            status="informational",
        )
        return res, None

    try:
        with httpx.Client(timeout=TIMEOUT, headers={"User-Agent": "paper-evidence/0.1"}) as c:
            r = c.get(f"{BASE}/{doi}", params={"email": EMAIL})
            if r.status_code == 404:
                res.add(
                    source="unpaywall", source_label="Unpaywall", kind="info",
                    title="Unpaywall 未收录该 DOI",
                    detail=f"在 Unpaywall 中查不到 DOI「{doi}」的开放获取信息。",
                    status="note", url=f"https://doi.org/{doi}",
                )
                return res, None
            r.raise_for_status()
            data = r.json()
    except Exception as e:
        res.add(
            source="unpaywall", source_label="Unpaywall", kind="lookup_skipped",
            title="Unpaywall 查询失败", detail=f"请求出错：{e}", status="informational",
        )
        return res, None

    is_oa = data.get("is_oa")
    best = data.get("best_oa_location") or {}
    pdf_url = best.get("url_for_pdf")
    landing = best.get("url")

    if is_oa and pdf_url:
        res.add(
            source="unpaywall", source_label="Unpaywall", kind="info",
            title="已找到开放获取全文（PDF），将自动分析",
            detail=f"来源类型：{best.get('host_type', '未知')}，版本：{best.get('version', '未知')}。"
                   f"工具会自动下载该 PDF 并对其运行文本/图像检测。",
            status="note", url=pdf_url,
        )
        return res, pdf_url

    if is_oa and landing:
        res.add(
            source="unpaywall", source_label="Unpaywall", kind="info",
            title="找到开放获取版本，但不是 PDF 直链",
            detail="Unpaywall 给出的是落地页而非可直接下载的 PDF，已跳过自动下载。"
                   "你可以打开链接手动获取 PDF 后上传分析。",
            status="note", url=landing,
        )
        return res, None

    res.add(
        source="unpaywall", source_label="Unpaywall", kind="info",
        title="未找到开放获取全文",
        detail="该 DOI 在 Unpaywall 中没有可用的开放获取版本（可能是付费墙文章）。"
               "如需分析正文，请手动上传 PDF。",
        status="note", url=f"https://doi.org/{doi}",
    )
    return res, None


def download_pdf(url: str, max_mb: int = 50):
    """
    安全下载 PDF：限大小、限超时、校验确为 PDF。
    返回 (bytes 或 None, 错误说明 或 None)。
    """
    if not (url.startswith("http://") or url.startswith("https://")):
        return None, "链接不是 http(s)"
    try:
        with httpx.Client(timeout=30.0, follow_redirects=True,
                          headers={"User-Agent": "paper-evidence/0.1"}) as c:
            with c.stream("GET", url) as r:
                if r.status_code != 200:
                    return None, f"HTTP {r.status_code}"
                ctype = r.headers.get("content-type", "").lower()
                chunks, total = [], 0
                for chunk in r.iter_bytes():
                    total += len(chunk)
                    if total > max_mb * 1024 * 1024:
                        return None, f"超过 {max_mb}MB 上限"
                    chunks.append(chunk)
                data = b"".join(chunks)
    except Exception as e:
        return None, str(e)

    if not data[:5].startswith(b"%PDF") and "pdf" not in ctype:
        return None, "返回内容不是 PDF（可能是落地页或验证页）"
    return data, None
