from pathlib import Path
from datetime import date
import json

from daily_agent.config import load_config
from daily_agent.models import ApprovedItem, MaterialRecord


def _approved_paper(**overrides):
    material = MaterialRecord(
        key=overrides.get("key", "doi:10.1234/pdf-cache"),
        source=overrides.get("source", "openalex"),
        item_type="paper",
        title=overrides.get("title", "Quantum Circuit Routing Agents"),
        url=overrides.get("url", "https://doi.org/10.1234/pdf-cache"),
        pdf_url=overrides.get("pdf_url", "https://paper.test/first.pdf"),
        authors=["Alice Zhang"],
        doi="10.1234/pdf-cache",
        evidence=overrides.get(
            "evidence",
            {"sources": {"openalex": {"pdf_url": "https://paper.test/second.pdf"}}},
        ),
    )
    return ApprovedItem(
        key=material.key,
        item_type="paper",
        title=material.title,
        source=material.source,
        url=material.url,
        final_fields={
            "problem": "问题",
            "method": "方法",
            "why_it_works": "原因",
            "key_result": "结果",
            "limitations": "局限",
        },
        material=material,
    )


def test_cache_selected_pdfs_saves_first_valid_pdf_and_records_local_links(tmp_path, monkeypatch):
    from daily_agent.connectors.pdf_cache import cache_selected_pdfs

    config = load_config(str(Path(__file__).resolve().parents[1]))
    object.__setattr__(config, "root", tmp_path)
    config.sources["pdf_cache"] = {
        "enabled": True,
        "max_papers_per_run": 1,
        "max_pdf_bytes": 100,
        "max_urls_per_paper": 2,
        "timeout_seconds": 5,
    }
    paper = _approved_paper()
    attempted: list[str] = []

    def fake_download(client, url, max_bytes, deadline=None):
        attempted.append(url)
        if url.endswith("first.pdf"):
            return b"not a pdf"
        return b"%PDF-1.7\nvalid pdf"

    monkeypatch.setattr("daily_agent.connectors.pdf_cache._download_limited", fake_download)

    cache_selected_pdfs([paper], config, date(2026, 5, 18))

    assert attempted == ["https://paper.test/first.pdf", "https://paper.test/second.pdf"]
    local_path = tmp_path / "data" / "pdfs" / "2026-05-18" / "01-quantum-circuit-routing-agents.pdf"
    assert local_path.read_bytes() == b"%PDF-1.7\nvalid pdf"
    assert paper.material.raw["local_pdf_path"] == str(local_path)
    assert paper.material.raw["local_pdf_report_url"] == "../data/pdfs/2026-05-18/01-quantum-circuit-routing-agents.pdf"
    assert paper.material.raw["pdf_cache_status"]["status"] == "cached"
    assert paper.material.raw["pdf_cache_status"]["source_url"] == "https://paper.test/second.pdf"


def test_selected_json_and_reports_include_local_pdf_links(tmp_path):
    from daily_agent.rendering.html import render_daily_html
    from daily_agent.rendering.markdown import render_daily_markdown
    from daily_agent.models import RunStatus
    from daily_agent.storage import write_selected

    config = load_config(str(Path(__file__).resolve().parents[1]))
    object.__setattr__(config, "root", tmp_path)
    paper = _approved_paper()
    paper.material.raw["local_pdf_path"] = str(tmp_path / "data" / "pdfs" / "2026-05-18" / "01-paper.pdf")
    paper.material.raw["local_pdf_report_url"] = "../data/pdfs/2026-05-18/01-paper.pdf"

    selected_path = write_selected(config, [paper], date(2026, 5, 18), dry_run=True)
    payload = json.loads(selected_path.read_text(encoding="utf-8"))
    markdown = render_daily_markdown([paper], date(2026, 5, 18), RunStatus())
    html = render_daily_html([paper], date(2026, 5, 18), RunStatus())

    assert payload["selected"][0]["local_pdf_path"].endswith("/data/pdfs/2026-05-18/01-paper.pdf")
    assert "[本地PDF](../data/pdfs/2026-05-18/01-paper.pdf)" in markdown
    assert 'href="../data/pdfs/2026-05-18/01-paper.pdf"' in html


def test_pipeline_caches_pdfs_before_rendering_and_selected_json(tmp_path, monkeypatch):
    from daily_agent import pipeline

    config = load_config(str(Path(__file__).resolve().parents[1]))
    object.__setattr__(config, "root", tmp_path)
    paper = _approved_paper(title="Quantum circuit optimization PDF Paper")

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
    monkeypatch.setattr(pipeline, "load_material_library", lambda config_arg: {paper.key: paper.material})
    monkeypatch.setattr(pipeline, "select_library_candidates", lambda config_arg, library, run_date: [paper.material])
    monkeypatch.setattr(pipeline, "enrich_paper_texts", lambda records, config_arg: records)
    monkeypatch.setattr(pipeline, "enrich_citation_contexts", lambda records, config_arg: records)
    monkeypatch.setattr(pipeline, "draft_report_items", lambda config_arg, shortlist, use_llm=True: [])
    monkeypatch.setattr(pipeline, "review_draft", lambda config_arg, drafts, use_llm=True: [])
    monkeypatch.setattr(pipeline, "approve_publication", lambda config_arg, shortlist, drafts, reviews: [paper])

    def fake_cache_pdf(items, config_arg, run_date):
        items[0].material.raw["local_pdf_path"] = str(tmp_path / "data" / "pdfs" / "2026-05-18" / "01-paper.pdf")
        items[0].material.raw["local_pdf_report_url"] = "../data/pdfs/2026-05-18/01-paper.pdf"
        return items

    monkeypatch.setattr(pipeline, "cache_selected_pdfs", fake_cache_pdf)

    result = pipeline.run_pipeline(root=tmp_path, run_date=date(2026, 5, 18), dry_run=True, use_llm=False)
    selected_payload = json.loads(result.selected_path.read_text(encoding="utf-8"))

    assert "[本地PDF](../data/pdfs/2026-05-18/01-paper.pdf)" in result.daily_markdown
    assert selected_payload["selected"][0]["local_pdf_path"].endswith("/data/pdfs/2026-05-18/01-paper.pdf")


def test_cleanup_retention_removes_old_pdf_cache_directories(tmp_path):
    from daily_agent.storage import cleanup_retention

    config = load_config(str(Path(__file__).resolve().parents[1]))
    object.__setattr__(config, "root", tmp_path)
    config.delivery["retention"]["selected_keep_weeks"] = 1
    config.sources["pdf_cache"] = {"enabled": True, "output_dir": "data/pdfs"}
    old_pdf = tmp_path / "data" / "pdfs" / "2026-05-01" / "01-old.pdf"
    recent_pdf = tmp_path / "data" / "pdfs" / "2026-05-18" / "01-recent.pdf"
    old_pdf.parent.mkdir(parents=True)
    recent_pdf.parent.mkdir(parents=True)
    old_pdf.write_bytes(b"%PDF-old")
    recent_pdf.write_bytes(b"%PDF-recent")

    cleanup_retention(config, date(2026, 5, 20))

    assert not old_pdf.parent.exists()
    assert recent_pdf.exists()
