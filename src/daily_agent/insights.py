from __future__ import annotations

from collections import Counter
from re import findall, search, escape

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
GENERIC_TAGS = {"paper", "repo", "quantum_ai", "arxiv", "github", "openalex", "semantic_scholar", "crossref", "google_scholar", "core", "dblp", "ieee", "openreview", "pmlr", "neurips", "trending"}

# Discovery tags can be noisy (e.g. 'rag' from unrelated words). A shared
# topic needs support in the approved description, not just library metadata.
TOPIC_ALIASES = {
    'hardware_aware': ('hardware aware', 'noise aware', '硬件感知', '噪声感知'),
    'rag': ('rag', '检索增强', 'retrieval augmented'),
    'quantum_circuit': ('quantum circuit', '量子线路', '量子电路'),
    'quantum_compilation': ('quantum compilation', '量子编译'),
    'quantum_error_correction': ('quantum error correction', '量子纠错'),
}


def _supported_tags(item):
    fields = ('problem', 'method', 'key_result') if item.item_type == 'paper' else ('what_it_is', 'core_capabilities')
    text = ' '.join([item.title] + [_clean_field(item.final_fields.get(f)) for f in fields]).lower().replace('-', ' ')
    supported = set()
    for tag in item.material.tags:
        normalized = tag.strip().lower()
        if not normalized or normalized in GENERIC_TAGS:
            continue
        terms = TOPIC_ALIASES.get(normalized, (normalized.replace('_', ' '),))
        if any(search(r'(?<![a-z0-9])'+escape(term)+r'(?![a-z0-9])', text) for term in terms):
            supported.add(tag.strip())
    return supported


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
    insights.append(_citation_context_insight(papers))
    # A list of limitations without a shared, evidenced comparison is not an
    # insight; keep it in each item and in the reading notes instead.
    if effective.get("research_gap_enabled", True) and any(insights):
        insights.append(_research_gap_insight(papers))
    return [insight for insight in insights if insight][:limit] or ["今日样本不足，暂不生成跨条目洞察。"]


def _trend_insight(items: list[ApprovedItem], papers: list[ApprovedItem], repos: list[ApprovedItem]) -> str:
    tags = _top_tags(items)
    if not tags:
        return ""
    tag = tags[0]
    related = [item for item in items if tag in _supported_tags(item)]
    evidence = "、".join(f"《{item.title}》" for item in related[:3])
    return f"今日共同主题：{tag}；依据：{evidence}。仅表示今日样本共现。"


def _method_difference_insight(papers: list[ApprovedItem]) -> str:
    supported = [item for item in papers
                 if item.material.reading.get("verification", {}).get("status") == "located"
                 and "method" in item.material.reading.get("verification", {}).get("valid_fields", [])]
    topics = _top_tags(supported)
    if not topics:
        return ""
    related = [item for item in supported if topics[0] in _supported_tags(item)]
    if len(related) < 2:
        return ""
    # Keep entire approved methods: clipping can remove a negation or condition.
    return "方法对照：" + "；".join(f"《{item.title}》：{_clean_field(item.final_fields.get('method'))}" for item in related[:2]) + "。共同主题不代表实验设置可直接比较。"


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
    supported = [item for item in papers if 'limitations' in item.material.reading.get('verification', {}).get('valid_fields', [])]
    if not supported:
        return ""
    return "已报告局限：" + "；".join(f"《{item.title}》：{_clean_field(item.final_fields.get('limitations'))}" for item in supported[:2])


def _top_tags(items: list[ApprovedItem]) -> list[str]:
    counter: Counter[str] = Counter()
    for item in items:
        for tag in _supported_tags(item):
            normalized = tag.strip()
            if normalized and normalized.lower() not in GENERIC_TAGS:
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
