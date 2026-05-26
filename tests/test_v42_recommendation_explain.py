from datetime import date, datetime, timezone

from daily_agent.config import load_config
from daily_agent.models import ApprovedItem, DigestItem, MaterialRecord, RunStatus
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
        score_breakdown={"domain": 20.0, "keywords": 8.0, "freshness": 16.0, "venue": 8.0, "evidence": 7.0},
        raw={"venue": "ICLR 2026", "openreview_url": "https://openreview.net/forum?id=OR-1"},
    )
    approved = ApprovedItem(
        key=material.key,
        item_type="paper",
        title=material.title,
        source=material.source,
        url=material.url,
        final_fields={"problem": "p", "method": "m", "method_steps": ["s"], "key_result": "r", "possible_use_or_impact": "i", "limitations": "l"},
        material=material,
    )

    markdown = render_daily_markdown([approved], date(2026, 5, 27), RunStatus())

    assert "- 入选理由：" in markdown
    assert "领域匹配" in markdown
    assert "顶会/权威来源" in markdown
    assert "多源交叉验证" in markdown
