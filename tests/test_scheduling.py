from shlex import quote
import sys

from daily_agent.cli import main
from daily_agent.config import load_config
from daily_agent.scheduling import build_schedule_jobs, build_schedule_preview


def test_build_schedule_jobs_uses_delivery_schedule_times(tmp_path):
    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)
    config.delivery["schedule"]["production_time"] = "03:17"
    config.delivery["schedule"]["review_time"] = "07:21"
    config.delivery["schedule"]["target_time"] = "08:23"

    jobs = build_schedule_jobs(config)

    assert [job.name for job in jobs] == ["overnight-dry-run", "morning-review-checkpoint", "formal-feishu-delivery"]
    assert [job.time.cron for job in jobs] == ["17 3 * * *", "21 7 * * *", "23 8 * * *"]
    assert "--dry-run" in jobs[0].command
    assert "quality enforce-full --root" in jobs[0].command
    assert "--write" in jobs[0].command
    assert "quality check --root" in jobs[0].command
    assert "--require-full" in jobs[0].command
    assert jobs[0].command.count("--require-full") == 2
    assert " && " in jobs[0].command
    assert quote(sys.executable) in jobs[0].command
    assert quote(sys.executable) in jobs[1].command
    assert "quality enforce-full --root" in jobs[1].command
    assert "quality check" in jobs[1].command
    assert "source check" in jobs[1].command
    assert "--window-days 7" in jobs[1].command
    assert "quality enforce-full --root" in jobs[2].command
    assert "quality check --root" in jobs[2].command
    assert "--require-full" in jobs[2].command
    assert jobs[2].command.count("--require-full") == 2
    assert " && " in jobs[2].command
    assert "--send feishu" in jobs[2].command
    assert str(tmp_path / "src") in jobs[0].command


def test_cc_connect_preview_does_not_install_schedule(tmp_path):
    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)

    preview = build_schedule_preview(config, backend="cc-connect")

    assert preview.backend == "cc-connect"
    assert "Preview only" in preview.body
    assert "cc-connect cron add" in preview.body
    assert "--session-mode new-per-run" in preview.body
    assert "--timeout-mins 60" in preview.body
    assert "--dry-run" in preview.body
    assert "quality enforce-full" in preview.body
    assert "--write" in preview.body
    assert preview.body.count("--require-full") >= 4
    assert "quality check" in preview.body
    assert "--require-full" in preview.body
    assert "source check" in preview.body
    assert "--send feishu" in preview.body


def test_launchd_preview_contains_plist_without_bootstrap(tmp_path):
    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)

    preview = build_schedule_preview(config, backend="launchd")

    assert preview.backend == "launchd"
    assert "Preview only" in preview.body
    assert "launchctl bootstrap" in preview.body
    assert "<key>Label</key>" in preview.body
    assert "com.daily-agent.overnight-dry-run" in preview.body
    assert "com.daily-agent.morning-review-checkpoint" in preview.body
    assert "com.daily-agent.formal-feishu-delivery" in preview.body
    assert "StartCalendarInterval" in preview.body


def test_schedule_preview_cli_prints_preview(tmp_path, capsys):
    code = main(["schedule", "preview", "--root", str(tmp_path), "--backend", "cc-connect"])

    assert code == 0
    out = capsys.readouterr().out
    assert "Schedule backend: cc-connect" in out
    assert "overnight-dry-run" in out
    assert "morning-review-checkpoint" in out
    assert "formal-feishu-delivery" in out
    assert "cc-connect cron add" in out


def test_schedule_preview_rejects_bad_time(tmp_path, capsys):
    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)
    config.delivery["schedule"]["production_time"] = "25:00"

    try:
        build_schedule_jobs(config)
    except ValueError as exc:
        assert "schedule.production_time" in str(exc)
    else:
        raise AssertionError("bad schedule time should fail")
