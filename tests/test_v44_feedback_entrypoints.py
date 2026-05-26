from datetime import date

from daily_agent.models import ApprovedItem, MaterialRecord, RunStatus
from daily_agent.rendering.html import render_daily_html
from daily_agent.rendering.markdown import render_daily_markdown


def _approved_paper(title="Quantum paper"):
    material = MaterialRecord(
        key="arxiv:2401.00001",
        source="arxiv",
        item_type="paper",
        title=title,
        url="https://arxiv.org/abs/2401.00001",
        pdf_url="https://arxiv.org/pdf/2401.00001",
        abstract="abstract",
    )
    return ApprovedItem(
        key=material.key,
        item_type="paper",
        title=material.title,
        source=material.source,
        url=material.url,
        final_fields={
            "problem": "p",
            "method": "m",
            "method_steps": ["s"],
            "key_result": "r",
            "possible_use_or_impact": "i",
            "limitations": "l",
        },
        material=material,
    )


def test_markdown_includes_feishu_comment_feedback_hint_and_cli_commands():
    markdown = render_daily_markdown([_approved_paper()], date(2026, 5, 27), RunStatus())

    assert "- 反馈：飞书评论可写「第 1 条不错」或「第 1 条不相关」" in markdown
    assert "`daily-agent feedback add --date 2026-05-27 --rank 1 --signal like`" in markdown
    assert "`daily-agent feedback add --date 2026-05-27 --rank 1 --signal dislike`" in markdown


def test_html_includes_copyable_feedback_commands_without_javascript():
    html = render_daily_html([_approved_paper()], date(2026, 5, 27), RunStatus())

    assert "反馈" in html
    assert "飞书评论：第 1 条不错 / 第 1 条不相关" in html
    assert "daily-agent feedback add --date 2026-05-27 --rank 1 --signal like" in html
    assert "daily-agent feedback add --date 2026-05-27 --rank 1 --signal dislike" in html
    assert "javascript:" not in html
    assert "onclick" not in html


def test_html_feedback_block_escapes_dynamic_rank_context():
    html = render_daily_html([_approved_paper("Paper <script>alert(1)</script>")], date(2026, 5, 27), RunStatus())

    assert "<script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
