"""
PubPeer analyzer
================
PubPeer 是发表后同行评议平台，是很多数据/图像问题最早被指出的地方
（同济 HDAC6 案就是先在 PubPeer 被匿名用户指出）。

重要：PubPeer 的 API 是【需要申请 key】的，并非完全开放。
  - 申请方式：到 https://pubpeer.com/contact 联系索取 key。
  - 不要去爬 PubPeer 网页：违反其服务条款，匿名评论的版权也有争议。

行为：
  - 若设置了环境变量 PUBPEER_API_KEY（以及 PUBPEER_API_BASE），按 DOI 查询评论；
  - 若未设置，则【不发起任何抓取】，只如实告知“未启用”，并附上官方手动检索链接，
    让使用者自己去 PubPeer 上看。
"""

import os
import httpx
from .base import AnalysisResult

API_KEY = os.environ.get("PUBPEER_API_KEY", "")
# 拿到 key 后，PubPeer 会告知确切的 endpoint 形态，按需在这里配置：
API_BASE = os.environ.get("PUBPEER_API_BASE", "")  # 例如其官方提供的查询 endpoint
TIMEOUT = 20.0


def analyze_doi(doi: str) -> AnalysisResult:
    res = AnalysisResult()
    doi = doi.strip()
    if not doi:
        return res

    search_url = f"https://pubpeer.com/search?q={doi}"

    if not (API_KEY and API_BASE):
        # 自动检索功能即将上线；当前提供手动检索入口（不抓取）
        res.add(
            source="pubpeer",
            source_label="PubPeer",
            kind="info",
            title="PubPeer 同行评议检索（即将上线）",
            detail="自动检索本论文在 PubPeer 上的同行评议讨论功能即将上线。"
                   "你可以点击右侧链接，前往 PubPeer 查看该论文是否已有相关讨论。",
            status="note",
            url=search_url,
        )
        return res

    # 已启用：按 key 查询（endpoint 形态以 PubPeer 提供的文档为准）
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
            r = client.get(
                API_BASE,
                params={"doi": doi},
                headers={"Authorization": f"Bearer {API_KEY}"},
            )
            r.raise_for_status()
            data = r.json()
            comments = data.get("comments") or data.get("data") or []
            if not comments:
                res.add(
                    source="pubpeer", source_label="PubPeer", kind="lookup_clean",
                    title="PubPeer 上未发现评论",
                    detail=f"已查询 DOI「{doi}」，PubPeer 上暂无相关评论。",
                    status="clean", url=search_url,
                )
            for c in comments:
                # 注意：评论原文照引、附链接，不做任何定性
                res.add(
                    source="pubpeer", source_label="PubPeer", kind="pubpeer_comment",
                    title="PubPeer 上存在同行评议评论",
                    detail=(c.get("text") or c.get("body") or "（见原文）")[:600],
                    status="open",
                    date=c.get("date") or c.get("created_at"),
                    url=c.get("url") or search_url,
                )
    except Exception as e:
        res.add(
            source="pubpeer", source_label="PubPeer", kind="lookup_skipped",
            title="PubPeer 查询失败",
            detail=f"请求出错：{e}。可手动检索（见链接）。",
            status="informational", url=search_url,
        )
    return res
