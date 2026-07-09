from __future__ import annotations

from datetime import date

from daily_agent.config import load_config
from daily_agent.editorial import approve_publication, draft_report_items, review_draft
from daily_agent.models import EditorialDraft, EditorialReview, MaterialRecord, RunStatus
from daily_agent.rendering.markdown import render_daily_markdown


def _paper_material(key: str = "arxiv:2607.00001", score: float = 100.0) -> MaterialRecord:
    return MaterialRecord(
        key=key,
        source="arxiv",
        item_type="paper",
        title=f"Quantum paper {key}",
        url=f"https://arxiv.org/abs/{key.split(':', 1)[1]}",
        abstract="A useful paper about quantum compilation.",
        tags=["quantum_ai"],
        score=score,
    )


def _good_fields(index: int = 0) -> dict:
    return {
        "problem": f"论文针对真实量子硬件约束下的线路编译问题 {index}，需要同时考虑连通性、噪声和执行成功率。",
        "method": f"方法把硬件拓扑、门错误率和线路结构编码成优化目标 {index}，再搜索更适合目标后端的编译方案。",
        "why_it_works": f"它有效的原因是把设备约束放进搜索过程 {index}，减少后处理修补带来的额外 SWAP 和深度开销。",
        "novelty_or_difference": f"相对只看门数或深度的编译策略 {index}，这篇把硬件反馈作为一等信号参与排序。",
        "method_steps": ["提取线路特征", "读取硬件拓扑", "搜索候选编译方案", "用执行指标评估"],
        "key_result": f"实验显示该方法在多个量子线路基准上降低深度并提升成功率 {index}。",
        "technical_route": f"先提取线路和硬件特征，再生成候选编译结果，最后按执行质量排序 {index}。",
        "possible_use_or_impact": f"适合迁移到硬件感知量子线路综合或编译器 pass 调优 {index}。",
        "limitations": f"局限是实验仍依赖有限后端和有限线路规模 {index}。",
        "evidence_from_source": "Full text sections describe method, experiments, and limitations.",
        "confidence": "medium",
    }


def test_editor_rejects_pdf_extraction_noise_fragments():
    config = load_config("/Users/wuzixie/Daily_Agent")
    draft = EditorialDraft(
        key="doi:10.1038/example",
        item_type="paper",
        title="Enhancing classical simulation with noisy quantum devices",
        draft_fields={
            **_good_fields(),
            "method": "Noise is usually treat ed as an obstacle to relia ble quan tum com putation.",
            "why_it_works": "F or any r otation gate e - i P = 2 gener ate d by a Pauli op er ator P.",
            "key_result": "F or 2 [0 ; = 4] , this de c omp osition achieves the minimal p ossible ` 1 -norm.",
            "limitations": "conclusion holds for eac h ffixed set of rotation angles.",
        },
        evidence_used=["PDF text was extracted from the article."],
    )

    review = review_draft(config, [draft], use_llm=False)[0]

    assert review.verdict == "FAIL"
    assert any("PDF 抽取噪声" in issue for issue in review.issues)


def test_editor_rejects_section_header_residue_in_core_fields():
    config = load_config("/Users/wuzixie/Daily_Agent")
    draft = EditorialDraft(
        key="arxiv:2607.03283",
        item_type="paper",
        title="Embodied Operators and Benchmarking",
        draft_fields={
            **_good_fields(),
            "novelty_or_difference": (
                "相对已有工作，Introduction Embodied intelligence aims to enable physical "
                "or simulated robotic agents to perceive their environments."
            ),
        },
        evidence_used=["PDF text was extracted from the article."],
    )

    review = review_draft(config, [draft], use_llm=False)[0]

    assert review.verdict == "FAIL"
    assert any("章节标题残片" in issue for issue in review.issues)


def test_rule_writer_paraphrases_embodied_operator_novelty():
    config = load_config("/Users/wuzixie/Daily_Agent")
    record = MaterialRecord(
        key="arxiv:2607.03283",
        source="arxiv",
        item_type="paper",
        title="Embodied Operators and Benchmarking: Toward Reusable and Deployable Embodied Intelligence Systems",
        url="https://arxiv.org/abs/2607.03283v1",
        abstract=(
            "Embodied intelligence aims to enable physical or simulated robotic agents to perceive "
            "their environments, understand task contexts, and perform interactions through embodied actuation. "
            "We introduce embodied operators and benchmarking for reusable and deployable embodied intelligence systems."
        ),
        paper_text_excerpt=(
            "Introduction Embodied intelligence aims to enable physical or simulated robotic agents to perceive "
            "their environments. We introduce embodied operators, input-output contracts, and benchmarking."
        ),
        tags=["embodied_and_agents"],
        score=99.0,
    )

    draft = draft_report_items(config, [record], use_llm=False)[0]

    novelty = draft.draft_fields["novelty_or_difference"]
    assert "Introduction" not in novelty
    assert "operator" in novelty
    assert "多维 benchmark" in novelty


def test_rule_writer_does_not_apply_embodied_operator_novelty_to_quantum_mapping():
    config = load_config("/Users/wuzixie/Daily_Agent")
    record = MaterialRecord(
        key="arxiv:2607.02616",
        source="arxiv",
        item_type="paper",
        title="MLIR for Quantum Beyond Gate Cancellation: Quantum Circuit Mapping Reimagined",
        url="https://arxiv.org/abs/2607.02616v1",
        abstract=(
            "We study quantum circuit mapping with sparse routing, qubit placement, and SWAP insertion. "
            "Benchmarking shows improvements over QMAP and TKET in runtime performance and solution quality."
        ),
        paper_text_excerpt=(
            "Results show that by leveraging the MLIR ecosystem it is possible to achieve substantial "
            "improvements in both runtime performance and solution quality compared to QMAP and TKET. "
            "The approach is evaluated on available coupling graphs and hardware qubits."
        ),
        tags=["quantum_circuit", "quantum_compilation"],
        score=98.0,
    )

    draft = draft_report_items(config, [record], use_llm=False)[0]

    fields = draft.draft_fields
    assert "具身" not in fields["novelty_or_difference"]
    assert "VLA" not in fields["novelty_or_difference"]
    assert "MLIR" in fields["novelty_or_difference"]
    assert fields["key_result"].startswith("结果显示")
    assert fields["limitations"].startswith("局限")


def test_approval_respects_max_items_even_when_more_drafts_pass():
    config = load_config("/Users/wuzixie/Daily_Agent")
    config.quota["max_items"] = 10
    materials = [_paper_material(f"arxiv:2607.{index:05d}", score=100 - index) for index in range(12)]
    drafts = [
        EditorialDraft(
            key=material.key,
            item_type="paper",
            title=material.title,
            draft_fields=_good_fields(index),
            evidence_used=["Full text sections describe method, experiments, and limitations."],
        )
        for index, material in enumerate(materials)
    ]
    reviews = [EditorialReview(key=draft.key, verdict="PASS") for draft in drafts]

    approved = approve_publication(config, materials, drafts, reviews)

    assert len(approved) == 10
    assert [item.key for item in approved] == [material.key for material in materials[:10]]


def test_storage_writes_standalone_daily_reports_separate_from_weekly_archive(tmp_path):
    from daily_agent.storage import write_daily_html_report, write_daily_report, write_weekly_report

    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)

    daily_path = write_daily_report(config, date(2026, 7, 9), "# Daily Agent 日报｜2026-07-09\n\nToday\n")
    html_path = write_daily_html_report(config, date(2026, 7, 9), "<h1>Daily Agent 日报｜2026-07-09</h1>\n")
    weekly_path = write_weekly_report(config, date(2026, 7, 8), "# Daily Agent 日报｜2026-07-08\n\nYesterday\n")
    write_weekly_report(config, date(2026, 7, 9), "# Daily Agent 日报｜2026-07-09\n\nToday\n")

    assert daily_path.name == "daily-agent-2026-07-09.md"
    assert html_path.name == "daily-agent-2026-07-09.html"
    assert "2026-07-08" not in daily_path.read_text(encoding="utf-8")
    assert "2026-07-09" in daily_path.read_text(encoding="utf-8")
    assert "2026-07-08" in weekly_path.read_text(encoding="utf-8")
    assert "2026-07-09" in weekly_path.read_text(encoding="utf-8")


def test_markdown_feedback_hint_is_compact_for_daily_reading():
    material = _paper_material()
    approved = [
        __import__("daily_agent.models").models.ApprovedItem(
            key=material.key,
            item_type="paper",
            title=material.title,
            source=material.source,
            url=material.url,
            final_fields=_good_fields(),
            material=material,
        )
    ]

    markdown = render_daily_markdown(approved, date(2026, 7, 9), RunStatus())

    assert "飞书评论可写「第 1 条不错」或「第 1 条不相关」" in markdown
    assert "127.0.0.1:8765" not in markdown
    assert "daily-agent feedback add" not in markdown


def test_empty_source_status_is_explicitly_marked_as_unrecorded():
    markdown = render_daily_markdown([], date(2026, 7, 9), RunStatus())

    assert "未记录数据源状态" in markdown
    assert "无数据源状态" not in markdown
