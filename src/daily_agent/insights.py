from __future__ import annotations

from collections import Counter
from re import findall

from daily_agent.models import ApprovedItem

METHOD_TERMS = [
    "routing",
    "synthesis",
    "compilation",
    "transpilation",
    "optimization",
    "search",
    "noise",
    "hardware",
    "qec",
    "decoder",
    "benchmark",
    "placement",
]
GENERIC_TAGS = {"paper", "repo", "quantum_ai"}


DEFAULT_INSIGHT_SETTINGS = {"enabled": True, "include_in_reports": True, "max_insights": 5, "min_items": 2, "research_gap_enabled": True}


def build_daily_insights(items: list[ApprovedItem], max_insights: int | None = None, settings: dict | None = None) -> list[str]:
    effective = {**DEFAULT_INSIGHT_SETTINGS, **(settings or {})}
    if not effective.get("enabled", True):
        return []
    limit = int(max_insights if max_insights is not None else effective.get("max_insights", 3))
    min_items = int(effective.get("min_items", 2))
    if len(items) < min_items or limit <= 0:
        return ["今日样本不足，暂不生成跨条目洞察。"]
    papers = [item for item in items if item.item_type == "paper"]
    repos = [item for item in items if item.item_type == "repo"]
    insights = [
        _trend_insight(items, papers, repos),
        _method_difference_insight(papers),
    ]
    if effective.get("research_gap_enabled", True):
        insights.append(_research_gap_insight(papers))
    insights.append(_citation_context_insight(papers))
    insights.append(_tracking_insight(items, papers))
    return [insight for insight in insights if insight][:limit] or ["今日样本不足，暂不生成跨条目洞察。"]


def _trend_insight(items: list[ApprovedItem], papers: list[ApprovedItem], repos: list[ApprovedItem]) -> str:
    tags = _top_tags(items)
    if tags:
        tag_text = "、".join(tags[:3])
        return f"共同趋势：今日条目集中在 {tag_text}，说明这些方向正在形成连续信号，而不是单篇孤立更新。"
    if len(papers) >= 2:
        return "共同趋势：今日入选论文都围绕相近研究问题展开，适合放在一起比较方法假设和实验设置。"
    if papers and repos:
        return "共同趋势：今日同时出现论文和项目更新，适合观察研究想法到工具实现之间的距离。"
    return ""


def _method_difference_insight(papers: list[ApprovedItem]) -> str:
    if len(papers) < 2:
        return ""
    terms = _top_method_terms(papers)
    if len(terms) >= 2:
        return f"方法差异：今日论文的技术路线主要分成 {terms[0]} 与 {terms[1]} 两类，精读时应比较它们各自依赖的数据、硬件假设和优化目标。"
    methods = [_clean_field(item.final_fields.get("method")) for item in papers]
    methods = [method for method in methods if method and method != "not_stated"]
    if len(methods) >= 2:
        return f"方法差异：至少两篇论文给出了不同实现路径，可重点比较“{_clip(methods[0], 42)}”和“{_clip(methods[1], 42)}”。"
    return ""


def _tracking_insight(items: list[ApprovedItem], papers: list[ApprovedItem]) -> str:
    strong_evidence = sum(1 for item in papers if (item.material.paper_text_status or {}).get("sufficient_for_deep_summary"))
    source_diversity = len({source for item in items for source in ((item.material.source_aliases or {item.source: item.key}).keys())})
    if papers and strong_evidence < len(papers):
        return "值得追踪：部分论文全文证据还不完整，后续排序应优先保留能覆盖方法、结果和局限章节的来源。"
    if source_diversity >= 3:
        return "值得追踪：今日入选内容有多源交叉信号，后续可观察这些论文是否继续获得引用、实现或 release 跟进。"
    return "值得追踪：后续重点看这些方向是否从单点结果扩展到真实硬件、公开代码或更大规模 benchmark。"


def _citation_context_insight(papers: list[ApprovedItem]) -> str:
    contexts = []
    for item in papers:
        context = (item.material.raw or {}).get("citation_context") or {}
        if not isinstance(context, dict):
            continue
        cited_by = _safe_int(context.get("cited_by_count"))
        citing_count = _list_count(context.get("citing"))
        referenced_count = _list_count(context.get("referenced"))
        if cited_by or citing_count or referenced_count:
            contexts.append((cited_by, citing_count, referenced_count, item))
    if not contexts:
        return ""
    cited_by, citing_count, referenced_count, item = max(contexts, key=lambda row: (row[0], row[1], row[2]))
    signals = [f"《{_clip(item.title, 28)}》"]
    if cited_by:
        signals.append(f"被引 {cited_by} 次")
    if citing_count:
        signals.append(f"有 {citing_count} 篇近期引用样本")
    if referenced_count:
        signals.append(f"有 {referenced_count} 篇关键参考样本")
    return "引用脉络：" + "，".join(signals) + "；精读时可先看它的上游基础和下游使用场景。"


def _research_gap_insight(papers: list[ApprovedItem]) -> str:
    if not papers:
        return ""
    limitations = [
        _clean_field(item.final_fields.get("limitations"))
        for item in papers
        if _clean_field(item.final_fields.get("limitations")) and _clean_field(item.final_fields.get("limitations")) != "not_stated"
    ]
    combined = " ".join(limitations).lower()
    if any(term in combined for term in ["小规模", "small", "simulation", "模拟", "12-qubit", "benchmark"]):
        return "研究空白：今天的论文仍普遍卡在模拟、小规模硬件或有限 benchmark，精读时应优先确认结果能否外推到真实硬件和更大规模任务。"
    if any(term in combined for term in ["not stated", "not_stated", "未说明"]):
        return "研究空白：部分论文没有充分交代局限，后续应重点追问实验边界、失败案例和与强基线的差距。"
    if limitations:
        return f"研究空白：当前条目的主要未解问题集中在“{_clip(limitations[0], 52)}”，适合整理成后续精读时的问题清单。"
    weak_evidence = [item for item in papers if not (item.material.paper_text_status or {}).get("sufficient_for_deep_summary")]
    if weak_evidence:
        return "研究空白：部分论文缺少足够全文证据，后续应先补齐方法、结果和局限章节再做强结论。"
    return "研究空白：今天的入选论文还需要继续观察是否出现跨数据集、跨硬件或公开复现实验。"


def _top_tags(items: list[ApprovedItem]) -> list[str]:
    counter: Counter[str] = Counter()
    for item in items:
        for tag in item.material.tags:
            normalized = tag.strip()
            if normalized and normalized not in GENERIC_TAGS:
                counter[normalized] += 1
    return [tag for tag, count in counter.most_common() if count >= 2]


def _top_method_terms(papers: list[ApprovedItem]) -> list[str]:
    counter: Counter[str] = Counter()
    for paper in papers:
        text = " ".join(
            _clean_field(paper.final_fields.get(name))
            for name in ["method", "technical_route", "why_it_works", "key_result"]
        ).lower()
        words = set(findall(r"[a-z][a-z0-9_-]{2,}", text))
        for term in METHOD_TERMS:
            if term in words or term in text:
                counter[term] += 1
    return [term for term, _ in counter.most_common(3)]


def _clean_field(value) -> str:
    if isinstance(value, list):
        return " ".join(str(item) for item in value)
    return str(value or "").strip()


def _safe_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _list_count(value) -> int:
    return len(value) if isinstance(value, list) else 0


def _clip(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"
