import json
from datetime import date, datetime, timezone

from daily_agent.config import load_config
from daily_agent.connectors.paper_text import _candidate_pdf_urls, enrich_paper_texts, extract_html_text, extract_pdf_text, select_paper_excerpt
from daily_agent.editorial import approve_publication, build_shortlist, draft_report_items, review_draft
from daily_agent.models import ApprovedItem, DigestItem, MaterialRecord, RunStatus, SelectedRecord
from daily_agent.rendering.markdown import render_daily_markdown
from daily_agent.scoring.dedup import deduplicate_items
from daily_agent.scoring.rules import score_items, select_items
from daily_agent.storage import load_material_library, mark_materials_published, select_library_candidates, upsert_materials, write_material_library


def _stub_other_pipeline_sources(monkeypatch):
    for name in [
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
        monkeypatch.setattr(f"daily_agent.pipeline.{name}", lambda config, target_dt, window_days=None: [])
    monkeypatch.setattr("daily_agent.pipeline.enrich_paper_texts", lambda records, config: records)


def test_arxiv_dedup_keeps_latest_version():
    old = DigestItem(
        id="2401.00001",
        source="arxiv",
        item_type="paper",
        title="Old",
        url="https://arxiv.org/abs/2401.00001v1",
        arxiv_id="2401.00001",
        arxiv_version="v1",
    )
    new = DigestItem(
        id="2401.00001",
        source="arxiv",
        item_type="paper",
        title="New",
        url="https://arxiv.org/abs/2401.00001v2",
        arxiv_id="2401.00001",
        arxiv_version="v2",
    )
    items = deduplicate_items([old, new])
    assert len(items) == 1
    assert items[0].title == "New"
    assert items[0].update_label == "version_update"


def test_github_dedup_merges_source_tags():
    first = DigestItem(
        id="owner/repo",
        source="github",
        item_type="repo",
        title="owner/repo",
        url="https://github.com/owner/repo",
        source_tags=["search"],
    )
    second = DigestItem(
        id="owner/repo",
        source="github",
        item_type="repo",
        title="owner/repo",
        url="https://github.com/owner/repo",
        source_tags=["trending"],
    )
    items = deduplicate_items([first, second])
    assert len(items) == 1
    assert items[0].source_tags == ["search", "trending"]


def test_score_items_does_not_accumulate_rule_breakdown():
    config = load_config("/Users/wuzixie/Daily_Agent")
    item = DigestItem(
        id="2401.00002",
        source="arxiv",
        item_type="paper",
        title="Quantum circuit optimization",
        url="https://arxiv.org/abs/2401.00002v1",
        pdf_url="https://arxiv.org/pdf/2401.00002v1",
        abstract="A paper about quantum circuit optimization.",
        categories=["quant-ph"],
        arxiv_id="2401.00002",
        arxiv_version="v1",
        updated_at="2026-05-16T00:00:00+00:00",
    )
    first = score_items([item], config, {}, datetime(2026, 5, 16, tzinfo=timezone.utc))[0].score
    second = score_items([item], config, {}, datetime(2026, 5, 16, tzinfo=timezone.utc))[0].score
    assert first == second


def test_material_upsert_deduplicates_by_key(tmp_path):
    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)
    item = DigestItem(
        id="owner/repo",
        source="github",
        item_type="repo",
        title="owner/repo",
        url="https://github.com/owner/repo",
        repo_description="An MCP server for agents.",
        stars=10,
        updated_at="2026-05-16T00:00:00+00:00",
    )
    library = upsert_materials(config, [item, item], date(2026, 5, 16))
    assert list(library) == ["github:owner/repo"]


def test_select_library_candidates_suppresses_recent_published_without_update_label(tmp_path):
    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)
    record = MaterialRecord(
        key="github:owner/repo",
        source="github",
        item_type="repo",
        title="owner/repo",
        url="https://github.com/owner/repo",
        score=99,
        quality_status="published",
        published_dates=["2026-05-17"],
    )

    candidates = select_library_candidates(config, {record.key: record}, date(2026, 5, 18))

    assert candidates == []


def test_select_library_candidates_allows_recent_published_with_update_label(tmp_path):
    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)
    record = MaterialRecord(
        key="arxiv:2401.00001",
        source="arxiv",
        item_type="paper",
        title="Updated Paper",
        url="https://arxiv.org/abs/2401.00001v2",
        score=99,
        quality_status="published",
        published_dates=["2026-05-17"],
        update_label="version_update",
    )

    candidates = select_library_candidates(config, {record.key: record}, date(2026, 5, 18))

    assert candidates == [record]


def test_mark_materials_published_consumes_update_label(tmp_path):
    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)
    record = MaterialRecord(
        key="github:owner/repo",
        source="github",
        item_type="repo",
        title="owner/repo",
        url="https://github.com/owner/repo",
        score=99,
        update_label="major_update",
    )
    write_material_library(config, {record.key: record})

    mark_materials_published(config, [record], date(2026, 5, 18))

    stored = load_material_library(config)[record.key]
    assert stored.published_dates == ["2026-05-18"]
    assert stored.quality_status == "published"
    assert stored.update_label is None


def test_github_major_update_history_gate():
    config = load_config("/Users/wuzixie/Daily_Agent")
    previous = SelectedRecord(
        key="github:owner/repo",
        selected_at="2026-05-17",
        rank=1,
        source="github",
        item_type="repo",
        title="owner/repo",
        url="https://github.com/owner/repo",
        github_pushed_at="2026-05-16T00:00:00Z",
        stars=100,
    )
    major = DigestItem(
        id="owner/repo",
        source="github",
        item_type="repo",
        title="Agent repo",
        url="https://github.com/owner/repo",
        repo_description="An agent repo",
        updated_at="2026-05-18T00:00:00+00:00",
        stars=160,
        raw={"pushed_at": "2026-05-18T00:00:00Z"},
    )
    minor = DigestItem(
        id="owner/repo2",
        source="github",
        item_type="repo",
        title="Agent repo two",
        url="https://github.com/owner/repo2",
        repo_description="An agent repo",
        updated_at="2026-05-18T00:00:00+00:00",
        stars=120,
        raw={"pushed_at": "2026-05-18T00:00:00Z"},
    )
    previous_minor = SelectedRecord(
        key="github:owner/repo2",
        selected_at="2026-05-17",
        rank=2,
        source="github",
        item_type="repo",
        title="owner/repo2",
        url="https://github.com/owner/repo2",
        github_pushed_at="2026-05-16T00:00:00Z",
        stars=100,
    )

    scored = score_items([major, minor], config, {previous.key: previous, previous_minor.key: previous_minor}, datetime(2026, 5, 18, tzinfo=timezone.utc))
    by_key = {item.canonical_key(): item for item in scored}

    assert by_key["github:owner/repo"].update_label == "major_update"
    assert by_key["github:owner/repo"].score_breakdown["history"] == 7.0
    assert by_key["github:owner/repo2"].update_label is None
    assert by_key["github:owner/repo2"].score_breakdown["history"] == -100.0
    assert select_items(scored, config) == [by_key["github:owner/repo"]]


def test_selected_record_stores_github_release_and_tag_baseline():
    item = DigestItem(
        id="owner/repo",
        source="github",
        item_type="repo",
        title="owner/repo",
        url="https://github.com/owner/repo",
        raw={
            "pushed_at": "2026-05-16T00:00:00Z",
            "latest_release_tag": "v1.0.0",
            "latest_release_published_at": "2026-05-16T01:00:00Z",
            "latest_tag_name": "v1.0.0",
        },
    )
    material = MaterialRecord.from_item(item, seen_at="2026-05-17T00:00:00+00:00")

    from_item = SelectedRecord.from_item(item)
    from_material = SelectedRecord.from_material(material)

    assert from_item.github_latest_release_tag == "v1.0.0"
    assert from_item.github_latest_release_published_at == "2026-05-16T01:00:00Z"
    assert from_item.github_latest_tag_name == "v1.0.0"
    assert from_material.github_latest_release_tag == "v1.0.0"
    assert from_material.github_latest_release_published_at == "2026-05-16T01:00:00Z"
    assert from_material.github_latest_tag_name == "v1.0.0"


def test_github_release_or_tag_change_counts_as_major_update():
    config = load_config("/Users/wuzixie/Daily_Agent")
    previous_release = SelectedRecord(
        key="github:owner/release-repo",
        selected_at="2026-05-17",
        rank=1,
        source="github",
        item_type="repo",
        title="owner/release-repo",
        url="https://github.com/owner/release-repo",
        github_pushed_at="2026-05-16T00:00:00Z",
        stars=100,
        github_latest_release_tag="v1.0.0",
        github_latest_release_published_at="2026-05-16T01:00:00Z",
    )
    previous_tag = SelectedRecord(
        key="github:owner/tag-repo",
        selected_at="2026-05-17",
        rank=2,
        source="github",
        item_type="repo",
        title="owner/tag-repo",
        url="https://github.com/owner/tag-repo",
        github_pushed_at="2026-05-16T00:00:00Z",
        stars=100,
        github_latest_tag_name="v1.0.0",
    )
    release_item = DigestItem(
        id="owner/release-repo",
        source="github",
        item_type="repo",
        title="Release repo",
        url="https://github.com/owner/release-repo",
        repo_description="An agent repo",
        updated_at="2026-05-18T00:00:00+00:00",
        stars=105,
        raw={
            "pushed_at": "2026-05-16T00:00:00Z",
            "latest_release_tag": "v1.1.0",
            "latest_release_published_at": "2026-05-18T01:00:00Z",
        },
    )
    tag_item = DigestItem(
        id="owner/tag-repo",
        source="github",
        item_type="repo",
        title="Tag repo",
        url="https://github.com/owner/tag-repo",
        repo_description="An agent repo",
        updated_at="2026-05-18T00:00:00+00:00",
        stars=105,
        raw={"pushed_at": "2026-05-16T00:00:00Z", "latest_tag_name": "v1.1.0"},
    )

    scored = score_items(
        [release_item, tag_item],
        config,
        {previous_release.key: previous_release, previous_tag.key: previous_tag},
        datetime(2026, 5, 18, tzinfo=timezone.utc),
    )
    by_key = {item.canonical_key(): item for item in scored}

    assert by_key["github:owner/release-repo"].update_label == "major_update"
    assert by_key["github:owner/release-repo"].score_breakdown["history"] == 7.0
    assert by_key["github:owner/tag-repo"].update_label == "major_update"
    assert by_key["github:owner/tag-repo"].score_breakdown["history"] == 7.0


def test_pipeline_shortlist_uses_library_candidate_suppression(tmp_path, monkeypatch):
    from daily_agent.pipeline import run_pipeline

    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)
    repeated = MaterialRecord(
        key="github:old/repo",
        source="github",
        item_type="repo",
        title="old/repo",
        url="https://github.com/old/repo",
        repo_description="An agent framework with reusable tools.",
        readme_excerpt="Provides agent tools, APIs, and examples.",
        stars=1000,
        language="Python",
        tags=["agent"],
        score=999,
        quality_status="published",
        published_dates=["2026-05-17"],
    )
    fresh = MaterialRecord(
        key="github:new/repo",
        source="github",
        item_type="repo",
        title="new/repo",
        url="https://github.com/new/repo",
        repo_description="An agent toolkit with MCP integrations.",
        readme_excerpt="Includes MCP server APIs and examples for coding agents.",
        stars=100,
        language="Python",
        tags=["agent"],
        score=100,
    )
    write_material_library(config, {repeated.key: repeated, fresh.key: fresh})
    monkeypatch.setattr("daily_agent.pipeline.load_config", lambda root=None: config)
    monkeypatch.setattr("daily_agent.pipeline.fetch_arxiv", lambda config, target_dt, window_days=None: [])
    monkeypatch.setattr("daily_agent.pipeline.fetch_github", lambda config, target_dt, window_days=None: [])
    _stub_other_pipeline_sources(monkeypatch)

    result = run_pipeline(root=tmp_path, run_date=date(2026, 5, 18), dry_run=True, use_llm=False)

    assert [item.key for item in result.items] == [fresh.key]


def test_pipeline_enriches_paper_text_before_rule_writer_when_llm_disabled(tmp_path, monkeypatch):
    from daily_agent.pipeline import run_pipeline

    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)
    config.quota["max_items"] = 1
    config.quota["paper_target"] = 1
    config.quota["github_target"] = 0
    config.quota["paper_review_multiplier"] = 1
    config.sources["paper_text"] = {"enabled": True, "max_papers_per_run": 1}
    paper = MaterialRecord(
        key="arxiv:2401.00999",
        source="arxiv",
        item_type="paper",
        title="Quantum compilation with sparse routing",
        url="https://arxiv.org/abs/2401.00999v1",
        pdf_url="https://arxiv.org/pdf/2401.00999v1",
        abstract="This paper studies a relevant quantum compilation problem.",
        tags=["quantum_ai"],
        score=99,
    )
    write_material_library(config, {paper.key: paper})
    monkeypatch.setattr("daily_agent.pipeline.load_config", lambda root=None: config)
    monkeypatch.setattr("daily_agent.pipeline.fetch_arxiv", lambda config, target_dt, window_days=None: [])
    monkeypatch.setattr("daily_agent.pipeline.fetch_github", lambda config, target_dt, window_days=None: [])
    _stub_other_pipeline_sources(monkeypatch)
    calls = []

    def fake_enrich_paper_texts(records, config):
        calls.append([record.key for record in records])
        for record in records:
            if record.key == paper.key:
                record.paper_text_excerpt = (
                    "Methods. We propose a sparse routing compilation method that models qubit placement and swap insertion together. "
                    "This works because the optimizer penalizes long-range moves before mapping gates to hardware. "
                    "Experiments show a 19% reduction in two-qubit gate depth. The limitation is evaluation on simulated devices."
                )
        return records

    monkeypatch.setattr("daily_agent.pipeline.enrich_paper_texts", fake_enrich_paper_texts)

    result = run_pipeline(root=tmp_path, run_date=date(2026, 5, 18), dry_run=True, use_llm=False)

    assert calls == [[paper.key]]
    assert result.items[0].key == paper.key
    assert result.items[0].final_fields["method"] != "not_stated"
    assert "19% reduction" in result.items[0].final_fields["key_result"]


def test_editorial_flow_approves_specific_draft():
    config = load_config("/Users/wuzixie/Daily_Agent")
    material = MaterialRecord(
        key="github:owner/repo",
        source="github",
        item_type="repo",
        title="owner/repo",
        url="https://github.com/owner/repo",
        repo_description="An MCP server that exposes Xcode build tools to coding agents.",
        readme_excerpt="Provides MCP tools for build, test, and project inspection through a server interface.",
        stars=100,
        language="TypeScript",
        source_updated_at="2026-05-16T00:00:00+00:00",
        tags=["mcp", "agent"],
        score=99,
    )
    drafts = draft_report_items(config, [material], use_llm=False)
    reviews = review_draft(config, drafts, use_llm=False)
    approved = approve_publication(config, [material], drafts, reviews)
    assert reviews[0].verdict == "PASS"
    assert approved[0].final_fields["core_capabilities"] != "not_stated"


def test_editorial_review_rejects_paper_without_method():
    config = load_config("/Users/wuzixie/Daily_Agent")
    material = MaterialRecord(
        key="arxiv:2401.00003",
        source="arxiv",
        item_type="paper",
        title="A Paper With No Method Evidence",
        url="https://arxiv.org/abs/2401.00003v1",
        abstract="This paper discusses a relevant quantum research problem but does not describe a concrete method or result.",
        tags=["quantum_ai"],
        score=99,
    )
    drafts = draft_report_items(config, [material], use_llm=False)
    reviews = review_draft(config, drafts, use_llm=False)
    approved = approve_publication(config, [material], drafts, reviews)
    assert reviews[0].verdict == "FAIL"
    assert approved == []


def test_paper_text_excerpt_supplies_missing_method():
    config = load_config("/Users/wuzixie/Daily_Agent")
    material = MaterialRecord(
        key="arxiv:2401.00006",
        source="arxiv",
        item_type="paper",
        title="Quantum compilation with sparse routing",
        url="https://arxiv.org/abs/2401.00006v1",
        abstract="This paper studies a relevant quantum compilation problem.",
        paper_text_excerpt=(
            "Methods. We propose a quantum compilation method based on sparse qubit allocation and routing optimization. "
            "Experiments show a 17% reduction in circuit depth. The limitation is evaluation on simulated devices."
        ),
        tags=["quantum_ai"],
        score=99,
    )

    drafts = draft_report_items(config, [material], use_llm=False)
    reviews = review_draft(config, drafts, use_llm=False)
    approved = approve_publication(config, [material], drafts, reviews)

    assert reviews[0].verdict == "PASS"
    assert approved[0].final_fields["method"] != "not_stated"
    assert "Experiments show" in approved[0].final_fields["key_result"]


def test_llm_draft_falls_back_to_rule_method_when_method_is_not_stated(monkeypatch):
    config = load_config("/Users/wuzixie/Daily_Agent")
    material = MaterialRecord(
        key="arxiv:2605.28599",
        source="arxiv",
        item_type="paper",
        title="Thermodynamic-limit dispersion relations on trapped-ion quantum hardware",
        url="https://arxiv.org/abs/2605.28599v1",
        abstract=(
            "We run a numerical linked-cluster expansion with a quantum algorithm (NLCE+QA), "
            "computing ground-state energies and one quasi-particle dispersions in the thermodynamic limit "
            "using a 20-qubit trapped-ion quantum processing unit."
        ),
        paper_text_excerpt="Our approach combines NLCEs with quantum algorithms and evaluates the pipeline on trapped-ion hardware.",
        tags=["quantum_ai"],
        score=99,
    )
    payload = [
        {
            "key": material.key,
            "draft_fields": {
                "problem": "量子多体热力学极限计算在真机上仍受噪声和规模限制。",
                "method": "not_stated",
                "why_it_works": "not_stated",
                "method_steps": ["not_stated"],
                "key_result": "not_stated",
                "technical_route": "not_stated",
                "possible_use_or_impact": "not_stated",
                "limitations": "not_stated",
                "evidence_from_source": "not_stated",
                "confidence": "low",
            },
            "evidence_used": ["abstract"],
            "writer_notes": "LLM draft",
        }
    ]

    class Result:
        stdout = json.dumps(payload)

    monkeypatch.setattr("daily_agent.editorial.subprocess.run", lambda *args, **kwargs: Result())

    drafts = draft_report_items(config, [material], use_llm=True)
    reviews = review_draft(config, drafts, use_llm=False)
    approved = approve_publication(config, [material], drafts, reviews)

    assert drafts[0].draft_fields["method"] != "not_stated"
    assert "numerical linked-cluster expansion" in drafts[0].draft_fields["method"]
    assert drafts[0].draft_fields["why_it_works"] != "not_stated"
    assert reviews[0].verdict == "PASS"
    assert approved


def test_pdf_text_extraction_handles_simple_text_stream():
    stream = b"BT (Methods. We propose a quantum compilation method.) Tj ET"
    compressed = __import__("zlib").compress(stream)
    pdf = b"%PDF-1.4\n1 0 obj\nstream\n" + compressed + b"\nendstream\nendobj\n%%EOF"

    text = extract_pdf_text(pdf)

    assert "quantum compilation method" in text


def test_arxiv_doi_pdf_candidate_prefers_arxiv_pdf():
    material = MaterialRecord(
        key="doi:10.48550/arxiv.2606.04955",
        source="openalex",
        item_type="paper",
        title="Expressibility, Noise, and Error Mitigation in VQE Ansatz Selection",
        url="https://doi.org/10.48550/arxiv.2606.04955",
        pdf_url="https://doi.org/10.48550/arxiv.2606.04955",
        doi="10.48550/arxiv.2606.04955",
        tags=["quantum_ai"],
        score=99,
    )

    urls = _candidate_pdf_urls(material)

    assert urls[0] == "https://arxiv.org/pdf/2606.04955"
    assert "https://doi.org/10.48550/arxiv.2606.04955" in urls


def test_pdf_candidates_include_multi_source_pdf_url_lists():
    material = MaterialRecord(
        key="google_scholar:GS-1",
        source="google_scholar",
        item_type="paper",
        title="Scholar paper with multiple resources",
        url="https://publisher.test/paper",
        pdf_url="https://publisher.test/first.pdf",
        evidence={
            "sources": {
                "google_scholar": {
                    "pdf_url": "https://publisher.test/first.pdf",
                    "pdf_urls": ["https://publisher.test/first.pdf", "https://open.test/second.pdf"],
                }
            }
        },
    )

    urls = _candidate_pdf_urls(material)

    assert urls[:2] == ["https://publisher.test/first.pdf", "https://open.test/second.pdf"]


def test_select_paper_excerpt_prioritizes_method_section():
    text = "Background. " + ("filler " * 500) + "Methods. We propose a quantum compilation method. Results show improvement."

    excerpt = select_paper_excerpt(text, max_chars=200)

    assert "Methods" in excerpt
    assert "quantum compilation method" in excerpt


def test_select_paper_excerpt_keeps_method_result_and_limitation_sections():
    text = (
        "Introduction. "
        + ("background filler " * 120)
        + "Methods. We propose a sparse routing compiler for quantum circuits. "
        + ("method filler " * 120)
        + "Results. Experiments show a 19% reduction in two-qubit gate depth. "
        + ("result filler " * 120)
        + "Limitations. The evaluation only covers simulated devices."
    )

    excerpt = select_paper_excerpt(text, max_chars=420)

    assert "Methods" in excerpt
    assert "Results" in excerpt
    assert "Limitations" in excerpt


def test_html_text_extraction_removes_boilerplate_and_keeps_article_sections():
    html = """
    <html>
      <head><title>Paper</title><script>ignore()</script><style>.x{}</style></head>
      <body>
        <nav>Navigation should not appear</nav>
        <article>
          <h1>Quantum Routing from HTML</h1>
          <p>Abstract. This paper studies hardware-aware quantum compilation.</p>
          <h2>Introduction</h2>
          <p>Existing compilers add too many swaps on sparse devices.</p>
          <h2>Method</h2>
          <p>We propose a routing method that jointly optimizes placement and swap insertion.</p>
          <h2>Results</h2>
          <p>Experiments show a 21% reduction in two-qubit depth.</p>
          <h2>Limitations</h2>
          <p>The evaluation covers only simulated superconducting devices.</p>
        </article>
      </body>
    </html>
    """

    text = extract_html_text(html.encode("utf-8"), max_chars=800)

    assert "Quantum Routing from HTML" in text
    assert "jointly optimizes placement" in text
    assert "21% reduction" in text
    assert "simulated superconducting devices" in text
    assert "Navigation should not appear" not in text
    assert "ignore()" not in text


def test_enrich_paper_texts_falls_back_to_html_when_no_pdf_url(tmp_path, monkeypatch):
    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)
    config.sources["paper_text"] = {
        "enabled": True,
        "max_papers_per_run": 1,
        "max_excerpt_chars": 800,
        "html_fallback_enabled": True,
    }
    material = MaterialRecord(
        key="openreview:html-paper",
        source="openreview",
        item_type="paper",
        title="HTML-only quantum compiler",
        url="https://openreview.net/forum?id=html-paper",
        abstract="A short abstract.",
        tags=["quantum_ai"],
        score=99,
    )

    class Response:
        status_code = 200
        content = (
            b"<html><body><main><h1>HTML-only quantum compiler</h1>"
            b"<h2>Method</h2><p>We propose an HTML fallback routing method for quantum circuits.</p>"
            b"<h2>Results</h2><p>Experiments show a 17% reduction in swap count.</p>"
            b"<h2>Limitations</h2><p>The limitation is that tests use simulated hardware.</p>"
            b"</main></body></html>"
        )
        headers = {"content-type": "text/html; charset=utf-8"}

        def raise_for_status(self):
            return None

    class Client:
        def __init__(self, *args, **kwargs):
            self.urls = []

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def get(self, url):
            self.urls.append(url)
            return Response()

    monkeypatch.setattr("daily_agent.connectors.paper_text.httpx.Client", Client)

    enriched = enrich_paper_texts([material], config)

    assert "HTML fallback routing method" in enriched[0].paper_text_excerpt
    assert "17% reduction" in enriched[0].paper_text_excerpt


def test_enrich_paper_texts_treats_non_pdf_pdf_url_as_html_landing_page(tmp_path, monkeypatch):
    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)
    config.sources["paper_text"] = {
        "enabled": True,
        "max_papers_per_run": 1,
        "max_excerpt_chars": 800,
        "html_fallback_enabled": True,
    }
    material = MaterialRecord(
        key="doi:10.1234/html-landing",
        source="crossref",
        item_type="paper",
        title="Landing page only quantum compiler",
        url="",
        pdf_url="https://doi.org/10.1234/html-landing",
        abstract="A short abstract.",
        tags=["quantum_ai"],
        score=99,
    )

    class Response:
        status_code = 200
        content = (
            b"<html><body><article><h1>Landing page only quantum compiler</h1>"
            b"<h2>Method</h2><p>The method extracts HTML landing-page evidence for quantum compilation.</p>"
            b"<h2>Results</h2><p>Experiments show a 14% reduction in routing overhead.</p>"
            b"</article></body></html>"
        )
        headers = {"content-type": "text/html; charset=utf-8"}

        def raise_for_status(self):
            return None

    class Client:
        def __init__(self, *args, **kwargs):
            self.urls = []

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def get(self, url):
            self.urls.append(url)
            return Response()

    monkeypatch.setattr("daily_agent.connectors.paper_text.httpx.Client", Client)

    enriched = enrich_paper_texts([material], config)

    assert "HTML landing-page evidence" in enriched[0].paper_text_excerpt
    assert "14% reduction" in enriched[0].paper_text_excerpt


def test_enrich_paper_texts_scans_beyond_final_excerpt_budget_for_html_sections(tmp_path, monkeypatch):
    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)
    config.sources["paper_text"] = {
        "enabled": True,
        "max_papers_per_run": 1,
        "max_excerpt_chars": 360,
        "max_raw_text_chars": 4_000,
        "html_fallback_enabled": True,
    }
    material = MaterialRecord(
        key="openreview:deep-html-paper",
        source="openreview",
        item_type="paper",
        title="Deep HTML paper",
        url="https://openreview.net/forum?id=deep-html-paper",
        abstract="A short abstract.",
        tags=["quantum_ai"],
        score=99,
    )
    filler = ("<p>Introduction filler about background.</p>" * 80).encode("utf-8")

    class Response:
        status_code = 200
        content = (
            b"<html><body><article><h1>Deep HTML paper</h1>"
            b"<p>Abstract. This paper studies hardware-aware quantum compilation.</p>"
            + filler
            + b"<h2>Method</h2><p>Method. We jointly optimize placement and swap insertion for sparse quantum hardware.</p>"
            + b"<h2>Results</h2><p>Results. Experiments show a 22% reduction in two-qubit depth.</p>"
            + b"<h2>Limitations</h2><p>Limitations. The evaluation covers only simulated devices.</p>"
            + b"</article></body></html>"
        )

        def raise_for_status(self):
            return None

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def get(self, url):
            return Response()

    monkeypatch.setattr("daily_agent.connectors.paper_text.httpx.Client", Client)

    enriched = enrich_paper_texts([material], config)
    excerpt = enriched[0].paper_text_excerpt

    assert len(excerpt) <= 4000
    assert enriched[0].paper_document["chunks"]
    assert "jointly optimize placement" in excerpt
    assert "22% reduction" in excerpt
    assert "simulated devices" in excerpt


def test_enrich_paper_texts_records_section_coverage_status(tmp_path, monkeypatch):
    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)
    config.sources["paper_text"] = {
        "enabled": True,
        "max_papers_per_run": 1,
        "max_excerpt_chars": 500,
        "max_raw_text_chars": 4_000,
        "html_fallback_enabled": True,
    }
    material = MaterialRecord(
        key="openreview:coverage-html-paper",
        source="openreview",
        item_type="paper",
        title="Coverage HTML paper",
        url="https://openreview.net/forum?id=coverage-html-paper",
        abstract="A short abstract.",
        tags=["quantum_ai"],
        score=99,
    )

    class Response:
        status_code = 200
        content = (
            b"<html><body><article><h1>Coverage HTML paper</h1>"
            b"<p>Abstract. This paper studies hardware-aware quantum compilation.</p>"
            b"<h2>Method</h2><p>Method. We jointly optimize placement and swap insertion.</p>"
            b"<h2>Results</h2><p>Results. Experiments show a 24% reduction in two-qubit depth.</p>"
            b"<h2>Limitations</h2><p>Limitations. The evaluation covers only simulated devices.</p>"
            b"</article></body></html>"
        )

        def raise_for_status(self):
            return None

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def get(self, url):
            return Response()

    monkeypatch.setattr("daily_agent.connectors.paper_text.httpx.Client", Client)

    enriched = enrich_paper_texts([material], config)
    status = enriched[0].paper_text_status

    assert status["available"] is True
    assert status["source_type"] == "html"
    assert status["sufficient_for_deep_summary"] is False  # parsing is not reading
    assert {"method", "results", "limitations"}.issubset(set(status["sections_found"]))


def test_paper_text_coverage_builds_section_notes_for_deep_summary():
    from daily_agent.connectors.paper_text import analyze_paper_text_coverage

    text = (
        "Abstract This paper studies hardware-aware quantum routing. "
        "Introduction Existing compilers add routing overhead on sparse devices. "
        "Methods We formulate routing as a constrained search over hardware coupling maps and noise-aware costs. "
        "Results Experiments show a 23% CNOT reduction and 17% lower circuit depth on benchmark circuits. "
        "Limitations The evaluation is limited to 12-qubit simulations and does not test all hardware backends. "
        "Conclusion The method suggests a practical path for hardware-aware compilation."
    )

    status = analyze_paper_text_coverage(text, source_type="pdf", source_url="https://paper.test/qc.pdf")

    notes = status.get("section_notes")
    assert isinstance(notes, dict)
    assert "constrained search" in notes["method"]
    assert "23% CNOT reduction" in notes["results"]
    assert "12-qubit simulations" in notes["limitations"]


def test_llm_prompt_includes_section_notes_before_raw_excerpt():
    from daily_agent import editorial

    record = MaterialRecord(
        key="arxiv:2605.00001",
        source="arxiv",
        item_type="paper",
        title="Hardware-aware quantum routing",
        url="https://arxiv.org/abs/2605.00001",
        abstract="abstract only",
        paper_text_excerpt="Methods and results are in the full text excerpt.",
        paper_text_status={
            "available": True,
            "sufficient_for_deep_summary": True,
            "section_notes": {
                "method": "Methods We formulate routing as constrained search.",
                "results": "Results Experiments show 23% CNOT reduction.",
                "limitations": "Limitations Evaluation is limited to 12-qubit simulations.",
            },
        },
    )

    prompt = editorial._llm_prompt([record])

    assert "paper_section_notes" in prompt
    assert "constrained search" in prompt
    assert prompt.index("paper_section_notes") < prompt.index("paper_text_excerpt")


def test_rule_writer_ignores_reference_only_method_names():
    from daily_agent import editorial

    record = MaterialRecord(
        key="arxiv:2607.07554",
        source="arxiv",
        item_type="paper",
        title="RubriQ: Rubric-Guided GRPO for Constraint-Aware Quantum Circuit Synthesis",
        url="https://arxiv.org/abs/2607.07554",
        abstract=(
            "RubriQ formulates hardware-constrained circuit synthesis as LLM code generation. "
            "It uses group relative policy optimization with a programmatic rubric reward. "
            "Experiments achieve 3.31x T-gate compression with less than 1% constraint violations."
        ),
        paper_text_excerpt=(
            "Methods RubriQ evaluates semantic correctness, T-gate cost, and hardware constraints. "
            "Related work includes QuTuner for compiler pass tuning."
        ),
        paper_text_status={
            "available": True,
            "sufficient_for_deep_summary": True,
            "section_notes": {
                "method": "RubriQ uses GRPO and a domain-grounded programmatic rubric as reward. Related work includes QuTuner.",
                "results": "RubriQ achieves 3.31x T-gate compression and less than 1% constraint violations.",
            },
        },
    )

    draft = editorial._rule_draft(record)

    assert "RubriQ" in draft.draft_fields["method"]
    assert "QuTuner" not in draft.draft_fields["method"]
    assert "pass 调优" not in draft.draft_fields["problem"]


def test_rule_writer_rejects_cookie_boilerplate_as_method():
    from daily_agent import editorial

    record = MaterialRecord(
        key="doi:10.0000/cookie",
        source="crossref",
        item_type="paper",
        title="Quantum processor architecture",
        url="https://publisher.test/cookie",
        abstract="This paper studies a quantum processor architecture.",
        paper_text_excerpt="Find out more on how we use cookies. Accept all cookies. Accept only essential cookies.",
        paper_text_status={
            "available": True,
            "sufficient_for_deep_summary": False,
            "section_notes": {
                "method": "Find out more on how we use cookies. Accept all cookies. Accept only essential cookies.",
            },
        },
    )

    draft = editorial._rule_draft(record)

    assert draft.draft_fields["method"] == "not_stated"


def test_fulltext_rule_fallback_qec_paper_passes_review():
    from daily_agent import editorial

    record = MaterialRecord(
        key="arxiv:2607.05814",
        source="arxiv",
        item_type="paper",
        title="Latency-Constrained Hardware-Aware Quantum Error Correction Co-Design",
        url="https://arxiv.org/abs/2607.05814",
        abstract=(
            "Real-time decoding is a bottleneck for surface-code QEC. "
            "A confidence-gated framework uses a neural fast path and sends low-confidence syndromes to MWPM."
        ),
        paper_text_excerpt=(
            "Methods A feed-forward neural network decodes most syndromes and escalates uncertain cases to MWPM. "
            "Results Routing 3.3%-6.2% of syndromes improves logical accuracy from 99.21% to 99.81%. "
            "Limitations Evaluation uses small code distances and simulated noise."
        ),
        paper_text_status={
            "available": True,
            "sufficient_for_deep_summary": True,
            "section_notes": {
                "introduction": "Real-time decoding latency limits practical surface-code deployment.",
                "method": "A neural fast path handles confident syndromes and MWPM refines low-confidence cases.",
                "results": "Routing 3.3%-6.2% of syndromes improves logical accuracy from 99.21% to 99.81%.",
                "limitations": "The evaluation uses small code distances and simulated noise.",
            },
        },
    )

    draft = editorial._rule_draft(record)
    review = editorial._rule_review(draft)

    assert review.verdict == "PASS"
    for field in ["method", "why_it_works", "key_result", "limitations"]:
        assert editorial._has_cjk(str(draft.draft_fields[field]))


def test_enrich_paper_texts_backfills_status_for_existing_excerpt(tmp_path):
    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)
    config.sources["paper_text"] = {"enabled": True, "max_papers_per_run": 1}
    material = MaterialRecord(
        key="arxiv:2601.00002",
        source="arxiv",
        item_type="paper",
        title="Existing excerpt paper",
        url="https://arxiv.org/abs/2601.00002",
        paper_text_excerpt=(
            "Abstract. This paper studies hardware-aware compilation. "
            "Method. We optimize placement and routing jointly. "
            "Results. Experiments show lower depth. "
            "Limitations. The evaluation covers simulated devices."
        ),
    )

    enriched = enrich_paper_texts([material], config)

    assert enriched[0].paper_text_status["available"] is True
    assert enriched[0].paper_text_status["sufficient_for_deep_summary"] is False
    assert enriched[0].paper_document["document_kind"] != "full_text"


def test_health_reports_full_text_evidence_gaps(tmp_path, monkeypatch):
    from daily_agent.health import evaluate_run_health
    from daily_agent.models import DeliveryStatus, EditorialDraft, EditorialReview

    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)
    monkeypatch.setattr("daily_agent.health.run_quality_check", lambda config: type("Profile", (), {"overall": "full", "checks": []})())
    paper = MaterialRecord(
        key="arxiv:2601.00001",
        source="arxiv",
        item_type="paper",
        title="Weak evidence paper",
        url="https://arxiv.org/abs/2601.00001",
        tags=["quantum_ai"],
        paper_text_status={
            "available": True,
            "sufficient_for_deep_summary": False,
            "sections_found": ["abstract"],
            "missing_sections": ["method", "results", "limitations"],
        },
    )
    approved = [
        ApprovedItem(
            key=paper.key,
            item_type="paper",
            title=paper.title,
            source=paper.source,
            url=paper.url,
            final_fields={"problem": "p", "method": "m", "why_it_works": "w", "method_steps": ["s"], "key_result": "r", "possible_use_or_impact": "i", "limitations": "l"},
            material=paper,
        )
    ]
    status = RunStatus(delivery=DeliveryStatus(requested_mode="local", final_mode="local", ok=True))
    editorial_path = tmp_path / "editorial"
    editorial_path.mkdir()
    for name in ["shortlist.json", "writer_draft.json", "editor_review.json", "approval.json"]:
        (editorial_path / name).write_text("[]", encoding="utf-8")
    weekly_report = tmp_path / "report.md"
    weekly_html = tmp_path / "report.html"
    selected = tmp_path / "selected.json"
    weekly_report.write_text("report", encoding="utf-8")
    weekly_html.write_text("html", encoding="utf-8")
    selected.write_text("{}", encoding="utf-8")

    health = evaluate_run_health(
        config,
        date(2026, 5, 18),
        True,
        status,
        approved,
        [paper],
        [EditorialDraft(key=paper.key, item_type="paper", title=paper.title, draft_fields=approved[0].final_fields, evidence_used=["evidence"])],
        [EditorialReview(key=paper.key, verdict="PASS", issues=[])],
        weekly_report,
        weekly_html,
        selected,
        editorial_path,
    )

    summary = health["current"]["summary"]
    checks = {check["id"]: check for check in health["current"]["checks"]}
    assert summary["paper_text_insufficient_count"] == 1
    assert checks["paper_text_evidence_gaps"]["status"] == "fail"


def test_health_counts_approved_paper_section_notes(tmp_path, monkeypatch):
    from daily_agent.health import evaluate_run_health
    from daily_agent.models import DeliveryStatus, EditorialDraft, EditorialReview

    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)
    monkeypatch.setattr("daily_agent.health.run_quality_check", lambda config: type("Profile", (), {"overall": "full", "checks": []})())
    paper = MaterialRecord(
        key="arxiv:2601.00001",
        source="arxiv",
        item_type="paper",
        title="Section notes paper",
        url="https://arxiv.org/abs/2601.00001",
        tags=["quantum_ai"],
        paper_text_status={
            "available": True,
            "sufficient_for_deep_summary": True,
            "sections_found": ["method", "results", "limitations"],
            "section_notes": {
                "method": "Methods use constrained routing.",
                "results": "Results show lower CNOT count.",
                "limitations": "Limitations mention small benchmarks.",
            },
        },
    )
    approved = [
        ApprovedItem(
            key=paper.key,
            item_type="paper",
            title=paper.title,
            source=paper.source,
            url=paper.url,
            final_fields={"problem": "p", "method": "m", "why_it_works": "w", "method_steps": ["s"], "key_result": "r", "possible_use_or_impact": "i", "limitations": "l"},
            material=paper,
        )
    ]
    status = RunStatus(delivery=DeliveryStatus(requested_mode="local", final_mode="local", ok=True))
    editorial_path = tmp_path / "editorial"
    editorial_path.mkdir()
    for name in ["shortlist.json", "writer_draft.json", "editor_review.json", "approval.json"]:
        (editorial_path / name).write_text("[]", encoding="utf-8")
    weekly_report = tmp_path / "report.md"
    weekly_html = tmp_path / "report.html"
    selected = tmp_path / "selected.json"
    weekly_report.write_text("report", encoding="utf-8")
    weekly_html.write_text("html", encoding="utf-8")
    selected.write_text("{}", encoding="utf-8")

    health = evaluate_run_health(
        config,
        date(2026, 5, 18),
        True,
        status,
        approved,
        [paper],
        [EditorialDraft(key=paper.key, item_type="paper", title=paper.title, draft_fields=approved[0].final_fields, evidence_used=["evidence"])],
        [EditorialReview(key=paper.key, verdict="PASS", issues=[])],
        weekly_report,
        weekly_html,
        selected,
        editorial_path,
    )

    assert health["current"]["summary"]["paper_text_section_note_count"] == 1


def test_rule_drafting_extracts_result_and_limitation_sentences():
    config = load_config("/Users/wuzixie/Daily_Agent")
    material = MaterialRecord(
        key="arxiv:2401.00005",
        source="arxiv",
        item_type="paper",
        title="Quantum compilation with learned routing",
        url="https://arxiv.org/abs/2401.00005v1",
        abstract=(
            "We propose a quantum compilation method for noisy circuits. "
            "Experiments show a 23% reduction in CNOT count on benchmark circuits. "
            "The main limitation is that evaluation covers only 12-qubit simulations."
        ),
        tags=["quantum_ai"],
        score=99,
    )

    draft = draft_report_items(config, [material], use_llm=False)[0]

    assert draft.draft_fields["key_result"] == "Experiments show a 23% reduction in CNOT count on benchmark circuits."
    assert draft.draft_fields["limitations"] == "局限方面，正文指出：The main limitation is that evaluation covers only 12-qubit simulations."


def test_llm_drafting_accepts_valid_structured_json(monkeypatch):
    config = load_config("/Users/wuzixie/Daily_Agent")
    material = MaterialRecord(
        key="arxiv:2401.00004",
        source="arxiv",
        item_type="paper",
        title="Validated LLM Paper",
        url="https://arxiv.org/abs/2401.00004v1",
        abstract="We propose a quantum compilation method and demonstrate improved circuit depth.",
        tags=["quantum_ai"],
        score=99,
    )
    payload = [
        {
            "key": material.key,
            "draft_fields": {
                "problem": "降低量子编译后的线路深度。",
                "method": "提出量子编译优化方法。",
                "why_it_works": "把硬件连通性和线路深度目标放进同一个优化过程，因此减少额外路由开销。",
                "novelty_or_difference": "相对已有工作，它把硬件连通性和线路深度目标一起放进优化过程。",
                "method_steps": ["建模线路", "搜索优化", "评估深度"],
                "key_result": "实验显示线路深度降低。",
                "technical_route": "量子编译优化路线。",
                "possible_use_or_impact": "可用于量子线路优化。",
                "limitations": "not_stated",
                "evidence_from_source": "abstract evidence",
                "confidence": "medium",
                "unknown_future_field": "ignored",
            },
            "evidence_used": ["abstract evidence"],
            "writer_notes": "validated",
        }
    ]

    class Result:
        stdout = json.dumps({"result": json.dumps(payload)})

    monkeypatch.setattr("daily_agent.editorial.subprocess.run", lambda *args, **kwargs: Result())

    drafts = draft_report_items(config, [material], use_llm=True)

    assert len(drafts) == 1
    assert drafts[0].draft_fields["problem"] == "降低量子编译后的线路深度。"
    assert drafts[0].draft_fields["novelty_or_difference"] == "相对已有工作，它把硬件连通性和线路深度目标一起放进优化过程。"
    assert drafts[0].draft_fields["method_steps"] == ["建模线路", "搜索优化", "评估深度"]
    assert "unknown_future_field" not in drafts[0].draft_fields
    assert drafts[0].evidence_used == ["abstract evidence"]
    assert drafts[0].writer_notes.startswith("LLM写手草稿")
    assert "validated" in drafts[0].writer_notes


def test_llm_drafting_falls_back_when_required_field_missing(monkeypatch):
    config = load_config("/Users/wuzixie/Daily_Agent")
    material = MaterialRecord(
        key="github:owner/repo",
        source="github",
        item_type="repo",
        title="owner/repo",
        url="https://github.com/owner/repo",
        repo_description="An MCP server for coding agents.",
        readme_excerpt="Provides MCP tools and server APIs for build automation.",
        stars=100,
        language="Python",
        tags=["mcp", "agent"],
        score=99,
    )
    payload = [{"key": material.key, "draft_fields": {"what_it_is": "A repo"}, "evidence_used": ["description"]}]

    class Result:
        stdout = json.dumps(payload)

    monkeypatch.setattr("daily_agent.editorial.subprocess.run", lambda *args, **kwargs: Result())

    drafts = draft_report_items(config, [material], use_llm=True)

    assert len(drafts) == 1
    assert drafts[0].draft_fields["core_capabilities"] != "not_stated"
    assert drafts[0].writer_notes.startswith("规则写手草稿")


def test_llm_drafting_falls_back_when_output_is_partial(monkeypatch):
    config = load_config("/Users/wuzixie/Daily_Agent")
    first = MaterialRecord(
        key="github:owner/one",
        source="github",
        item_type="repo",
        title="owner/one",
        url="https://github.com/owner/one",
        repo_description="An MCP server for coding agents.",
        readme_excerpt="Provides MCP tools and examples.",
        tags=["mcp"],
        score=99,
    )
    second = MaterialRecord(
        key="github:owner/two",
        source="github",
        item_type="repo",
        title="owner/two",
        url="https://github.com/owner/two",
        repo_description="An agent framework.",
        readme_excerpt="Framework with server APIs.",
        tags=["agent"],
        score=98,
    )
    payload = [
        {
            "key": first.key,
            "draft_fields": {
                "what_it_is": "A repo",
                "core_capabilities": "MCP tools",
                "typical_use_cases": "agent integration",
                "architecture_or_api": "server API",
                "maturity_signal": "metadata",
                "reusable_point": "tool design",
                "evidence_from_source": "README",
                "confidence": "medium",
            },
            "evidence_used": ["README"],
        }
    ]

    class Result:
        stdout = json.dumps(payload)

    monkeypatch.setattr("daily_agent.editorial.subprocess.run", lambda *args, **kwargs: Result())

    drafts = draft_report_items(config, [first, second], use_llm=True)

    assert len(drafts) == 2
    assert all(draft.writer_notes.startswith("规则写手草稿") for draft in drafts)


def test_llm_drafting_falls_back_when_json_is_malformed(monkeypatch):
    config = load_config("/Users/wuzixie/Daily_Agent")
    material = MaterialRecord(
        key="github:owner/repo",
        source="github",
        item_type="repo",
        title="owner/repo",
        url="https://github.com/owner/repo",
        repo_description="An MCP server for coding agents.",
        readme_excerpt="Provides MCP tools and server APIs.",
        tags=["mcp"],
        score=99,
    )

    class Result:
        stdout = "not json"

    monkeypatch.setattr("daily_agent.editorial.subprocess.run", lambda *args, **kwargs: Result())

    drafts = draft_report_items(config, [material], use_llm=True)

    assert len(drafts) == 1
    assert drafts[0].writer_notes.startswith("规则写手草稿")


def test_markdown_renders_deep_repo_fields():
    config = load_config("/Users/wuzixie/Daily_Agent")
    material = MaterialRecord(
        key="github:owner/repo",
        source="github",
        item_type="repo",
        title="owner/repo",
        url="https://github.com/owner/repo",
        repo_description="An MCP server for agents.",
        stars=10,
        language="Python",
        source_updated_at="2026-05-16T00:00:00+00:00",
        tags=["mcp"],
        score=99,
    )
    drafts = draft_report_items(config, [material], use_llm=False)
    reviews = review_draft(config, drafts, use_llm=False)
    approved = approve_publication(config, [material], drafts, reviews)
    markdown = render_daily_markdown(approved, date(2026, 5, 16), status=__import__("daily_agent.models").models.RunStatus())
    assert "核心能力" in markdown
    assert "典型使用场景" in markdown
    assert "架构/API" in markdown
