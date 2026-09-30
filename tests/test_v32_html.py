import json
import os
from datetime import date, datetime, timezone

from daily_agent.cli import main
from daily_agent.config import load_config
from daily_agent.models import ApprovedItem, DeliveryStatus, DigestItem, MaterialRecord, RunStatus, SourceStatus
from daily_agent.pipeline import run_pipeline
from daily_agent.rendering.html import render_daily_html
from daily_agent.storage import cleanup_retention, load_health_report, weekly_html_report_path, write_material_library, write_weekly_html_report


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


def _paper(update_label=None, unsafe=False):
    title = 'Quantum <script>alert(1)</script> & paper' if unsafe else "Quantum circuit method"
    url = "javascript:alert(1)" if unsafe else "https://arxiv.org/abs/2401.00001v1"
    material = MaterialRecord(
        key="arxiv:2401.00001",
        source="arxiv",
        item_type="paper",
        title=title,
        url=url,
        pdf_url="https://arxiv.org/pdf/2401.00001v1",
        abstract="abstract",
        tags=["quantum_ai", "tag<script>"],
        raw={"is_historical_supplement": True, "published_at": "2026-05-18T00:00:00+00:00"},
        source_updated_at="2026-05-18T00:00:00+00:00",
        update_label=update_label,
    )
    return ApprovedItem(
        key=material.key,
        item_type="paper",
        title=material.title,
        source=material.source,
        url=material.url,
        final_fields={
            "problem": "solve <unsafe> & issue" if unsafe else "solve circuit design",
            "method": "quantum compilation",
            "method_steps": ["step 1", "step 2"],
            "key_result": "reported results",
            "possible_use_or_impact": "compiler use",
            "limitations": "small benchmark",
        },
        material=material,
    )


def _repo(update_label=None):
    material = MaterialRecord(
        key="github:owner/repo",
        source="github",
        item_type="repo",
        title="owner/repo",
        url="https://github.com/owner/repo",
        repo_description="Agent toolkit",
        tags=["agent"],
        stars=42,
        language="Python",
        source_updated_at="2026-05-18T00:00:00+00:00",
        update_label=update_label,
    )
    return ApprovedItem(
        key=material.key,
        item_type="repo",
        title=material.title,
        source=material.source,
        url=material.url,
        final_fields={
            "what_it_is": "A repo",
            "core_capabilities": "MCP tools",
            "typical_use_cases": "agent integration",
            "architecture_or_api": "server API",
            "maturity_signal": "42 stars",
            "reusable_point": "tool API",
        },
        material=material,
    )


def test_render_daily_html_sections_fields_labels_and_ranks():
    status = RunStatus(sources=[SourceStatus(name="arXiv/7d", ok=True, item_count=1)], delivery=DeliveryStatus(requested_mode="dry-run", final_mode="local", ok=True))

    html = render_daily_html([_paper(update_label="version_update"), _repo(update_label="major_update")], date(2026, 5, 18), status)

    assert "今日必看" in html
    assert "最新论文" in html
    assert "GitHub 项目" in html
    assert "运行状态" in html
    assert "1. 【历史补充/版本更新】Quantum circuit method" in html
    assert "2. 【重大更新】owner/repo" in html
    assert "solve circuit design" in html
    assert "quantum compilation" in html
    assert "阅读状态" in html
    assert "证据缺口" in html
    assert "核心能力" in html
    assert "stars/language/updated_at" in html
    assert "反馈编号" in html
    assert "第 1 条" in html
    assert "第 2 条" in html


def test_render_daily_html_escapes_text_and_blocks_unsafe_urls():
    status = RunStatus(errors=["bad <script>alert(2)</script> & error"])

    html = render_daily_html([_paper(unsafe=True)], date(2026, 5, 18), status)

    assert "<script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; paper" in html
    assert "solve &lt;unsafe&gt; &amp; issue" in html
    assert "tag&lt;script&gt;" in html
    assert "javascript:alert" not in html
    assert "bad &lt;script&gt;alert(2)&lt;/script&gt; &amp; error" in html


def test_render_daily_html_empty_sections():
    html = render_daily_html([], date(2026, 5, 18), RunStatus())

    assert "今日没有筛选出足够高质量的内容。" in html
    assert "今日未筛选出论文条目。" in html
    assert "今日未筛选出 GitHub 项目。" in html


def test_write_weekly_html_report_upserts_newest_first(tmp_path):
    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)

    first = write_weekly_html_report(config, date(2026, 5, 18), "<h1>first</h1>")
    write_weekly_html_report(config, date(2026, 5, 19), "<h1>second</h1>")
    write_weekly_html_report(config, date(2026, 5, 18), "<h1>first updated</h1>")
    content = first.read_text(encoding="utf-8")

    assert content.startswith("<!doctype html>")
    assert content.count("daily-agent-date:2026-05-18") == 2
    assert "<h1>first</h1>" not in content
    assert content.find("daily-agent-date:2026-05-18") < content.find("daily-agent-date:2026-05-19")
    assert '<article class="daily-report" data-run-date="2026-05-18">' in content


def test_cleanup_retention_removes_old_html_reports(tmp_path):
    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)
    config.delivery["retention"]["weekly_html_keep_days"] = 1
    old = config.reports_dir / "daily-agent-2025-W01.html"
    old.parent.mkdir(parents=True)
    old.write_text("old", encoding="utf-8")
    old_time = datetime(2026, 5, 1, tzinfo=timezone.utc).timestamp()
    os.utime(old, (old_time, old_time))

    cleanup_retention(config, date(2026, 5, 20))

    assert not old.exists()


def test_pipeline_writes_html_and_health_tracks_it(tmp_path, monkeypatch):
    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)
    material = MaterialRecord(
        key="github:owner/repo",
        source="github",
        item_type="repo",
        title="owner/repo",
        url="https://github.com/owner/repo",
        repo_description="Agent toolkit",
        readme_excerpt="Provides API examples for agents.",
        tags=["agent"],
        score=99,
    )
    write_material_library(config, {material.key: material})
    monkeypatch.setattr("daily_agent.pipeline.load_config", lambda root=None: config)
    monkeypatch.setattr("daily_agent.pipeline.fetch_arxiv", lambda config, target_dt, window_days=None: [])
    monkeypatch.setattr("daily_agent.pipeline.fetch_github", lambda config, target_dt, window_days=None: [])
    _stub_other_pipeline_sources(monkeypatch)

    result = run_pipeline(root=tmp_path, run_date=date(2026, 5, 18), dry_run=True, use_llm=False)
    health = load_health_report(config)
    payload = json.dumps(health, ensure_ascii=False)

    assert result.weekly_html_path == weekly_html_report_path(config, date(2026, 5, 18))
    assert result.weekly_html_path.exists()
    assert health["current"]["signals"]["artifacts"]["weekly_html_report"] is True
    assert str(tmp_path) not in payload


def test_cli_prints_html_report_path(tmp_path, monkeypatch, capsys):
    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)
    material = MaterialRecord(key="github:owner/repo", source="github", item_type="repo", title="owner/repo", url="https://github.com/owner/repo", repo_description="Agent toolkit", score=99)
    write_material_library(config, {material.key: material})
    monkeypatch.setattr("daily_agent.pipeline.load_config", lambda root=None: config)
    monkeypatch.setattr("daily_agent.pipeline.fetch_arxiv", lambda config, target_dt, window_days=None: [])
    monkeypatch.setattr("daily_agent.pipeline.fetch_github", lambda config, target_dt, window_days=None: [])
    _stub_other_pipeline_sources(monkeypatch)

    assert main(["run", "--root", str(tmp_path), "--date", "2026-05-18", "--dry-run", "--no-llm"]) == 0
    output = capsys.readouterr().out

    assert "Daily Agent report generated:" in output
    assert "HTML report:" in output
    assert "daily-agent-2026-W21.html" in output
