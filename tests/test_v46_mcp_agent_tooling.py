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


def test_readme_uses_simple_mcp_add_flow_for_codex_and_claude_code():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    chinese = (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")

    assert "codex mcp add daily-agent -- daily-agent-mcp" in readme
    assert "claude mcp add daily-agent -- daily-agent-mcp" in readme
    assert "daily-agent-mcp" in readme
    assert "codex mcp add daily-agent -- daily-agent-mcp" in chinese
    assert "claude mcp add daily-agent -- daily-agent-mcp" in chinese
    assert "正常使用时，不需要手动先运行 `daily-agent-mcp`" in chinese
