from pathlib import Path
import csv
from datetime import date, datetime, timezone
import os
import xml.etree.ElementTree as ET

from daily_agent.config import load_config
from daily_agent.models import ApprovedItem, MaterialRecord


def _approved_paper(**overrides):
    material = MaterialRecord(
        key=overrides.get("key", "doi:10.1234/qc.1"),
        source=overrides.get("source", "openalex"),
        item_type="paper",
        title=overrides.get("title", "Quantum {Circuit} Routing & Agents"),
        url=overrides.get("url", "https://doi.org/10.1234/qc.1"),
        authors=overrides.get("authors", ["Alice Zhang", "Bob Li"]),
        doi=overrides.get("doi", "10.1234/qc.1"),
        source_updated_at=overrides.get("source_updated_at", "2026-05-18"),
        raw=overrides.get("raw", {"venue": "Journal of Tests"}),
    )
    return ApprovedItem(
        key=material.key,
        item_type="paper",
        title=material.title,
        source=material.source,
        url=material.url,
        final_fields={},
        material=material,
    )


def test_write_bibtex_export_writes_selected_papers_only(tmp_path):
    from daily_agent.storage import write_bibtex_export

    config = load_config(str(Path(__file__).resolve().parents[1]))
    object.__setattr__(config, "root", tmp_path)
    paper = _approved_paper()
    repo_material = MaterialRecord(key="github:owner/repo", source="github", item_type="repo", title="owner/repo", url="https://github.com/owner/repo")
    repo = ApprovedItem(key=repo_material.key, item_type="repo", title=repo_material.title, source="github", url=repo_material.url, final_fields={}, material=repo_material)

    path = write_bibtex_export(config, [paper, repo], date(2026, 5, 18), dry_run=True)

    assert path.name == "selected-2026-05-18.dry-run.bib"
    content = path.read_text(encoding="utf-8")
    assert "@article{zhang2026quantumcircuit," in content
    assert "title = {Quantum \\{Circuit\\} Routing \\& Agents}" in content
    assert "author = {Alice Zhang and Bob Li}" in content
    assert "doi = {10.1234/qc.1}" in content
    assert "url = {https://doi.org/10.1234/qc.1}" in content
    assert "journal = {Journal of Tests}" in content
    assert "year = {2026}" in content
    assert "owner/repo" not in content


def test_write_ris_export_writes_selected_papers_only(tmp_path):
    from daily_agent.storage import write_ris_export

    config = load_config(str(Path(__file__).resolve().parents[1]))
    object.__setattr__(config, "root", tmp_path)
    paper = _approved_paper()
    repo_material = MaterialRecord(key="github:owner/repo", source="github", item_type="repo", title="owner/repo", url="https://github.com/owner/repo")
    repo = ApprovedItem(key=repo_material.key, item_type="repo", title=repo_material.title, source="github", url=repo_material.url, final_fields={}, material=repo_material)

    path = write_ris_export(config, [paper, repo], date(2026, 5, 18), dry_run=True)

    assert path.name == "selected-2026-05-18.dry-run.ris"
    content = path.read_text(encoding="utf-8")
    assert "TY  - JOUR" in content
    assert "TI  - Quantum {Circuit} Routing & Agents" in content
    assert "AU  - Alice Zhang" in content
    assert "AU  - Bob Li" in content
    assert "PY  - 2026" in content
    assert "JO  - Journal of Tests" in content
    assert "DO  - 10.1234/qc.1" in content
    assert "UR  - https://doi.org/10.1234/qc.1" in content
    assert content.rstrip().endswith("ER  -")
    assert "owner/repo" not in content


def test_write_endnote_xml_export_writes_selected_papers_only(tmp_path):
    from daily_agent.storage import write_endnote_xml_export

    config = load_config(str(Path(__file__).resolve().parents[1]))
    object.__setattr__(config, "root", tmp_path)
    paper = _approved_paper()
    repo_material = MaterialRecord(key="github:owner/repo", source="github", item_type="repo", title="owner/repo", url="https://github.com/owner/repo")
    repo = ApprovedItem(key=repo_material.key, item_type="repo", title=repo_material.title, source="github", url=repo_material.url, final_fields={}, material=repo_material)

    path = write_endnote_xml_export(config, [paper, repo], date(2026, 5, 18), dry_run=True)

    assert path.name == "selected-2026-05-18.dry-run.xml"
    root = ET.fromstring(path.read_text(encoding="utf-8"))
    titles = [node.text for node in root.findall(".//title")]
    authors = [node.text for node in root.findall(".//author")]
    assert "Quantum {Circuit} Routing & Agents" in titles
    assert "Journal of Tests" in [node.text for node in root.findall(".//secondary-title")]
    assert authors == ["Alice Zhang", "Bob Li"]
    assert root.findtext(".//year") == "2026"
    assert root.findtext(".//electronic-resource-num") == "10.1234/qc.1"
    assert root.findtext(".//related-urls") == "https://doi.org/10.1234/qc.1"
    assert "owner/repo" not in "".join(titles)


def test_write_csv_export_writes_reading_queue_for_all_selected_items(tmp_path):
    from daily_agent.storage import write_csv_export

    config = load_config(str(Path(__file__).resolve().parents[1]))
    object.__setattr__(config, "root", tmp_path)
    paper = _approved_paper()
    paper.material.pdf_url = "https://paper.test/qc.pdf"
    paper.material.score = 42.5
    paper.material.tags = ["quantum", "agent"]
    repo_material = MaterialRecord(
        key="github:owner/repo",
        source="github",
        item_type="repo",
        title="owner/repo",
        url="https://github.com/owner/repo",
        authors=[],
        score=21.0,
        tags=["tooling"],
    )
    repo = ApprovedItem(key=repo_material.key, item_type="repo", title=repo_material.title, source="github", url=repo_material.url, final_fields={}, material=repo_material)

    path = write_csv_export(config, [paper, repo], date(2026, 5, 18), dry_run=True)

    assert path.name == "selected-2026-05-18.dry-run.csv"
    rows = list(csv.DictReader(path.read_text(encoding="utf-8").splitlines()))
    assert rows[0]["rank"] == "1"
    assert rows[0]["item_type"] == "paper"
    assert rows[0]["title"] == "Quantum {Circuit} Routing & Agents"
    assert rows[0]["authors"] == "Alice Zhang; Bob Li"
    assert rows[0]["doi"] == "10.1234/qc.1"
    assert rows[0]["pdf_url"] == "https://paper.test/qc.pdf"
    assert rows[0]["score"] == "42.5"
    assert rows[0]["tags"] == "quantum; agent"
    assert rows[1]["rank"] == "2"
    assert rows[1]["item_type"] == "repo"
    assert rows[1]["url"] == "https://github.com/owner/repo"


def test_pipeline_writes_bibtex_sidecar(tmp_path, monkeypatch):
    from daily_agent import pipeline

    config = load_config(str(Path(__file__).resolve().parents[1]))
    object.__setattr__(config, "root", tmp_path)
    paper = _approved_paper(title="Daily Agent Paper", doi="10.1234/daily.1", key="doi:10.1234/daily.1")

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

    result = pipeline.run_pipeline(root=tmp_path, run_date=date(2026, 5, 18), dry_run=True, use_llm=False)

    assert result.bibtex_path.name == "selected-2026-05-18.dry-run.bib"
    assert result.bibtex_path.exists()
    assert "Daily Agent Paper" in result.bibtex_path.read_text(encoding="utf-8")
    assert result.ris_path.name == "selected-2026-05-18.dry-run.ris"
    assert result.ris_path.exists()
    assert "Daily Agent Paper" in result.ris_path.read_text(encoding="utf-8")
    assert result.csv_path.name == "selected-2026-05-18.dry-run.csv"
    assert result.csv_path.exists()
    assert "Daily Agent Paper" in result.csv_path.read_text(encoding="utf-8")
    assert result.endnote_xml_path.name == "selected-2026-05-18.dry-run.xml"
    assert result.endnote_xml_path.exists()
    assert "Daily Agent Paper" in result.endnote_xml_path.read_text(encoding="utf-8")


def test_cleanup_retention_removes_old_bibtex_exports(tmp_path):
    from daily_agent.storage import cleanup_retention

    config = load_config(str(Path(__file__).resolve().parents[1]))
    object.__setattr__(config, "root", tmp_path)
    config.delivery["retention"]["selected_keep_weeks"] = 1
    old = config.selected_dir / "selected-2026-05-01.bib"
    old.parent.mkdir(parents=True)
    old.write_text("@article{old}", encoding="utf-8")
    old_time = datetime(2026, 5, 1, tzinfo=timezone.utc).timestamp()
    os.utime(old, (old_time, old_time))

    cleanup_retention(config, date(2026, 5, 20))

    assert not old.exists()


def test_cleanup_retention_removes_old_ris_exports(tmp_path):
    from daily_agent.storage import cleanup_retention

    config = load_config(str(Path(__file__).resolve().parents[1]))
    object.__setattr__(config, "root", tmp_path)
    config.delivery["retention"]["selected_keep_weeks"] = 1
    old = config.selected_dir / "selected-2026-05-01.ris"
    old.parent.mkdir(parents=True)
    old.write_text("TY  - JOUR\nER  -", encoding="utf-8")
    old_time = datetime(2026, 5, 1, tzinfo=timezone.utc).timestamp()
    os.utime(old, (old_time, old_time))

    cleanup_retention(config, date(2026, 5, 20))

    assert not old.exists()


def test_cleanup_retention_removes_old_csv_exports(tmp_path):
    from daily_agent.storage import cleanup_retention

    config = load_config(str(Path(__file__).resolve().parents[1]))
    object.__setattr__(config, "root", tmp_path)
    config.delivery["retention"]["selected_keep_weeks"] = 1
    old = config.selected_dir / "selected-2026-05-01.csv"
    old.parent.mkdir(parents=True)
    old.write_text("rank,title\n1,old", encoding="utf-8")
    old_time = datetime(2026, 5, 1, tzinfo=timezone.utc).timestamp()
    os.utime(old, (old_time, old_time))

    cleanup_retention(config, date(2026, 5, 20))

    assert not old.exists()


def test_cleanup_retention_removes_old_endnote_xml_exports(tmp_path):
    from daily_agent.storage import cleanup_retention

    config = load_config(str(Path(__file__).resolve().parents[1]))
    object.__setattr__(config, "root", tmp_path)
    config.delivery["retention"]["selected_keep_weeks"] = 1
    old = config.selected_dir / "selected-2026-05-01.xml"
    old.parent.mkdir(parents=True)
    old.write_text("<?xml version='1.0' encoding='utf-8'?><xml />", encoding="utf-8")
    old_time = datetime(2026, 5, 1, tzinfo=timezone.utc).timestamp()
    os.utime(old, (old_time, old_time))

    cleanup_retention(config, date(2026, 5, 20))

    assert not old.exists()
