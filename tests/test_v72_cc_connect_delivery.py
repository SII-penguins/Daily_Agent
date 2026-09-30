from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace

from daily_agent.config import load_config
from daily_agent.models import DeliveryStatus, RunStatus


def test_cc_connect_sender_targets_configured_project_and_session(tmp_path, monkeypatch):
    from daily_agent.delivery.cc_connect import send_via_cc_connect

    report = tmp_path / "daily.md"
    report.write_text("# Daily", encoding="utf-8")
    captured = {}

    def fake_run(command, check):
        captured["command"] = command
        captured["check"] = check

    monkeypatch.setattr("daily_agent.delivery.cc_connect.subprocess.run", fake_run)

    send_via_cc_connect(
        report,
        message="日报已生成",
        project="my-project",
        session="feishu:chat:user",
    )

    assert captured["command"] == [
        "cc-connect",
        "send",
        "--file",
        str(report),
        "--message",
        "日报已生成",
        "--project",
        "my-project",
        "--session",
        "feishu:chat:user",
    ]
    assert captured["check"] is True


def test_cc_connect_delivery_uses_target_from_daily_agent_config(tmp_path, monkeypatch):
    from daily_agent.delivery.feishu import deliver_weekly_report

    config = load_config("/Users/wuzixie/Daily_Agent")
    config.delivery["delivery"]["cc_connect"] = {
        "enabled": True,
        "send_file": True,
        "project_env": "DAILY_AGENT_CC_CONNECT_PROJECT",
        "session_env": "DAILY_AGENT_CC_CONNECT_SESSION",
    }
    report = tmp_path / "daily.md"
    report.write_text("# Daily", encoding="utf-8")
    captured = {}

    def fake_send(path, message=None, *, project=None, session=None):
        captured.update(path=path, message=message, project=project, session=session)

    monkeypatch.setattr("daily_agent.delivery.feishu.send_via_cc_connect", fake_send)
    monkeypatch.setattr(
        "daily_agent.delivery.feishu.credential_value",
        lambda name: {
            "DAILY_AGENT_CC_CONNECT_PROJECT": "my-project",
            "DAILY_AGENT_CC_CONNECT_SESSION": "feishu:chat:user",
        }.get(name),
    )

    status = deliver_weekly_report(
        report,
        "cc-connect",
        config=config,
        run_date=date(2026, 7, 10),
        daily_markdown="# Daily",
    )

    assert status.ok is True
    assert status.final_mode == "cc-connect"
    assert captured["path"] == report
    assert captured["project"] == "my-project"
    assert captured["session"] == "feishu:chat:user"


def test_cli_returns_nonzero_when_external_delivery_fails(tmp_path, monkeypatch):
    from daily_agent import cli

    report = tmp_path / "daily.md"
    report.write_text("# Daily", encoding="utf-8")
    result = SimpleNamespace(
        weekly_report_path=report,
        weekly_html_path=tmp_path / "weekly.html",
        selected_path=tmp_path / "selected.json",
        bibtex_path=tmp_path / "selected.bib",
        ris_path=tmp_path / "selected.ris",
        csv_path=tmp_path / "selected.csv",
        endnote_xml_path=tmp_path / "selected.xml",
        items=[],
        health={},
        status=RunStatus(
            delivery=DeliveryStatus(
                requested_mode="cc-connect",
                final_mode="local",
                ok=False,
                error="no active session",
            )
        ),
    )
    monkeypatch.setattr(cli, "enforce_full_profile", lambda root, write: SimpleNamespace(changed=False))
    monkeypatch.setattr(cli, "run_pipeline", lambda **kwargs: result)
    args = SimpleNamespace(
        root=str(tmp_path),
        date="2026-07-10",
        require_full=False,
        dry_run=False,
        send="cc-connect",
        allow_degraded=True,
        llm=True,
    )

    assert cli._run_with_runtime(args) != 0


def test_cc_connect_delivery_publishes_feishu_doc_and_sends_clickable_url(tmp_path, monkeypatch):
    from daily_agent.delivery.feishu import deliver_weekly_report

    config = load_config("/Users/wuzixie/Daily_Agent")
    config.delivery["delivery"]["cc_connect"]["publish_feishu_doc"] = True
    report = tmp_path / "daily.md"
    report.write_text("# Daily", encoding="utf-8")
    captured = {}

    monkeypatch.setattr(
        "daily_agent.delivery.feishu._deliver_to_feishu",
        lambda config, path, run_date, daily_markdown: DeliveryStatus(
            requested_mode="feishu",
            final_mode="feishu",
            ok=True,
            document_url="https://feishu.cn/docx/doc123",
        ),
    )

    def fake_send(path, message=None, *, project=None, session=None):
        captured.update(path=path, message=message)

    monkeypatch.setattr("daily_agent.delivery.feishu.send_via_cc_connect", fake_send)

    status = deliver_weekly_report(
        report,
        "cc-connect",
        config=config,
        run_date=date(2026, 7, 10),
        daily_markdown="# Daily",
    )

    assert status.ok is True
    assert status.final_mode == "cc-connect"
    assert status.document_url == "https://feishu.cn/docx/doc123"
    assert captured["path"] is None
    assert "https://feishu.cn/docx/doc123" in captured["message"]


def test_cc_connect_delivery_falls_back_to_markdown_attachment_when_doc_publish_fails(tmp_path, monkeypatch):
    from daily_agent.delivery.feishu import deliver_weekly_report

    config = load_config("/Users/wuzixie/Daily_Agent")
    config.delivery["delivery"]["cc_connect"]["publish_feishu_doc"] = True
    report = tmp_path / "daily.md"
    report.write_text("# Daily", encoding="utf-8")
    captured = {}

    def fail_publish(*args, **kwargs):
        raise RuntimeError("doc API unavailable")

    monkeypatch.setattr("daily_agent.delivery.feishu._deliver_to_feishu", fail_publish)
    monkeypatch.setattr(
        "daily_agent.delivery.feishu.send_via_cc_connect",
        lambda path, message=None, **kwargs: captured.update(path=path, message=message),
    )

    status = deliver_weekly_report(
        report,
        "cc-connect",
        config=config,
        run_date=date(2026, 7, 10),
        daily_markdown="# Daily",
    )

    assert status.ok is True
    assert status.fallback_used is True
    assert status.error == "doc API unavailable"
    assert captured["path"] == report
    assert "Markdown" in captured["message"]


def test_feishu_text_payload_preserves_markdown_links_as_native_links():
    from daily_agent.delivery.feishu import _text_payload

    payload = _text_payload("链接：[论文原文](https://arxiv.org/abs/2607.07554)")
    elements = payload["elements"]

    assert "".join(item["text_run"]["content"] for item in elements) == "链接：论文原文"
    assert elements[1]["text_run"]["text_element_style"]["link"]["url"] == "https://arxiv.org/abs/2607.07554"
