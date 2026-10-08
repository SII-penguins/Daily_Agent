from __future__ import annotations

from datetime import date
from pathlib import Path

from daily_agent.config import load_config
from daily_agent.models import ApprovedItem, MaterialRecord, RunStatus
from daily_agent.rendering.html import render_daily_html
from daily_agent.storage import write_daily_html_report


def _approved_paper() -> ApprovedItem:
    material = MaterialRecord(
        key="arxiv:2607.00001",
        source="arxiv",
        item_type="paper",
        title="Quantum paper",
        url="https://arxiv.org/abs/2607.00001",
        pdf_url="https://arxiv.org/pdf/2607.00001",
        abstract="abstract",
        tags=["quantum_ai"],
    )
    return ApprovedItem(
        key=material.key,
        item_type="paper",
        title=material.title,
        source=material.source,
        url=material.url,
        final_fields={
            "problem": "量子线路映射问题。",
            "method": "硬件感知编译。",
            "why_it_works": "把硬件约束放入优化目标。",
            "novelty_or_difference": "联合考虑映射和路由。",
            "method_steps": ["建模", "搜索", "评估"],
            "key_result": "降低双量子门深度。",
            "technical_route": "硬件约束建模后搜索。",
            "possible_use_or_impact": "可用于编译器。",
            "limitations": "规模仍有限。",
        },
        material=material,
    )


def test_html_feedback_block_is_responsive_and_does_not_show_terminal_instruction():
    html = render_daily_html([_approved_paper()], date(2026, 7, 9), RunStatus())

    assert 'class="feedback-panel"' in html
    assert 'class="feedback-copy"' in html
    assert 'class="feedback-actions"' in html
    assert "Daily Agent 本地预览服务" in html
    assert "先运行" not in html
    assert "daily-agent feedback serve" not in html


def test_html_document_styles_feedback_panel_to_wrap_without_overlap(tmp_path):
    config = load_config(str(Path(__file__).resolve().parents[1]))
    object.__setattr__(config, "root", tmp_path)

    path = write_daily_html_report(config, date(2026, 7, 9), render_daily_html([_approved_paper()], date(2026, 7, 9), RunStatus()))
    html = path.read_text(encoding="utf-8")

    assert ".feedback-panel" in html
    assert "flex-wrap: wrap" in html
    assert "overflow-wrap: anywhere" in html
    assert ".feedback-actions" in html


def test_preview_start_cli_uses_combined_preview_server(monkeypatch, tmp_path, capsys):
    from daily_agent import cli

    calls: dict[str, object] = {}

    def fake_start_preview_server(config, *, host, report_port, feedback_port, report):
        calls["root"] = config.root
        calls["host"] = host
        calls["report_port"] = report_port
        calls["feedback_port"] = feedback_port
        calls["report"] = report
        return {
            "pid": 12345,
            "already_running": False,
            "html_url": "http://127.0.0.1:8766/reports/daily-agent-2026-W28.html",
            "markdown_url": "http://127.0.0.1:8766/reports/daily-agent-2026-W28.md",
            "feedback_url": "http://127.0.0.1:8765/",
        }

    monkeypatch.setattr(cli, "start_preview_server", fake_start_preview_server)

    code = cli.main(["preview", "start", "--root", str(tmp_path), "--report", "latest"])
    out = capsys.readouterr().out

    assert code == 0
    assert calls["root"] == tmp_path
    assert calls["report"] == "latest"
    assert "HTML report: http://127.0.0.1:8766/reports/daily-agent-2026-W28.html" in out
    assert "Feedback buttons: http://127.0.0.1:8765/" in out
    assert "pid=12345" in out


def test_mcp_preview_report_starts_preview_server(monkeypatch):
    from daily_agent import mcp_server

    seen: dict[str, list[str]] = {}

    def fake_run_cli(args):
        seen["args"] = args
        return "exit_code=0\nHTML report: http://127.0.0.1:8766/reports/daily-agent-2026-W28.html"

    monkeypatch.setattr(mcp_server, "_run_cli", fake_run_cli)

    output = mcp_server.preview_report("/tmp/daily-agent", report="latest")

    assert output.startswith("exit_code=0")
    assert seen["args"] == ["preview", "start", "--root", "/tmp/daily-agent", "--report", "latest"]


def test_readme_links_to_agent_started_preview_instead_of_manual_feedback_server():
    root = Path(__file__).resolve().parents[1]
    readme = (root / "README.md").read_text(encoding="utf-8")
    chinese = (root / "README.zh-CN.md").read_text(encoding="utf-8")

    assert "](docs/local-guide.md)" in readme
    assert "](docs/local-guide.zh-CN.md)" in chinese
    local_guide = (root / "docs/local-guide.md").read_text(encoding="utf-8")
    chinese_guide = (root / "docs/local-guide.zh-CN.md").read_text(encoding="utf-8")

    assert "preview_report" in local_guide
    assert "preview_report" in chinese_guide
    assert "ask Codex or Claude Code" in local_guide
    assert "让 Codex 或 Claude Code" in chinese_guide
