import tomllib
from pathlib import Path

from daily_agent.mcp_server import setup_checklist


ROOT = Path(__file__).resolve().parents[1]


def test_pyproject_exposes_daily_agent_mcp_script_and_optional_dependency():
    payload = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert payload["project"]["scripts"]["daily-agent-mcp"] == "daily_agent.mcp_server:main"
    assert "mcp[cli]>=1.0.0" in payload["project"]["optional-dependencies"]["mcp"]


def test_setup_checklist_prompts_agent_to_customize_core_preferences():
    text = setup_checklist()

    for phrase in ["信源", "偏好内容", "工作时间节点", "推送方式", "02:00", "07:20", "08:00"]:
        assert phrase in text
    assert "SERPAPI_API_KEY" in text
    assert "supervised_scholar_fallback" in text
    for source in ["arXiv", "GitHub", "OpenAlex", "Semantic Scholar", "Google Scholar", "Crossref", "DBLP", "IEEE", "OpenReview", "PMLR", "NeurIPS"]:
        assert source in text


def test_readme_uses_simple_mcp_add_flow_for_codex_and_claude_code():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    chinese = (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")

    assert "codex mcp add daily-agent -- daily-agent-mcp" in readme
    assert "claude mcp add daily-agent -- daily-agent-mcp" in readme
    assert "daily-agent-mcp" in readme
    assert "codex mcp add daily-agent -- daily-agent-mcp" in chinese
    assert "claude mcp add daily-agent -- daily-agent-mcp" in chinese
    assert "正常使用时，不需要手动先运行 `daily-agent-mcp`" in chinese


def test_mcp_secrets_template_invokes_quality_template_cli(monkeypatch):
    from daily_agent import mcp_server

    seen = {}

    def fake_run_cli(args):
        seen["args"] = args
        return "exit_code=0"

    monkeypatch.setattr(mcp_server, "_run_cli", fake_run_cli)

    output = mcp_server.secrets_template(path="/tmp/secrets.toml", force=True)

    assert output == "exit_code=0"
    assert seen["args"] == ["quality", "secrets-template", "--path", "/tmp/secrets.toml", "--force"]


def test_mcp_secrets_template_can_request_missing_only(monkeypatch):
    from daily_agent import mcp_server

    seen = {}

    def fake_run_cli(args):
        seen["args"] = args
        return "exit_code=0"

    monkeypatch.setattr(mcp_server, "_run_cli", fake_run_cli)

    output = mcp_server.secrets_template(path="/tmp/missing.toml", missing_only=True)

    assert output == "exit_code=0"
    assert seen["args"] == ["quality", "secrets-template", "--path", "/tmp/missing.toml", "--missing-only"]


def test_mcp_secrets_status_invokes_quality_status_cli(monkeypatch):
    from daily_agent import mcp_server

    seen = {}

    def fake_run_cli(args):
        seen["args"] = args
        return "exit_code=0"

    monkeypatch.setattr(mcp_server, "_run_cli", fake_run_cli)

    output = mcp_server.secrets_status()

    assert output == "exit_code=0"
    assert seen["args"] == ["quality", "secrets-status"]


def test_mcp_quality_source_and_run_can_request_supervised_scholar_fallback(monkeypatch):
    from daily_agent import mcp_server

    seen = []

    def fake_run_cli(args):
        seen.append(args)
        return "exit_code=0"

    monkeypatch.setattr(mcp_server, "_run_cli", fake_run_cli)

    assert mcp_server.quality_check("/tmp/daily-agent", supervised_scholar_fallback=True) == "exit_code=0"
    assert mcp_server.source_check("/tmp/daily-agent", supervised_scholar_fallback=True) == "exit_code=0"
    assert mcp_server.run_digest("/tmp/daily-agent", supervised_scholar_fallback=True) == "exit_code=0"

    assert seen[0] == ["quality", "check", "--root", "/tmp/daily-agent", "--require-full", "--supervised-scholar-fallback"]
    assert "--supervised-scholar-fallback" in seen[1]
    assert "--supervised-scholar-fallback" in seen[2]
    assert "--require-full" in seen[2]


def test_mcp_run_digest_requires_full_preflight_by_default(monkeypatch):
    from daily_agent import mcp_server

    seen = {}

    def fake_run_cli(args):
        seen["args"] = args
        return "exit_code=2"

    monkeypatch.setattr(mcp_server, "_run_cli", fake_run_cli)

    assert mcp_server.run_digest("/tmp/daily-agent") == "exit_code=2"

    assert seen["args"] == [
        "run",
        "--root",
        "/tmp/daily-agent",
        "--date",
        "today",
        "--dry-run",
        "--llm",
        "--require-full",
    ]


def test_mcp_run_digest_can_bypass_full_preflight_for_debugging(monkeypatch):
    from daily_agent import mcp_server

    seen = {}

    def fake_run_cli(args):
        seen["args"] = args
        return "exit_code=0"

    monkeypatch.setattr(mcp_server, "_run_cli", fake_run_cli)

    assert mcp_server.run_digest("/tmp/daily-agent", require_full=False) == "exit_code=0"

    assert seen["args"] == [
        "run",
        "--root",
        "/tmp/daily-agent",
        "--date",
        "today",
        "--dry-run",
        "--llm",
    ]


def test_mcp_run_digest_can_disable_llm_for_debugging(monkeypatch):
    from daily_agent import mcp_server

    seen = {}

    def fake_run_cli(args):
        seen["args"] = args
        return "exit_code=0"

    monkeypatch.setattr(mcp_server, "_run_cli", fake_run_cli)

    assert mcp_server.run_digest("/tmp/daily-agent", use_llm=False, require_full=False) == "exit_code=0"

    assert seen["args"] == [
        "run",
        "--root",
        "/tmp/daily-agent",
        "--date",
        "today",
        "--dry-run",
        "--no-llm",
    ]


def test_mcp_source_check_can_bypass_full_profile_guard(monkeypatch):
    from daily_agent import mcp_server

    seen = {}

    def fake_run_cli(args):
        seen["args"] = args
        return "exit_code=0"

    monkeypatch.setattr(mcp_server, "_run_cli", fake_run_cli)

    assert mcp_server.source_check("/tmp/daily-agent", enforce_full=False) == "exit_code=0"

    assert seen["args"] == [
        "source",
        "check",
        "--root",
        "/tmp/daily-agent",
        "--date",
        "today",
        "--window-days",
        "7",
        "--sample-limit",
        "3",
        "--no-enforce-full",
    ]
