import json
from datetime import date, datetime, timezone

from daily_agent.config import load_config
from daily_agent.editorial import approve_publication, build_shortlist, draft_report_items, review_draft
from daily_agent.models import DigestItem, MaterialRecord, SelectedRecord
from daily_agent.rendering.markdown import render_daily_markdown
from daily_agent.scoring.dedup import deduplicate_items
from daily_agent.scoring.rules import score_items, select_items
from daily_agent.storage import load_material_library, mark_materials_published, select_library_candidates, upsert_materials, write_material_library


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

    result = run_pipeline(root=tmp_path, run_date=date(2026, 5, 18), dry_run=True, use_llm=False)

    assert [item.key for item in result.items] == [fresh.key]


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
    assert draft.draft_fields["limitations"] == "The main limitation is that evaluation covers only 12-qubit simulations."


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
    assert drafts[0].draft_fields["method_steps"] == ["建模线路", "搜索优化", "评估深度"]
    assert "unknown_future_field" not in drafts[0].draft_fields
    assert drafts[0].evidence_used == ["abstract evidence"]
    assert drafts[0].writer_notes == "validated"


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
