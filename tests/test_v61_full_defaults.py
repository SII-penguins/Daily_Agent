from pathlib import Path
from datetime import date

from daily_agent.config import load_config
from daily_agent.editorial import draft_report_items
from daily_agent.models import EditorialDraft, MaterialRecord


def _stub_pipeline_fetchers(monkeypatch):
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
        monkeypatch.setattr(f"daily_agent.pipeline.{name}", lambda config, target_dt, window_days=None: [])


def test_draft_report_items_defaults_to_llm(monkeypatch):
    config = load_config(str(Path(__file__).resolve().parents[1]))
    material = MaterialRecord(
        key="paper:test",
        source="arxiv",
        item_type="paper",
        title="A full default paper",
        url="https://arxiv.org/abs/2601.00001",
        abstract="A test abstract.",
    )
    seen = {}

    def fake_draft_with_llm(shortlist):
        seen["count"] = len(shortlist)
        return [
            EditorialDraft(
                key=material.key,
                item_type=material.item_type,
                title=material.title,
                draft_fields={"problem": "LLM path"},
                writer_notes="llm",
            )
        ]

    monkeypatch.setattr("daily_agent.editorial._draft_with_llm", fake_draft_with_llm)

    drafts = draft_report_items(config, [material])

    assert seen["count"] == 1
    assert drafts[0].writer_notes == "llm"


def test_run_pipeline_defaults_to_llm_writer(tmp_path, monkeypatch):
    from daily_agent import pipeline

    config = load_config(str(Path(__file__).resolve().parents[1]))
    object.__setattr__(config, "root", tmp_path)
    seen = {}

    _stub_pipeline_fetchers(monkeypatch)
    monkeypatch.setattr(pipeline, "load_config", lambda root=None: config)
    monkeypatch.setattr(pipeline, "enrich_paper_texts", lambda records, config_arg: records)
    monkeypatch.setattr(pipeline, "enrich_citation_contexts", lambda records, config_arg: records)

    def fake_draft_report_items(config_arg, shortlist, use_llm=True):
        seen["use_llm"] = use_llm
        return []

    monkeypatch.setattr(pipeline, "draft_report_items", fake_draft_report_items)
    monkeypatch.setattr(pipeline, "review_draft", lambda config_arg, drafts, use_llm=True: [])
    monkeypatch.setattr(pipeline, "approve_publication", lambda config_arg, shortlist, drafts, reviews: [])

    pipeline.run_pipeline(root=tmp_path, run_date=date(2026, 7, 7), dry_run=True)

    assert seen.get("use_llm", True) is True
    # Empty candidate pools do not invoke a writer.
    import inspect
    assert inspect.signature(pipeline.run_pipeline).parameters["use_llm"].default is True
