from __future__ import annotations
from pathlib import Path

from datetime import date, datetime, timezone

from daily_agent.config import load_config
from daily_agent.editorial import build_shortlist
from daily_agent.models import DigestItem, MaterialRecord
from daily_agent.scoring.rules import score_items, select_items


def test_topic_relevance_gate_rejects_source_tag_only_quantum_match():
    config = load_config(str(Path(__file__).resolve().parents[1]))
    weak = DigestItem(
        id="W21161329",
        source="openalex",
        item_type="paper",
        title="Dihedral Modular Cosmology and Standard Model flavour structure",
        url="https://doi.org/10.5281/zenodo.21161329",
        abstract=(
            "A modular-flavour cosmology framework derives CP phases from a dihedral group, "
            "with a residual SU(2) number and electroweak scale ledger."
        ),
        categories=["Particle physics", "Cosmology", "Gauge theory", "Quantum mechanics"],
        source_tags=["openalex", "hardware_aware_quantum_circuit_synthesis", "quantum_circuit"],
        quota_group="quantum",
        doi="10.5281/zenodo.21161329",
        updated_at="2026-07-08T00:00:00+00:00",
        raw={"domain": "hardware_aware_quantum_circuit_synthesis"},
    )

    scored = score_items([weak], config, {}, datetime(2026, 7, 8, tzinfo=timezone.utc))[0]

    assert scored.score_breakdown["topic_relevance"] < config.sources["selection"]["min_topic_relevance_score"]
    assert scored.score_breakdown["off_topic_penalty"] < 0
    assert select_items([scored], config) == []


def test_topic_relevance_gate_keeps_quantum_compilation_paper():
    config = load_config(str(Path(__file__).resolve().parents[1]))
    strong = DigestItem(
        id="2607.03275",
        source="arxiv",
        item_type="paper",
        title="Comparing and learning figures of merit for quantum circuit compilation",
        url="https://arxiv.org/abs/2607.03275v1",
        pdf_url="https://arxiv.org/pdf/2607.03275v1",
        abstract=(
            "To make quantum algorithms executable on a particular quantum device, they need to be "
            "compiled into circuits that respect constraints of the quantum hardware. We learn figures "
            "of merit for quantum circuit compilation and evaluate routing choices."
        ),
        categories=["quant-ph"],
        arxiv_id="2607.03275",
        arxiv_version="v1",
        updated_at="2026-07-08T00:00:00+00:00",
    )

    scored = score_items([strong], config, {}, datetime(2026, 7, 8, tzinfo=timezone.utc))[0]

    assert scored.score_breakdown["topic_relevance"] >= config.sources["selection"]["min_topic_relevance_score"]
    assert "off_topic_penalty" not in scored.score_breakdown
    assert select_items([scored], config) == [scored]


def test_build_shortlist_filters_polluted_library_records():
    config = load_config(str(Path(__file__).resolve().parents[1]))
    config.quota["paper_target"] = 2
    config.quota["github_target"] = 0
    config.quota["paper_review_multiplier"] = 1
    polluted = MaterialRecord(
        key="doi:10.5281/zenodo.21161329",
        source="openalex",
        item_type="paper",
        title="Dihedral Modular Cosmology and Standard Model flavour structure",
        url="https://doi.org/10.5281/zenodo.21161329",
        abstract="A cosmology framework with SU(2) symmetry and electroweak flavour structure.",
        categories=["Particle physics", "Cosmology", "Gauge theory", "Quantum mechanics"],
        tags=["quantum_circuit", "hardware_aware_quantum_circuit_synthesis"],
        quota_group="quantum",
        score=999,
        quality_status="library",
        score_breakdown={"domain": 27.0, "keywords": 4.7},
    )
    strong = MaterialRecord(
        key="arxiv:2607.03275",
        source="arxiv",
        item_type="paper",
        title="Comparing and learning figures of merit for quantum circuit compilation",
        url="https://arxiv.org/abs/2607.03275v1",
        pdf_url="https://arxiv.org/pdf/2607.03275v1",
        abstract=(
            "This paper studies quantum circuit compilation for constrained quantum hardware and "
            "learns figures of merit for routing and circuit quality."
        ),
        categories=["quant-ph"],
        tags=["quantum_ai", "quantum_circuit", "quantum_compilation"],
        quota_group="quantum",
        score=100,
        quality_status="library",
    )

    shortlist = build_shortlist(config, {polluted.key: polluted, strong.key: strong}, date(2026, 7, 8))

    assert [record.key for record in shortlist] == [strong.key]
