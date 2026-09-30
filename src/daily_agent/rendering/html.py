from __future__ import annotations
from daily_agent.source_screenshots import screenshot_html

from datetime import date
from html import escape
from urllib.parse import urlparse

from daily_agent.feedback.server import feedback_form_action
from daily_agent.insights import build_daily_insights
from daily_agent.rendering.composition import featured_keys, introduction, paper_paragraphs
from daily_agent.rendering.notes import reading_label, card_gaps, must_read, result_conditions
from daily_agent.models import ApprovedItem, RunStatus
from daily_agent.rendering.markdown import _citation_context_text, _list_value, _paper_time, _prefix_label, _recommendation_reason, _tags, _value


def render_daily_html(items: list[ApprovedItem], run_date: date, status: RunStatus, insight_config: dict | None = None, writing_config: dict | None = None) -> str:
    ranks = {item.key: index for index, item in enumerate(items, start=1)}
    papers = [item for item in items if item.item_type == "paper"]
    repos = [item for item in items if item.item_type == "repo"]
    sections = [
        f"<h1>Daily Agent 日报｜{_text(run_date.isoformat())}</h1>",
        f'<p class="issue-intro">{_text(introduction(items, writing_config))}</p>',
        _render_must_read(items),
    ]
    if _insights_enabled_for_report(insight_config):
        sections.append(_render_insights(items, insight_config))
    sections.extend([
        _render_papers(papers, ranks, run_date, writing_config),
        _render_repos(repos, ranks, run_date),
        _render_status(status),
    ])
    return "\n".join(sections).rstrip() + "\n"


def _render_must_read(items: list[ApprovedItem]) -> str:
    items = [i for i in items if must_read(i)]
    lines = ['<section class="must-read">', "<h2>今日必看</h2>"]
    if not items:
        lines.append("<p>今日没有筛选出足够高质量的内容。</p>")
    else:
        lines.append("<ul>")
        for item in items[:3]:
            lines.append(f"<li>{_text(_prefix_label(item))}{_safe_link(item.title, item.url)}</li>")
        lines.append("</ul>")
    lines.append("</section>")
    return "\n".join(lines)


def _render_insights(items: list[ApprovedItem], insight_config: dict | None = None) -> str:
    lines = ['<section class="daily-insights">', "<h2>今日洞察</h2>", "<ul>"]
    for insight in build_daily_insights(items, settings=insight_config):
        lines.append(f"<li>{_text(insight)}</li>")
    lines.extend(["</ul>", "</section>"])
    return "\n".join(lines)


def _insights_enabled_for_report(insight_config: dict | None) -> bool:
    if not insight_config:
        return True
    return bool(insight_config.get("enabled", True) and insight_config.get("include_in_reports", True))


def _render_papers(items: list[ApprovedItem], ranks: dict[str, int], run_date: date, writing_config: dict | None = None) -> str:
    lines = ['<section class="papers">', "<h2>最新论文</h2>"]
    if not items:
        return "\n".join(lines + ["<p>今日未筛选出论文条目。</p>", "</section>"])
    featured = featured_keys(items, writing_config)
    for item in items:
        material = item.material
        rank = ranks.get(item.key)
        mode = "重点解读" if item.key in featured else "简讯"
        lines.extend(['<article class="report-item paper">',
                      f"<h3>{rank}. {_text(_prefix_label(item))}{_text(item.title)}</h3>",
                      f'<p class="paper-meta">{mode} · {_text(_paper_time(material))}</p>'])
        for paragraph in paper_paragraphs(item, item.key in featured):
            label = f"<strong>{_text(paragraph.label)}：</strong>" if paragraph.label else ""
            lines.append(f'<p class="paper-paragraph">{label}{_text(paragraph.text)}</p>')
        lines.extend([f'<p class="reading-status">阅读状态：{_text(reading_label(material))}；置信度 {_text(item.final_fields.get("confidence", "low"))}</p>',
                      f'<p class="evidence-gaps">证据缺口：{_text(card_gaps(material))}</p>'])
        lines.append(screenshot_html(material, preview=True))
        links = [_safe_link("原文", material.url)]
        if material.pdf_url:
            links.append(_safe_link("PDF", material.pdf_url))
        if material.raw.get('reading_note_html_url'):
            links.append(_local_note_link(material.raw['reading_note_html_url']))
        lines.append('<p class="reading-links">' + ' / '.join(links) + '</p>')
        lines.extend(['<details class="paper-details"><summary>资料索引与反馈</summary><dl>',
                      _field("来源/时间", _paper_time(material)),
                      _field("入选理由", _recommendation_reason(material)),
                      _field("标签", _tags(item)),
                      _field("引用脉络", _citation_context_text(material)) if _citation_context_text(material) else "",
                      f"<dt>链接</dt><dd>{_paper_links(material)}</dd>",
                      _field("反馈编号", f"第 {rank} 条"),
                      f"<dt>反馈</dt><dd>{_feedback_html(run_date, rank)}</dd>",
                      '</dl></details></article>'])
    return "\n".join(lines + ['</section>'])


def _render_repos(items: list[ApprovedItem], ranks: dict[str, int], run_date: date) -> str:
    lines = ['<section class="repos">', "<h2>GitHub 项目</h2>"]
    if not items:
        lines.append("<p>今日未筛选出 GitHub 项目。</p>")
        lines.append("</section>")
        return "\n".join(lines)
    for item in items:
        fields = item.final_fields
        material = item.material
        rank = ranks.get(item.key)
        prefix = f"{rank}. " if rank else ""
        lines.extend(
            [
                '<article class="report-item repo">',
                f"<h3>{_text(prefix)}{_text(_prefix_label(item))}{_text(item.title)}</h3>",
                "<dl>",
                _field("它是什么", _value(fields.get("what_it_is"))),
                _field("核心能力", _value(fields.get("core_capabilities"))),
                _field("典型使用场景", _value(fields.get("typical_use_cases"))),
                _field("架构/API", _value(fields.get("architecture_or_api"))),
                _field("成熟度信号", _value(fields.get("maturity_signal"))),
                _field("可复用点", _value(fields.get("reusable_point"))),
                _field("stars/language/updated_at", f"{material.stars or 0} / {material.language or 'unknown'} / {material.source_updated_at or 'unknown'}"),
                _field("入选理由", _recommendation_reason(material)),
                _field("标签", _tags(item)),
                f"<dt>链接</dt><dd>{_safe_link(material.url, material.url)}</dd>",
            ]
        )
        if rank:
            lines.append(_field("反馈编号", f"第 {rank} 条"))
            lines.append(f"<dt>反馈</dt><dd>{_feedback_html(run_date, rank)}</dd>")
        lines.extend(["</dl>", "</article>"])
    lines.append("</section>")
    return "\n".join(lines)


def _render_status(status: RunStatus) -> str:
    lines = ['<section class="run-status">', "<h2>运行状态</h2>", "<ul>"]
    lines.append(f"<li><strong>数据源：</strong>{_text(status.source_line())}</li>")
    lines.append(f"<li><strong>生成时间：</strong>{_text(status.generated_at)}</li>")
    if status.delivery:
        delivery = "成功" if status.delivery.ok else "失败"
        detail = status.delivery.final_mode
        if status.delivery.fallback_used:
            detail += "（已兜底）"
        if status.delivery.document_url:
            detail += f" / {status.delivery.document_url}"
        lines.append(f"<li><strong>投递：</strong>{_text(delivery)}（{_safe_delivery_detail(detail)}）</li>")
    if status.fallback:
        lines.append(f"<li><strong>Fallback：</strong>{_text(status.fallback)}</li>")
    if status.errors:
        lines.append(f"<li><strong>错误提醒：</strong>{_text('；'.join(status.errors))}</li>")
    lines.extend(["</ul>", "</section>"])
    return "\n".join(lines)


def _paper_links(material) -> str:
    links = [_safe_link("abs", material.url)]
    if material.pdf_url:
        links.append(_safe_link("PDF", material.pdf_url))
    if material.raw.get("local_pdf_report_url"):
        links.append(_safe_local_pdf_link("本地PDF", material.raw["local_pdf_report_url"]))
    if getattr(material, "doi", None):
        links.append(_safe_link("DOI", f"https://doi.org/{material.doi}"))
    if "openalex" in getattr(material, "source_aliases", {}):
        links.append(_safe_link("OpenAlex", material.raw.get("openalex_url") or material.url))
    if material.raw.get("semantic_scholar_url"):
        links.append(_safe_link("Semantic Scholar", material.raw["semantic_scholar_url"]))
    if material.raw.get("google_scholar_url"):
        links.append(_safe_link("Google Scholar", material.raw["google_scholar_url"]))
    if material.raw.get("google_scholar_cited_by_url"):
        links.append(_safe_link("Scholar cited by", material.raw["google_scholar_cited_by_url"]))
    if material.raw.get("google_scholar_related_url"):
        links.append(_safe_link("Scholar related", material.raw["google_scholar_related_url"]))
    if material.raw.get("google_scholar_versions_url"):
        links.append(_safe_link("Scholar versions", material.raw["google_scholar_versions_url"]))
    if material.raw.get("google_scholar_bibtex_url"):
        links.append(_safe_link("Scholar BibTeX", material.raw["google_scholar_bibtex_url"]))
    if material.raw.get("google_scholar_endnote_url"):
        links.append(_safe_link("Scholar EndNote", material.raw["google_scholar_endnote_url"]))
    if material.raw.get("google_scholar_refman_url"):
        links.append(_safe_link("Scholar RefMan", material.raw["google_scholar_refman_url"]))
    if material.raw.get("google_scholar_refworks_url"):
        links.append(_safe_link("Scholar RefWorks", material.raw["google_scholar_refworks_url"]))
    if material.raw.get("unpaywall_url"):
        links.append(_safe_link("Unpaywall", material.raw["unpaywall_url"]))
    if material.raw.get("dblp_url"):
        links.append(_safe_link("DBLP", material.raw["dblp_url"]))
    if material.raw.get("core_url"):
        links.append(_safe_link("CORE", material.raw["core_url"]))
    if material.raw.get("ieee_url"):
        links.append(_safe_link("IEEE", material.raw["ieee_url"]))
    if material.raw.get("openreview_url"):
        links.append(_safe_link("OpenReview", material.raw["openreview_url"]))
    if material.raw.get("pmlr_url"):
        links.append(_safe_link("PMLR", material.raw["pmlr_url"]))
    if material.raw.get("neurips_url"):
        links.append(_safe_link("NeurIPS", material.raw["neurips_url"]))
    return " / ".join(links)


def _field(label: str, value: str) -> str:
    return f"<dt>{_text(label)}</dt><dd>{_text(value)}</dd>"


def _feedback_html(run_date: date, rank: int) -> str:
    comment_hint = f"飞书评论：第 {rank} 条不错 / 第 {rank} 条不相关"
    action = feedback_form_action()
    return (
        '<div class="feedback-panel">'
        '<div class="feedback-copy">'
        f'<span class="feedback-title">{_text(comment_hint)}</span>'
        '<span class="feedback-note">按钮反馈由 Daily Agent 本地预览服务记录</span>'
        "</div>"
        f'<form class="feedback-actions" method="post" action="{_attr(action)}">'
        f'<input type="hidden" name="date" value="{_attr(run_date.isoformat())}">'
        f'<input type="hidden" name="rank" value="{_attr(rank)}">'
        '<button type="submit" name="signal" value="like">有用</button>'
        '<button type="submit" name="signal" value="dislike">不相关</button>'
        "</form>"
        "</div>"
    )


def _safe_delivery_detail(value: str) -> str:
    if " / " not in value:
        return _text(value)
    mode, maybe_url = value.rsplit(" / ", 1)
    return f"{_text(mode)} / {_safe_link(maybe_url, maybe_url)}"


def _safe_link(label: str, url: str | None) -> str:
    if not url or not _is_safe_url(url):
        return _text(label if label else "链接不可用")
    return f'<a href="{_attr(url)}" rel="noreferrer noopener">{_text(label)}</a>'


def _safe_local_pdf_link(label: str, url: str | None) -> str:
    if not url or not _is_safe_local_pdf_url(url):
        return _text(label)
    return f'<a href="{_attr(url)}">{_text(label)}</a>'


def _is_safe_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _is_safe_local_pdf_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme or parsed.netloc:
        return False
    if not url.startswith("../data/pdfs/") or not url.endswith(".pdf"):
        return False
    tail = url[len("../data/pdfs/") :]
    return "\\" not in tail and "\x00" not in tail and ".." not in tail.split("/")


def _text(value) -> str:
    return escape(str(value), quote=False)


def _attr(value) -> str:
    return escape(str(value), quote=True)


def _local_note_link(url):
    from pathlib import PurePosixPath
    if not isinstance(url, str) or not url.startswith("notes/") or ".." in PurePosixPath(url).parts:
        return "笔记路径无效"
    return '<a href="' + escape(url, quote=True) + '">完整阅读笔记（本地）</a>'
