from __future__ import annotations

from datetime import date
from html import escape
from urllib.parse import urlparse

from daily_agent.feedback.server import feedback_form_action
from daily_agent.models import ApprovedItem, RunStatus
from daily_agent.rendering.markdown import _list_value, _paper_time, _prefix_label, _recommendation_reason, _tags, _value


def render_daily_html(items: list[ApprovedItem], run_date: date, status: RunStatus) -> str:
    ranks = {item.key: index for index, item in enumerate(items, start=1)}
    papers = [item for item in items if item.item_type == "paper"]
    repos = [item for item in items if item.item_type == "repo"]
    sections = [
        f"<h1>Daily Agent 日报｜{_text(run_date.isoformat())}</h1>",
        _render_must_read(items),
        _render_papers(papers, ranks, run_date),
        _render_repos(repos, ranks, run_date),
        _render_status(status),
    ]
    return "\n".join(sections).rstrip() + "\n"


def _render_must_read(items: list[ApprovedItem]) -> str:
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


def _render_papers(items: list[ApprovedItem], ranks: dict[str, int], run_date: date) -> str:
    lines = ['<section class="papers">', "<h2>最新论文</h2>"]
    if not items:
        lines.append("<p>今日未筛选出论文条目。</p>")
        lines.append("</section>")
        return "\n".join(lines)
    for item in items:
        fields = item.final_fields
        material = item.material
        rank = ranks.get(item.key)
        prefix = f"{rank}. " if rank else ""
        lines.extend(
            [
                '<article class="report-item paper">',
                f"<h3>{_text(prefix)}{_text(_prefix_label(item))}{_text(item.title)}</h3>",
                "<dl>",
                _field("解决问题", _value(fields.get("problem"))),
                _field("方法/技术路线", _value(fields.get("technical_route") or fields.get("method"))),
                _field("关键步骤", _list_value(fields.get("method_steps"))),
                _field("结果/发现", _value(fields.get("key_result"))),
                _field("可能用途/影响", _value(fields.get("possible_use_or_impact"))),
                _field("局限", _value(fields.get("limitations"))),
                _field("来源/时间", _paper_time(material)),
                _field("入选理由", _recommendation_reason(material)),
                _field("标签", _tags(item)),
                f"<dt>链接</dt><dd>{_paper_links(material)}</dd>",
            ]
        )
        if rank:
            lines.append(_field("反馈编号", f"第 {rank} 条"))
            lines.append(f"<dt>反馈</dt><dd>{_feedback_html(run_date, rank)}</dd>")
        lines.extend(["</dl>", "</article>"])
    lines.append("</section>")
    return "\n".join(lines)


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
    if getattr(material, "doi", None):
        links.append(_safe_link("DOI", f"https://doi.org/{material.doi}"))
    if "openalex" in getattr(material, "source_aliases", {}):
        links.append(_safe_link("OpenAlex", material.raw.get("openalex_url") or material.url))
    if material.raw.get("semantic_scholar_url"):
        links.append(_safe_link("Semantic Scholar", material.raw["semantic_scholar_url"]))
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
    like_command = f"daily-agent feedback add --date {run_date.isoformat()} --rank {rank} --signal like"
    dislike_command = f"daily-agent feedback add --date {run_date.isoformat()} --rank {rank} --signal dislike"
    comment_hint = f"飞书评论：第 {rank} 条不错 / 第 {rank} 条不相关"
    action = feedback_form_action()
    return (
        f"{_text(comment_hint)}<br>"
        "按钮服务：先运行 <code>daily-agent feedback serve</code>"
        f'<form class="feedback-actions" method="post" action="{_attr(action)}">'
        f'<input type="hidden" name="date" value="{_attr(run_date.isoformat())}">'
        f'<input type="hidden" name="rank" value="{_attr(rank)}">'
        '<button type="submit" name="signal" value="like">有用</button>'
        '<button type="submit" name="signal" value="dislike">不相关</button>'
        "</form>"
        f"有用：<code>{_text(like_command)}</code><br>"
        f"不相关：<code>{_text(dislike_command)}</code>"
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


def _is_safe_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _text(value) -> str:
    return escape(str(value), quote=False)


def _attr(value) -> str:
    return escape(str(value), quote=True)
