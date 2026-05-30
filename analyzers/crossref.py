"""
Crossref / Retraction Watch analyzer
=====================================
按 DOI 查询：这篇论文是否被撤稿 / 勘误 / 发布过关注声明。

数据来源：Crossref REST API（免费、无需 key）。
Retraction Watch 数据库自 2023 年起已并入 Crossref 并公开，撤稿/更正信息
会出现在 Crossref 记录里。本工具用两路查询并合并：
  1. GET /works/{doi}            —— 看这条记录本身是否声明它“更新了”别的文献
  2. GET /works?filter=updates:{doi} —— 反查“哪些通知（撤稿/勘误）指向了这篇论文”

请在环境变量 CROSSREF_MAILTO 里填你的邮箱（进入 Crossref 的 polite 池，更稳定）。
"""

import os
import httpx
from .base import AnalysisResult, DEFAULT_CONTACT_EMAIL

CROSSREF_BASE = "https://api.crossref.org"
# 环境变量优先；没设则用 base.py 里写死的邮箱（进入 Crossref polite 池，更稳定）
MAILTO = os.environ.get("CROSSREF_MAILTO", "") or DEFAULT_CONTACT_EMAIL
TIMEOUT = 20.0

# Crossref 的 update-type → (中文 kind 说明, 我们的 status)
UPDATE_TYPE_MAP = {
    "retraction":            ("retraction", "retracted"),
    "withdrawal":            ("retraction", "retracted"),
    "removal":               ("retraction", "retracted"),
    "correction":            ("correction", "resolved"),
    "corrigendum":           ("correction", "resolved"),
    "erratum":               ("correction", "resolved"),
    "addendum":              ("correction", "resolved"),
    "expression_of_concern": ("expression_of_concern", "open"),
    "expressionofconcern":   ("expression_of_concern", "open"),
}


def _normalize_doi(doi: str) -> str:
    doi = doi.strip()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:", "DOI:"):
        if doi.lower().startswith(prefix.lower()):
            doi = doi[len(prefix):]
    return doi.strip().lower()


def _params() -> dict:
    return {"mailto": MAILTO} if MAILTO else {}


def analyze_doi(raw_doi: str) -> AnalysisResult:
    res = AnalysisResult()
    doi = _normalize_doi(raw_doi)
    if not doi:
        return res

    found_any = False
    title_hint = None

    with httpx.Client(timeout=TIMEOUT, headers={"User-Agent": "paper-evidence/0.1"}) as client:
        # ---- 1. 读取论文本身的记录 ----
        try:
            r = client.get(f"{CROSSREF_BASE}/works/{doi}", params=_params())
            if r.status_code == 200:
                msg = r.json().get("message", {})
                titles = msg.get("title") or []
                title_hint = titles[0] if titles else None
                # 这条记录是否“更新了”别的文献（说明它本身可能是一则勘误/撤稿通知）
                for upd in msg.get("update-to", []) or []:
                    ut = (upd.get("type") or "").lower().replace("-", "_")
                    kind, status = UPDATE_TYPE_MAP.get(ut, ("correction", "informational"))
                    found_any = True
                    res.add(
                        source="crossref",
                        source_label="Crossref / Retraction Watch",
                        kind=kind,
                        title=f"该 DOI 本身是一则 {upd.get('type', '更新')} 通知",
                        detail=f"此记录在 Crossref 中标注为对 {upd.get('DOI', '某文献')} 的 "
                               f"{upd.get('type', '更新')}。如果你查的是论文正文，请确认 DOI 是否填成了通知的 DOI。",
                        status=status,
                        date=upd.get("updated", {}).get("date-time"),
                        url=f"https://doi.org/{upd.get('DOI')}" if upd.get("DOI") else None,
                    )
            elif r.status_code == 404:
                res.add(
                    source="crossref", source_label="Crossref / Retraction Watch",
                    kind="lookup_skipped",
                    title="Crossref 未收录该 DOI",
                    detail=f"在 Crossref 中找不到 DOI「{doi}」。请检查 DOI 是否正确。",
                    status="informational",
                    url=f"https://doi.org/{doi}",
                )
        except Exception as e:
            res.add(
                source="crossref", source_label="Crossref / Retraction Watch",
                kind="lookup_skipped",
                title="Crossref 查询失败",
                detail=f"请求出错：{e}。可能是网络问题，可稍后重试。",
                status="informational",
            )

        # ---- 2. 反查：哪些通知指向了这篇论文 ----
        try:
            r = client.get(
                f"{CROSSREF_BASE}/works",
                params={**_params(), "filter": f"updates:{doi}", "rows": 20},
            )
            if r.status_code == 200:
                for item in r.json().get("message", {}).get("items", []):
                    notice_doi = item.get("DOI")
                    notice_title = (item.get("title") or ["（无标题）"])[0]
                    # 找出该通知针对本文的 update 类型
                    ut_label, kind, status = "update", "correction", "informational"
                    for upd in item.get("update-to", []) or []:
                        if _normalize_doi(upd.get("DOI", "")) == doi:
                            ut_label = upd.get("type", "update")
                            k = ut_label.lower().replace("-", "_")
                            kind, status = UPDATE_TYPE_MAP.get(k, ("correction", "informational"))
                            break
                    found_any = True
                    res.add(
                        source="crossref",
                        source_label="Crossref / Retraction Watch",
                        kind=kind,
                        title=f"检索到针对本文的「{ut_label}」记录",
                        detail=f"通知标题：{notice_title}",
                        status=status,
                        date="-".join(str(x) for x in
                                      (item.get("published", {}).get("date-parts", [[None]])[0] or [])
                                      if x is not None) or None,
                        url=f"https://doi.org/{notice_doi}" if notice_doi else None,
                    )
        except Exception:
            pass  # 第一路已给出过错误提示，这里静默

    # ---- 3. 如果两路都没查到任何问题，如实展示“查过且干净” ----
    if not found_any:
        res.add(
            source="crossref",
            source_label="Crossref / Retraction Watch",
            kind="lookup_clean",
            title="未发现撤稿 / 勘误 / 关注声明",
            detail=f"已在 Crossref（含 Retraction Watch 数据）中检索 DOI「{doi}」"
                   + (f"（{title_hint}）" if title_hint else "")
                   + "，未发现相关的撤稿、更正或关注声明记录。这不代表论文无问题，仅表示该来源暂无记录。",
            status="clean",
            url=f"https://doi.org/{doi}",
        )

    return res
