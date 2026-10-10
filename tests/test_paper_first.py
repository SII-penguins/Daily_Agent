"""Synthetic paper-first workflow regressions; no real models or scientific proof.

These tests use native-text PDFs and deterministic *model-response fixtures* through
parent_writer.import_response. They establish transport, state, rendering and
fail-closed contracts, not whether an actual model reads or reviews correctly.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

import fitz
import pytest

from daily_agent import paper_first as workflow
from daily_agent import parent_writer
from daily_agent.workflow_state import StateCorrupt


ISSUE_DAY = "2026-10-10"
ANCHOR = "The measured result is 12 percent on the synthetic benchmark."
AUTHOR_ANCHOR = "Authors: Alice Example, Synthetic Research Laboratory."
APPENDIX_ANCHOR = "Appendix: this synthetic result does not establish real-world performance."


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _state(root):
    return _read(root / "state.json")


def _manifest(tmp_path, key="alpha", *, source_date="2026-10-01"):
    title = f"Synthetic {key.title()} Research Paper"
    pdf_path = tmp_path / f"{key}.pdf"
    with fitz.open() as document:
        first = document.new_page(width=600, height=800)
        lines = [title, AUTHOR_ANCHOR, ANCHOR]
        lines += [f"Evidence {i}: The method compares controlled trials with a fixed baseline."
                  for i in range(27)]
        lines += ["The limitations require cautious interpretation and independent replication."]
        for index, line in enumerate(lines):
            first.insert_text((35, 35 + 20 * index), line, fontsize=10)
        second = document.new_page(width=600, height=800)
        second.insert_text((35, 35), APPENDIX_ANCHOR, fontsize=10)
        second.insert_text((85, 150), "Figure 1(d): Synthetic comparison", fontsize=14)
        second.draw_rect(fitz.Rect(100, 190, 510, 540), color=(0, 0, 0), width=2)
        second.draw_rect(fitz.Rect(150, 380, 250, 540), color=(0, 0, 1), fill=(0.2, 0.4, 0.8))
        second.draw_rect(fitz.Rect(330, 310, 430, 540), color=(0, 0, 1), fill=(0.2, 0.4, 0.8))
        second.insert_text((110, 180), "Score (percent); n = 10 trials", fontsize=12)
        second.insert_text((150, 565), "Baseline               Method", fontsize=12)
        second.insert_text((100, 590), "Legend: blue bars are fixture observations", fontsize=11)
        document.save(pdf_path)
    return {
        "key": key, "title": title, "version": "v1", "source_date": source_date,
        "url": f"https://example.org/papers/{key}",
        "pdf_url": f"https://example.org/papers/{key}.pdf", "pdf_path": str(pdf_path),
    }


def _issue(tmp_path, keys=("alpha",), *, target=None):
    root = tmp_path / "fresh-issue"
    papers = [_manifest(tmp_path, key) for key in keys]
    workflow.create_issue(root, ISSUE_DAY, papers, diagnostic=True, target=target)
    return root


def _draft(*, label="Figure 1(d)", figures=True):
    paragraphs = (
        "研究关注受控合成基准上的比较问题，结论只适用于论文报告的固定设置与基线。",
        "方法在固定基线与相同受控条件下比较试验结果，具体机制需结合原文上下文理解。",
        "编者从受控比较中得到的启发是先固定评价条件，再分析方法差异，仍须独立验证。",
        "论文报告合成基准上的测量结果为百分之十二，该结果限于文中指定的受控试验。",
        "附录明确提示，合成基准上的结果不能证明真实环境中的表现，推广结论尚需验证。",
        "值得借鉴的是保留固定基线及条件说明的比较方法，迁移到其他任务前应重新验证。",
    )
    claims = []
    for index, (section, paragraph) in enumerate(zip(workflow.SECTIONS, paragraphs), 1):
        claims.append({
            "id": f"c{index}", "section": section, "text": paragraph,
            "basis": "editor_inference" if section in {"insight", "reuse"} else "author_claim",
            "anchors": [{"page": 2, "quote": APPENDIX_ANCHOR}] if section == "limits"
                       else [{"page": 1, "quote": ANCHOR}],
            "conditions": "Synthetic benchmark; fixed baseline; fixture-only evidence",
        })
    return {
        "read_all_pages": True, "claims": claims,
        "figures": [{"id": "f1", "role": "result", "label": label, "page": 2,
                     "bbox": [0.12, 0.16, 0.90, 0.76],
                     "caption": "图示仅呈现受控合成试验的比较，保留坐标、单位、图例及条件，不代表真实环境表现。",
                     "claim_ids": ["c4"]}] if figures else [],
        "no_figure_reason": "The fixture variant has no useful visual." if not figures else "",
        "author_context": {"text": "作者为 Alice Example，机构为 Synthetic Research Laboratory；团队历史与通讯身份未知。",
                           "anchors": [{"page": 1, "quote": AUTHOR_ANCHOR}]},
    }


def _review(draft=None, *, verdict="PASS", problem=None):
    draft = draft or _draft()
    return {
        "verdict": verdict, "full_source_checked": True,
        "claims": [{"id": row["id"], "supported": True, "numbers_and_conditions_checked": True,
                    "basis_correct": True, "reason": "Deterministic source-check fixture, not a live model review."}
                   for row in draft["claims"]],
        "figures": [{"id": row["id"], "pixels_inspected": True,
                     "label_matches": verdict == "PASS", "crop_complete": True,
                     "caption_supported": True, "reason": "Fixture verdict for the synthetic original region."}
                    for row in draft["figures"]],
        "author_context_supported": True, "figure_selection_appropriate": True,
        "problems": [] if verdict == "PASS" else [problem or "Correct the subpanel label to Figure 1(d)."],
    }


def _answer(root, key, role, response, *, round_=0, worker=None):
    record = _state(root)["papers"][key]
    job_id = record["calls"][f"{role}:{round_}"]
    worker = worker or f"{'reviewer' if role == 'review' else 'reader'}-{key}-{round_}"
    return parent_writer.import_response(root, job_id, response, worker)


def _qualify(root, key="alpha", draft=None):
    draft = draft or _draft()
    workflow.advance(root)
    _answer(root, key, "draft", draft)
    workflow.advance(root)
    _answer(root, key, "review", _review(draft))
    result = workflow.advance(root)
    assert result["papers"][key]["status"] == "qualified"
    return result


def _job_files(root):
    return sorted((root / "data" / "writer-queue").glob("*.job.json"))


def test_two_job_complete_paper_produces_qualified_original_pixel_html(tmp_path):
    root = _issue(tmp_path)
    result = _qualify(root)
    assert (result["qualified"], result["pending"], result["model_jobs"]) == (1, 0, 2)
    jobs = [_read(path) for path in _job_files(root)]
    assert sorted(job["stage"] for job in jobs) == ["draft", "review"]
    reader = next(job for job in jobs if job["stage"] == "draft")
    review = next(job for job in jobs if job["stage"] == "review")
    assert ANCHOR in reader["prompt"] and APPENDIX_ANCHOR in reader["prompt"]
    assert APPENDIX_ANCHOR in review["prompt"]
    assert len(review["images"]) == 2  # Full source page plus the actual crop.
    for image in review["images"]:
        content = (root / image["path"]).read_bytes()
        assert content.startswith(b"\x89PNG")
        assert hashlib.sha256(content).hexdigest() == image["sha256"]
    html = workflow.render(root, seal=True).read_text(encoding="utf-8")
    for label in workflow.LABELS.values():
        assert label in html
    assert "data:image/png;base64," in html and "Figure 1(d)" in html
    assert "正式发表状态未独立确认" in html
    assert "编者推断：" in html and "#page=2" in html
    assert "已独立核验 1 篇论文" in html


def test_repeat_resume_and_render_emit_zero_new_jobs_and_never_reopen_seal(tmp_path):
    root = _issue(tmp_path, ("alpha", "beta"), target=5)
    _qualify(root, "alpha")
    preview = workflow.render(root).read_bytes()
    before_jobs = {path.name: path.read_bytes() for path in _job_files(root)}
    for _ in range(3):
        workflow.advance(root)
        assert workflow.render(root).read_bytes() == preview
    assert {path.name: path.read_bytes() for path in _job_files(root)} == before_jobs
    report = workflow.render(root, seal=True)
    sealed_bytes = report.read_bytes()
    sealed_state = (root / "state.json").read_bytes()
    for _ in range(3):
        workflow.advance(root)
        assert workflow.render(root) == report
        assert workflow.render(root, seal=True) == report
    assert report.read_bytes() == sealed_bytes
    assert (root / "state.json").read_bytes() == sealed_state
    assert {path.name: path.read_bytes() for path in _job_files(root)} == before_jobs
    with pytest.raises(ValueError, match="new root|reopening"):
        workflow.create_issue(root, ISSUE_DAY, [_manifest(tmp_path)], diagnostic=True)
    report.write_bytes(sealed_bytes + b"tampered")
    with pytest.raises(StateCorrupt, match="Sealed HTML changed"):
        workflow.render(root)


def test_pending_slow_paper_does_not_block_another_paper_or_partial_preview(tmp_path):
    root = _issue(tmp_path, ("alpha", "beta"))
    workflow.advance(root)
    initial = _state(root)
    assert initial["papers"]["alpha"]["calls"]["draft:0"]
    assert initial["papers"]["beta"]["calls"]["draft:0"]
    _qualify(root, "beta")
    status = workflow.status(root)
    assert (status["qualified"], status["pending"], status["model_jobs"]) == (1, 1, 3)
    assert status["papers"]["alpha"]["status"] == "ready"
    html = workflow.render(root).read_text(encoding="utf-8")
    assert "Synthetic Beta Research Paper" in html
    assert "Synthetic Alpha Research Paper" not in html
    assert "1 篇待完成" in html


@pytest.mark.parametrize("last_verdict", ["PASS", "REPAIR"])
def test_one_targeted_repair_round_is_bounded_at_four_jobs(tmp_path, last_verdict):
    root = _issue(tmp_path, ("alpha", "beta"))
    _qualify(root, "beta")
    bad_label_draft = _draft(label="Figure 1")
    _answer(root, "alpha", "draft", bad_label_draft)
    workflow.advance(root)
    _answer(root, "alpha", "review", _review(bad_label_draft, verdict="REPAIR"))
    status = workflow.advance(root)
    assert status["papers"]["alpha"]["round"] == 1
    assert status["papers"]["beta"]["status"] == "qualified"
    workflow.advance(root)
    repair_id = _state(root)["papers"]["alpha"]["calls"]["draft:1"]
    repair_job = _read(root / "data/writer-queue" / f"{repair_id}.job.json")
    assert "REPAIR:" in repair_job["prompt"] and "Previous draft:" in repair_job["prompt"]
    assert "Figure 1(d)" in repair_job["prompt"]
    corrected = _draft()
    _answer(root, "alpha", "draft", corrected, round_=1)
    workflow.advance(root)
    _answer(root, "alpha", "review", _review(corrected, verdict=last_verdict), round_=1)
    result = workflow.advance(root)
    expected = "qualified" if last_verdict == "PASS" else "rejected"
    assert result["papers"]["alpha"]["status"] == expected
    assert len(_state(root)["papers"]["alpha"]["calls"]) == 4
    assert result["model_jobs"] == 6
    for _ in range(3):
        assert workflow.advance(root)["model_jobs"] == 6
    html = workflow.render(root, seal=True).read_text(encoding="utf-8")
    assert "Synthetic Beta Research Paper" in html
    assert ("Synthetic Alpha Research Paper" in html) == (last_verdict == "PASS")
    if last_verdict != "PASS":
        assert "1 篇未通过" in html


def test_bad_exact_anchor_is_rejected_without_losing_other_paper(tmp_path):
    root = _issue(tmp_path, ("alpha", "beta"))
    _qualify(root, "beta")
    bad = _draft()
    bad["claims"][0]["anchors"] = [{"page": 1, "quote": "This assertion is absent from the source."}]
    for round_ in (0, 1):
        workflow.advance(root)
        _answer(root, "alpha", "draft", bad, round_=round_)
        workflow.advance(root)
    result = workflow.status(root)
    assert result["papers"]["alpha"]["status"] == "rejected"
    assert result["papers"]["beta"]["status"] == "qualified"
    assert len(_state(root)["papers"]["alpha"]["calls"]) == 2
    assert all("review" not in call for call in _state(root)["papers"]["alpha"]["calls"])
    html = workflow.render(root, seal=True).read_text(encoding="utf-8")
    assert "Synthetic Beta Research Paper" in html and "Synthetic Alpha Research Paper" not in html


def test_bad_label_rejected_by_supplied_independent_review_keeps_good_paper(tmp_path):
    root = _issue(tmp_path, ("alpha", "beta"))
    _qualify(root, "beta")
    bad = _draft(label="Figure 9(z)")
    _answer(root, "alpha", "draft", bad)
    workflow.advance(root)
    _answer(root, "alpha", "review", _review(bad, verdict="REJECT", problem="Figure 9(z) is absent; source panel is 1(d)."))
    result = workflow.advance(root)
    assert (result["qualified"], result["rejected"], result["model_jobs"]) == (1, 1, 4)
    html = workflow.render(root, seal=True).read_text(encoding="utf-8")
    assert "Figure 9(z)" not in html and "Synthetic Beta Research Paper" in html


@pytest.mark.parametrize("target", ["pdf", "document", "draft", "crop", "page_image", "review", "receipt", "queue_answer"])
def test_evidence_byte_tamper_blocks_seal(tmp_path, target):
    root = _issue(tmp_path)
    _qualify(root)
    state = _state(root)
    meta, record = state["inputs"][0], state["papers"]["alpha"]
    if target in {"pdf", "document"}:
        path = root / meta[target]
    elif target in {"crop", "page_image"}:
        path = root / record["assets"][0][target]
    elif target == "queue_answer":
        path = root / "data/writer-queue" / f'{record["calls"]["review:0"]}.answer.json'
        value = _read(path)
        value["worker_id"] = "tampered-reviewer"
        _write(path, value)
    else:
        path = root / record[target]
    if target != "queue_answer":
        path.write_bytes(path.read_bytes() + b"tampered")
    with pytest.raises(StateCorrupt):
        workflow.render(root, seal=True)
    assert _state(root)["sealed"] is None
    assert not (root / "report.html").exists()


@pytest.mark.parametrize("field", ["job_id", "input_sha256"])
def test_archived_queue_answer_identity_tamper_blocks_seal(tmp_path, field):
    root = _issue(tmp_path)
    _qualify(root)
    record = _state(root)["papers"]["alpha"]
    path = root / "data/writer-queue" / f'{record["calls"]["review:0"]}.answer.json'
    value = _read(path)
    value[field] = "0" * 64
    _write(path, value)
    with pytest.raises(StateCorrupt):
        workflow.render(root, seal=True)
    assert _state(root)["sealed"] is None


def test_archived_queue_prompt_tamper_blocks_seal(tmp_path):
    root = _issue(tmp_path)
    _qualify(root)
    record = _state(root)["papers"]["alpha"]
    path = root / "data/writer-queue" / f'{record["calls"]["review:0"]}.job.json'
    value = _read(path)
    value["prompt"] = "Unrelated review; the original evidence was never supplied."
    _write(path, value)
    with pytest.raises(StateCorrupt):
        workflow.render(root, seal=True)
    assert _state(root)["sealed"] is None


def test_actual_transport_requires_distinct_review_worker(tmp_path):
    root = _issue(tmp_path)
    workflow.advance(root)
    _answer(root, "alpha", "draft", _draft(), worker="same-worker")
    workflow.advance(root)
    with pytest.raises(ValueError, match="different worker"):
        _answer(root, "alpha", "review", _review(), worker="same-worker")
    assert workflow.status(root)["qualified"] == 0
    _answer(root, "alpha", "review", _review(), worker="independent-worker")
    assert workflow.advance(root)["qualified"] == 1


def test_new_root_guard_does_not_import_or_overwrite_old_cached_outputs(tmp_path):
    root = tmp_path / "legacy-root"
    root.mkdir()
    legacy = {"cached_reading.json": b'{"approved":true}', "report.html": b"old sealed report"}
    for name, content in legacy.items():
        (root / name).write_bytes(content)
    with pytest.raises(ValueError, match="empty new root"):
        workflow.create_issue(root, ISSUE_DAY, [_manifest(tmp_path)], diagnostic=True)
    assert {path.name: path.read_bytes() for path in root.iterdir()} == legacy
    fresh = tmp_path / "new-root"
    workflow.create_issue(fresh, ISSUE_DAY, [_manifest(tmp_path)], diagnostic=True)
    assert workflow.status(fresh)["qualified"] == 0
    result = workflow.advance(fresh)
    assert result["model_jobs"] == 1 and result["qualified"] == 0
    assert len(parent_writer.pending(fresh)) == 1


@pytest.mark.parametrize("source_date,allowed", [("2026-07-10", True), ("2026-07-09", False),
                                                  ("2026-10-10", True), ("2026-10-11", False)])
def test_exact_three_calendar_month_window(tmp_path, source_date, allowed):
    root = tmp_path / "fresh"
    manifest = [_manifest(tmp_path, source_date=source_date)]
    if allowed:
        workflow.create_issue(root, ISSUE_DAY, manifest, diagnostic=True)
        assert workflow.status(root)["pending"] == 1
    else:
        with pytest.raises(ValueError, match="three calendar months"):
            workflow.create_issue(root, ISSUE_DAY, manifest, diagnostic=True)
        assert not root.exists()


def test_calendar_month_end_clamping_is_not_a_ninety_day_window(tmp_path):
    root = tmp_path / "fresh"
    manifest = [_manifest(tmp_path, source_date="2026-02-28")]
    workflow.create_issue(root, "2026-05-31", manifest, diagnostic=True)
    assert workflow.status(root)["pending"] == 1


def test_past_date_requires_diagnostic_and_diagnostic_output_is_marked(tmp_path):
    day = datetime.now(timezone.utc).date() - timedelta(days=1)
    manifest = [_manifest(tmp_path, source_date=str(day))]
    root = tmp_path / "fresh"
    with pytest.raises(ValueError, match="diagnostic"):
        workflow.create_issue(root, day, manifest)
    assert not root.exists()
    workflow.create_issue(root, day, manifest, diagnostic=True)
    _qualify(root)
    assert "隔离验证" in workflow.render(root).read_text(encoding="utf-8")


def test_zero_qualified_cannot_seal_and_no_useful_figure_has_explicit_reason(tmp_path):
    root = _issue(tmp_path)
    with pytest.raises(ValueError, match="zero-qualified"):
        workflow.render(root, seal=True)
    assert _state(root)["sealed"] is None
    _qualify(root, draft=_draft(figures=False))
    html = workflow.render(root, seal=True).read_text(encoding="utf-8")
    assert "<article" in html and "<figure>" not in html


@pytest.mark.parametrize("phase", ["pending_draft", "pending_review", "qualified"])
def test_moved_issue_resumes_exact_jobs_without_cold_cache_rerun(tmp_path, phase):
    root = _issue(tmp_path)
    workflow.advance(root)
    if phase in {"pending_review", "qualified"}:
        _answer(root, "alpha", "draft", _draft())
        workflow.advance(root)
    if phase == "qualified":
        _answer(root, "alpha", "review", _review())
        workflow.advance(root)
    before_jobs = {path.name: path.read_bytes() for path in _job_files(root)}
    before_count = workflow.status(root)["model_jobs"]
    restored = tmp_path / "restored-in-a-different-directory"
    root.rename(restored)
    for _ in range(3):
        assert workflow.advance(restored)["model_jobs"] == before_count
    assert {path.name: path.read_bytes() for path in _job_files(restored)} == before_jobs
    if phase == "pending_draft":
        _answer(restored, "alpha", "draft", _draft())
        workflow.advance(restored)
    if phase != "qualified":
        _answer(restored, "alpha", "review", _review())
        workflow.advance(restored)
    assert workflow.status(restored)["model_jobs"] == 2
    assert "Synthetic Alpha Research Paper" in workflow.render(restored, seal=True).read_text()


@pytest.mark.parametrize("target", ["issue_date", "diagnostic", "target", "implementation_sha256"])
def test_enrollment_metadata_tamper_is_rejected(tmp_path, target):
    root = _issue(tmp_path)
    state = _state(root)
    state[target] = {"issue_date": "2026-10-11", "diagnostic": False,
                     "target": 999, "implementation_sha256": "0" * 64}[target]
    _write(root / "state.json", state)
    with pytest.raises(StateCorrupt):
        workflow.advance(root)
    assert not _job_files(root)


@pytest.mark.parametrize("bad_record", [{"round": 2}, {"calls": {"draft:2": "0" * 64}},
                                         {"status": "published"}])
def test_tampered_operation_budget_is_rejected(tmp_path, bad_record):
    root = _issue(tmp_path)
    state = _state(root)
    state["papers"]["alpha"].update(bad_record)
    _write(root / "state.json", state)
    with pytest.raises(StateCorrupt):
        workflow.advance(root)
    assert not _job_files(root)


def test_duplicate_pdf_cannot_count_as_two_distinct_papers(tmp_path):
    first = _manifest(tmp_path)
    duplicate = {**first, "key": "different-key"}
    root = tmp_path / "fresh"
    with pytest.raises(ValueError, match="Duplicate PDF"):
        workflow.create_issue(root, ISSUE_DAY, [first, duplicate], diagnostic=True)
    assert not root.exists()


@pytest.mark.parametrize("broken_field", ["claim", "anchor", "figure"])
def test_malformed_response_rows_do_not_stop_another_paper(tmp_path, broken_field):
    root = _issue(tmp_path, ("alpha", "beta"))
    workflow.advance(root)
    bad = _draft()
    if broken_field == "claim":
        bad["claims"][0] = None
    elif broken_field == "anchor":
        bad["claims"][0]["anchors"][0] = None
    else:
        bad["figures"][0] = None
    _answer(root, "alpha", "draft", bad)
    _answer(root, "beta", "draft", _draft())
    result = workflow.advance(root)
    assert result["papers"]["alpha"]["round"] == 1
    assert result["papers"]["beta"]["status"] == "reviewing"
    assert _state(root)["papers"]["beta"]["calls"]["review:0"]


def test_nonstr_label_cannot_qualify_and_break_final_html(tmp_path):
    root = _issue(tmp_path)
    workflow.advance(root)
    bad = _draft()
    bad["figures"][0]["label"] = 123
    _answer(root, "alpha", "draft", bad)
    result = workflow.advance(root)
    assert result["papers"]["alpha"]["round"] == 1
    assert result["papers"]["alpha"]["status"] == "ready"
    assert "review:0" not in _state(root)["papers"]["alpha"]["calls"]


@pytest.mark.parametrize("broken_field", ["claims", "figures"])
def test_malformed_review_rows_do_not_block_another_qualified_paper(tmp_path, broken_field):
    root = _issue(tmp_path, ("alpha", "beta"))
    workflow.advance(root)
    for key in ("alpha", "beta"):
        _answer(root, key, "draft", _draft())
    workflow.advance(root)
    bad = _review()
    bad[broken_field][0] = None
    _answer(root, "alpha", "review", bad)
    _answer(root, "beta", "review", _review())
    result = workflow.advance(root)
    assert result["papers"]["alpha"]["round"] == 1
    assert result["papers"]["beta"]["status"] == "qualified"


@pytest.mark.parametrize("violation", ["missing_claim", "duplicate_claim", "uninspected_pixels", "unchecked_numbers", "wrong_basis"])
def test_pass_label_alone_never_qualifies_incomplete_or_contradictory_review(tmp_path, violation):
    root = _issue(tmp_path)
    workflow.advance(root)
    _answer(root, "alpha", "draft", _draft())
    workflow.advance(root)
    bad = _review()
    if violation == "missing_claim":
        bad["claims"].pop()
    elif violation == "duplicate_claim":
        bad["claims"][-1] = dict(bad["claims"][0])
    elif violation == "uninspected_pixels":
        bad["figures"][0]["pixels_inspected"] = False
    elif violation == "unchecked_numbers":
        bad["claims"][0]["numbers_and_conditions_checked"] = False
    else:
        bad["claims"][0]["basis_correct"] = False
    _answer(root, "alpha", "review", bad)
    result = workflow.advance(root)
    assert result["qualified"] == 0
    assert result["papers"]["alpha"]["round"] == 1
    with pytest.raises(ValueError, match="zero-qualified"):
        workflow.render(root, seal=True)


def test_original_source_pdf_mutation_after_enrollment_does_not_change_preserved_source(tmp_path):
    manifest = _manifest(tmp_path)
    root = tmp_path / "fresh"
    workflow.create_issue(root, ISSUE_DAY, [manifest], diagnostic=True)
    original_hash = hashlib.sha256(Path(manifest["pdf_path"]).read_bytes()).hexdigest()
    Path(manifest["pdf_path"]).write_bytes(b"outside original is now unavailable")
    state = _state(root)
    assert hashlib.sha256((root / state["inputs"][0]["pdf"]).read_bytes()).hexdigest() == original_hash
    _qualify(root)
    assert workflow.render(root, seal=True).exists()


def test_render_preserves_reviewed_numeric_conditions_not_just_paragraph_text(tmp_path):
    root = _issue(tmp_path)
    draft = _draft()
    condition = "Synthetic benchmark; n = 10 trials; baseline X; percent, not percentage points"
    draft["claims"][3]["conditions"] = condition
    _qualify(root, draft=draft)
    html = workflow.render(root, seal=True).read_text(encoding="utf-8")
    assert condition in html


def test_subpixel_crop_is_a_local_repair_not_a_batch_crash(tmp_path):
    root = _issue(tmp_path, ("alpha", "beta"))
    workflow.advance(root)
    bad = _draft()
    bad["figures"][0]["bbox"] = [0, 0, 1e-8, 0.5]
    _answer(root, "alpha", "draft", bad)
    _answer(root, "beta", "draft", _draft())
    result = workflow.advance(root)
    assert result["papers"]["alpha"]["round"] == 1
    assert result["papers"]["beta"]["status"] == "reviewing"
    assert _state(root)["papers"]["beta"]["calls"]["review:0"]


@pytest.mark.parametrize("publication_supported", [False, True])
def test_formal_venue_requires_snapshot_and_explicit_independent_review(tmp_path, publication_supported):
    manifest = _manifest(tmp_path)
    snapshot = tmp_path / "primary-venue.html"
    quote = "Synthetic Alpha Research Paper was published in Fixture Proceedings 2026."
    snapshot.write_text(f"<html><p>{quote}</p></html>")
    manifest["publication"] = {"venue": "Fixture Proceedings", "url": "https://example.org/proceedings/alpha",
                               "evidence_path": str(snapshot), "quote": quote}
    root = tmp_path / "fresh"
    workflow.create_issue(root, ISSUE_DAY, [manifest], diagnostic=True)
    workflow.advance(root)
    _answer(root, "alpha", "draft", _draft())
    workflow.advance(root)
    record = _state(root)["papers"]["alpha"]
    job = _read(root / "data/writer-queue" / f'{record["calls"]["review:0"]}.job.json')
    assert snapshot.read_text() in job["prompt"]
    review = _review()
    review["publication_supported"] = publication_supported
    _answer(root, "alpha", "review", review)
    result = workflow.advance(root)
    assert result["qualified"] == int(publication_supported)
    if publication_supported:
        assert "Fixture Proceedings" in workflow.render(root, seal=True).read_text()
    else:
        with pytest.raises(ValueError, match="zero-qualified"):
            workflow.render(root, seal=True)
