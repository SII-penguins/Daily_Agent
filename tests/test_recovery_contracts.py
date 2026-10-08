"""Recovery contracts tested without network, credentials, or production state."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from daily_agent import scheduling as schedule, storage
from daily_agent.config import load_config
from daily_agent.models import ApprovedItem, DeliveryStatus, MaterialRecord, SelectedRecord
from daily_agent.workflow_state import StateCorrupt, atomic_json, read_json

DAY = date(2026, 10, 8)
NOW = datetime(2026, 10, 7, 17, tzinfo=timezone.utc)


def config(root, **recovery):
    (root / "config").mkdir(exist_ok=True)
    (root / "config/delivery.yaml").write_text(yaml.safe_dump({
        "report": {"timezone": "Asia/Shanghai"},
        "delivery": {"default": "cc-connect"},
        "schedule": {"recovery": {"heartbeat_seconds": 1, "stale_seconds": 10,
                                    "base_backoff_seconds": 0, **recovery}},
    }))
    return load_config(root)


def pending_retry(root, monkeypatch, seconds=10, **recovery):
    cfg = config(root, **recovery)
    clock = {"elapsed": 0.0}
    monkeypatch.setattr(schedule, "_now", lambda: NOW + timedelta(seconds=clock["elapsed"]))
    monkeypatch.setattr(schedule.time, "sleep", lambda seconds: clock.update(elapsed=clock["elapsed"] + seconds))
    path = schedule._state_path(root, DAY)
    atomic_json(path, {"schema_version": 2, "date": str(DAY), "events": [], "stages": {
        "overnight": {"attempts": 1, "revision": schedule._revision(root), "failed": True,
                      "failure_kind": "transient", "circuit_open": False,
                      "next_retry_at": (NOW + timedelta(seconds=seconds)).isoformat()},
    }})
    return cfg, path, clock


def sealed(root):
    cfg = config(root)
    record = MaterialRecord(key="arxiv:test", source="arxiv", item_type="paper",
                            title="Verified result", url="https://example.org/paper")
    item = ApprovedItem(key=record.key, item_type=record.item_type, title=record.title,
                        source=record.source, url=record.url, final_fields={}, material=record)
    cfg.reports_dir.mkdir()
    (cfg.reports_dir / f"daily-agent-{DAY}.md").write_text("# Verified result\n")
    atomic_json(root / "data/editorial" / str(DAY) / "approval.json", [item.to_dict()])
    schedule.seal_ready_report(cfg, DAY)
    return cfg


def test_restart_obeys_persisted_retry_time_without_spending_an_attempt_early(tmp_path, monkeypatch):
    cfg, path, clock = pending_retry(tmp_path, monkeypatch)
    observed = []
    def execute(*args):
        entry = read_json(path)["stages"]["overnight"]
        observed.append((clock["elapsed"], entry["attempts"]))
        return 0
    schedule.run_scheduled_stage(tmp_path, "overnight", DAY, executor=execute)
    assert observed == [(10.0, 2)]
    entry = read_json(path)["stages"]["overnight"]
    assert entry["completed"] is True and not entry.get("waiting")
    assert "next_retry_at" not in entry


def test_pending_retry_cannot_outlive_remaining_budget(tmp_path, monkeypatch):
    cfg, path, clock = pending_retry(tmp_path, monkeypatch, overnight_budget_seconds=.1)
    with pytest.raises(schedule.StageFailure) as caught:
        schedule.run_scheduled_stage(tmp_path, "overnight", DAY,
                                     executor=lambda *args: pytest.fail("attempt started before ETA"))
    assert caught.value.kind == "deadline"
    entry = read_json(path)["stages"]["overnight"]
    assert entry["attempts"] == 1 and entry["circuit_open"] is True
    assert clock["elapsed"] == 0


def test_interrupted_retry_wait_preserves_eta_and_attempt_count(tmp_path, monkeypatch):
    cfg, path, clock = pending_retry(tmp_path, monkeypatch)
    def cancel(seconds):
        raise KeyboardInterrupt
    monkeypatch.setattr(schedule.time, "sleep", cancel)
    with pytest.raises(schedule.StageFailure) as caught:
        schedule.run_scheduled_stage(tmp_path, "overnight", DAY,
                                     executor=lambda *args: pytest.fail("attempt started before ETA"))
    assert caught.value.kind == "cancelled"
    entry = read_json(path)["stages"]["overnight"]
    assert entry["attempts"] == 1 and entry["next_retry_at"] == (NOW + timedelta(seconds=10)).isoformat()
    assert not entry.get("running") and not entry.get("waiting")
    with schedule.exclusive_lock(schedule._stage_lock(tmp_path)):
        pass  # cancellation released the controller lease


@pytest.mark.parametrize("stamp", ["tomorrow", "2026-10-08T01:00:00", 123, None])
def test_invalid_retry_clock_fails_closed(tmp_path, monkeypatch, stamp):
    cfg, path, clock = pending_retry(tmp_path, monkeypatch)
    payload = read_json(path)
    payload["stages"]["overnight"]["next_retry_at"] = stamp
    atomic_json(path, payload)
    before = path.read_bytes()
    with pytest.raises(StateCorrupt):
        schedule.run_scheduled_stage(tmp_path, "overnight", DAY, executor=lambda *args: pytest.fail("ran"))
    assert path.read_bytes() == before


def test_expired_retry_time_is_removed_on_success(tmp_path, monkeypatch):
    cfg, path, clock = pending_retry(tmp_path, monkeypatch, seconds=-1)
    schedule.run_scheduled_stage(tmp_path, "overnight", DAY, executor=lambda *args: 0)
    assert "next_retry_at" not in read_json(path)["stages"]["overnight"]
    assert clock["elapsed"] == 0


@pytest.mark.parametrize("content", [
    '{"attempts":1,"attempts":0}', '{"value":NaN}', '{"value":Infinity}', '{"value":1e999}',
])
def test_ambiguous_json_cannot_become_durable_state(tmp_path, content):
    path = tmp_path / "state.json"
    path.write_text(content)
    with pytest.raises(StateCorrupt):
        read_json(path)
    assert path.read_text() == content


def test_nonfinite_write_preserves_the_previous_checkpoint(tmp_path):
    path = tmp_path / "state.json"
    atomic_json(path, {"attempts": 1})
    before = path.read_bytes()
    with pytest.raises(ValueError):
        atomic_json(path, {"attempts": float("nan")})
    assert path.read_bytes() == before


@pytest.mark.parametrize("payload", ["{broken", {}, {"selected": [None]},
                                      {"selected": [{"key": "missing-required-fields"}]}])
def test_corrupt_formal_selection_cannot_silently_rewrite_history(tmp_path, payload):
    cfg = config(tmp_path)
    path = cfg.selected_dir / f"selected-{DAY}.json"
    path.parent.mkdir(parents=True)
    if isinstance(payload, str):
        path.write_text(payload)
    else:
        atomic_json(path, {"date": str(DAY), "dry_run": False, **payload})
    history = cfg.state_dir / "history_index.json"
    atomic_json(history, {"records": []})
    before = (path.read_bytes(), history.read_bytes())
    with pytest.raises(StateCorrupt):
        storage.load_history(cfg, DAY)
    assert (path.read_bytes(), history.read_bytes()) == before


@pytest.mark.parametrize("field,value", [("key", ""), ("selected_at", "invalid"),
                                         ("selected_at", 1), ("item_type", "other"),
                                         ("rank", True)])
def test_invalid_history_rows_do_not_disappear_on_reload(tmp_path, field, value):
    cfg = config(tmp_path)
    row = SelectedRecord(key="arxiv:old", source="arxiv", item_type="paper", title="Old paper",
                         url="https://example.org/old", selected_at=str(DAY)).to_dict()
    row[field] = value
    path = cfg.state_dir / "history_index.json"
    atomic_json(path, {"records": [row]})
    before = path.read_bytes()
    with pytest.raises(StateCorrupt):
        storage.load_history(cfg, DAY)
    assert path.read_bytes() == before


def test_dry_run_filename_is_always_excluded_from_formal_history(tmp_path):
    cfg = config(tmp_path)
    record = SelectedRecord(key="arxiv:preview", source="arxiv", item_type="paper", title="Preview",
                            url="https://example.org/preview", selected_at=str(DAY))
    path = cfg.selected_dir / f"selected-{DAY}.dry-run.json"
    atomic_json(path, {"date": str(DAY), "dry_run": False, "selected": [record.to_dict()]})
    assert storage.load_history(cfg, DAY) == {}
    path.write_text("{broken")
    assert storage.load_history(cfg, DAY) == {}


@pytest.mark.parametrize("mode", ["cc-connect", "feishu"])
def test_unavailable_delivery_configuration_never_creates_an_uncertain_journal(tmp_path, monkeypatch, mode):
    cfg = sealed(tmp_path)
    cfg.delivery["delivery"] = {"default": mode, "cc_connect": {"enabled": True, "publish_feishu_doc": True},
                                "feishu": {"enabled": False, "fallback_to_cc_connect": False}}
    monkeypatch.setattr(schedule.shutil, "which", lambda *args: None)
    monkeypatch.setattr("daily_agent.delivery.feishu.deliver_weekly_report",
                        lambda *args, **kwargs: pytest.fail("adapter called before preflight"))
    with pytest.raises(schedule.StageFailure) as caught:
        schedule.deliver_ready_report(cfg, DAY)
    assert caught.value.kind == "preflight"
    path = schedule._ready_path(cfg, DAY)
    assert not path.with_suffix(".outbox.json").exists()
    assert not path.with_suffix(".delivered.json").exists()
    # Correcting the cause can then send; no remote-outcome decision is needed.
    cfg.delivery["delivery"] = {"default": "cc-connect", "cc_connect": {"enabled": True}}
    monkeypatch.setattr(schedule.shutil, "which", lambda *args: "/fake/cc-connect")
    calls = []
    monkeypatch.setattr("daily_agent.delivery.feishu.deliver_weekly_report", lambda *args, **kwargs:
                        calls.append(args) or DeliveryStatus(requested_mode="cc-connect", final_mode="cc-connect"))
    schedule.deliver_ready_report(cfg, DAY)
    assert len(calls) == 1


def test_missing_feishu_credentials_without_fallback_are_preflight_failure(tmp_path, monkeypatch):
    cfg = sealed(tmp_path)
    cfg.delivery["delivery"] = {"default": "feishu", "feishu": {"enabled": True, "fallback_to_cc_connect": False}}
    monkeypatch.setattr("daily_agent.delivery.feishu._load_cc_connect_feishu_config", lambda: {})
    monkeypatch.setattr("daily_agent.delivery.feishu._env_value", lambda *args: None)
    monkeypatch.setattr("httpx.post", lambda *args, **kwargs: pytest.fail("network called"))
    with pytest.raises(schedule.StageFailure) as caught:
        schedule.deliver_ready_report(cfg, DAY)
    assert caught.value.kind == "preflight"
    assert not schedule._ready_path(cfg, DAY).with_suffix(".outbox.json").exists()


def test_enabled_cc_fallback_remains_available_when_feishu_is_disabled(tmp_path, monkeypatch):
    cfg = sealed(tmp_path)
    cfg.delivery["delivery"] = {"default": "feishu", "feishu": {"enabled": False, "fallback_to_cc_connect": True},
                                "cc_connect": {"enabled": True}}
    monkeypatch.setattr(schedule.shutil, "which", lambda *args: "/fake/cc-connect")
    calls = []
    monkeypatch.setattr("daily_agent.delivery.feishu.send_via_cc_connect", lambda *args, **kwargs: calls.append(args))
    schedule.deliver_ready_report(cfg, DAY)
    assert len(calls) == 1
    assert read_json(schedule._ready_path(cfg, DAY).with_suffix(".delivered.json"))["status"]["fallback_used"]


def test_post_preflight_failure_still_blocks_blind_resend(tmp_path, monkeypatch):
    cfg = sealed(tmp_path)
    monkeypatch.setattr(schedule.shutil, "which", lambda *args: "/fake/cc-connect")
    calls = []
    def partial_delivery(*args, **kwargs):
        calls.append(args)
        raise FileNotFoundError("document created; notification executable disappeared")
    monkeypatch.setattr("daily_agent.delivery.feishu.deliver_weekly_report", partial_delivery)
    for _ in range(2):
        with pytest.raises(schedule.StageFailure) as caught:
            schedule.deliver_ready_report(cfg, DAY)
        assert caught.value.kind == "delivery_uncertain"
    assert len(calls) == 1


def test_waiting_status_is_read_only_and_has_a_fresh_heartbeat(tmp_path, monkeypatch):
    cfg, path, clock = pending_retry(tmp_path, monkeypatch)
    inspected = []
    def sleep(seconds):
        before = path.read_bytes()
        status = schedule.inspect_schedule_state(tmp_path, DAY)
        assert path.read_bytes() == before
        assert status["ok"] and status["state"]["stages"]["overnight"]["waiting"]
        assert not status["state"]["stages"]["overnight"]["running"]
        assert status["state"]["stages"]["overnight"]["attempts"] == 1
        inspected.append(status)
        clock["elapsed"] += seconds
    monkeypatch.setattr(schedule.time, "sleep", sleep)
    schedule.run_scheduled_stage(tmp_path, "overnight", DAY, executor=lambda *args: 0)
    assert inspected


def direct_feishu(cfg, monkeypatch):
    cfg.delivery["delivery"] = {"default": "feishu", "feishu": {"enabled": True},
                                "cc_connect": {"enabled": True}}
    monkeypatch.setattr("daily_agent.delivery.feishu._load_cc_connect_feishu_config", lambda: {})
    monkeypatch.setattr("daily_agent.delivery.feishu._env_value", lambda settings, key:
                        "test-capability" if key in {"app_id_env", "app_secret_env"} else None)


def test_direct_feishu_does_not_require_cc_connect_executable(tmp_path, monkeypatch):
    cfg = sealed(tmp_path)
    direct_feishu(cfg, monkeypatch)
    monkeypatch.setattr(schedule.shutil, "which", lambda *args: None)
    calls = []
    monkeypatch.setattr("daily_agent.delivery.feishu._deliver_to_feishu", lambda *args:
                        calls.append(args) or DeliveryStatus(requested_mode="feishu", final_mode="feishu"))
    monkeypatch.setattr("daily_agent.delivery.feishu.send_via_cc_connect", lambda *args, **kwargs:
                        pytest.fail("fallback called"))
    schedule.deliver_ready_report(cfg, DAY)
    assert len(calls) == 1


@pytest.mark.parametrize("timeout", [0, -1, "invalid", float("inf")])
def test_invalid_direct_feishu_timeout_is_preflight_failure(tmp_path, monkeypatch, timeout):
    cfg = sealed(tmp_path)
    direct_feishu(cfg, monkeypatch)
    cfg.delivery["delivery"]["feishu"]["request_timeout_seconds"] = timeout
    monkeypatch.setattr("daily_agent.delivery.feishu.deliver_weekly_report", lambda *args, **kwargs:
                        pytest.fail("adapter called"))
    with pytest.raises(schedule.StageFailure) as caught:
        schedule.deliver_ready_report(cfg, DAY)
    assert caught.value.kind == "preflight"
    assert not schedule._ready_path(cfg, DAY).with_suffix(".outbox.json").exists()


def test_corrupt_feishu_state_is_checked_before_authentication_or_fallback(tmp_path, monkeypatch):
    cfg = sealed(tmp_path)
    direct_feishu(cfg, monkeypatch)
    path = cfg.state_dir / "feishu_delivery.json"
    path.write_text("{broken")
    monkeypatch.setattr("httpx.post", lambda *args, **kwargs: pytest.fail("network called"))
    monkeypatch.setattr("daily_agent.delivery.feishu.send_via_cc_connect", lambda *args, **kwargs:
                        pytest.fail("corruption hidden by fallback"))
    with pytest.raises(schedule.StageFailure) as caught:
        schedule.deliver_ready_report(cfg, DAY)
    assert caught.value.kind == "preflight"
    assert path.read_text() == "{broken"
    assert not schedule._ready_path(cfg, DAY).with_suffix(".outbox.json").exists()
