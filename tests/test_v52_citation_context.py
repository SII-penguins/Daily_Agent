from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

import yaml

from daily_agent.config import load_config
from daily_agent.health import evaluate_run_health
from daily_agent.models import ApprovedItem, DeliveryStatus, EditorialDraft, EditorialReview, MaterialRecord, RunStatus
from daily_agent.rendering.html import render_daily_html
from daily_agent.rendering.markdown import render_daily_markdown


ROOT = Path(__file__).resolve().parents[1]


def _copy_config(tmp_path: Path) -> Path:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    for name in ["sources.yaml", "interests.yaml", "delivery.yaml", "feedback.yaml"]:
        shutil.copy(ROOT / "config" / name, config_dir / name)
    return tmp_path


def _artifact_paths(tmp_path: Path):
    report = tmp_path / "reports" / "daily.md"
    html = tmp_path / "reports" / "daily.html"
    selected = tmp_path / "data" / "selected" / "selected.json"
    editorial = tmp_path / "data" / "editorial" / "2026-07-07"
    report.parent.mkdir(parents=True, exist_ok=True)
    selected.parent.mkdir(parents=True, exist_ok=True)
    editorial.mkdir(parents=True, exist_ok=True)
    report.write_text("report", encoding="utf-8")
    html.write_text("html", encoding="utf-8")
    selected.write_text("{}", encoding="utf-8")
    for name in ["shortlist.json", "writer_draft.json", "editor_review.json", "approval.json"]:
        (editorial / name).write_text("[]", encoding="utf-8")
    return report, html, selected, editorial


def test_enrich_citation_contexts_fetches_openalex_citing_and_referenced_papers(tmp_path, monkeypatch):
    from daily_agent.connectors import citation_context
    from daily_agent.connectors.citation_context import enrich_citation_contexts

    root = _copy_config(tmp_path)
    config = load_config(root)
    config.sources["citation_context"] = {
        "enabled": True,
        "max_papers_per_run": 3,
        "max_citing_papers": 2,
        "max_referenced_papers": 2,
        "timeout_seconds": 10,
    }
    material = MaterialRecord(
        key="doi:10.1234/test",
        source="openalex",
        item_type="paper",
        title="Quantum Daily Agents",
        url="https://doi.org/10.1234/test",
        doi="10.1234/test",
    )

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self.payload

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, params=None):
            text = f"{url}?{params or {}}"
            if "doi:10.1234/test" in text:
                return Response(
                    {
                        "id": "https://openalex.org/W123",
                        "title": "Quantum Daily Agents",
                        "publication_year": 2026,
                        "cited_by_count": 42,
                        "referenced_works": ["https://openalex.org/W2"],
                    }
                )
            if params and params.get("filter") == "cites:W123":
                return Response(
                    {
                        "results": [
                            {
                                "id": "https://openalex.org/W9",
                                "title": "Citing Quantum Agent Systems",
                                "publication_year": 2026,
                                "cited_by_count": 9,
                                "doi": "https://doi.org/10.9999/citing",
                            }
                        ]
                    }
                )
            if params and params.get("filter") == "openalex:W2":
                return Response(
                    {
                        "results": [
                            {
                                "id": "https://openalex.org/W2",
                                "title": "Foundational Quantum Compiler",
                                "publication_year": 2024,
                                "cited_by_count": 88,
                                "doi": "https://doi.org/10.2222/ref",
                            }
                        ]
                    }
                )
            return Response({"results": []})

    monkeypatch.setattr(citation_context.httpx, "Client", Client)

    enriched = enrich_citation_contexts([material], config)

    context = enriched[0].raw["citation_context"]
    assert context["source"] == "openalex"
    assert context["work_id"] == "W123"
    assert context["cited_by_count"] == 42
    assert context["citing"][0]["title"] == "Citing Quantum Agent Systems"
    assert context["referenced"][0]["title"] == "Foundational Quantum Compiler"


def test_enrich_citation_contexts_uses_arxiv_doi_fallback_when_record_has_no_doi(tmp_path, monkeypatch):
    from daily_agent.connectors import citation_context
    from daily_agent.connectors.citation_context import enrich_citation_contexts

    root = _copy_config(tmp_path)
    config = load_config(root)
    config.sources["citation_context"] = {
        "enabled": True,
        "max_papers_per_run": 3,
        "max_citing_papers": 0,
        "max_referenced_papers": 0,
        "timeout_seconds": 10,
    }
    material = MaterialRecord(
        key="arxiv:2607.03275v1",
        source="arxiv",
        item_type="paper",
        title="Comparing and learning figures of merit for quantum circuit compilation",
        url="https://arxiv.org/abs/2607.03275v1",
    )
    seen_urls = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "id": "https://openalex.org/W260703275",
                "title": material.title,
                "publication_year": 2026,
                "cited_by_count": 5,
                "referenced_works": [],
            }

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url, params=None):
            seen_urls.append(url)
            return Response()

    monkeypatch.setattr(citation_context.httpx, "Client", Client)

    enriched = enrich_citation_contexts([material], config)

    assert seen_urls[0].endswith("/doi:10.48550/arxiv.2607.03275")
    assert enriched[0].raw["citation_context"]["work_id"] == "W260703275"


def test_pipeline_enriches_citation_context_before_rule_writer(tmp_path, monkeypatch):
    from daily_agent import pipeline
    from daily_agent.storage import write_material_library

    root = _copy_config(tmp_path)
    config = load_config(root)
    config.quota["max_items"] = 1
    config.quota["paper_target"] = 1
    config.quota["paper_review_multiplier"] = 1
    config.quota["github_target"] = 0
    config.sources["citation_context"] = {"enabled": True, "max_papers_per_run": 3}
    material = MaterialRecord(
        key="doi:10.1234/context",
        source="openalex",
        item_type="paper",
        title="Citation-aware Quantum Compilation",
        source_updated_at="2026-07-07T00:00:00Z",
        url="https://doi.org/10.1234/context",
        doi="10.1234/context",
        abstract=(
            "This paper studies the problem of hardware-aware quantum circuit compilation. "
            "We propose a citation-aware circuit synthesis method with reranking. "
            "This works because citation context identifies upstream foundations and downstream use. "
            "Experiments reduce two-qubit gate depth by 21%. Limitations include API coverage."
        ),
        paper_text_excerpt=(
            "Methods. We propose hardware-aware quantum circuit synthesis with citation-aware reranking over upstream references "
            "and downstream citing papers. This works because citation context identifies foundational methods "
            "and follow-on use before final mapping. Results. Experiments reduce two-qubit gate depth by 21% "
            "on quantum compilation tasks. Limitations include API coverage and delayed citation availability."
        ),
        paper_text_status={
            "available": True,
            "sufficient_for_deep_summary": True,
            "section_notes": {
                "method": "hardware-aware quantum circuit synthesis with citation-aware reranking",
                "results": "21% lower two-qubit gate depth",
                "limitations": "API coverage and delayed citations",
            },
        },
        raw={"openalex_id": "W123", "cited_by_count": 7},
        score=99,
    )
    write_material_library(config, {material.key: material})

    for name in [
        "fetch_arxiv",
        "fetch_github",
        "fetch_openalex",
        "fetch_semantic_scholar",
        "fetch_google_scholar",
        "fetch_crossref",
        "fetch_dblp",
        "fetch_ieee",
        "fetch_openreview",
        "fetch_pmlr",
        "fetch_neurips",
    ]:
        monkeypatch.setattr(pipeline, name, lambda *args, **kwargs: [])
    monkeypatch.setattr(pipeline, "load_config", lambda root_arg=None: config)
    monkeypatch.setattr(pipeline, "enrich_paper_texts", lambda records, config_arg: records)

    def fake_enrich_citation_contexts(records, config_arg):
        for record in records:
            record.raw["citation_context"] = {
                "source": "openalex",
                "work_id": "W123",
                "cited_by_count": 12,
                "citing": [{"title": "Downstream Agent Survey", "year": 2026, "cited_by_count": 3}],
                "referenced": [{"title": "Foundational Retrieval Paper", "year": 2024, "cited_by_count": 44}],
            }
        return records

    monkeypatch.setattr(pipeline, "enrich_citation_contexts", fake_enrich_citation_contexts)

    result = pipeline.run_pipeline(root=root, run_date=date(2026, 7, 7), dry_run=True, use_llm=False)

    assert result.items
    assert "引用脉络" in result.items[0].final_fields["evidence_from_source"]
    assert "Downstream Agent Survey" in result.daily_markdown


def test_reports_render_citation_context_in_markdown_and_html():
    material = MaterialRecord(
        key="doi:10.1234/report",
        source="openalex",
        item_type="paper",
        title="Report Citation Context",
        url="https://doi.org/10.1234/report",
        doi="10.1234/report",
        raw={
            "citation_context": {
                "source": "openalex",
                "work_id": "W123",
                "cited_by_count": 42,
                "citing": [{"title": "Citing <Agent> Systems", "year": 2026, "cited_by_count": 9}],
                "referenced": [{"title": "Foundational Compiler", "year": 2024, "cited_by_count": 88}],
            }
        },
    )
    approved = ApprovedItem(
        key=material.key,
        item_type="paper",
        title=material.title,
        source=material.source,
        url=material.url,
        material=material,
        final_fields={
            "problem": "problem",
            "method": "method",
            "why_it_works": "why",
            "technical_route": "route",
            "method_steps": ["step"],
            "key_result": "result",
            "possible_use_or_impact": "impact",
            "limitations": "limits",
        },
    )

    markdown = render_daily_markdown([approved], date(2026, 7, 7), RunStatus())
    html = render_daily_html([approved], date(2026, 7, 7), RunStatus())

    assert "引用脉络：被引 42 次" in markdown
    assert "近期引用：Citing <Agent> Systems" in markdown
    assert "关键参考：Foundational Compiler" in markdown
    assert "引用脉络" in html
    assert "Citing &lt;Agent&gt; Systems" in html


def test_quality_check_requires_full_citation_context_profile(tmp_path):
    root = _copy_config(tmp_path)
    config = load_config(root)
    config.sources["citation_context"] = {
        "enabled": True,
        "max_papers_per_run": 30,
        "max_citing_papers": 8,
        "max_referenced_papers": 8,
        "timeout_seconds": 60,
    }

    from daily_agent.quality import run_quality_check

    by_key = {check.key: check for check in run_quality_check(config).checks}

    assert by_key["citation_context"].status == "full"


def test_health_counts_citation_context_coverage(tmp_path):
    root = _copy_config(tmp_path)
    config = load_config(root)
    material = MaterialRecord(
        key="doi:10.1234/health",
        source="openalex",
        item_type="paper",
        title="Health Citation Context",
        url="https://doi.org/10.1234/health",
        doi="10.1234/health",
        raw={
            "citation_context": {
                "source": "openalex",
                "work_id": "W123",
                "cited_by_count": 5,
                "citing": [{"title": "Citing Paper"}],
                "referenced": [{"title": "Reference Paper"}],
            }
        },
    )
    approved = ApprovedItem(
        key=material.key,
        item_type="paper",
        title=material.title,
        source=material.source,
        url=material.url,
        material=material,
        final_fields={"method": "method", "why_it_works": "why", "evidence_from_source": "evidence"},
    )
    report, html, selected, editorial = _artifact_paths(tmp_path)

    health = evaluate_run_health(
        config,
        date(2026, 7, 7),
        True,
        RunStatus(delivery=DeliveryStatus(requested_mode="dry-run", final_mode="local", ok=True)),
        [approved],
        [material],
        [EditorialDraft(key=material.key, item_type="paper", title=material.title, draft_fields=approved.final_fields, evidence_used=["evidence"])],
        [EditorialReview(key=material.key, verdict="PASS")],
        report,
        html,
        selected,
        editorial,
    )
    checks = {check["id"]: check for check in health["current"]["checks"]}

    assert health["current"]["summary"]["citation_context_available_count"] == 1
    assert health["current"]["summary"]["citation_context_missing_count"] == 0
    assert checks["citation_context_coverage"]["status"] == "pass"
