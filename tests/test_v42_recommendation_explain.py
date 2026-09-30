from datetime import date, datetime, timezone

from daily_agent.config import load_config
from daily_agent.models import ApprovedItem, DigestItem, MaterialRecord, RunStatus
from daily_agent.rendering.html import render_daily_html
from daily_agent.rendering.markdown import render_daily_markdown
from daily_agent.scoring.rules import score_items


def test_top_conference_venue_adds_scoring_signal():
    config = load_config("/Users/wuzixie/Daily_Agent")
    item = DigestItem(
        id="pmlr-v267-paper",
        source="pmlr",
        item_type="paper",
        title="Quantum circuit optimization for learning",
        url="https://proceedings.mlr.press/v267/paper.html",
        abstract="We propose a method for quantum circuit optimization.",
        published_at="2026",
        updated_at="2026",
        raw={"venue": "ICML 2026", "pmlr_id": "pmlr-v267-paper"},
    )

    scored = score_items([item], config, {}, datetime(2026, 5, 27, tzinfo=timezone.utc))[0]

    assert scored.score_breakdown["venue"] > 0


def test_markdown_renders_recommendation_reason_from_score_breakdown():
    material = MaterialRecord(
        key="doi:10.1234/qc.1",
        source="openreview",
        item_type="paper",
        title="Quantum paper",
        url="https://openreview.net/forum?id=OR-1",
        pdf_url="https://openreview.net/pdf?id=OR-1",
        doi="10.1234/qc.1",
        source_aliases={"openreview": "OR-1", "semantic_scholar": "S2-1"},
        score_breakdown={"domain": 20.0, "keywords": 8.0, "freshness": 16.0, "venue": 8.0, "evidence": 7.0, "citation_discovery": 6.0},
        raw={"venue": "ICLR 2026", "openreview_url": "https://openreview.net/forum?id=OR-1"},
    )
    approved = ApprovedItem(
        key=material.key,
        item_type="paper",
        title=material.title,
        source=material.source,
        url=material.url,
        final_fields={"problem": "p", "method": "m", "method_steps": ["s"], "key_result": "r", "novelty_or_difference": "相对已有工作，它把硬件噪声直接纳入指标。", "possible_use_or_impact": "i", "limitations": "l"},
        material=material,
    )

    markdown = render_daily_markdown([approved], date(2026, 5, 27), RunStatus())

    assert "- 入选理由：" in markdown
    assert "阅读状态：" in markdown
    assert "领域匹配" in markdown
    assert "顶会/权威来源" in markdown
    assert "引用邻域发现" in markdown
    assert "多源元数据收录" in markdown


def test_markdown_recommendation_reason_explains_fulltext_and_citation_signals():
    material = MaterialRecord(
        key="doi:10.1234/qc.2",
        source="openalex",
        item_type="paper",
        title="Quantum routing with evidence",
        url="https://openalex.org/W1",
        pdf_url="https://example.org/paper.pdf",
        source_aliases={"openalex": "W1", "semantic_scholar": "S2-1", "google_scholar": "GS-1"},
        score_breakdown={"domain": 20.0, "freshness": 16.0, "venue": 8.0, "scholarly_impact": 12.0, "evidence": 10.0},
        paper_text_status={
            "available": True,
            "sufficient_for_deep_summary": True,
            "sections_found": ["introduction", "method", "results", "limitations"],
        },
        raw={
            "venue": "ICLR 2026",
            "google_scholar_url": "https://scholar.google.com/scholar?q=Quantum+routing",
            "citation_context": {
                "cited_by_count": 128,
                "citing": [{"title": "Follow-up use case"}],
                "referenced": [{"title": "Foundational routing"}],
            },
        },
    )
    approved = ApprovedItem(
        key=material.key,
        item_type="paper",
        title=material.title,
        source=material.source,
        url=material.url,
        final_fields={"problem": "p", "method": "m", "why_it_works": "w", "method_steps": ["s"], "key_result": "r", "possible_use_or_impact": "i", "limitations": "l"},
        material=material,
    )

    markdown = render_daily_markdown([approved], date(2026, 5, 27), RunStatus())
    reason_line = next(line for line in markdown.splitlines() if line.startswith("- 入选理由："))

    assert "全文证据覆盖" not in reason_line  # legacy status cannot prove reading
    assert "引用脉络被引 128 次" in reason_line
    assert "Google Scholar 可追溯" in reason_line


def test_html_renders_paper_novelty_or_difference():
    material = MaterialRecord(
        key="doi:10.1234/qc.3",
        source="openreview",
        item_type="paper",
        title="Quantum novelty paper",
        url="https://openreview.net/forum?id=OR-3",
    )
    approved = ApprovedItem(
        key=material.key,
        item_type="paper",
        title=material.title,
        source=material.source,
        url=material.url,
        final_fields={
            "problem": "p",
            "method": "m",
            "why_it_works": "w",
            "novelty_or_difference": "相对已有工作，它把 pass embedding 用于候选序列排序。",
            "method_steps": ["s"],
            "key_result": "r",
            "possible_use_or_impact": "i",
            "limitations": "l",
        },
        material=material,
    )

    html = render_daily_html([approved], date(2026, 5, 27), RunStatus())

    assert 'class="reading-status"' in html and "阅读状态：" in html
    assert "未核验的旧版阅读记录" in html
