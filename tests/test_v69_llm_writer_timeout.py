from __future__ import annotations

import json
import subprocess

from daily_agent.config import load_config
from daily_agent.editorial import draft_report_items
from daily_agent.models import MaterialRecord


def _repo_material(index: int) -> MaterialRecord:
    return MaterialRecord(
        key=f"github:owner/repo-{index}",
        source="github",
        item_type="repo",
        title=f"owner/repo-{index}",
        url=f"https://github.com/owner/repo-{index}",
        repo_description="An MCP server for coding agents.",
        readme_excerpt="Provides MCP tools, server APIs, and examples for build automation.",
        tags=["mcp", "agent"],
        score=100 - index,
    )


def _llm_repo_payload(material: MaterialRecord) -> list[dict]:
    return [
        {
            "key": material.key,
            "draft_fields": {
                "what_it_is": "A repo for agent tooling.",
                "core_capabilities": "MCP tools and server APIs.",
                "typical_use_cases": "Agent integration and build automation.",
                "architecture_or_api": "MCP server API with examples.",
                "maturity_signal": "README evidence.",
                "reusable_point": "Tool interface design.",
                "evidence_from_source": "README",
                "confidence": "medium",
            },
            "evidence_used": ["README"],
            "writer_notes": "validated",
        }
    ]


def test_llm_writer_uses_configured_batch_size_and_timeout(monkeypatch):
    config = load_config("/Users/wuzixie/Daily_Agent")
    config.sources["llm_writer"] = {"batch_size": 1, "timeout_seconds": 12, "run_budget_seconds": 120}
    material = _repo_material(1)
    captured: dict[str, object] = {}

    class Result:
        stdout = json.dumps(_llm_repo_payload(material))

    def fake_run(*args, **kwargs):
        captured["timeout"] = kwargs["timeout"]
        return Result()

    monkeypatch.setattr("daily_agent.editorial.subprocess.run", fake_run)

    drafts = draft_report_items(config, [material], use_llm=True)

    assert captured["timeout"] == 12
    assert drafts[0].writer_notes.startswith("LLM写手草稿")


def test_llm_writer_stops_calling_backend_after_run_budget(monkeypatch):
    config = load_config("/Users/wuzixie/Daily_Agent")
    config.sources["llm_writer"] = {"batch_size": 1, "timeout_seconds": 60, "run_budget_seconds": 30}
    materials = [_repo_material(1), _repo_material(2), _repo_material(3)]
    calls = {"count": 0}
    monotonic_values = iter([0.0, 0.0, 31.0])

    class Result:
        stdout = json.dumps(_llm_repo_payload(materials[0]))

    def fake_run(*args, **kwargs):
        calls["count"] += 1
        return Result()

    monkeypatch.setattr("daily_agent.editorial.time.monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr("daily_agent.editorial.subprocess.run", fake_run)

    drafts = draft_report_items(config, materials, use_llm=True)

    assert calls["count"] == 1
    assert drafts[0].writer_notes.startswith("LLM写手草稿")
    assert drafts[1].writer_notes.startswith("规则写手草稿")
    assert drafts[2].writer_notes.startswith("规则写手草稿")


def test_llm_writer_timeout_falls_back_to_rule_writer(monkeypatch):
    config = load_config("/Users/wuzixie/Daily_Agent")
    config.sources["llm_writer"] = {"batch_size": 1, "timeout_seconds": 1, "run_budget_seconds": 30}
    material = _repo_material(1)

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["codex"], timeout=kwargs["timeout"])

    monkeypatch.setattr("daily_agent.editorial.subprocess.run", fake_run)

    drafts = draft_report_items(config, [material], use_llm=True)

    assert drafts[0].writer_notes.startswith("规则写手草稿")


def test_llm_writer_stops_after_first_backend_failure(monkeypatch):
    config = load_config("/Users/wuzixie/Daily_Agent")
    config.sources["llm_writer"] = {"batch_size": 1, "timeout_seconds": 1, "run_budget_seconds": 30}
    materials = [_repo_material(1), _repo_material(2), _repo_material(3)]
    calls = {"count": 0}

    def fake_run(*args, **kwargs):
        calls["count"] += 1
        raise subprocess.TimeoutExpired(cmd=["codex"], timeout=kwargs["timeout"])

    monkeypatch.setattr("daily_agent.editorial.subprocess.run", fake_run)

    drafts = draft_report_items(config, materials, use_llm=True)

    assert calls["count"] == 1
    assert all(draft.writer_notes.startswith("规则写手草稿") for draft in drafts)


def test_llm_writer_supports_codex_backend_and_caps_excerpt(monkeypatch):
    config = load_config("/Users/wuzixie/Daily_Agent")
    config.sources["llm_writer"] = {
        "provider": "codex",
        "batch_size": 1,
        "timeout_seconds": 12,
        "run_budget_seconds": 120,
        "max_input_chars_per_item": 120,
        "reasoning_effort": "low",
    }
    material = _repo_material(1)
    material.readme_excerpt = "X" * 1000
    captured: dict[str, object] = {}

    class Result:
        stdout = json.dumps(_llm_repo_payload(material))

    def fake_run(args, **kwargs):
        captured["args"] = args
        return Result()

    monkeypatch.setattr("daily_agent.editorial.subprocess.run", fake_run)

    drafts = draft_report_items(config, [material], use_llm=True)

    args = captured["args"]
    assert isinstance(args, list)
    assert args[:2] == ["codex", "exec"]
    assert "model_reasoning_effort=\"low\"" in args
    prompt = args[-1]
    assert isinstance(prompt, str)
    assert "X" * 120 in prompt
    assert "X" * 121 not in prompt
    assert "不要调用任何工具" in prompt
    assert "禁止数组和对象" in prompt
    assert drafts[0].writer_notes.startswith("LLM写手草稿")
