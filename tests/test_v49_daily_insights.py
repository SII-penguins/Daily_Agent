from __future__ import annotations

from datetime import date

from daily_agent.health import evaluate_run_health
from daily_agent.insights import build_daily_insights
from daily_agent.models import ApprovedItem, DeliveryStatus, EditorialDraft, EditorialReview, MaterialRecord, RunStatus
from daily_agent.rendering.html import render_daily_html
from daily_agent.rendering.markdown import render_daily_markdown


def _paper(
    key: str,
    title: str,
    method: str,
    result: str,
    tags: list[str] | None = None,
    score: float = 90,
    citation_context: dict | None = None,
) -> ApprovedItem:
    material = MaterialRecord(
        key=key,
        source="arxiv",
        item_type="paper",
        title=title,
        url=f"https://arxiv.org/abs/{key.split(':', 1)[1]}",
        tags=tags or ["quantum_ai", "hardware_aware"],
        score=score,
        score_breakdown={"domain": 20, "evidence": 8, "venue": 3},
        paper_text_status={"available": True, "sufficient_for_deep_summary": True, "sections_found": ["method", "results", "limitations"]},
        raw={"citation_context": citation_context} if citation_context else {},
    )
    fields = {
        "problem": "真实量子硬件约束下线路深度和路由开销难以同时控制。",
        "method": method,
        "why_it_works": "把硬件连通性、噪声和搜索目标放进同一个优化过程，减少后处理路由损失。",
        "method_steps": ["建模硬件约束", "搜索候选线路", "用 benchmark 验证"],
        "key_result": result,
        "technical_route": method,
        "possible_use_or_impact": "可用于硬件感知量子线路综合和编译。",
        "limitations": "主要局限是实验仍集中在模拟或小规模硬件。",
    }
    return ApprovedItem(key=material.key, item_type="paper", title=title, source="arxiv", url=material.url, final_fields=fields, material=material)


def test_build_daily_insights_compares_approved_papers():
    items = [
        _paper("arxiv:2601.00001", "Hardware-aware routing", "提出硬件感知 routing 方法，联合优化 placement 和 swap insertion。", "实验显示 two-qubit depth 降低 24%。"),
        _paper("arxiv:2601.00002", "Noise-aware circuit synthesis", "提出噪声感知 circuit synthesis 方法，把 backend noise 放入搜索目标。", "在真实硬件后端上 fidelity 提升 11%。"),
    ]

    insights = build_daily_insights(items)

    assert len(insights) == 4
    assert insights[0].startswith("共同趋势：")
    assert "hardware_aware" in insights[0] or "硬件" in insights[0]
    assert insights[1].startswith("方法差异：")
    assert "routing" in insights[1] or "synthesis" in insights[1]
    assert insights[2].startswith("研究空白：")
    assert "小规模硬件" in insights[2] or "真实硬件" in insights[2]
    assert insights[3].startswith("值得追踪：")


def test_build_daily_insights_respects_configured_max_insights():
    items = [
        _paper("arxiv:2601.00001", "Hardware-aware routing", "提出硬件感知 routing 方法，联合优化 placement 和 swap insertion。", "实验显示 two-qubit depth 降低 24%。"),
        _paper("arxiv:2601.00002", "Noise-aware circuit synthesis", "提出噪声感知 circuit synthesis 方法，把 backend noise 放入搜索目标。", "在真实硬件后端上 fidelity 提升 11%。"),
    ]

    insights = build_daily_insights(items, settings={"enabled": True, "max_insights": 2, "min_items": 2, "research_gap_enabled": True})

    assert len(insights) == 2
    assert insights[0].startswith("共同趋势：")
    assert insights[1].startswith("方法差异：")


def test_build_daily_insights_adds_citation_context_when_available():
    items = [
        _paper(
            "arxiv:2601.00001",
            "Hardware-aware routing",
            "提出硬件感知 routing 方法，联合优化 placement 和 swap insertion。",
            "实验显示 two-qubit depth 降低 24%。",
            citation_context={
                "cited_by_count": 42,
                "citing": [{"title": "Follow-up compiler benchmark"}],
                "referenced": [{"title": "Foundational quantum routing"}],
            },
        ),
        _paper("arxiv:2601.00002", "Noise-aware circuit synthesis", "提出噪声感知 circuit synthesis 方法，把 backend noise 放入搜索目标。", "在真实硬件后端上 fidelity 提升 11%。"),
    ]

    insights = build_daily_insights(items, settings={"enabled": True, "max_insights": 5, "min_items": 2, "research_gap_enabled": True})

    assert len(insights) == 5
    assert any(insight.startswith("引用脉络：") for insight in insights)
    assert "被引 42 次" in " ".join(insights)


def test_render_daily_reports_include_daily_insights():
    items = [
        _paper("arxiv:2601.00001", "Hardware-aware routing", "提出硬件感知 routing 方法，联合优化 placement 和 swap insertion。", "实验显示 two-qubit depth 降低 24%。"),
        _paper("arxiv:2601.00002", "Noise-aware circuit synthesis", "提出噪声感知 circuit synthesis 方法，把 backend noise 放入搜索目标。", "在真实硬件后端上 fidelity 提升 11%。"),
    ]

    markdown = render_daily_markdown(items, date(2026, 5, 18), RunStatus())
    html = render_daily_html(items, date(2026, 5, 18), RunStatus())

    assert markdown.index("## 今日必看") < markdown.index("## 今日洞察") < markdown.index("## 最新论文")
    assert "- 共同趋势：" in markdown
    assert "<h2>今日洞察</h2>" in html
    assert "共同趋势：" in html


def test_render_daily_reports_can_disable_daily_insights():
    items = [
        _paper("arxiv:2601.00001", "Hardware-aware routing", "提出硬件感知 routing 方法，联合优化 placement 和 swap insertion。", "实验显示 two-qubit depth 降低 24%。"),
        _paper("arxiv:2601.00002", "Noise-aware circuit synthesis", "提出噪声感知 circuit synthesis 方法，把 backend noise 放入搜索目标。", "在真实硬件后端上 fidelity 提升 11%。"),
    ]
    insight_config = {"enabled": False, "include_in_reports": False, "max_insights": 3, "min_items": 2}

    markdown = render_daily_markdown(items, date(2026, 5, 18), RunStatus(), insight_config=insight_config)
    html = render_daily_html(items, date(2026, 5, 18), RunStatus(), insight_config=insight_config)

    assert "## 今日洞察" not in markdown
    assert "<h2>今日洞察</h2>" not in html


def test_render_daily_insights_fallback_when_sample_is_too_small():
    markdown = render_daily_markdown([_paper("arxiv:2601.00001", "One paper", "提出 routing 方法。", "实验显示 depth 降低。")], date(2026, 5, 18), RunStatus())

    assert "## 今日洞察" in markdown
    assert "今日样本不足，暂不生成跨条目洞察。" in markdown


def test_health_summary_counts_daily_insights(tmp_path, monkeypatch):
    from daily_agent.config import load_config

    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)
    monkeypatch.setattr("daily_agent.health.run_quality_check", lambda config: type("Profile", (), {"overall": "full", "checks": []})())
    items = [
        _paper("arxiv:2601.00001", "Hardware-aware routing", "提出硬件感知 routing 方法，联合优化 placement 和 swap insertion。", "实验显示 two-qubit depth 降低 24%。"),
        _paper("arxiv:2601.00002", "Noise-aware circuit synthesis", "提出噪声感知 circuit synthesis 方法，把 backend noise 放入搜索目标。", "在真实硬件后端上 fidelity 提升 11%。"),
    ]
    editorial_path = tmp_path / "editorial"
    editorial_path.mkdir()
    for name in ["shortlist.json", "writer_draft.json", "editor_review.json", "approval.json"]:
        (editorial_path / name).write_text("[]", encoding="utf-8")
    weekly_report = tmp_path / "report.md"
    weekly_html = tmp_path / "report.html"
    selected = tmp_path / "selected.json"
    weekly_report.write_text("report", encoding="utf-8")
    weekly_html.write_text("html", encoding="utf-8")
    selected.write_text("{}", encoding="utf-8")

    health = evaluate_run_health(
        config,
        date(2026, 5, 18),
        True,
        RunStatus(delivery=DeliveryStatus(requested_mode="local", final_mode="local", ok=True)),
        items,
        [item.material for item in items],
        [EditorialDraft(key=item.key, item_type=item.item_type, title=item.title, draft_fields=item.final_fields, evidence_used=["evidence"]) for item in items],
        [EditorialReview(key=item.key, verdict="PASS", issues=[]) for item in items],
        weekly_report,
        weekly_html,
        selected,
        editorial_path,
    )

    assert health["current"]["summary"]["insight_count"] == 4


def test_quality_check_requires_daily_insights_full_profile(tmp_path):
    from daily_agent.config import load_config
    from daily_agent.quality import run_quality_check

    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)
    config.sources["insights"] = {"enabled": True, "include_in_reports": True, "max_insights": 1, "min_items": 3}

    profile = run_quality_check(config)
    by_key = {check.key: check for check in profile.checks}

    assert by_key["daily_insights"].status == "partial"
    assert "max_insights=1" in by_key["daily_insights"].detail
    assert "min_items=3" in by_key["daily_insights"].detail


def test_quality_check_requires_research_gap_insights_full_profile(tmp_path):
    from daily_agent.config import load_config
    from daily_agent.quality import run_quality_check

    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)
    config.sources["insights"] = {"enabled": True, "include_in_reports": True, "max_insights": 4, "min_items": 2, "research_gap_enabled": False}

    profile = run_quality_check(config)
    by_key = {check.key: check for check in profile.checks}

    assert by_key["daily_insights"].status == "partial"
    assert "research_gap_enabled=false" in by_key["daily_insights"].detail
