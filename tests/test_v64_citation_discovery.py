from pathlib import Path
from datetime import date, datetime, timezone
import json

import httpx

from daily_agent.config import load_config
from daily_agent.models import ApprovedItem, MaterialRecord


class _Client:
    def __init__(self, handler, **kwargs):
        self.handler = handler
        self.kwargs = kwargs

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get(self, url, **kwargs):
        return self.handler(url, **kwargs)


def _response(payload):
    return httpx.Response(200, request=httpx.Request("GET", "https://api.openalex.org/works"), json=payload)


def _seed_record(**overrides):
    return MaterialRecord(
        key=overrides.get("key", "doi:10.1234/seed"),
        source=overrides.get("source", "openalex"),
        item_type="paper",
        title=overrides.get("title", "Seed Quantum Compiler"),
        url=overrides.get("url", "https://doi.org/10.1234/seed"),
        doi=overrides.get("doi", "10.1234/seed"),
        tags=overrides.get("tags", ["quantum", "compiler"]),
        quota_group=overrides.get("quota_group", "quantum"),
        score=overrides.get("score", 88.0),
        published_dates=overrides.get("published_dates", ["2026-07-01"]),
        source_aliases=overrides.get("source_aliases", {}),
        raw=overrides.get("raw", {}),
    )


def test_fetch_citation_discovery_finds_recent_openalex_citing_papers(tmp_path, monkeypatch):
    from daily_agent.connectors import citation_discovery
    from daily_agent.connectors.citation_discovery import fetch_citation_discovery

    config = load_config(str(Path(__file__).resolve().parents[1]))
    object.__setattr__(config, "root", tmp_path)
    config.sources["citation_discovery"] = {
        "enabled": True,
        "recent_days": 14,
        "max_seed_papers": 2,
        "max_results_per_seed": 2,
        "timeout_seconds": 30,
    }
    seed = _seed_record()
    seen: list[tuple[str, dict]] = []

    def handler(url, **kwargs):
        seen.append((url, kwargs.get("params") or {}))
        if url.endswith("/doi:10.1234/seed"):
            return _response({"id": "https://openalex.org/WSEED", "title": "Seed Quantum Compiler"})
        return _response(
            {
                "results": [
                    {
                        "id": "https://openalex.org/WCITING",
                        "title": "Citing Quantum Compiler Agents",
                        "doi": "https://doi.org/10.9999/citing",
                        "publication_date": "2026-07-08",
                        "updated_date": "2026-07-08",
                        "cited_by_count": 12,
                        "abstract_inverted_index": {"We": [0], "extend": [1], "compilers": [2]},
                        "authorships": [{"author": {"display_name": "Alice Citation"}}],
                        "concepts": [{"display_name": "Quantum computing"}],
                        "primary_location": {
                            "landing_page_url": "https://paper.test/citing",
                            "pdf_url": "https://paper.test/citing.pdf",
                            "source": {"display_name": "Citation Tests"},
                        },
                        "type": "article",
                    }
                ]
            }
        )

    monkeypatch.setattr(citation_discovery.httpx, "Client", lambda **kwargs: _Client(handler, **kwargs))

    items = fetch_citation_discovery(
        config,
        datetime(2026, 7, 9, tzinfo=timezone.utc),
        window_days=14,
        library={seed.key: seed},
    )

    assert len(items) == 1
    assert seen[1][1]["filter"] == "cites:WSEED,from_publication_date:2026-06-25,to_publication_date:2026-07-10,type:article"
    assert items[0].source == "openalex"
    assert items[0].title == "Citing Quantum Compiler Agents"
    assert items[0].doi == "10.9999/citing"
    assert items[0].abstract == "We extend compilers"
    assert items[0].pdf_url == "https://paper.test/citing.pdf"
    assert "citation_discovery" in items[0].source_tags
    assert items[0].quota_group == "quantum"
    assert items[0].raw["citation_discovery"]["seed_key"] == "doi:10.1234/seed"
    assert items[0].raw["citation_discovery"]["seed_title"] == "Seed Quantum Compiler"
    assert items[0].raw["openalex_id"] == "WCITING"


def test_pipeline_fetches_citation_discovery_from_material_library(tmp_path, monkeypatch):
    from daily_agent import pipeline

    config = load_config(str(Path(__file__).resolve().parents[1]))
    object.__setattr__(config, "root", tmp_path)
    seed = _seed_record()
    citing_material = MaterialRecord(
        key="doi:10.9999/citing",
        source="openalex",
        item_type="paper",
        title="Citing Quantum Compiler Agents",
        source_updated_at="2026-07-09T00:00:00Z",
        url="https://paper.test/citing",
        doi="10.9999/citing",
        abstract="This paper studies quantum compiler agents. We propose citation-guided routing. It works by grounding discovery in prior papers. Experiments improve depth by 17%. Limitations include OpenAlex coverage.",
        score=90,
        tags=["citation_discovery", "quantum"],
        quota_group="quantum",
        raw={"citation_discovery": {"seed_key": seed.key}},
    )
    approved = ApprovedItem(
        key=citing_material.key,
        item_type="paper",
        title=citing_material.title,
        source="openalex",
        url=citing_material.url,
        final_fields={
            "problem": "problem",
            "method": "method",
            "why_it_works": "why",
            "key_result": "result",
            "limitations": "limitations",
        },
        material=citing_material,
    )

    for name in [
        "fetch_arxiv",
        "fetch_github",
        "fetch_openalex",
        "fetch_semantic_scholar",
        "fetch_google_scholar",
        "fetch_crossref",
        "fetch_core",
        "fetch_dblp",
        "fetch_ieee",
        "fetch_openreview",
        "fetch_pmlr",
        "fetch_neurips",
    ]:
        monkeypatch.setattr(pipeline, name, lambda config_arg, target_dt, window_days=None: [])
    monkeypatch.setattr(pipeline, "load_config", lambda root=None: config)
    monkeypatch.setattr(pipeline, "load_material_library", lambda config_arg: {seed.key: seed, citing_material.key: citing_material})
    monkeypatch.setattr(pipeline, "fetch_citation_discovery", lambda config_arg, target_dt, window_days=None, library=None: [citing_material.to_digest_item()])
    monkeypatch.setattr(pipeline, "enrich_paper_texts", lambda records, config_arg: records)
    monkeypatch.setattr(pipeline, "enrich_citation_contexts", lambda records, config_arg: records)
    monkeypatch.setattr(pipeline, "draft_report_items", lambda config_arg, shortlist, use_llm=True: [])
    monkeypatch.setattr(pipeline, "review_draft", lambda config_arg, drafts, use_llm=True: [])
    monkeypatch.setattr(pipeline, "approve_publication", lambda config_arg, shortlist, drafts, reviews: [approved])
    monkeypatch.setattr(pipeline, "cache_selected_pdfs", lambda items, config_arg, run_date: items)

    result = pipeline.run_pipeline(root=tmp_path, run_date=date(2026, 7, 9), dry_run=True, use_llm=False)
    payload = json.loads(result.selected_path.read_text(encoding="utf-8"))
    statuses = {source.name: source for source in result.status.sources}

    assert "Citation Discovery/91d" in statuses
    assert statuses["Citation Discovery/91d"].ok is True
    assert statuses["Citation Discovery/91d"].item_count == 1
    assert payload["selected"][0]["key"] == "doi:10.9999/citing"


def test_quality_check_requires_citation_discovery_full_profile(tmp_path):
    from daily_agent.quality import run_quality_check

    config = load_config(str(Path(__file__).resolve().parents[1]))
    object.__setattr__(config, "root", tmp_path)
    config.sources["citation_discovery"] = {
        "enabled": False,
        "recent_days": 7,
        "max_seed_papers": 2,
        "max_results_per_seed": 1,
        "timeout_seconds": 10,
    }

    by_key = {check.key: check for check in run_quality_check(config).checks}

    assert by_key["citation_discovery"].status == "disabled"
    assert "enabled=false" in by_key["citation_discovery"].detail
    assert "max_seed_papers=2" in by_key["citation_discovery"].detail
    assert "max_results_per_seed=1" in by_key["citation_discovery"].detail


def test_scoring_adds_citation_discovery_signal(tmp_path):
    from daily_agent.scoring.rules import score_items

    config = load_config(str(Path(__file__).resolve().parents[1]))
    object.__setattr__(config, "root", tmp_path)
    item = _seed_record().to_digest_item()
    item.id = "W-CITING"
    item.title = "Quantum compiler agents follow-up"
    item.doi = "10.9999/followup"
    item.raw["citation_discovery"] = {"seed_key": "doi:10.1234/seed", "seed_title": "Seed Quantum Compiler"}

    scored = score_items([item], config, target_date=datetime(2026, 7, 9, tzinfo=timezone.utc))

    assert scored[0].score_breakdown["citation_discovery"] == 6.0
    assert scored[0].score >= 6.0
