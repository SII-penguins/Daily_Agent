from __future__ import annotations

import json

import pytest

from daily_agent.config import load_config
from daily_agent.models import DigestItem


def test_legacy_item_summarizer_uses_configured_codex_runtime(monkeypatch):
    from daily_agent.scoring.llm import summarize_items

    config = load_config("/Users/wuzixie/Daily_Agent")
    item = DigestItem(
        id="2607.00001",
        source="arxiv",
        item_type="paper",
        title="A quantum compilation paper",
        url="https://arxiv.org/abs/2607.00001",
        abstract="We study hardware-aware quantum compilation.",
    )
    payload = [
        {
            "id": item.id,
            "summary_zh": "论文研究硬件感知量子编译。",
            "technical_route": "联合建模线路和硬件约束。",
            "possible_use_or_impact": "可用于量子编译器优化。",
            "reusable_point": "",
            "recommendation_reason": "与量子编译方向直接相关。",
            "llm_tags": ["quantum_compilation"],
        }
    ]
    captured = {}

    class Result:
        stdout = json.dumps(payload, ensure_ascii=False)

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["timeout"] = kwargs["timeout"]
        return Result()

    monkeypatch.setattr("daily_agent.scoring.llm.subprocess.run", fake_run)

    result = summarize_items([item], config=config)

    assert captured["args"][:2] == [config.sources["llm_writer"]["command"], "exec"]
    assert "claude" not in captured["args"]
    assert captured["timeout"] == 600
    assert result[0].summary_zh == "论文研究硬件感知量子编译。"


def test_cli_help_names_codex_as_internal_writer(capsys):
    from daily_agent.cli import main

    with pytest.raises(SystemExit) as exc_info:
        main(["run", "--help"])

    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    assert "Use local Codex" in output
    assert "local Claude Code" not in output


def test_quality_check_rejects_claude_as_internal_writer(monkeypatch):
    from daily_agent.quality import run_quality_check

    config = load_config("/Users/wuzixie/Daily_Agent")
    config.sources["llm_writer"] = {"provider": "claude", "command": "claude"}
    monkeypatch.setattr("daily_agent.quality._which", lambda name: f"/usr/bin/{name}")

    check = {item.key: item for item in run_quality_check(config).checks}["llm_writer"]

    assert check.status == "missing"
    assert check.ok is False
    assert "provider=claude" in check.detail
    assert "provider to codex" in (check.action or "")


def test_api_wrapper_keeps_user_provider_config():
    from daily_agent.editorial import _llm_writer_command
    args = _llm_writer_command({"command": "/local/codex-api", "load_user_config": True}, "test")
    assert args[:2] == ["/local/codex-api", "exec"]
    assert "--ignore-user-config" not in args
    assert "read-only" in args
    assert "--ignore-user-config" in _llm_writer_command({}, "test")


def test_full_profile_preserves_api_wrapper(tmp_path):
    import yaml
    from daily_agent.full_profile import enforce_full_profile
    folder = tmp_path / "config"
    folder.mkdir()
    path = folder / "sources.yaml"
    path.write_text(yaml.safe_dump({"llm_writer": {"provider":"codex", "command":"/local/codex-api", "load_user_config":True}}))
    enforce_full_profile(tmp_path, write=True)
    config = yaml.safe_load(path.read_text())
    assert config["llm_writer"]["command"] == "/local/codex-api"
    assert config["llm_writer"]["load_user_config"] is True
