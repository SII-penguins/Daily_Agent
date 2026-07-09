from datetime import date
from types import SimpleNamespace

import httpx

from daily_agent.cli import main
from daily_agent.config import load_config
from daily_agent.delivery.feishu import deliver_weekly_report
from daily_agent.models import ApprovedItem, DeliveryStatus, DigestItem, FeedbackEvent, MaterialRecord, RunStatus, utc_now_iso
from daily_agent.feedback.profile import build_feedback_profile, render_feedback_profile
from daily_agent.feedback.suggestions import parse_suggestion_response_text
from daily_agent.pipeline import _run_post_delivery_checks
from daily_agent.feedback.ingest import parse_feedback_text
from daily_agent.rendering.markdown import render_daily_markdown
from daily_agent.scoring.feedback import apply_feedback_scores
from daily_agent.storage import append_feedback_event, load_feedback_events, load_feedback_suggestions, load_feishu_delivery_state, resolve_published_item, set_feedback_event_status, upsert_feedback_event, write_feishu_delivery_state, write_published_index
from daily_agent.connectors.github import enrich_github_readmes


def test_parse_feedback_text_multiple_ranks_and_priority():
    parsed = parse_feedback_text("第 3 条不好，第 4 条和 #5 有用")
    assert [(item.rank, item.signal) for item in parsed] == [(3, "dislike"), (4, "like"), (5, "like")]


def test_feedback_event_from_dict_accepts_v1_rows():
    event = FeedbackEvent.from_dict(
        {
            "event_id": "fb_old",
            "created_at": "2026-05-17T00:00:00+00:00",
            "run_date": "2026-05-17",
            "rank": 1,
            "key": "github:owner/repo",
            "title": "owner/repo",
            "item_type": "repo",
            "signal": "like",
            "unknown_future_field": "ignored",
        }
    )
    assert event.origin == "cli"
    assert event.external_id is None
    assert event.status == "active"


def test_set_feedback_event_status_marks_event(tmp_path):
    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)
    event = FeedbackEvent(
        event_id="fb_1",
        created_at="2026-05-17T00:00:00+00:00",
        run_date="2026-05-17",
        rank=1,
        key="github:owner/repo",
        title="owner/repo",
        item_type="repo",
        signal="like",
    )
    append_feedback_event(config, event)
    set_feedback_event_status(config, "fb_1", "test", "verification")
    loaded = load_feedback_events(config)[0]
    assert loaded.status == "test"
    assert loaded.status_reason == "verification"
    assert loaded.status_updated_at


def test_upsert_feedback_event_preserves_revoked_status(tmp_path):
    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)
    event = FeedbackEvent(
        event_id="fb_1",
        created_at="2026-05-17T00:00:00+00:00",
        run_date="2026-05-17",
        rank=1,
        key="github:owner/repo",
        title="owner/repo",
        item_type="repo",
        signal="like",
        origin="feishu_reply",
        external_id="reply1",
    )
    upsert_feedback_event(config, event)
    set_feedback_event_status(config, "fb_1", "revoked", "wrong")
    upsert_feedback_event(config, FeedbackEvent(**{**event.to_dict(), "event_id": "fb_2", "title": "owner/repo updated"}))
    loaded = load_feedback_events(config)[0]
    assert loaded.status == "revoked"
    assert loaded.status_reason == "wrong"
    assert loaded.title == "owner/repo updated"


def test_upsert_feedback_event_deduplicates_external_id(tmp_path):
    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)
    first = FeedbackEvent(
        event_id="fb_1",
        created_at="2026-05-17T00:00:00+00:00",
        run_date="2026-05-17",
        rank=1,
        key="github:owner/repo",
        title="owner/repo",
        item_type="repo",
        signal="like",
        origin="feishu_reply",
        external_id="reply1",
        note="第 1 条不错",
    )
    second = FeedbackEvent(
        event_id="fb_2",
        created_at="2026-05-17T01:00:00+00:00",
        run_date="2026-05-17",
        rank=1,
        key="github:owner/repo",
        title="owner/repo updated",
        item_type="repo",
        signal="like",
        origin="feishu_reply",
        external_id="reply1",
        note="第 1 条很有用",
    )
    upsert_feedback_event(config, first)
    upsert_feedback_event(config, second)
    events = load_feedback_events(config)
    assert len(events) == 1
    assert events[0].event_id == "fb_1"
    assert events[0].note == "第 1 条很有用"
    assert events[0].title == "owner/repo updated"


def test_github_readme_enrichment_skips_unavailable_http_client(monkeypatch):
    material = MaterialRecord(
        key="github:owner/repo",
        source="github",
        item_type="repo",
        title="owner/repo",
        url="https://github.com/owner/repo",
    )

    def broken_client(*args, **kwargs):
        raise ImportError("Using SOCKS proxy, but the 'socksio' package is not installed.")

    monkeypatch.setattr("daily_agent.connectors.github.httpx.Client", broken_client)

    assert enrich_github_readmes([material]) == [material]
    assert material.readme_excerpt is None


def test_send_local_is_formal_run(monkeypatch):
    captured = {}

    def fake_run_pipeline(**kwargs):
        captured.update(kwargs)

        class Result:
            weekly_report_path = "report.md"
            weekly_html_path = "report.html"
            selected_path = "selected.json"
            items = []

            class Status:
                delivery = None
                errors = []

            status = Status()

        return Result()

    monkeypatch.setattr("daily_agent.cli.run_pipeline", fake_run_pipeline)
    assert main(["run", "--root", "/tmp/project", "--date", "2026-05-17", "--send", "local"]) == 0
    assert captured["dry_run"] is False
    assert captured["delivery_mode"] == "local"
    assert captured["use_llm"] is True


def test_run_cli_allows_explicit_no_llm(monkeypatch):
    captured = {}

    def fake_run_pipeline(**kwargs):
        captured.update(kwargs)

        class Result:
            weekly_report_path = "report.md"
            weekly_html_path = "report.html"
            selected_path = "selected.json"
            items = []

            class Status:
                delivery = None
                errors = []

            status = Status()

        return Result()

    monkeypatch.setattr("daily_agent.cli.run_pipeline", fake_run_pipeline)
    assert main(["run", "--root", "/tmp/project", "--date", "2026-05-17", "--send", "local", "--no-llm"]) == 0
    assert captured["use_llm"] is False


def test_run_cli_prints_llm_writer_fallback_warning(monkeypatch, capsys):
    def fake_run_pipeline(**kwargs):
        class Result:
            weekly_report_path = "report.md"
            weekly_html_path = "report.html"
            selected_path = "selected.json"
            items = []
            health = {
                "current": {
                    "overall": "warning",
                    "checks": [{"id": "llm_writer_fallback", "status": "fail"}],
                    "signals": {
                        "writer": {
                            "llm_requested": True,
                            "llm_draft_count": 3,
                            "rule_draft_count": 2,
                            "llm_fallback_count": 2,
                        }
                    },
                }
            }

            class Status:
                delivery = None
                errors = []

            status = Status()

        return Result()

    monkeypatch.setattr("daily_agent.cli.run_pipeline", fake_run_pipeline)

    assert main(["run", "--root", "/tmp/project", "--date", "2026-05-17", "--send", "local"]) == 0

    assert "Writer: LLM requested, 2 rule fallback drafts" in capsys.readouterr().out


def test_run_cli_blocks_formal_external_delivery_when_quality_is_degraded(monkeypatch, capsys):
    from daily_agent.quality import QualityCheck, QualityProfile

    called = False

    def fake_run_pipeline(**kwargs):
        nonlocal called
        called = True
        raise AssertionError("pipeline should not run when full-quality preflight fails")

    profile = QualityProfile(
        overall="degraded",
        checks=[QualityCheck(key="ieee", name="IEEE Xplore", status="missing", ok=False, detail="missing IEEE_XPLORE_API_KEY")],
    )
    monkeypatch.setattr("daily_agent.cli.load_config", lambda root=None: load_config("/Users/wuzixie/Daily_Agent"))
    monkeypatch.setattr("daily_agent.cli.run_quality_check", lambda config: profile)
    monkeypatch.setattr("daily_agent.cli.run_pipeline", fake_run_pipeline)

    code = main(["run", "--root", "/tmp/project", "--date", "2026-05-17", "--send", "feishu"])

    assert code == 2
    assert called is False
    assert "Full-quality preflight failed" in capsys.readouterr().out


def test_run_cli_require_full_blocks_degraded_dry_run(monkeypatch, capsys):
    from daily_agent.quality import QualityCheck, QualityProfile

    called = False

    def fake_run_pipeline(**kwargs):
        nonlocal called
        called = True
        raise AssertionError("pipeline should not run when required full-quality preflight fails")

    profile = QualityProfile(
        overall="degraded",
        checks=[QualityCheck(key="core", name="CORE", status="missing", ok=False, detail="missing CORE_API_KEY")],
    )
    monkeypatch.setattr("daily_agent.cli.load_config", lambda root=None: load_config("/Users/wuzixie/Daily_Agent"))
    monkeypatch.setattr("daily_agent.cli.run_quality_check", lambda config: profile)
    monkeypatch.setattr("daily_agent.cli.run_pipeline", fake_run_pipeline)

    code = main(["run", "--root", "/tmp/project", "--date", "2026-05-17", "--dry-run", "--require-full"])

    assert code == 2
    assert called is False
    assert "Full-quality preflight failed" in capsys.readouterr().out


def test_run_cli_require_full_overrides_allow_degraded(monkeypatch):
    from daily_agent.quality import QualityCheck, QualityProfile

    def fake_run_pipeline(**kwargs):
        raise AssertionError("pipeline should not run when --require-full is explicit")

    profile = QualityProfile(
        overall="degraded",
        checks=[QualityCheck(key="ieee", name="IEEE Xplore", status="missing", ok=False, detail="missing IEEE_XPLORE_API_KEY")],
    )
    monkeypatch.setattr("daily_agent.cli.load_config", lambda root=None: load_config("/Users/wuzixie/Daily_Agent"))
    monkeypatch.setattr("daily_agent.cli.run_quality_check", lambda config: profile)
    monkeypatch.setattr("daily_agent.cli.run_pipeline", fake_run_pipeline)

    assert main(["run", "--root", "/tmp/project", "--date", "2026-05-17", "--dry-run", "--require-full", "--allow-degraded"]) == 2


def test_run_cli_allow_degraded_bypasses_formal_delivery_preflight(monkeypatch):
    from daily_agent.quality import QualityCheck, QualityProfile

    captured = {}

    def fake_run_pipeline(**kwargs):
        captured.update(kwargs)

        class Result:
            weekly_report_path = "report.md"
            weekly_html_path = "report.html"
            selected_path = "selected.json"
            items = []

            class Status:
                delivery = None
                errors = []

            status = Status()

        return Result()

    profile = QualityProfile(
        overall="degraded",
        checks=[QualityCheck(key="ieee", name="IEEE Xplore", status="missing", ok=False, detail="missing IEEE_XPLORE_API_KEY")],
    )
    monkeypatch.setattr("daily_agent.cli.load_config", lambda root=None: load_config("/Users/wuzixie/Daily_Agent"))
    monkeypatch.setattr("daily_agent.cli.run_quality_check", lambda config: profile)
    monkeypatch.setattr("daily_agent.cli.run_pipeline", fake_run_pipeline)

    code = main(["run", "--root", "/tmp/project", "--date", "2026-05-17", "--send", "feishu", "--allow-degraded"])

    assert code == 0
    assert captured["delivery_mode"] == "feishu"


def test_run_cli_prints_quality_blockers_from_health(monkeypatch, capsys):
    def fake_run_pipeline(**kwargs):
        return SimpleNamespace(
            weekly_report_path="report.md",
            weekly_html_path="report.html",
            selected_path="selected.json",
            items=[],
            status=SimpleNamespace(delivery=None, errors=[]),
            health={
                "current": {
                    "overall": "warning",
                    "checks": [{"status": "fail"}],
                    "signals": {
                        "quality": {
                            "overall": "degraded",
                            "missing_count": 1,
                            "fallback_count": 1,
                            "blockers": [
                                {"name": "IEEE Xplore", "status": "missing"},
                                {"name": "GitHub", "status": "fallback"},
                            ],
                        }
                    },
                }
            },
        )

    monkeypatch.setattr("daily_agent.cli.enforce_full_profile", lambda root, write=True: SimpleNamespace(changed=False))
    monkeypatch.setattr("daily_agent.cli.run_pipeline", fake_run_pipeline)

    code = main(["run", "--root", "/tmp/project", "--date", "2026-05-17", "--dry-run"])
    output = capsys.readouterr().out

    assert code == 0
    assert "Health: warning (1 issue)" in output
    assert "Quality: degraded (1 missing, 1 fallback/partial, 2 blockers)" in output
    assert "Quality blocker: IEEE Xplore missing" in output
    assert "Quality blocker: GitHub fallback" in output


def test_feishu_disabled_falls_back_to_local(tmp_path):
    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)
    config.delivery["delivery"]["feishu"]["enabled"] = False
    config.delivery["delivery"]["feishu"]["fallback_to_cc_connect"] = False
    report = tmp_path / "report.md"
    report.write_text("# report", encoding="utf-8")
    status = deliver_weekly_report(report, "feishu", config=config, run_date=date(2026, 5, 17), daily_markdown="# report")
    assert status.ok is False
    assert status.final_mode == "local"
    assert "disabled" in (status.error or "")
    assert load_feishu_delivery_state(config)["weeks"] == {}


def test_write_published_index_resolves_latest_rank(tmp_path):
    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)
    material = MaterialRecord(
        key="github:owner/repo",
        source="github",
        item_type="repo",
        title="owner/repo",
        url="https://github.com/owner/repo",
        tags=["agent"],
        quota_group="exploratory",
        score=42,
    )
    approved = ApprovedItem(
        key=material.key,
        item_type=material.item_type,
        title=material.title,
        source=material.source,
        url=material.url,
        final_fields={},
        material=material,
    )
    write_published_index(config, [approved], date(2026, 5, 17))
    run_date, item = resolve_published_item(config, "latest", 1)
    assert run_date == "2026-05-17"
    assert item["key"] == "github:owner/repo"
    assert item["rank"] == 1


def test_feedback_text_creates_multiple_events(tmp_path, monkeypatch):
    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)
    monkeypatch.setattr("daily_agent.storage.date", type("FakeDate", (), {"today": staticmethod(lambda: date(2026, 5, 17))}))
    monkeypatch.setattr("daily_agent.cli.date", type("FakeDate", (), {"today": staticmethod(lambda: date(2026, 5, 17))}))
    materials = []
    for index in range(1, 9):
        material = MaterialRecord(key=f"github:owner/repo{index}", source="github", item_type="repo", title=f"owner/repo{index}", url=f"https://github.com/owner/repo{index}")
        materials.append(ApprovedItem(key=material.key, item_type="repo", title=material.title, source="github", url=material.url, final_fields={}, material=material))
    write_published_index(config, materials, date(2026, 5, 17))

    code = main(["feedback", "add", "--root", str(tmp_path), "--text", "今天第 3 条不行，第 8 条不错"])

    assert code == 0
    events = load_feedback_events(config)
    assert [(event.rank, event.signal) for event in events] == [(3, "dislike"), (8, "like")]
    assert events[0].note == "今天第 3 条不行"


def test_feedback_text_rejects_missing_signal(tmp_path):
    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)
    material = MaterialRecord(key="github:owner/repo", source="github", item_type="repo", title="owner/repo", url="https://github.com/owner/repo")
    approved = ApprovedItem(key=material.key, item_type="repo", title=material.title, source="github", url=material.url, final_fields={}, material=material)
    write_published_index(config, [approved], date.today())

    code = main(["feedback", "add", "--root", str(tmp_path), "--text", "今天第 1 条"])

    assert code == 1
    assert load_feedback_events(config) == []


def test_resolve_published_item_supports_today(tmp_path, monkeypatch):
    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)
    monkeypatch.setattr("daily_agent.storage.date", type("FakeDate", (), {"today": staticmethod(lambda: date(2026, 5, 17))}))
    material = MaterialRecord(key="github:owner/repo", source="github", item_type="repo", title="owner/repo", url="https://github.com/owner/repo")
    approved = ApprovedItem(key=material.key, item_type="repo", title=material.title, source="github", url=material.url, final_fields={}, material=material)
    write_published_index(config, [approved], date(2026, 5, 17))

    run_date, item = resolve_published_item(config, "today", 1)

    assert run_date == "2026-05-17"
    assert item["key"] == "github:owner/repo"


def test_inactive_feedback_events_do_not_affect_scoring(tmp_path):
    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)
    event = FeedbackEvent(
        event_id="fb_test",
        created_at=utc_now_iso(),
        run_date="2026-05-17",
        rank=1,
        key="arxiv:2401.00001",
        title="Quantum Regression",
        item_type="paper",
        signal="like",
        tags_snapshot=["quantum_ai"],
        keywords=["regression"],
        status="test",
    )
    append_feedback_event(config, event)
    item = DigestItem(
        id="2401.00001",
        source="arxiv",
        item_type="paper",
        title="Quantum Regression",
        url="https://arxiv.org/abs/2401.00001v1",
        abstract="A regression method for quantum models.",
        source_tags=["quantum_ai"],
        arxiv_id="2401.00001",
    )
    scored = apply_feedback_scores([item], config)[0]
    assert "feedback" not in scored.score_breakdown


def test_feedback_events_affect_scoring(tmp_path):
    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)
    event = FeedbackEvent(
        event_id="fb_test",
        created_at=utc_now_iso(),
        run_date="2026-05-17",
        rank=1,
        key="arxiv:2401.00001",
        title="Quantum Regression",
        item_type="paper",
        signal="like",
        tags_snapshot=["quantum_ai"],
        keywords=["regression"],
    )
    append_feedback_event(config, event)
    item = DigestItem(
        id="2401.00001",
        source="arxiv",
        item_type="paper",
        title="Quantum Regression",
        url="https://arxiv.org/abs/2401.00001v1",
        abstract="A regression method for quantum models.",
        source_tags=["quantum_ai"],
        arxiv_id="2401.00001",
    )
    scored = apply_feedback_scores([item], config)[0]
    assert scored.score_breakdown["feedback"] > 0
    assert load_feedback_events(config)[0].key == "arxiv:2401.00001"


def test_feishu_delivery_uses_docx_children_requests(tmp_path, monkeypatch):
    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)
    config.delivery["delivery"]["feishu"]["enabled"] = True
    config.delivery["delivery"]["feishu"]["fallback_to_cc_connect"] = False
    monkeypatch.setenv("DAILY_AGENT_FEISHU_APP_ID", "app-id")
    monkeypatch.setenv("DAILY_AGENT_FEISHU_APP_SECRET", "app-secret")

    calls = []

    def fake_post(url, **kwargs):
        calls.append(("POST", url, kwargs))
        if url.endswith("/auth/v3/tenant_access_token/internal"):
            return httpx.Response(200, request=httpx.Request("POST", url), json={"tenant_access_token": "tenant-token"})
        if url.endswith("/docx/v1/documents"):
            return httpx.Response(200, request=httpx.Request("POST", url), json={"data": {"document": {"document_id": "doc123", "url": "https://feishu.test/doc123"}}})
        if url.endswith("/docx/v1/documents/doc123/blocks/doc123/children"):
            assert kwargs["params"]["document_revision_id"] == -1
            assert "client_token" in kwargs["params"]
            children = kwargs["json"]["children"]
            assert children[0]["heading1"]["elements"][0]["text_run"]["content"] == "Title"
            assert children[1]["bullet"]["elements"][0]["text_run"]["content"] == "Item"
            return httpx.Response(200, request=httpx.Request("POST", url), json={"data": {"children": [{"block_id": "b1"}, {"block_id": "b2"}]}})
        raise AssertionError(url)

    monkeypatch.setattr("daily_agent.delivery.feishu.httpx.post", fake_post)

    report = tmp_path / "report.md"
    report.write_text("# report", encoding="utf-8")
    status = deliver_weekly_report(report, "feishu", config=config, run_date=date(2026, 5, 17), daily_markdown="# Title\n- Item")

    assert status.ok is True
    assert status.document_url == "https://feishu.test/doc123"
    state = load_feishu_delivery_state(config)
    day_state = state["weeks"]["2026-W20"]["days"]["2026-05-17"]
    assert day_state["block_ids"] == ["b1", "b2"]
    assert day_state["block_count"] == 2
    assert any(method == "POST" and url.endswith("/blocks/doc123/children") for method, url, _ in calls)


def test_feishu_rerun_deletes_previous_managed_range(tmp_path, monkeypatch):
    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)
    config.delivery["delivery"]["feishu"]["enabled"] = True
    config.delivery["delivery"]["feishu"]["fallback_to_cc_connect"] = False
    monkeypatch.setenv("DAILY_AGENT_FEISHU_APP_ID", "app-id")
    monkeypatch.setenv("DAILY_AGENT_FEISHU_APP_SECRET", "app-secret")
    write_feishu_delivery_state(
        config,
        {
            "schema_version": 1,
            "weeks": {
                "2026-W20": {
                    "document_id": "doc123",
                    "document_url": "https://feishu.test/doc123",
                    "title": "Daily Agent 日报｜2026 第 20 周",
                    "days": {
                        "2026-05-17": {"block_ids": ["old1", "old2"], "block_count": 2, "start_index": 0, "content_hash": "sha256:old"},
                        "2026-05-16": {"block_ids": ["prev"], "block_count": 1, "start_index": 2, "content_hash": "sha256:prev"},
                    },
                }
            },
        },
    )
    calls = []

    def fake_post(url, **kwargs):
        calls.append(("POST", url, kwargs))
        if url.endswith("/auth/v3/tenant_access_token/internal"):
            return httpx.Response(200, request=httpx.Request("POST", url), json={"tenant_access_token": "tenant-token"})
        if url.endswith("/docx/v1/documents/doc123/blocks/doc123/children"):
            children = kwargs["json"]["children"]
            return httpx.Response(200, request=httpx.Request("POST", url), json={"data": {"children": [{"block_id": f"new{i}"} for i, _ in enumerate(children, start=1)]}})
        raise AssertionError(url)

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        assert method == "DELETE"
        assert url.endswith("/docx/v1/documents/doc123/blocks/doc123/children/batch_delete")
        assert kwargs["params"]["document_revision_id"] == -1
        assert kwargs["json"] == {"start_index": 0, "end_index": 2}
        return httpx.Response(200, request=httpx.Request("DELETE", url), json={"data": {}})

    monkeypatch.setattr("daily_agent.delivery.feishu.httpx.post", fake_post)
    monkeypatch.setattr("daily_agent.delivery.feishu.httpx.request", fake_request)

    report = tmp_path / "report.md"
    report.write_text("# report", encoding="utf-8")
    status = deliver_weekly_report(report, "feishu", config=config, run_date=date(2026, 5, 17), daily_markdown="# Title")

    assert status.ok is True
    state = load_feishu_delivery_state(config)
    week_days = state["weeks"]["2026-W20"]["days"]
    assert week_days["2026-05-17"]["block_count"] == 1
    assert week_days["2026-05-16"]["start_index"] == 1
    assert any(method == "DELETE" for method, _, _ in calls)


def test_feishu_token_failure_falls_back_without_state_write(tmp_path, monkeypatch):
    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)
    config.delivery["delivery"]["feishu"]["enabled"] = True
    config.delivery["delivery"]["feishu"]["fallback_to_cc_connect"] = True
    config.delivery["delivery"]["cc_connect"]["enabled"] = True
    monkeypatch.setenv("DAILY_AGENT_FEISHU_APP_ID", "app-id")
    monkeypatch.setenv("DAILY_AGENT_FEISHU_APP_SECRET", "app-secret")
    sent = []

    def fake_post(url, **kwargs):
        return httpx.Response(401, request=httpx.Request("POST", url), json={"code": 99991663, "msg": "invalid token"})

    monkeypatch.setattr("daily_agent.delivery.feishu.httpx.post", fake_post)
    monkeypatch.setattr("daily_agent.delivery.feishu.send_via_cc_connect", lambda path, message=None: sent.append((path, message)))
    report = tmp_path / "report.md"
    report.write_text("# report", encoding="utf-8")

    status = deliver_weekly_report(report, "feishu", config=config, run_date=date(2026, 5, 17), daily_markdown="# Title")

    assert status.ok is True
    assert status.final_mode == "cc-connect"
    assert status.fallback_used is True
    assert "Feishu API 401" in (status.error or "")
    assert sent and "飞书云文档写入失败" in (sent[0][1] or "")
    assert load_feishu_delivery_state(config)["weeks"] == {}


def test_feishu_block_write_failure_keeps_previous_state_and_falls_back(tmp_path, monkeypatch):
    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)
    config.delivery["delivery"]["feishu"]["enabled"] = True
    config.delivery["delivery"]["feishu"]["fallback_to_cc_connect"] = True
    config.delivery["delivery"]["cc_connect"]["enabled"] = True
    monkeypatch.setenv("DAILY_AGENT_FEISHU_APP_ID", "app-id")
    monkeypatch.setenv("DAILY_AGENT_FEISHU_APP_SECRET", "app-secret")
    previous_state = {
        "schema_version": 1,
        "weeks": {
            "2026-W20": {
                "document_id": "doc123",
                "document_url": "https://feishu.test/doc123",
                "title": "Daily Agent 日报｜2026 第 20 周",
                "days": {"2026-05-17": {"block_ids": ["old1"], "block_count": 1, "start_index": 0, "content_hash": "sha256:old"}},
            }
        },
    }
    write_feishu_delivery_state(config, previous_state)
    sent = []
    calls = []

    def fake_post(url, **kwargs):
        calls.append(("POST", url, kwargs))
        if url.endswith("/auth/v3/tenant_access_token/internal"):
            return httpx.Response(200, request=httpx.Request("POST", url), json={"tenant_access_token": "tenant-token"})
        if url.endswith("/docx/v1/documents/doc123/blocks/doc123/children"):
            return httpx.Response(500, request=httpx.Request("POST", url), json={"code": 1, "msg": "write failed"})
        raise AssertionError(url)

    def fake_request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        raise AssertionError("old managed blocks should not be deleted when replacement write fails")

    monkeypatch.setattr("daily_agent.delivery.feishu.httpx.post", fake_post)
    monkeypatch.setattr("daily_agent.delivery.feishu.httpx.request", fake_request)
    monkeypatch.setattr("daily_agent.delivery.feishu.send_via_cc_connect", lambda path, message=None: sent.append((path, message)))
    report = tmp_path / "report.md"
    report.write_text("# report", encoding="utf-8")

    status = deliver_weekly_report(report, "feishu", config=config, run_date=date(2026, 5, 17), daily_markdown="# New Title")

    assert status.ok is True
    assert status.final_mode == "cc-connect"
    assert status.fallback_used is True
    assert "Feishu API 500" in (status.error or "")
    assert sent
    assert load_feishu_delivery_state(config) == previous_state
    assert not any(method == "DELETE" for method, _, _ in calls)


def test_feishu_delete_failure_cleans_inserted_replacement_and_preserves_state(tmp_path, monkeypatch):
    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)
    config.delivery["delivery"]["feishu"]["enabled"] = True
    config.delivery["delivery"]["feishu"]["fallback_to_cc_connect"] = True
    config.delivery["delivery"]["cc_connect"]["enabled"] = True
    monkeypatch.setenv("DAILY_AGENT_FEISHU_APP_ID", "app-id")
    monkeypatch.setenv("DAILY_AGENT_FEISHU_APP_SECRET", "app-secret")
    previous_state = {
        "schema_version": 1,
        "weeks": {
            "2026-W20": {
                "document_id": "doc123",
                "document_url": "https://feishu.test/doc123",
                "title": "Daily Agent 日报｜2026 第 20 周",
                "days": {"2026-05-17": {"block_ids": ["old1"], "block_count": 1, "start_index": 2, "content_hash": "sha256:old"}},
            }
        },
    }
    write_feishu_delivery_state(config, previous_state)
    sent = []
    delete_payloads = []

    def fake_post(url, **kwargs):
        if url.endswith("/auth/v3/tenant_access_token/internal"):
            return httpx.Response(200, request=httpx.Request("POST", url), json={"tenant_access_token": "tenant-token"})
        if url.endswith("/docx/v1/documents/doc123/blocks/doc123/children"):
            return httpx.Response(200, request=httpx.Request("POST", url), json={"data": {"children": [{"block_id": "new1"}]}})
        raise AssertionError(url)

    def fake_request(method, url, **kwargs):
        delete_payloads.append(kwargs["json"])
        if kwargs["json"] == {"start_index": 2, "end_index": 3}:
            return httpx.Response(500, request=httpx.Request("DELETE", url), json={"code": 1, "msg": "delete failed"})
        if kwargs["json"] == {"start_index": 0, "end_index": 1}:
            return httpx.Response(200, request=httpx.Request("DELETE", url), json={"data": {}})
        raise AssertionError(kwargs["json"])

    monkeypatch.setattr("daily_agent.delivery.feishu.httpx.post", fake_post)
    monkeypatch.setattr("daily_agent.delivery.feishu.httpx.request", fake_request)
    monkeypatch.setattr("daily_agent.delivery.feishu.send_via_cc_connect", lambda path, message=None: sent.append((path, message)))
    report = tmp_path / "report.md"
    report.write_text("# report", encoding="utf-8")

    status = deliver_weekly_report(report, "feishu", config=config, run_date=date(2026, 5, 17), daily_markdown="# New Title")

    assert status.ok is True
    assert status.final_mode == "cc-connect"
    assert status.fallback_used is True
    assert delete_payloads == [{"start_index": 2, "end_index": 3}, {"start_index": 0, "end_index": 1}]
    assert sent
    assert load_feishu_delivery_state(config) == previous_state


def test_feishu_feedback_sync_dry_run_parses_replies(tmp_path, monkeypatch):
    from daily_agent.feedback.feishu_comments import sync_feishu_feedback

    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)
    monkeypatch.setattr("daily_agent.storage.date", type("FakeDate", (), {"today": staticmethod(lambda: date(2026, 5, 17))}))
    material = MaterialRecord(key="github:owner/repo", source="github", item_type="repo", title="owner/repo", url="https://github.com/owner/repo")
    approved = ApprovedItem(key=material.key, item_type="repo", title=material.title, source="github", url=material.url, final_fields={}, material=material)
    write_published_index(config, [approved], date(2026, 5, 17))
    write_feishu_delivery_state(config, {"schema_version": 1, "weeks": {"2026-W20": {"document_id": "doc123", "days": {}}}})
    monkeypatch.setattr("daily_agent.feedback.feishu_comments._load_cc_connect_feishu_config", lambda: {"app_id": "app", "app_secret": "secret"})
    monkeypatch.setattr("daily_agent.feedback.feishu_comments._tenant_access_token", lambda app, secret, timeout: "token")

    def fake_get(url, **kwargs):
        if url.endswith("/drive/v1/files/doc123/comments"):
            return httpx.Response(
                200,
                request=httpx.Request("GET", url),
                json={
                    "data": {
                        "items": [
                            {
                                "comment_id": "comment1",
                                "has_more": False,
                                "reply_list": {"replies": [{"reply_id": "reply1", "update_time": 1778950000000, "content": {"elements": [{"text_run": {"content": "今天第 1 条不错"}}]}}]},
                            },
                            {"comment_id": "comment2", "has_more": False, "reply_list": {"replies": [{"reply_id": "reply2", "content": {"elements": [{"text_run": {"content": "这条随便看看"}}]}}]}},
                        ],
                        "has_more": False,
                    }
                },
            )
        raise AssertionError(url)

    monkeypatch.setattr("daily_agent.feedback.feishu_comments.httpx.get", fake_get)
    result = sync_feishu_feedback(config, dry_run=True)

    assert result.seen == 2
    assert result.parsed == 1
    assert result.recorded == 0
    assert result.unresolved == 1
    assert load_feedback_events(config) == []


def test_feishu_feedback_sync_deduplicates_inline_and_paginated_replies(tmp_path, monkeypatch):
    from daily_agent.feedback.feishu_comments import sync_feishu_feedback

    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)
    material = MaterialRecord(key="github:owner/repo", source="github", item_type="repo", title="owner/repo", url="https://github.com/owner/repo")
    approved = ApprovedItem(key=material.key, item_type="repo", title=material.title, source="github", url=material.url, final_fields={}, material=material)
    write_published_index(config, [approved], date(2026, 5, 17))
    write_feishu_delivery_state(config, {"schema_version": 1, "weeks": {"2026-W20": {"document_id": "doc123", "days": {}}}})
    monkeypatch.setattr("daily_agent.feedback.feishu_comments._load_cc_connect_feishu_config", lambda: {"app_id": "app", "app_secret": "secret"})
    monkeypatch.setattr("daily_agent.feedback.feishu_comments._tenant_access_token", lambda app, secret, timeout: "token")

    reply_calls = []

    def fake_get(url, **kwargs):
        params = kwargs.get("params", {})
        if url.endswith("/drive/v1/files/doc123/comments"):
            return httpx.Response(
                200,
                request=httpx.Request("GET", url),
                json={
                    "data": {
                        "items": [
                            {
                                "comment_id": "comment1",
                                "has_more": True,
                                "reply_list": {"replies": [{"reply_id": "reply1", "content": {"elements": [{"text_run": {"content": "第 1 条不错"}}]}}]},
                            }
                        ],
                        "has_more": False,
                    }
                },
            )
        if url.endswith("/drive/v1/files/doc123/comments/comment1/replies"):
            reply_calls.append(params.get("page_token"))
            if not params.get("page_token"):
                return httpx.Response(
                    200,
                    request=httpx.Request("GET", url),
                    json={"data": {"items": [{"reply_id": "reply1", "content": {"elements": [{"text_run": {"content": "第 1 条不错"}}]}}], "has_more": True, "page_token": "p2"}},
                )
            return httpx.Response(
                200,
                request=httpx.Request("GET", url),
                json={"data": {"items": [{"reply_id": "reply2", "content": {"elements": [{"text_run": {"content": "第 1 条不行"}}]}}], "has_more": False}},
            )
        raise AssertionError(url)

    monkeypatch.setattr("daily_agent.feedback.feishu_comments.httpx.get", fake_get)

    result = sync_feishu_feedback(config, date_selector="2026-05-17", dry_run=True)

    assert reply_calls == [None, "p2"]
    assert result.seen == 2
    assert result.parsed == 2
    assert [event.external_id for event in result.events] == ["reply1:1", "reply2:1"]


def test_feishu_feedback_sync_can_exclude_replies(tmp_path, monkeypatch):
    from daily_agent.feedback.feishu_comments import sync_feishu_feedback

    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)
    config.feedback["feishu_comments"]["include_replies"] = False
    material = MaterialRecord(key="github:owner/repo", source="github", item_type="repo", title="owner/repo", url="https://github.com/owner/repo")
    approved = ApprovedItem(key=material.key, item_type="repo", title=material.title, source="github", url=material.url, final_fields={}, material=material)
    write_published_index(config, [approved], date(2026, 5, 17))
    write_feishu_delivery_state(config, {"schema_version": 1, "weeks": {"2026-W20": {"document_id": "doc123", "days": {}}}})
    monkeypatch.setattr("daily_agent.feedback.feishu_comments._load_cc_connect_feishu_config", lambda: {"app_id": "app", "app_secret": "secret"})
    monkeypatch.setattr("daily_agent.feedback.feishu_comments._tenant_access_token", lambda app, secret, timeout: "token")

    def fake_get(url, **kwargs):
        if url.endswith("/drive/v1/files/doc123/comments"):
            return httpx.Response(
                200,
                request=httpx.Request("GET", url),
                json={
                    "data": {
                        "items": [
                            {
                                "comment_id": "comment1",
                                "quote": "第 1 条不错",
                                "has_more": True,
                                "reply_list": {"replies": [{"reply_id": "reply1", "content": {"elements": [{"text_run": {"content": "第 1 条不行"}}]}}]},
                            }
                        ],
                        "has_more": False,
                    }
                },
            )
        raise AssertionError("Replies should not be fetched when include_replies is false")

    monkeypatch.setattr("daily_agent.feedback.feishu_comments.httpx.get", fake_get)

    result = sync_feishu_feedback(config, date_selector="2026-05-17", dry_run=True)

    assert result.seen == 1
    assert result.parsed == 1
    assert result.events[0].origin == "feishu_comment"
    assert result.events[0].signal == "like"


def test_feishu_feedback_sync_handles_suggestion_response(tmp_path, monkeypatch):
    from daily_agent.feedback.feishu_comments import sync_feishu_feedback

    feedback_path = tmp_path / "config" / "feedback.yaml"
    feedback_path.parent.mkdir(parents=True)
    feedback_path.write_text("liked:\n  tags: []\n  keywords: []\ndisliked:\n  tags: []\n  keywords: []\n", encoding="utf-8")
    config = load_config(tmp_path)
    _seed_profile_feedback(config)
    main(["feedback", "suggest", "--root", str(tmp_path)])
    suggestion = next(item for item in load_feedback_suggestions(config) if item["type"] == "liked.keyword")
    write_feishu_delivery_state(config, {"schema_version": 1, "weeks": {"2026-W20": {"document_id": "doc123", "days": {}}}})
    monkeypatch.setattr("daily_agent.feedback.feishu_comments._load_cc_connect_feishu_config", lambda: {"app_id": "app", "app_secret": "secret"})
    monkeypatch.setattr("daily_agent.feedback.feishu_comments._tenant_access_token", lambda app, secret, timeout: "token")

    def fake_get(url, **kwargs):
        if url.endswith("/drive/v1/files/doc123/comments"):
            return httpx.Response(
                200,
                request=httpx.Request("GET", url),
                json={"data": {"items": [{"comment_id": "comment1", "has_more": False, "reply_list": {"replies": [{"reply_id": "reply1", "content": {"elements": [{"text_run": {"content": f"确认建议 {suggestion['id']}"}}]}}]}}], "has_more": False}},
            )
        raise AssertionError(url)

    before_events = load_feedback_events(config)
    monkeypatch.setattr("daily_agent.feedback.feishu_comments.httpx.get", fake_get)
    result = sync_feishu_feedback(config)

    assert result.seen == 1
    assert result.suggestions_applied == 1
    assert result.parsed == 0
    assert load_feedback_events(config) == before_events
    assert "量子线路设计" in feedback_path.read_text(encoding="utf-8")


def test_feedback_profile_aggregates_active_signals(tmp_path):
    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)
    material = MaterialRecord(key="arxiv:paper", source="arxiv", item_type="paper", title="QNN Paper", url="https://arxiv.org/abs/1", tags=["qnn", "quantum_circuit"], score_breakdown={"feedback": -10.0})
    from daily_agent.storage import write_material_library

    write_material_library(config, {material.key: material})
    append_feedback_event(
        config,
        FeedbackEvent(
            event_id="fb_active",
            created_at="2026-05-17T00:00:00+00:00",
            run_date="2026-05-17",
            rank=4,
            key="arxiv:paper",
            title="QNN Paper",
            item_type="paper",
            signal="dislike",
            tags_snapshot=["qnn"],
            note="第4条是QNN，和我关注的领域有出入，我主要还是关注量子线路设计",
        ),
    )
    append_feedback_event(
        config,
        FeedbackEvent(
            event_id="fb_test",
            created_at="2026-05-17T00:00:00+00:00",
            run_date="2026-05-17",
            rank=1,
            key="arxiv:paper",
            title="QNN Paper",
            item_type="paper",
            signal="like",
            tags_snapshot=["qnn"],
            note="verification",
            status="test",
        ),
    )

    profile = build_feedback_profile(config)
    text = render_feedback_profile(profile, config)

    assert profile.active_events == 1
    assert profile.ignored_events == 1
    assert ("qnn", 1.0) in profile.negative_tags
    assert any(keyword == "量子线路设计" for keyword, _ in profile.positive_keywords)
    assert "Consider adding liked keyword: 量子线路设计" in text


def test_feedback_profile_cli_is_read_only(tmp_path, capsys):
    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)
    append_feedback_event(
        config,
        FeedbackEvent(
            event_id="fb_active",
            created_at="2026-05-17T00:00:00+00:00",
            run_date="2026-05-17",
            rank=1,
            key="github:owner/repo",
            title="owner/repo",
            item_type="repo",
            signal="like",
            tags_snapshot=["agent"],
            note="第 1 条不错",
        ),
    )
    before = (tmp_path / "data" / "state" / "feedback_events.json").read_text(encoding="utf-8")
    assert main(["feedback", "profile", "--root", str(tmp_path), "--limit", "5"]) == 0
    out = capsys.readouterr().out
    after = (tmp_path / "data" / "state" / "feedback_events.json").read_text(encoding="utf-8")
    assert "Feedback profile" in out
    assert "Recommendations" in out
    assert before == after


def _seed_profile_feedback(config):
    material = MaterialRecord(key="arxiv:paper", source="arxiv", item_type="paper", title="QNN Paper", url="https://arxiv.org/abs/1", tags=["qnn", "quantum_circuit"], score_breakdown={"feedback": -10.0})
    from daily_agent.storage import write_material_library

    write_material_library(config, {material.key: material})
    append_feedback_event(
        config,
        FeedbackEvent(
            event_id="fb_active",
            created_at="2026-05-17T00:00:00+00:00",
            run_date="2026-05-17",
            rank=4,
            key="arxiv:paper",
            title="QNN Paper",
            item_type="paper",
            signal="dislike",
            tags_snapshot=["qnn"],
            note="第4条是QNN，和我关注的领域有出入，我主要还是关注量子线路设计",
        ),
    )


def test_feedback_suggest_dry_run_does_not_write_state(tmp_path, capsys):
    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)
    _seed_profile_feedback(config)

    assert main(["feedback", "suggest", "--root", str(tmp_path), "--dry-run"]) == 0

    out = capsys.readouterr().out
    assert "liked.keyword" in out
    assert "quantum_ai" not in out
    assert "quantum_circuit" not in out
    assert load_feedback_suggestions(config) == []


def test_feedback_suggest_generates_pending_once(tmp_path):
    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)
    _seed_profile_feedback(config)

    assert main(["feedback", "suggest", "--root", str(tmp_path)]) == 0
    assert main(["feedback", "suggest", "--root", str(tmp_path)]) == 0

    suggestions = load_feedback_suggestions(config)
    identities = {(item["type"], item["value"]) for item in suggestions}
    assert len(suggestions) == len(identities)
    assert any(item["type"] == "liked.keyword" and item["value"] == "量子线路设计" for item in suggestions)


def test_feedback_reject_suggestion_prevents_regeneration(tmp_path):
    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)
    _seed_profile_feedback(config)
    main(["feedback", "suggest", "--root", str(tmp_path)])
    suggestion_id = load_feedback_suggestions(config)[0]["id"]

    assert main(["feedback", "reject-suggestion", "--root", str(tmp_path), "--id", suggestion_id, "--reason", "not stable"]) == 0
    assert main(["feedback", "suggest", "--root", str(tmp_path)]) == 0

    rejected = [item for item in load_feedback_suggestions(config) if item["id"] == suggestion_id][0]
    assert rejected["status"] == "rejected"
    assert rejected["status_reason"] == "not stable"


def test_feedback_apply_suggestion_updates_feedback_yaml(tmp_path):
    feedback_path = tmp_path / "config" / "feedback.yaml"
    feedback_path.parent.mkdir(parents=True)
    feedback_path.write_text(
        "liked:\n  tags: []\n  keywords: []\ndisliked:\n  tags: []\n  keywords: []\nevents:\n  enabled: true\nfeishu_comments:\n  enabled: false\n",
        encoding="utf-8",
    )
    config = load_config(tmp_path)
    _seed_profile_feedback(config)
    main(["feedback", "suggest", "--root", str(tmp_path)])
    suggestion = next(item for item in load_feedback_suggestions(config) if item["type"] == "liked.keyword")

    assert main(["feedback", "apply-suggestion", "--root", str(tmp_path), "--id", suggestion["id"]]) == 0

    text = feedback_path.read_text(encoding="utf-8")
    assert "量子线路设计" in text
    assert "events:" in text
    assert "feishu_comments:" in text
    applied = next(item for item in load_feedback_suggestions(config) if item["id"] == suggestion["id"])
    assert applied["status"] == "applied"


def test_parse_suggestion_response_requires_action_and_id():
    assert parse_suggestion_response_text("确认建议 sug_20260517_142049_002")[0].action == "apply"
    rejected = parse_suggestion_response_text("拒绝建议 sug_20260517_142049_001 因为太宽泛")[0]
    assert rejected.action == "reject"
    assert rejected.reason == "太宽泛"
    assert parse_suggestion_response_text("量子线路设计这个建议不错") == []


def test_feedback_respond_dry_run_is_read_only(tmp_path):
    feedback_path = tmp_path / "config" / "feedback.yaml"
    feedback_path.parent.mkdir(parents=True)
    feedback_path.write_text("liked:\n  tags: []\n  keywords: []\ndisliked:\n  tags: []\n  keywords: []\n", encoding="utf-8")
    config = load_config(tmp_path)
    _seed_profile_feedback(config)
    main(["feedback", "suggest", "--root", str(tmp_path)])
    suggestion = next(item for item in load_feedback_suggestions(config) if item["type"] == "liked.keyword")
    before_yaml = feedback_path.read_text(encoding="utf-8")
    before_suggestions = load_feedback_suggestions(config)

    assert main(["feedback", "respond", "--root", str(tmp_path), "--text", f"确认建议 {suggestion['id']}", "--dry-run"]) == 0

    assert feedback_path.read_text(encoding="utf-8") == before_yaml
    assert load_feedback_suggestions(config) == before_suggestions


def test_feedback_respond_applies_and_rejects_suggestions(tmp_path):
    feedback_path = tmp_path / "config" / "feedback.yaml"
    feedback_path.parent.mkdir(parents=True)
    feedback_path.write_text("liked:\n  tags: []\n  keywords: []\ndisliked:\n  tags: []\n  keywords: []\n", encoding="utf-8")
    config = load_config(tmp_path)
    _seed_profile_feedback(config)
    main(["feedback", "suggest", "--root", str(tmp_path)])
    liked = next(item for item in load_feedback_suggestions(config) if item["type"] == "liked.keyword")
    disliked = next(item for item in load_feedback_suggestions(config) if item["type"] == "disliked.tag")

    assert main(["feedback", "respond", "--root", str(tmp_path), "--text", f"确认建议 {liked['id']}"]) == 0
    assert main(["feedback", "respond", "--root", str(tmp_path), "--text", f"拒绝建议 {disliked['id']} 因为太宽泛"]) == 0

    suggestions = {item["id"]: item for item in load_feedback_suggestions(config)}
    assert suggestions[liked["id"]]["status"] == "applied"
    assert suggestions[disliked["id"]]["status"] == "rejected"
    assert suggestions[disliked["id"]]["status_reason"] == "太宽泛"
    assert "量子线路设计" in feedback_path.read_text(encoding="utf-8")


def test_feedback_cleanup_dry_run_and_explain_cli(tmp_path, capsys):
    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)
    material = MaterialRecord(key="github:owner/repo", source="github", item_type="repo", title="owner/repo", url="https://github.com/owner/repo", score_breakdown={"feedback": 4.0})
    approved = ApprovedItem(key=material.key, item_type="repo", title=material.title, source="github", url=material.url, final_fields={}, material=material)
    write_published_index(config, [approved], date.today())
    append_feedback_event(
        config,
        FeedbackEvent(
            event_id="fb_verify",
            created_at="2026-05-17T00:00:00+00:00",
            run_date=date.today().isoformat(),
            rank=1,
            key="github:owner/repo",
            title="owner/repo",
            item_type="repo",
            signal="like",
            note="v2.1 verification event",
        ),
    )
    assert main(["feedback", "cleanup", "--root", str(tmp_path), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "candidate fb_verify" in out
    assert load_feedback_events(config)[0].status == "active"
    assert main(["feedback", "explain", "--root", str(tmp_path), "--limit", "5"]) == 0
    assert "Feedback explain" in capsys.readouterr().out


def test_feedback_status_cli_commands(tmp_path):
    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)
    append_feedback_event(
        config,
        FeedbackEvent(
            event_id="fb_cli",
            created_at="2026-05-17T00:00:00+00:00",
            run_date="2026-05-17",
            rank=1,
            key="github:owner/repo",
            title="owner/repo",
            item_type="repo",
            signal="like",
        ),
    )
    assert main(["feedback", "mark-test", "--root", str(tmp_path), "--event-id", "fb_cli", "--reason", "verification"]) == 0
    assert load_feedback_events(config)[0].status == "test"
    assert main(["feedback", "undo", "--root", str(tmp_path), "--event-id", "fb_cli", "--reason", "wrong"]) == 0
    assert load_feedback_events(config)[0].status == "revoked"


def test_feedback_sync_cli_dry_run(tmp_path, monkeypatch):
    from daily_agent.feedback.feishu_comments import FeishuFeedbackSyncResult

    def fake_sync(config, week="latest", date_selector=None, dry_run=False):
        assert dry_run is True
        return FeishuFeedbackSyncResult(seen=1, parsed=1, recorded=0, unresolved=0)

    monkeypatch.setattr("daily_agent.cli.sync_feishu_feedback", fake_sync)
    assert main(["feedback", "sync", "--root", str(tmp_path), "--source", "feishu", "--dry-run"]) == 0


def test_markdown_renders_global_rank():
    material = MaterialRecord(
        key="github:owner/repo",
        source="github",
        item_type="repo",
        title="owner/repo",
        url="https://github.com/owner/repo",
        repo_description="An MCP server for agents.",
        tags=["mcp"],
    )
    approved = ApprovedItem(
        key=material.key,
        item_type=material.item_type,
        title=material.title,
        source=material.source,
        url=material.url,
        final_fields={
            "what_it_is": "A repo",
            "core_capabilities": "MCP tools",
            "typical_use_cases": "agent integration",
            "architecture_or_api": "server",
            "maturity_signal": "stars=1",
            "reusable_point": "tool API",
        },
        material=material,
    )
    markdown = render_daily_markdown([approved], date(2026, 5, 17), RunStatus())
    assert "### 1. owner/repo" in markdown
    assert "- 反馈编号：第 1 条" in markdown
    assert "## 今日必看\n\n- [owner/repo]" in markdown


def test_post_delivery_checks_warn_on_feishu_fallback(tmp_path):
    status = RunStatus(delivery=DeliveryStatus(requested_mode="feishu", final_mode="cc-connect", ok=True, fallback_used=True, error="Feishu failed"))
    report = tmp_path / "report.md"
    html = tmp_path / "report.html"
    selected = tmp_path / "selected.json"
    report.write_text("report", encoding="utf-8")
    html.write_text("html", encoding="utf-8")
    selected.write_text("{}", encoding="utf-8")

    _run_post_delivery_checks(status, [object()], report, html, selected, "feishu", dry_run=False)

    assert "Feishu delivery used fallback: Feishu failed" in status.errors


def test_post_delivery_checks_accept_feishu_success(tmp_path):
    status = RunStatus(delivery=DeliveryStatus(requested_mode="feishu", final_mode="feishu", ok=True, document_url="https://feishu.test/doc"))
    report = tmp_path / "report.md"
    html = tmp_path / "report.html"
    selected = tmp_path / "selected.json"
    report.write_text("report", encoding="utf-8")
    html.write_text("html", encoding="utf-8")
    selected.write_text("{}", encoding="utf-8")

    _run_post_delivery_checks(status, [object()], report, html, selected, "feishu", dry_run=False)

    assert status.errors == []


def test_post_delivery_checks_warn_on_zero_approved(tmp_path):
    status = RunStatus()
    report = tmp_path / "report.md"
    html = tmp_path / "report.html"
    selected = tmp_path / "selected.json"
    report.write_text("report", encoding="utf-8")
    html.write_text("html", encoding="utf-8")
    selected.write_text("{}", encoding="utf-8")

    _run_post_delivery_checks(status, [], report, html, selected, "local", dry_run=False)

    assert "No approved items" in status.errors
