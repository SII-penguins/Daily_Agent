import json
from datetime import date

from daily_agent.config import load_config
from daily_agent.health import evaluate_run_health
from daily_agent.models import ApprovedItem, DeliveryStatus, EditorialDraft, EditorialReview, MaterialRecord, RunStatus, SourceStatus
from daily_agent.pipeline import run_pipeline
from daily_agent.quality import QualityCheck, QualityProfile
from daily_agent.storage import load_health_report, write_material_library


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


def _material(key="github:owner/repo", update_label=None):
    return MaterialRecord(
        key=key,
        source="github",
        item_type="repo",
        title=key.split(":", 1)[1],
        url="https://github.com/owner/repo",
        repo_description="An agent toolkit.",
        readme_excerpt="Provides API examples for agents.",
        tags=["agent"],
        score=99,
        update_label=update_label,
    )


def _approved(key="github:owner/repo", update_label=None):
    material = _material(key, update_label=update_label)
    return ApprovedItem(key=material.key, item_type=material.item_type, title=material.title, source=material.source, url=material.url, final_fields={"core_capabilities": "API tools"}, material=material)


def _paths(tmp_path):
    report = tmp_path / "reports" / "daily-agent-2026-W21.md"
    html = tmp_path / "reports" / "daily-agent-2026-W21.html"
    selected = tmp_path / "data" / "selected" / "selected-2026-05-18.dry-run.json"
    editorial = tmp_path / "data" / "editorial" / "2026-05-18"
    report.parent.mkdir(parents=True)
    selected.parent.mkdir(parents=True)
    editorial.mkdir(parents=True)
    report.write_text("report", encoding="utf-8")
    html.write_text("html", encoding="utf-8")
    selected.write_text("{}", encoding="utf-8")
    for name in ["shortlist.json", "writer_draft.json", "editor_review.json", "approval.json"]:
        (editorial / name).write_text("[]", encoding="utf-8")
    return report, html, selected, editorial


def _health(tmp_path, approved=None, drafts=None, reviews=None, status=None, previous=None, use_llm=False):
    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)
    report, html, selected, editorial = _paths(tmp_path)
    return evaluate_run_health(
        config,
        date(2026, 5, 18),
        True,
        status or RunStatus(delivery=DeliveryStatus(requested_mode="dry-run", final_mode="local", ok=True)),
        approved if approved is not None else [_approved(), _approved("github:owner/two"), _approved("github:owner/three")],
        [_material()],
        drafts or [EditorialDraft(key="github:owner/repo", item_type="repo", title="owner/repo", draft_fields={"core_capabilities": "API tools"})],
        reviews or [EditorialReview(key="github:owner/repo", verdict="PASS")],
        report,
        html,
        selected,
        editorial,
        previous,
        use_llm=use_llm,
    )


def test_health_zero_approved_failed(tmp_path):
    health = _health(tmp_path, approved=[])

    assert health["current"]["overall"] == "failed"
    assert health["current"]["summary"]["approved_count"] == 0


def test_health_low_approved_warning(tmp_path):
    health = _health(tmp_path, approved=[_approved()])

    assert health["current"]["overall"] == "warning"
    assert any(check["id"] == "approved_count_low" and check["status"] == "fail" for check in health["current"]["checks"])


def test_health_feishu_fallback_is_sanitized(tmp_path):
    status = RunStatus(delivery=DeliveryStatus(requested_mode="feishu", final_mode="cc-connect", ok=True, fallback_used=True, document_url="https://feishu.cn/docx/doc123", error="block_id secret content_hash"))
    health = _health(tmp_path, status=status)
    payload = json.dumps(health, ensure_ascii=False)

    assert health["current"]["overall"] == "warning"
    assert health["current"]["summary"]["feishu_fallback_used"] is True
    assert "https://feishu.cn" not in payload
    assert "doc123" not in payload
    assert "block_id" not in payload
    assert "content_hash" not in payload


def test_health_missing_artifacts_failed_without_paths(tmp_path):
    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)
    report = tmp_path / "missing-report.md"
    html = tmp_path / "missing-report.html"
    selected = tmp_path / "missing-selected.json"
    editorial = tmp_path / "missing-editorial"

    health = evaluate_run_health(
        config,
        date(2026, 5, 18),
        True,
        RunStatus(delivery=DeliveryStatus(requested_mode="dry-run", final_mode="local", ok=True)),
        [_approved(), _approved("github:owner/two"), _approved("github:owner/three")],
        [],
        [],
        [],
        report,
        html,
        selected,
        editorial,
    )
    payload = json.dumps(health, ensure_ascii=False)

    assert health["current"]["overall"] == "failed"
    assert health["current"]["summary"]["missing_artifact_count"] == 7
    assert str(tmp_path) not in payload


def test_health_counts_quality_signals(tmp_path):
    drafts = [EditorialDraft(key="github:owner/repo", item_type="repo", title="owner/repo", draft_fields={"core_capabilities": "not_stated", "typical_use_cases": "值得关注"})]
    reviews = [EditorialReview(key="github:owner/repo", verdict="FAIL")]
    approved = [_approved(), _approved(), _approved("github:owner/updated", update_label="major_update")]

    health = _health(tmp_path, approved=approved, drafts=drafts, reviews=reviews)
    summary = health["current"]["summary"]

    assert summary["not_stated_count"] == 1
    assert summary["generic_phrase_count"] == 1
    assert summary["review_failures"] == 1
    assert summary["duplicate_approved_keys"] == 1
    assert summary["update_signal_count"] == 1
    assert health["current"]["overall"] == "warning"


def test_health_records_quality_profile_without_local_paths(tmp_path, monkeypatch):
    profile = QualityProfile(
        overall="degraded",
        checks=[
            QualityCheck("python_runtime", "Python runtime", "full", True, "Python at /Users/wuzixie/anaconda3/bin/python3"),
            QualityCheck("ieee", "IEEE Xplore", "missing", False, "missing IEEE_XPLORE_API_KEY"),
            QualityCheck("github", "GitHub", "fallback", True, "missing GITHUB_TOKEN"),
        ],
    )
    monkeypatch.setattr("daily_agent.health.run_quality_check", lambda config: profile, raising=False)

    health = _health(tmp_path)
    payload = json.dumps(health, ensure_ascii=False)

    assert health["current"]["summary"]["quality_overall"] == "degraded"
    assert health["current"]["summary"]["quality_missing_count"] == 1
    assert health["current"]["summary"]["quality_fallback_count"] == 1
    assert health["current"]["summary"]["quality_blocker_count"] == 2
    assert any(check["id"] == "quality_profile_degraded" and check["status"] == "fail" for check in health["current"]["checks"])
    checks = {check["id"]: check for check in health["current"]["checks"]}
    assert checks["quality_profile_degraded"]["metrics"]["blockers"] == 2
    assert health["current"]["signals"]["quality"]["checks"][0] == {"key": "python_runtime", "name": "Python runtime", "status": "full", "ok": True}
    assert health["current"]["signals"]["quality"]["blockers"] == [
        {"key": "ieee", "name": "IEEE Xplore", "status": "missing", "ok": False, "action": None},
        {"key": "github", "name": "GitHub", "status": "fallback", "ok": True, "action": None},
    ]
    assert "/Users/" not in payload


def test_health_flags_rule_writer_fallback_when_llm_requested(tmp_path):
    drafts = [
        EditorialDraft(
            key="github:owner/repo",
            item_type="repo",
            title="owner/repo",
            draft_fields={"core_capabilities": "API tools"},
            writer_notes="规则写手草稿：基于 README 片段、项目描述和元数据生成；未说明处标记 not_stated。",
        )
    ]

    health = _health(tmp_path, drafts=drafts, use_llm=True)
    summary = health["current"]["summary"]
    checks = {check["id"]: check for check in health["current"]["checks"]}

    assert summary["llm_requested"] is True
    assert summary["llm_draft_count"] == 0
    assert summary["rule_draft_count"] == 1
    assert summary["llm_fallback_count"] == 1
    assert checks["llm_writer_fallback"]["status"] == "fail"
    assert health["current"]["signals"]["writer"] == {
        "llm_requested": True,
        "llm_draft_count": 0,
        "rule_draft_count": 1,
        "llm_fallback_count": 1,
    }


def test_health_does_not_flag_llm_draft_when_llm_requested(tmp_path):
    drafts = [
        EditorialDraft(
            key="github:owner/repo",
            item_type="repo",
            title="owner/repo",
            draft_fields={"core_capabilities": "API tools"},
            writer_notes="LLM写手草稿：validated",
        )
    ]

    health = _health(tmp_path, drafts=drafts, use_llm=True)
    summary = health["current"]["summary"]
    checks = {check["id"]: check for check in health["current"]["checks"]}

    assert summary["llm_draft_count"] == 1
    assert summary["rule_draft_count"] == 0
    assert summary["llm_fallback_count"] == 0
    assert checks["llm_writer_fallback"]["status"] == "pass"


def test_health_counts_consecutive_problem_runs(tmp_path):
    previous = {"schema_version": 1, "history": [{"overall": "healthy"}, {"overall": "warning"}, {"overall": "failed"}]}
    health = _health(tmp_path, previous=previous)

    assert health["current"]["summary"]["consecutive_problem_runs"] == 2
    assert any(check["id"] == "consecutive_problem_runs" and check["status"] == "fail" for check in health["current"]["checks"])


def test_pipeline_dry_run_writes_health_json(tmp_path, monkeypatch):
    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)
    material = _material()
    write_material_library(config, {material.key: material})
    monkeypatch.setattr("daily_agent.pipeline.load_config", lambda root=None: config)
    monkeypatch.setattr("daily_agent.pipeline.fetch_arxiv", lambda config, target_dt, window_days=None: [])
    monkeypatch.setattr("daily_agent.pipeline.fetch_github", lambda config, target_dt, window_days=None: [])
    _stub_other_pipeline_sources(monkeypatch)

    result = run_pipeline(root=tmp_path, run_date=date(2026, 5, 18), dry_run=True, use_llm=False)
    payload = json.dumps(load_health_report(config), ensure_ascii=False)

    assert result.health_path == tmp_path / "data" / "state" / "health.json"
    assert result.health["current"]["run_date"] == "2026-05-18"
    assert "Health" not in payload
    assert str(tmp_path) not in payload
    assert "/Users/" not in payload
    assert "反馈" not in payload
