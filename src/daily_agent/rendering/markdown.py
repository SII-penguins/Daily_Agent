from __future__ import annotations
from daily_agent.source_screenshots import screenshot_markdown

from datetime import date

from daily_agent.insights import build_daily_insights
from daily_agent.rendering.composition import featured_keys, introduction, paper_paragraphs
from daily_agent.rendering.notes import reading_label, card_gaps, must_read, result_conditions, assert_native_renderable
from daily_agent.models import ApprovedItem, RunStatus


def render_daily_markdown(items: list[ApprovedItem], run_date: date, status: RunStatus, insight_config: dict | None = None, writing_config: dict | None = None) -> str:
    assert_native_renderable(items)
    lines: list[str] = [f"# Daily Agent 日报｜{run_date.isoformat()}", ""]
    lines.extend([introduction(items, writing_config), ""])
    ranks = {item.key: index for index, item in enumerate(items, start=1)}
    lines.extend(_render_must_read(items))
    if _insights_enabled_for_report(insight_config):
        lines.extend(_render_insights(items, insight_config))
    papers = [item for item in items if item.item_type == "paper"]
    repos = [item for item in items if item.item_type == "repo"]
    lines.extend(_render_papers(papers, ranks, run_date, writing_config))
    lines.extend(_render_repos(repos, ranks, run_date))
    lines.extend(_render_paper_details(papers, ranks, run_date))
    lines.extend(_render_status(status))
    return "\n".join(lines).rstrip() + "\n"


def _render_must_read(items: list[ApprovedItem]) -> list[str]:
    lines = ["## 今日必看", ""]
    for item in [i for i in items if must_read(i)][:3]:
        label = _prefix_label(item)
        lines.append(f"- {label}[{item.title}]({item.url})")
    if len(lines) == 2:
        lines.append("- 今日没有筛选出足够高质量的内容。")
    lines.append("")
    return lines


def _render_insights(items: list[ApprovedItem], insight_config: dict | None = None) -> list[str]:
    lines = ["## 今日洞察", ""]
    for insight in build_daily_insights(items, settings=insight_config):
        lines.append(f"- {insight}")
    lines.append("")
    return lines


def _insights_enabled_for_report(insight_config: dict | None) -> bool:
    if not insight_config:
        return True
    return bool(insight_config.get("enabled", True) and insight_config.get("include_in_reports", True))


def _render_papers(items: list[ApprovedItem], ranks: dict[str, int], run_date: date, writing_config: dict | None = None) -> list[str]:
    assert_native_renderable(items)
    lines = ["## 最新论文", ""]
    if not items:
        return lines + ["今日未筛选出论文条目。", ""]
    featured = featured_keys(items, writing_config)
    for item in items:
        material = item.material
        rank = ranks.get(item.key)
        mode = "重点解读" if item.key in featured else "简讯"
        lines.extend([f"### {rank}. {_prefix_label(item)}{item.title}", "", f"{mode} · {_paper_time(material)}", ""])
        from daily_agent.scientific_analysis import analysis_paragraphs, GAP
        analysis = analysis_paragraphs(item)
        for key, value in analysis:
            label = {'insight': '最有价值的科学启发', 'explanation': '问题、瓶颈与必要复杂性', 'argument': '论证推进、关键证据与未解问题'}[key]
            lines.extend([f'**{label}：**{value}', ''])
        if not analysis:
            lines.extend([GAP, ''])
        for paragraph in paper_paragraphs(item, item.key in featured):
            label = f"**{paragraph.label}：**" if paragraph.label else ""
            lines.extend([label + paragraph.text, ""])
        lines.extend([f"阅读状态：{reading_label(material)}；置信度 {item.final_fields.get('confidence', 'low')}", "",
                      f"证据缺口：{card_gaps(material)}", ""])
        lines.extend([screenshot_markdown(material, preview=True), ""])
        links = [f"[原文]({material.url})"]
        if material.pdf_url:
            links.append(f"[PDF]({material.pdf_url})")
        if material.raw.get('reading_note_url'):
            links.append(f"[完整阅读笔记（本地）]({material.raw['reading_note_url']})")
        lines.extend([" / ".join(links), ""])
    return lines


def _render_paper_details(items, ranks, run_date):
    if not items:
        return []
    lines = ["## 资料索引", ""]
    for item in items:
        material = item.material
        rank = ranks[item.key]
        lines.extend([f"### 第 {rank} 条 · {item.title}", "",
                      f"- 来源/时间：{_paper_time(material)}",
                      f"- 入选理由：{_recommendation_reason(material)}",
                      f"- 标签：{_tags(item)}"])
        if _citation_context_text(material):
            lines.append(f"- 引用脉络：{_citation_context_text(material)}")
        lines.extend([f"- 链接：{' / '.join(_paper_links(material))}",
                      f"- 反馈编号：第 {rank} 条", f"- 反馈：{_feedback_text(run_date, rank)}", ""])
    return lines


def _render_repos(items: list[ApprovedItem], ranks: dict[str, int], run_date: date) -> list[str]:
    lines = ["## GitHub 项目", ""]
    if not items:
        lines.extend(["今日未筛选出 GitHub 项目。", ""])
        return lines
    for item in items:
        fields = item.final_fields
        material = item.material
        rank = ranks.get(item.key)
        prefix = f"{rank}. " if rank else ""
        lines.append(f"### {prefix}{_prefix_label(item)}{item.title}")
        lines.append(f"- 它是什么：{_value(fields.get('what_it_is'))}")
        lines.append(f"- 核心能力：{_value(fields.get('core_capabilities'))}")
        lines.append(f"- 典型使用场景：{_value(fields.get('typical_use_cases'))}")
        lines.append(f"- 架构/API：{_value(fields.get('architecture_or_api'))}")
        lines.append(f"- 成熟度信号：{_value(fields.get('maturity_signal'))}")
        lines.append(f"- 可复用点：{_value(fields.get('reusable_point'))}")
        lines.append(f"- stars/language/updated_at：{material.stars or 0} / {material.language or 'unknown'} / {material.source_updated_at or 'unknown'}")
        lines.append(f"- 入选理由：{_recommendation_reason(material)}")
        lines.append(f"- 标签：{_tags(item)}")
        lines.append(f"- 链接：[{material.url}]({material.url})")
        if rank:
            lines.append(f"- 反馈编号：第 {rank} 条")
            lines.append(f"- 反馈：{_feedback_text(run_date, rank)}")
        lines.append("")
    return lines


def _render_status(status: RunStatus) -> list[str]:
    lines = ["## 运行状态", ""]
    lines.append(f"- 数据源：{status.source_line()}")
    lines.append(f"- 生成时间：{status.generated_at}")
    if status.delivery:
        delivery = "成功" if status.delivery.ok else "失败"
        detail = status.delivery.final_mode
        if status.delivery.fallback_used:
            detail += "（已兜底）"
        if status.delivery.document_url:
            detail += f" / {status.delivery.document_url}"
        lines.append(f"- 投递：{delivery}（{detail}）")
    if status.fallback:
        lines.append(f"- Fallback：{status.fallback}")
    if status.errors:
        lines.append(f"- 错误提醒：{'；'.join(status.errors)}")
    lines.append("")
    return lines


def _prefix_label(item: ApprovedItem) -> str:
    labels = []
    if item.material.raw.get("is_historical_supplement"):
        labels.append("历史补充")
    if item.material.update_label == "version_update":
        labels.append("版本更新")
    if item.material.update_label == "major_update":
        labels.append("重大更新")
    return f"【{'/'.join(labels)}】" if labels else ""


def _paper_time(material) -> str:
    parts = [_source_label(material)]
    published_at = material.raw.get("published_at")
    if published_at:
        parts.append(f"首次发布 {published_at}")
    if material.source_updated_at:
        parts.append(f"当前版本 {material.source_updated_at}")
    return " / ".join(parts)


def _source_label(material) -> str:
    labels = {
        "arxiv": "arXiv",
        "openalex": "OpenAlex",
        "semantic_scholar": "Semantic Scholar",
        "google_scholar": "Google Scholar",
        "crossref": "Crossref",
        "core": "CORE",
        "dblp": "DBLP",
        "ieee": "IEEE",
        "openreview": "OpenReview",
        "pmlr": "PMLR",
        "neurips": "NeurIPS",
    }
    aliases = material.source_aliases or {material.source: material.key}
    ordered = [labels[source] for source in ["arxiv", "openreview", "pmlr", "neurips", "dblp", "core", "openalex", "semantic_scholar", "google_scholar", "crossref", "ieee"] if source in aliases]
    return " / ".join(ordered) if ordered else labels.get(material.source, material.source)


def _paper_links(material) -> list[str]:
    links = [f"[abs]({material.url})"]
    if material.pdf_url:
        links.append(f"[PDF]({material.pdf_url})")
    if material.raw.get("local_pdf_report_url"):
        links.append(f"[本地PDF]({material.raw['local_pdf_report_url']})")
    if material.doi:
        links.append(f"[DOI](https://doi.org/{material.doi})")
    if "openalex" in material.source_aliases:
        links.append(f"[OpenAlex]({material.raw.get('openalex_url') or material.url})")
    if material.raw.get("semantic_scholar_url"):
        links.append(f"[Semantic Scholar]({material.raw['semantic_scholar_url']})")
    if material.raw.get("google_scholar_url"):
        links.append(f"[Google Scholar]({material.raw['google_scholar_url']})")
    if material.raw.get("google_scholar_cited_by_url"):
        links.append(f"[Scholar cited by]({material.raw['google_scholar_cited_by_url']})")
    if material.raw.get("google_scholar_related_url"):
        links.append(f"[Scholar related]({material.raw['google_scholar_related_url']})")
    if material.raw.get("google_scholar_versions_url"):
        links.append(f"[Scholar versions]({material.raw['google_scholar_versions_url']})")
    if material.raw.get("google_scholar_bibtex_url"):
        links.append(f"[Scholar BibTeX]({material.raw['google_scholar_bibtex_url']})")
    if material.raw.get("google_scholar_endnote_url"):
        links.append(f"[Scholar EndNote]({material.raw['google_scholar_endnote_url']})")
    if material.raw.get("google_scholar_refman_url"):
        links.append(f"[Scholar RefMan]({material.raw['google_scholar_refman_url']})")
    if material.raw.get("google_scholar_refworks_url"):
        links.append(f"[Scholar RefWorks]({material.raw['google_scholar_refworks_url']})")
    if material.raw.get("unpaywall_url"):
        links.append(f"[Unpaywall]({material.raw['unpaywall_url']})")
    if material.raw.get("dblp_url"):
        links.append(f"[DBLP]({material.raw['dblp_url']})")
    if material.raw.get("core_url"):
        links.append(f"[CORE]({material.raw['core_url']})")
    if material.raw.get("ieee_url"):
        links.append(f"[IEEE]({material.raw['ieee_url']})")
    if material.raw.get("openreview_url"):
        links.append(f"[OpenReview]({material.raw['openreview_url']})")
    if material.raw.get("pmlr_url"):
        links.append(f"[PMLR]({material.raw['pmlr_url']})")
    if material.raw.get("neurips_url"):
        links.append(f"[NeurIPS]({material.raw['neurips_url']})")
    return links


def _recommendation_reason(material) -> str:
    breakdown = material.score_breakdown or {}
    labels = [
        ("domain", "领域匹配"),
        ("keywords", "关键词命中"),
        ("freshness", "新鲜度"),
        ("venue", "顶会/权威来源"),
        ("scholarly_impact", "引用影响"),
        ("citation_discovery", "引用邻域发现"),
        ("evidence", "元数据证据"),
        ("github_quality", "GitHub质量"),
        ("history", "更新信号"),
        ("feedback", "反馈偏好"),
    ]
    reasons = []
    if len(material.source_aliases or {}) > 1:
        reasons.append("多源元数据收录")
    if material.item_type == "paper":
        full_text_reason = "正文已分块阅读" if material.reading.get("complete") and material.paper_document.get("document_kind") == "full_text" else ""
        if full_text_reason:
            reasons.append(full_text_reason)
        citation_reason = _citation_reason((material.raw or {}).get("citation_context") or {})
        if citation_reason:
            reasons.append(citation_reason)
        if (material.raw or {}).get("google_scholar_url") or "google_scholar" in (material.source_aliases or {}):
            reasons.append("Google Scholar 可追溯")
    reasons.extend(label for key, label in labels if float(breakdown.get(key) or 0) > 0)
    if material.update_label == "version_update":
        reasons.append("版本更新")
    if material.update_label == "major_update":
        reasons.append("重大更新")
    return "、".join(_dedupe(reasons)[:8]) if reasons else "规则评分靠前"


def _full_text_reason(status: dict) -> str:
    if not status.get("sufficient_for_deep_summary"):
        return ""
    sections = {str(section).strip().lower() for section in status.get("sections_found") or []}
    labels = []
    if {"method", "methods", "methodology"} & sections:
        labels.append("方法")
    if {"result", "results", "evaluation", "experiment", "experiments"} & sections:
        labels.append("结果")
    if {"limitation", "limitations", "discussion"} & sections:
        labels.append("局限")
    if labels:
        return "全文证据覆盖" + "/".join(labels)
    return "全文证据充足"


def _citation_reason(context: dict) -> str:
    if not isinstance(context, dict):
        return ""
    cited_by = _safe_int(context.get("cited_by_count"))
    if cited_by:
        return f"引用脉络被引 {cited_by} 次"
    if context.get("citing") or context.get("referenced"):
        return "引用脉络可追溯"
    return ""


def _citation_context_text(material) -> str:
    context = (material.raw or {}).get("citation_context") or {}
    if not isinstance(context, dict) or not context:
        return ""
    parts = []
    cited_by = context.get("cited_by_count")
    if cited_by is not None:
        parts.append(f"被引 {cited_by} 次")
    citing_titles = _citation_titles(context.get("citing"))
    if citing_titles:
        parts.append(f"近期引用：{'、'.join(citing_titles)}")
    referenced_titles = _citation_titles(context.get("referenced"))
    if referenced_titles:
        parts.append(f"关键参考：{'、'.join(referenced_titles)}")
    return "；".join(parts)


def _safe_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _dedupe(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        if value and value not in seen:
            result.append(value)
            seen.add(value)
    return result


def _citation_titles(values, limit: int = 2) -> list[str]:
    if not isinstance(values, list):
        return []
    titles = []
    for value in values:
        if isinstance(value, dict) and value.get("title"):
            titles.append(str(value["title"]))
        if len(titles) >= limit:
            break
    return titles


def _tags(item: ApprovedItem) -> str:
    tags = item.material.tags[:8]
    return "、".join(tags) if tags else "未标注"


def _feedback_text(run_date: date, rank: int) -> str:
    return f"飞书评论可写「第 {rank} 条不错」或「第 {rank} 条不相关」。"


def _value(value) -> str:
    if value is None or value == "" or value == []:
        return "not_stated"
    if isinstance(value, list):
        return "；".join(str(item) for item in value) if value else "not_stated"
    return str(value)


def _list_value(value) -> str:
    if not value:
        return "not_stated"
    if isinstance(value, list):
        return "；".join(str(item) for item in value)
    return str(value)
