from __future__ import annotations

from datetime import date

from daily_agent.feedback.server import feedback_url
from daily_agent.models import ApprovedItem, RunStatus


def render_daily_markdown(items: list[ApprovedItem], run_date: date, status: RunStatus) -> str:
    lines: list[str] = [f"# Daily Agent 日报｜{run_date.isoformat()}", ""]
    ranks = {item.key: index for index, item in enumerate(items, start=1)}
    lines.extend(_render_must_read(items))
    papers = [item for item in items if item.item_type == "paper"]
    repos = [item for item in items if item.item_type == "repo"]
    lines.extend(_render_papers(papers, ranks, run_date))
    lines.extend(_render_repos(repos, ranks, run_date))
    lines.extend(_render_status(status))
    return "\n".join(lines).rstrip() + "\n"


def _render_must_read(items: list[ApprovedItem]) -> list[str]:
    lines = ["## 今日必看", ""]
    for item in items[:3]:
        label = _prefix_label(item)
        lines.append(f"- {label}[{item.title}]({item.url})")
    if len(lines) == 2:
        lines.append("- 今日没有筛选出足够高质量的内容。")
    lines.append("")
    return lines


def _render_papers(items: list[ApprovedItem], ranks: dict[str, int], run_date: date) -> list[str]:
    lines = ["## 最新论文", ""]
    if not items:
        lines.extend(["今日未筛选出论文条目。", ""])
        return lines
    for item in items:
        fields = item.final_fields
        material = item.material
        rank = ranks.get(item.key)
        prefix = f"{rank}. " if rank else ""
        lines.append(f"### {prefix}{_prefix_label(item)}{item.title}")
        lines.append(f"- 解决问题：{_value(fields.get('problem'))}")
        lines.append(f"- 方法/技术路线：{_value(fields.get('technical_route') or fields.get('method'))}")
        lines.append(f"- 关键步骤：{_list_value(fields.get('method_steps'))}")
        lines.append(f"- 结果/发现：{_value(fields.get('key_result'))}")
        lines.append(f"- 可能用途/影响：{_value(fields.get('possible_use_or_impact'))}")
        lines.append(f"- 局限：{_value(fields.get('limitations'))}")
        lines.append(f"- 来源/时间：{_paper_time(material)}")
        lines.append(f"- 入选理由：{_recommendation_reason(material)}")
        lines.append(f"- 标签：{_tags(item)}")
        lines.append(f"- 链接：{' / '.join(_paper_links(material))}")
        if rank:
            lines.append(f"- 反馈编号：第 {rank} 条")
            lines.append(f"- 反馈：{_feedback_text(run_date, rank)}")
        lines.append("")
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
        "crossref": "Crossref",
        "ieee": "IEEE",
        "openreview": "OpenReview",
        "pmlr": "PMLR",
        "neurips": "NeurIPS",
    }
    aliases = material.source_aliases or {material.source: material.key}
    ordered = [labels[source] for source in ["arxiv", "openreview", "pmlr", "neurips", "openalex", "semantic_scholar", "crossref", "ieee"] if source in aliases]
    return " / ".join(ordered) if ordered else labels.get(material.source, material.source)


def _paper_links(material) -> list[str]:
    links = [f"[abs]({material.url})"]
    if material.pdf_url:
        links.append(f"[PDF]({material.pdf_url})")
    if material.doi:
        links.append(f"[DOI](https://doi.org/{material.doi})")
    if "openalex" in material.source_aliases:
        links.append(f"[OpenAlex]({material.raw.get('openalex_url') or material.url})")
    if material.raw.get("semantic_scholar_url"):
        links.append(f"[Semantic Scholar]({material.raw['semantic_scholar_url']})")
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
        ("evidence", "证据完整"),
        ("github_quality", "GitHub质量"),
        ("history", "更新信号"),
        ("feedback", "反馈偏好"),
    ]
    reasons = []
    if len(material.source_aliases or {}) > 1:
        reasons.append("多源交叉验证")
    reasons.extend(label for key, label in labels if float(breakdown.get(key) or 0) > 0)
    if material.update_label == "version_update":
        reasons.append("版本更新")
    if material.update_label == "major_update":
        reasons.append("重大更新")
    return "、".join(reasons[:5]) if reasons else "规则评分靠前"


def _tags(item: ApprovedItem) -> str:
    tags = item.material.tags[:8]
    return "、".join(tags) if tags else "未标注"


def _feedback_text(run_date: date, rank: int) -> str:
    like_command = f"daily-agent feedback add --date {run_date.isoformat()} --rank {rank} --signal like"
    dislike_command = f"daily-agent feedback add --date {run_date.isoformat()} --rank {rank} --signal dislike"
    like_url = feedback_url(run_date.isoformat(), rank, "like")
    dislike_url = feedback_url(run_date.isoformat(), rank, "dislike")
    return (
        f"飞书评论可写「第 {rank} 条不错」或「第 {rank} 条不相关」；"
        f"本地按钮服务（先运行 `daily-agent feedback serve`）：[有用]({like_url}) / [不相关]({dislike_url})；"
        f"CLI：`{like_command}` / `{dislike_command}`"
    )


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
