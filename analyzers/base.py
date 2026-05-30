"""
证据项（EvidenceItem）—— 所有 analyzer 的统一输出格式。

设计原则（重要）：
    本工具只“呈现证据”，不“下结论”。
    因此这里【没有】任何打分 / 造假概率 / 可信度字段。
    每一条证据都必须能追溯到来源（url）并标明其当前状态（status），
    判断权完全交给使用者。
"""

from dataclasses import dataclass, asdict, field
from typing import Optional, List


# ============================================================
#  在这里填你的邮箱（Crossref polite 池 与 Unpaywall 都会用到）
#  Unpaywall 不需要 API key，只需要一个邮箱；Crossref 填了更稳定。
#  环境变量 UNPAYWALL_EMAIL / CROSSREF_MAILTO 若设置，会优先于此。
# ============================================================
DEFAULT_CONTACT_EMAIL = "pinhechen698@gmail.com"


# kind：这条证据“是什么类型”的信号
#   retraction            撤稿
#   correction            勘误 / 更正
#   expression_of_concern 关注声明
#   pubpeer_comment        PubPeer 上的评论
#   stats_anomaly          数据统计异常（末位数字、等差、重复行……）
#   text_pattern           文本可疑模式（AI 残留、扭曲术语……）
#   image_duplicate        图像疑似重复 / 拼接
#   lookup_clean           查询了某来源但未发现记录（如实展示“查过且干净”）
#   lookup_skipped         某来源未启用（如缺 API key）
KIND_VALUES = {
    "retraction", "correction", "expression_of_concern", "pubpeer_comment",
    "stats_anomaly", "text_pattern", "image_duplicate",
    "lookup_clean", "lookup_skipped",
}


@dataclass
class EvidenceItem:
    source: str                      # 机器标识，如 "crossref" / "excel_stats"
    source_label: str                # 给人看的来源名，如 "Crossref / Retraction Watch"
    kind: str                        # 见上方 KIND_VALUES
    title: str                       # 一句话标题
    detail: str                      # 中性、如实的描述（原文照引，不做定性）
    status: str = "informational"    # open / addressed_by_author / resolved / retracted / informational / clean
    date: Optional[str] = None       # 相关日期（若有）
    url: Optional[str] = None        # 指向原始出处的链接（尽量都给）
    locus: Optional[str] = None      # 在输入中的位置，如 "Sheet1, 列 expression" / "p.7" / "Fig 3"
    images: Optional[List[dict]] = None  # 可选：内嵌缩略图 [{"label":..,"data_uri":"data:image/png;base64,.."}]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AnalysisResult:
    """单个 analyzer 跑完后的结果。"""
    items: List[EvidenceItem] = field(default_factory=list)

    def add(self, **kwargs) -> None:
        self.items.append(EvidenceItem(**kwargs))

    def extend(self, other: "AnalysisResult") -> None:
        self.items.extend(other.items)
