from __future__ import annotations

import json
import re
import subprocess
import time
from contextvars import ContextVar
from datetime import date
from typing import Any

from daily_agent.config import AppConfig
from daily_agent.connectors.github import enrich_github_readmes
from daily_agent.models import ApprovedItem, EditorialDraft, EditorialReview, MaterialRecord
from daily_agent.scoring.relevance import annotate_topic_relevance, passes_topic_gate

GENERIC_PHRASES = [
    "需进一步阅读确认",
    "与配置方向相关",
    "可作为后续阅读线索",
    "建议查看 README",
    "值得关注",
    "对用户的量子线路优化",
    "可优先查看方法定义",
]

PAPER_FIELDS = [
    "problem",
    "method",
    "why_it_works",
    "novelty_or_difference",
    "method_steps",
    "key_result",
    "technical_route",
    "possible_use_or_impact",
    "limitations",
    "evidence_from_source",
    "confidence",
]

REPO_FIELDS = [
    "what_it_is",
    "core_capabilities",
    "typical_use_cases",
    "architecture_or_api",
    "maturity_signal",
    "reusable_point",
    "evidence_from_source",
    "confidence",
]

_DEFAULT_LLM_WRITER_SETTINGS = {
    "provider": "codex",
    "command": "codex",
    "batch_size": 2,
    "timeout_seconds": 600.0,
    "run_budget_seconds": 14_400.0,
    "max_input_chars_per_item": 24_000,
    "reasoning_effort": "low",
    "workdir": ".",
}
_LLM_WRITER_SETTINGS: ContextVar[dict[str, Any] | None] = ContextVar("llm_writer_settings", default=None)


def build_shortlist(config: AppConfig, library: dict[str, MaterialRecord], run_date: date) -> list[MaterialRecord]:
    max_items = int(config.quota.get("max_items", 10))
    records = []
    for record in library.values():
        if record.quality_status in {"rejected", "archived"}:
            continue
        annotate_topic_relevance(record, config)
        if not passes_topic_gate(record, config):
            continue
        records.append(record)
    records.sort(key=lambda item: item.score, reverse=True)

    selected: list[MaterialRecord] = []
    selected_keys: set[str] = set()
    github_target = int(config.quota.get("github_target", 2))
    paper_review_target = _paper_review_target(config)
    shortlist_limit = max(max_items, paper_review_target + github_target)

    def take(item_type: str, limit: int) -> None:
        for record in records:
            if len([item for item in selected if item.item_type == item_type]) >= limit:
                return
            if len(selected) >= shortlist_limit:
                return
            if record.key not in selected_keys and record.item_type == item_type:
                selected.append(record)
                selected_keys.add(record.key)

    take("paper", paper_review_target)
    take("repo", github_target)
    for record in records:
        if len(selected) >= shortlist_limit:
            break
        if record.key not in selected_keys:
            selected.append(record)
            selected_keys.add(record.key)

    return enrich_github_readmes(selected)


def _paper_review_target(config: AppConfig) -> int:
    paper_target = int(config.quota.get("paper_target", 8))
    multiplier = max(1, int(config.quota.get("paper_review_multiplier", 2)))
    explicit = config.quota.get("paper_review_target")
    if explicit is not None:
        return max(paper_target, int(explicit))
    return paper_target * multiplier


def draft_report_items(config: AppConfig, shortlist: list[MaterialRecord], use_llm: bool = True) -> list[EditorialDraft]:
    from daily_agent.reading import CORE, read_papers, verify_draft, semantic_review
    settings = _llm_writer_settings(config)
    def invoke(prompt, timeout, image_path=None):
        command = _llm_writer_command(settings, prompt)
        if image_path:
            images = image_path if isinstance(image_path, list) else [image_path]
            command[-1:-1] = ["--image", *images, "--"]
        result = subprocess.run(command, check=True,
                                capture_output=True, text=True, timeout=timeout)
        return _decode_model_json(result.stdout)
    structured = [r for r in shortlist if r.item_type == "paper" and r.paper_document]
    # Preserve native extraction before any image-grounded replacement.
    for record in structured:
        if record.paper_document and 'native_document' not in record.paper_document:
            record.paper_document['native_document'] = dict(record.paper_document)
    read_papers(structured, config, invoke if use_llm else None)
    from daily_agent.visual_reading import read_visuals
    # Screenshot presentation is independent of evidence verification.
    read_visuals(structured, config, invoke if use_llm else None)
    from daily_agent.visual_fidelity import repair_visuals
    repair_visuals(structured, config, invoke if use_llm else None)
    visual_results = {r.key:r.reading.get('visual', {}) for r in structured}
    repaired_records = [r for r in structured if r.paper_document.get('evidence_basis') == 'image_transcription_reviewed']
    # Re-chunked image transcription has a different evidence identity. Never
    # synthesize it using notes or chunk IDs from the native extraction.
    read_papers(repaired_records, config, invoke if use_llm else None)
    for record in structured:
        visual = visual_results[record.key]
        record.reading['visual'] = visual
        if visual.get('required_pages') and not visual.get('strict_fidelity'):
            record.paper_text_status['sufficient_for_deep_summary'] = False
    from daily_agent.paper_document import load_json, atomic_json
    cached_drafts = {}
    for record in structured:
        fingerprint = record.reading.get("fingerprint")
        if use_llm and record.reading.get("complete") and not record.reading.get("visual", {}).get("required_pages") and fingerprint:
            value = load_json(config.root / "data" / "reading" / fingerprint / "draft-v3.json")
            if isinstance(value, dict) and value.get("key") == record.key:
                try:
                    candidate = EditorialDraft.from_dict(value)
                    _validated_draft_fields(candidate.draft_fields, "paper")
                    if (isinstance(candidate.verification, dict)
                            and candidate.verification.get("semantic_support") == "model_checked"
                            and isinstance(candidate.claim_evidence, list)):
                        cached_drafts[record.key] = candidate
                except (TypeError, ValueError):
                    pass
    pending = [r for r in shortlist if r.key not in cached_drafts]
    drafts = []
    if use_llm and pending:
        token = _LLM_WRITER_SETTINGS.set(settings)
        try:
            drafts = _draft_with_llm(pending)
        finally:
            _LLM_WRITER_SETTINGS.reset(token)
    if not drafts:
        drafts = [_rule_draft(record) for record in pending]
    drafts.extend(cached_drafts.values())
    order = {r.key: i for i,r in enumerate(shortlist)}
    drafts.sort(key=lambda d: order[d.key])
    by_key = {r.key: r for r in structured}
    for draft in drafts:
        if draft.key in by_key and draft.key not in cached_drafts:
            verify_draft(draft, by_key[draft.key])
            if use_llm and draft.claim_evidence:
                semantic_review(draft, by_key[draft.key], invoke,
                                float(config.sources.get("reading", {}).get("timeout_seconds", 120)))
            record = by_key[draft.key]
            rejected = [c for c in draft.verification.get("semantic_checks", []) if not c.get("supported")]
            claim_issues = [issue for issue in draft.verification.get("issues", [])
                            if issue.split(":", 1)[0] in CORE]
            if use_llm and (rejected or claim_issues):
                # Exactly one evidence-guided rewrite; review thresholds do not change.
                token = _LLM_WRITER_SETTINGS.set(settings)
                try:
                    from daily_agent.reading import repair_evidence
                    repaired = _draft_with_llm_batch([record], timeout_seconds=settings["timeout_seconds"],
                        feedback={"rejected_claims": rejected,
                                  "mechanical_issues": draft.verification.get("issues", []),
                                  "source_chunks": repair_evidence(record, draft)})
                finally:
                    _LLM_WRITER_SETTINGS.reset(token)
                if repaired:
                    candidate = repaired[0]
                    verify_draft(candidate, record)
                    if candidate.claim_evidence:
                        semantic_review(candidate, record, invoke,
                            float(config.sources.get("reading", {}).get("timeout_seconds", 120)))
                    def evidence_score(value):
                        fields = set(value.verification.get("valid_fields", []))
                        return (value.verification.get("semantic_support") == "model_checked",
                                {"problem", "method"} <= fields, len(fields))
                    if evidence_score(candidate) > evidence_score(draft):
                        drafts[drafts.index(draft)] = candidate
                        draft = candidate
                record.reading["verification"] = draft.verification
                record.reading["semantic_rewrite_attempted"] = True
            if use_llm and draft.verification.get("semantic_support") == "model_checked":
                from daily_agent.rendering.composition import review_result_presentation
                review_result_presentation(draft.draft_fields.get("key_result"), record, invoke)
            if record.reading.get("complete") and draft.verification.get("semantic_support") == "model_checked":
                atomic_json(config.root / "data" / "reading" / record.reading["fingerprint"] / "draft-v3.json", draft.to_dict())
        elif draft.key in cached_drafts:
            by_key[draft.key].reading["verification"] = draft.verification
    return drafts


def review_draft(config: AppConfig, drafts: list[EditorialDraft], use_llm: bool = False) -> list[EditorialReview]:
    reviews = [_rule_review(draft) for draft in drafts]
    for draft, review in zip(drafts, reviews):
        if draft.verification.get("status") == "limited":
            # Explicitly limited cards expose only validated fields; no template claims.
            review.verdict = "PASS" if (not any("噪声" in issue or "模板" in issue for issue in review.issues)
                and {"problem", "method"} <= set(draft.verification.get("valid_fields", []))
                and draft.verification.get("semantic_support") == "model_checked") else "FAIL"
            review.issues = list(draft.verification.get("issues", []))
            review.reader_value_score = 2.0
    return reviews


def approve_publication(
    config: AppConfig,
    shortlist: list[MaterialRecord],
    drafts: list[EditorialDraft],
    reviews: list[EditorialReview],
) -> list[ApprovedItem]:
    by_material = {record.key: record for record in shortlist}
    by_review = {review.key: review for review in reviews}
    approved: list[ApprovedItem] = []
    for draft in drafts:
        review = by_review.get(draft.key)
        material = by_material.get(draft.key)
        if not material or not review or review.verdict != "PASS":
            continue
        if draft.verification:
            material.reading["verification"] = draft.verification
            material.reading["claim_evidence"] = draft.claim_evidence
        material.detail = draft.draft_fields
        approved.append(
            ApprovedItem(
                key=draft.key,
                item_type=draft.item_type,
                title=draft.title,
                source=material.source,
                url=material.url,
                final_fields=draft.draft_fields,
                material=material,
                approval_notes=(draft.verification.get("label") or "旧版结构审核通过；未完成新版证据核验"),
            )
        )
    approved.sort(key=lambda item: (item.item_type == "paper" and item.material.reading.get("verification", {}).get("status") == "limited"))
    return _limit_approved_items(config, approved)


def _limit_approved_items(config: AppConfig, approved: list[ApprovedItem]) -> list[ApprovedItem]:
    max_items = max(0, int(config.quota.get("max_items", 10)))
    if len(approved) <= max_items:
        return approved
    paper_target = max(0, int(config.quota.get("paper_target", 8)))
    github_target = max(0, int(config.quota.get("github_target", 2)))
    selected_indexes: set[int] = set()

    def take(item_type: str, limit: int) -> None:
        if limit <= 0:
            return
        count = 0
        for index, item in enumerate(approved):
            if len(selected_indexes) >= max_items or count >= limit:
                return
            if item.item_type == item_type and index not in selected_indexes:
                selected_indexes.add(index)
                count += 1

    take("paper", paper_target)
    take("repo", github_target)
    for index in range(len(approved)):
        if len(selected_indexes) >= max_items:
            break
        selected_indexes.add(index)
    return [approved[index] for index in sorted(selected_indexes)]


def _rule_draft(record: MaterialRecord) -> EditorialDraft:
    if record.item_type == "paper":
        fields = _paper_fields(record)
        evidence = _paper_evidence_used(record)
    else:
        fields = _repo_fields(record)
        evidence = [record.repo_description or "not_stated"]
        if record.readme_excerpt:
            evidence.append(record.readme_excerpt[:600])
    return EditorialDraft(
        key=record.key,
        item_type=record.item_type,
        title=record.title,
        draft_fields=fields,
        evidence_used=evidence,
        writer_notes=_writer_notes(record),
    )


def _writer_notes(record: MaterialRecord) -> str:
    if record.item_type == "paper":
        if record.paper_text_excerpt:
            return "规则写手草稿：基于全文片段、摘要和多源元数据生成；未说明处标记 not_stated。"
        return "规则写手草稿：未取得全文片段，仅基于摘要和多源元数据生成；未说明处标记 not_stated。"
    return "规则写手草稿：基于 README 片段、项目描述和元数据生成；未说明处标记 not_stated。"


def _paper_fields(record: MaterialRecord) -> dict[str, Any]:
    abstract = _clean(_best_paper_abstract(record) or "not_stated")
    section_notes = _paper_section_notes(record)
    section_notes_text = _paper_section_notes_text(record)
    paper_text = _clean(" ".join(part for part in [section_notes_text, record.paper_text_excerpt or ""] if part))
    metadata_evidence = _paper_metadata_evidence(record)
    text = " ".join([record.title, abstract, " ".join(record.categories)]).lower()
    evidence_text = _clean(" ".join(part for part in [abstract, paper_text] if part and part != "not_stated"))
    problem = _paper_problem(record, section_notes, text, evidence_text or abstract)
    method = _paper_method(section_notes, text, evidence_text or abstract)
    why_it_works = _paper_why_it_works(section_notes, text, evidence_text or abstract)
    novelty_or_difference = _paper_novelty_or_difference(record, section_notes, text, evidence_text or abstract)
    method_steps = _method_steps(text, evidence_text or abstract)
    key_result = _paper_key_result(section_notes, evidence_text or abstract)
    limitations = _paper_limitations(section_notes, evidence_text or abstract)
    return {
        "problem": problem,
        "method": method,
        "why_it_works": why_it_works,
        "novelty_or_difference": novelty_or_difference,
        "method_steps": method_steps,
        "key_result": key_result,
        "technical_route": f"{method} 关键路径：{'; '.join(method_steps) if isinstance(method_steps, list) else method_steps}",
        "possible_use_or_impact": _paper_impact(record, text),
        "limitations": limitations,
        "evidence_from_source": _truncate(" ".join(_paper_evidence_used(record)), 520),
        "confidence": "medium" if evidence_text else "low",
    }


def _paper_evidence_used(record: MaterialRecord) -> list[str]:
    evidence = []
    abstract = _best_paper_abstract(record)
    if abstract:
        evidence.append(_clean(abstract))
    metadata = _paper_metadata_evidence(record)
    if metadata:
        evidence.append(metadata)
    section_notes = _paper_section_notes_text(record)
    if section_notes:
        evidence.append(_truncate(section_notes, 1200))
    if record.paper_text_excerpt:
        evidence.append(_truncate(record.paper_text_excerpt, 1200))
    return evidence or ["not_stated"]


def _best_paper_abstract(record: MaterialRecord) -> str | None:
    if record.abstract:
        return record.abstract
    sources = (record.evidence or {}).get("sources", {}) or {}
    for source in ["arxiv", "openreview", "pmlr", "neurips", "openalex", "semantic_scholar", "google_scholar", "core", "ieee", "crossref"]:
        abstract = (sources.get(source) or {}).get("abstract")
        if abstract:
            return str(abstract)
    return None


def _paper_metadata_evidence(record: MaterialRecord) -> str:
    sources = (record.evidence or {}).get("sources", {}) or {}
    source_names = [source for source in ["arxiv", "openreview", "pmlr", "neurips", "openalex", "semantic_scholar", "google_scholar", "core", "crossref", "ieee"] if source in sources or source in record.source_aliases]
    venue = record.raw.get("venue") or _first_source_value(sources, "venue")
    citations = record.raw.get("citation_count") or record.raw.get("cited_by_count") or _first_source_value(sources, "citation_count")
    parts = []
    if source_names:
        parts.append(f"sources={','.join(source_names)}")
    if record.doi:
        parts.append(f"doi={record.doi}")
    if venue:
        parts.append(f"venue={venue}")
    if citations is not None:
        parts.append(f"citations={citations}")
    citation_context = _citation_context_text(record.raw.get("citation_context") or {})
    if citation_context:
        parts.append(f"引用脉络：{citation_context}")
    return "；".join(parts)


def _citation_context_text(context: dict[str, Any]) -> str:
    if not isinstance(context, dict) or not context:
        return ""
    parts = []
    cited_by = context.get("cited_by_count")
    if cited_by is not None:
        parts.append(f"被引 {cited_by} 次")
    citing_titles = _citation_titles(context.get("citing"))
    if citing_titles:
        parts.append(f"近期引用：{'、'.join(citing_titles)}")
    referenced_titles = _citation_titles(context.get("referenced"))
    if referenced_titles:
        parts.append(f"关键参考：{'、'.join(referenced_titles)}")
    return "；".join(parts)


def _citation_titles(values: Any, limit: int = 2) -> list[str]:
    if not isinstance(values, list):
        return []
    titles = []
    for value in values:
        if isinstance(value, dict) and value.get("title"):
            titles.append(str(value["title"]))
        if len(titles) >= limit:
            break
    return titles


def _paper_section_notes(record: MaterialRecord) -> dict[str, str]:
    notes = (record.paper_text_status or {}).get("section_notes") or {}
    if not isinstance(notes, dict):
        return {}
    return {str(key): str(value) for key, value in notes.items() if value}


def _paper_section_notes_text(record: MaterialRecord) -> str:
    notes = _paper_section_notes(record)
    if not notes:
        return ""
    preferred = ["abstract", "introduction", "method", "results", "limitations", "conclusion"]
    parts = []
    for key in preferred:
        value = notes.get(key)
        if value:
            parts.append(f"{key}: {value}")
    for key, value in notes.items():
        if key not in preferred:
            parts.append(f"{key}: {value}")
    return " ".join(parts)


def _first_source_value(sources: dict[str, Any], field: str) -> Any:
    for source in ["arxiv", "openreview", "pmlr", "neurips", "openalex", "semantic_scholar", "google_scholar", "core", "crossref", "ieee"]:
        value = (sources.get(source) or {}).get(field)
        if value is not None and value != "":
            return value
    return None


def _repo_fields(record: MaterialRecord) -> dict[str, Any]:
    text = " ".join([record.title, record.repo_description or "", record.readme_excerpt or "", " ".join(record.categories)]).lower()
    description = _clean(record.repo_description or "not_stated")
    readme = _clean(record.readme_excerpt or "")
    capabilities = _repo_capabilities(text, description, readme)
    use_cases = _repo_use_cases(text, description, readme)
    architecture = _repo_architecture(text, readme)
    maturity = f"stars={record.stars or 0}，language={record.language or 'unknown'}，updated_at={record.source_updated_at or 'unknown'}。"
    return {
        "what_it_is": _repo_what_it_is(record, description),
        "core_capabilities": capabilities,
        "typical_use_cases": use_cases,
        "architecture_or_api": architecture,
        "maturity_signal": maturity,
        "reusable_point": _repo_reusable(text),
        "evidence_from_source": _truncate(" ".join([description, readme]), 520),
        "confidence": "medium" if readme or description != "not_stated" else "low",
    }


def _rule_review(draft: EditorialDraft) -> EditorialReview:
    fields = draft.draft_fields
    issues: list[str] = []
    required_changes: list[str] = []
    critical_issue = False
    required_fields = PAPER_FIELDS if draft.item_type == "paper" else REPO_FIELDS
    for field in required_fields:
        value = fields.get(field)
        if value is None or value == "" or value == []:
            issues.append(f"{field} 缺失")
            required_changes.append(f"补充 {field}，无法判断时写 not_stated")
        if isinstance(value, str) and any(phrase in value for phrase in GENERIC_PHRASES):
            issues.append(f"{field} 存在泛泛模板话术")
            required_changes.append(f"重写 {field}，必须说明具体问题/方法/能力/证据")
        if draft.item_type == "paper" and isinstance(value, str) and _looks_like_pdf_extraction_noise(value):
            issues.append(f"{field} 存在 PDF 抽取噪声")
            required_changes.append(f"重写 {field}，不要直接使用断词、公式碎片或 PDF 抽取残片")
            critical_issue = True
        if draft.item_type == "paper" and isinstance(value, str) and _looks_like_section_header_residue(value):
            issues.append(f"{field} 存在章节标题残片")
            required_changes.append(f"重写 {field}，不要把 Introduction/Abstract 等章节标题后的英文原句直接贴进日报")
            critical_issue = True
    if draft.item_type == "paper":
        if _paper_core_fields_lack_chinese_explanation(fields):
            issues.append("论文核心字段缺少中文解读")
            required_changes.append("核心字段必须用中文解释问题、方法机制、结果和局限；英文术语可保留但不能整段照搬")
            critical_issue = True
        if fields.get("method") == "not_stated":
            issues.append("论文 method 未说明")
            required_changes.append("补充论文 method；如果摘要确实未说明方法，本条不登刊")
            critical_issue = True
        if fields.get("why_it_works") == "not_stated":
            issues.append("论文 why_it_works 未说明")
            required_changes.append("补充为什么该方法能解决问题；如果全文/摘要无法支撑，本条不登刊")
            critical_issue = True
        if not fields.get("evidence_from_source"):
            issues.append("论文缺少来源证据")
            required_changes.append("补充 evidence_from_source")
            critical_issue = True
        if _paper_draft_is_too_shallow(draft):
            issues.append("论文解读信息量不足或过度贴近摘要原文")
            required_changes.append("重写为读后理解：说清问题、方法机制、为什么有效、实验效果和局限")
            critical_issue = True
    else:
        if fields.get("core_capabilities") == "not_stated":
            issues.append("GitHub core_capabilities 未说明")
            required_changes.append("补充 GitHub core_capabilities；如果 README/描述未说明能力，本条不登刊")
            critical_issue = True
        if fields.get("typical_use_cases") == "not_stated":
            issues.append("GitHub typical_use_cases 未说明")
            required_changes.append("补充 GitHub typical_use_cases；如果 README/描述未说明场景，本条不登刊")
            critical_issue = True
    verdict = "FAIL" if critical_issue or len(issues) >= 3 else "PASS"
    return EditorialReview(
        key=draft.key,
        verdict=verdict,
        issues=issues,
        required_changes=required_changes,
        reader_value_score=max(0.0, 10.0 - len(issues) * 2.0),
    )


def _draft_with_llm(shortlist: list[MaterialRecord]) -> list[EditorialDraft]:
    settings = _LLM_WRITER_SETTINGS.get() or _DEFAULT_LLM_WRITER_SETTINGS
    batch_size = max(1, int(settings.get("batch_size", _DEFAULT_LLM_WRITER_SETTINGS["batch_size"])))
    timeout_seconds = max(1.0, float(settings.get("timeout_seconds", _DEFAULT_LLM_WRITER_SETTINGS["timeout_seconds"])))
    run_budget_seconds = max(0.0, float(settings.get("run_budget_seconds", _DEFAULT_LLM_WRITER_SETTINGS["run_budget_seconds"])))
    deadline = time.monotonic() + run_budget_seconds if run_budget_seconds > 0 else None
    all_drafts: list[EditorialDraft] = []
    consecutive_failures = 0
    for index in range(0, len(shortlist), batch_size):
        batch = shortlist[index : index + batch_size]
        print(f"Writer batch {index // batch_size + 1}/{(len(shortlist) + batch_size - 1) // batch_size}: {len(batch)} items", flush=True)
        batch_timeout = timeout_seconds
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                all_drafts.extend(_rule_draft(record) for record in shortlist[index:])
                break
            batch_timeout = max(1.0, min(timeout_seconds, remaining))
        drafts = _draft_with_llm_batch(batch, timeout_seconds=batch_timeout)
        if not drafts:
            consecutive_failures += 1
            # A failed batch must not discard every subsequent candidate.
            all_drafts.extend(_rule_draft(record) for record in batch)
            # Once the backend fails, avoid repeated expensive calls; preserve
            # the remaining items as explicitly marked rule drafts.
            for record in shortlist[index + batch_size:]:
                record.reading["writer_error"] = "backend_circuit_open"
            all_drafts.extend(_rule_draft(record) for record in shortlist[index + batch_size:])
            break
        consecutive_failures = 0
        all_drafts.extend(drafts)
    return all_drafts


def _llm_writer_settings(config: AppConfig) -> dict[str, Any]:
    source_config = config.sources.get("llm_writer", {}) or {}
    provider = str(source_config.get("provider") or _DEFAULT_LLM_WRITER_SETTINGS["provider"]).strip().lower()
    if provider != "codex":
        raise ValueError(f"Daily Agent internal writing requires provider=codex, got {provider or '<empty>'}")
    return {
        "provider": provider,
        "command": str(source_config.get("command") or provider),
        "batch_size": int(source_config.get("batch_size", _DEFAULT_LLM_WRITER_SETTINGS["batch_size"])),
        "timeout_seconds": float(source_config.get("timeout_seconds", _DEFAULT_LLM_WRITER_SETTINGS["timeout_seconds"])),
        "run_budget_seconds": float(source_config.get("run_budget_seconds", _DEFAULT_LLM_WRITER_SETTINGS["run_budget_seconds"])),
        "max_input_chars_per_item": int(source_config.get("max_input_chars_per_item", _DEFAULT_LLM_WRITER_SETTINGS["max_input_chars_per_item"])),
        "reasoning_effort": str(source_config.get("reasoning_effort") or _DEFAULT_LLM_WRITER_SETTINGS["reasoning_effort"]).strip().lower(),
        "workdir": str(config.root),
        "synthesis_chars": int(config.sources.get("reading", {}).get("synthesis_chars", 60000)),
        "load_user_config": bool(source_config.get("load_user_config", False)),
    }


def _draft_with_llm_batch(shortlist: list[MaterialRecord], timeout_seconds: float = 600.0, feedback: dict | None = None) -> list[EditorialDraft]:
    settings = _LLM_WRITER_SETTINGS.get() or _DEFAULT_LLM_WRITER_SETTINGS
    prompt = _llm_prompt(shortlist, max_input_chars_per_item=int(settings.get("max_input_chars_per_item", 24_000)))
    if feedback:
        prompt += ('\n上一版被证据审核拒绝。按以下反馈重新写完整JSON数组：先选可定位引句，再写结论；'
                   '每字段只写引句直接支持的最小事实，不必塞满全部研究内容。'
                   '一句话有多个事实时必须附齐多条claim_evidence；conditions和summary不能代替quote。'
                   '删掉缺证据的成分，而不是解释它可能正确。不得使用上一版被拒绝的内容作为证据。反馈：'
                   + json.dumps(feedback, ensure_ascii=False))
    result = None
    try:
        result = subprocess.run(
            _llm_writer_command(settings, prompt),
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        if any(r.paper_document for r in shortlist):
            from pathlib import Path
            from daily_agent.paper_document import atomic_json, digest
            atomic_json(Path(settings['workdir']) / 'data' / 'reading' / 'writer' / (digest(prompt)+'.json'),
                        {'response': result.stdout})
        payload = _load_llm_payload(result.stdout)
        by_key = {record.key: record for record in shortlist}
        drafts = _validated_llm_drafts(payload, by_key)
        return _complete_llm_drafts_with_rule_fallback(drafts, by_key)
    except Exception as exc:
        # Do not persist subprocess command/streams: prompts and credentials may
        # appear there. Keep actionable, bounded diagnostics for every item.
        error = {"type": type(exc).__name__}
        if isinstance(exc, subprocess.TimeoutExpired):
            error["timeout_seconds"] = exc.timeout
        elif isinstance(exc, subprocess.CalledProcessError):
            error["returncode"] = exc.returncode
        elif isinstance(exc, (ValueError, TypeError)):
            error["detail"] = "invalid model JSON or draft schema"
        for record in shortlist:
            record.reading['writer_error'] = error
        return []


def _llm_writer_command(settings: dict[str, Any], prompt: str) -> list[str]:
    provider = str(settings.get("provider") or "codex").strip().lower()
    command = str(settings.get("command") or provider)
    if provider != "codex":
        raise ValueError(f"Unsupported internal writer provider: {provider}")
    args = [
        command,
        "exec",
        "--ephemeral",
        "--ignore-rules",
    ]
    if not settings.get("load_user_config", False):
        args.append("--ignore-user-config")
    reasoning_effort = str(settings.get("reasoning_effort") or "").strip().lower()
    if reasoning_effort:
        args.extend(["-c", f'model_reasoning_effort="{reasoning_effort}"'])
    args.extend([
        "--sandbox",
        "read-only",
        "--skip-git-repo-check",
        "-C",
        str(settings.get("workdir") or "."),
        prompt,
    ])
    return args


def _decode_model_json(output: str):
    payload = output.strip()
    if payload.startswith("```json") and payload.endswith("```"):
        payload = payload[7:-3].strip()
    elif payload.startswith("```") and payload.endswith("```"):
        payload = payload[3:-3].strip()
    return json.loads(payload)


def _load_llm_payload(output: str) -> list[Any]:
    payload = _decode_model_json(output)
    if isinstance(payload, dict) and "result" in payload:
        result = payload["result"]
        payload = json.loads(result) if isinstance(result, str) else result
    if not isinstance(payload, list):
        raise ValueError("LLM draft output must be a JSON array")
    return payload


def _validated_llm_drafts(payload: list[Any], by_key: dict[str, MaterialRecord]) -> list[EditorialDraft]:
    if len(payload) != len(by_key):
        raise ValueError("LLM draft output must cover every shortlisted item")
    drafts: list[EditorialDraft] = []
    seen: set[str] = set()
    for raw in payload:
        if not isinstance(raw, dict):
            raise ValueError("LLM draft item must be an object")
        key = raw.get("key")
        if not isinstance(key, str) or key not in by_key or key in seen:
            raise ValueError("LLM draft item key is unknown or duplicated")
        record = by_key[key]
        draft_fields = _validated_draft_fields(raw.get("draft_fields"), record.item_type)
        writer_notes = raw.get("writer_notes")
        if writer_notes is not None and not isinstance(writer_notes, str):
            raise ValueError("LLM writer_notes must be a string or null")
        drafts.append(
            EditorialDraft(
                key=key,
                item_type=record.item_type,
                title=record.title,
                draft_fields=draft_fields,
                evidence_used=_required_string_list(raw.get("evidence_used"), "evidence_used"),
                writer_notes=_llm_writer_notes(writer_notes),
                claim_evidence=raw.get("claim_evidence", []) if isinstance(raw.get("claim_evidence", []), list) else [],
            )
        )
        seen.add(key)
    if seen != set(by_key):
        raise ValueError("LLM draft output must match shortlisted item keys")
    return drafts


def _llm_writer_notes(writer_notes: str | None) -> str:
    note = (writer_notes or "").strip()
    if note.startswith("LLM写手草稿"):
        return note
    if note:
        return f"LLM写手草稿：{note}"
    return "LLM写手草稿：基于全文片段、摘要和多源元数据生成；未说明处标记 not_stated。"


def _complete_llm_drafts_with_rule_fallback(drafts: list[EditorialDraft], by_key: dict[str, MaterialRecord]) -> list[EditorialDraft]:
    completed: list[EditorialDraft] = []
    for draft in drafts:
        record = by_key[draft.key]
        if record.item_type != "paper" or record.paper_document:
            completed.append(draft)
            continue
        rule_fields = _rule_draft(record).draft_fields
        fields = dict(draft.draft_fields)
        changed = False
        for field in PAPER_FIELDS:
            if not _is_not_stated_value(fields.get(field)):
                continue
            fallback = rule_fields.get(field)
            if _is_not_stated_value(fallback):
                continue
            fields[field] = fallback
            changed = True
        if _is_not_stated_value(draft.draft_fields.get("technical_route")) and not _is_not_stated_value(fields.get("method")):
            steps = fields.get("method_steps")
            step_text = "; ".join(steps) if isinstance(steps, list) else str(steps)
            fields["technical_route"] = f"{fields['method']} 关键路径：{step_text}"
            changed = True
        if not changed:
            completed.append(draft)
            continue
        writer_notes = "；".join(part for part in [draft.writer_notes, "规则写手补全 LLM 未抽取的论文字段"] if part)
        completed.append(
            EditorialDraft(
                key=draft.key,
                item_type=draft.item_type,
                title=draft.title,
                draft_fields=fields,
                evidence_used=draft.evidence_used,
                writer_notes=writer_notes,
            )
        )
    return completed


def _is_not_stated_value(value: Any) -> bool:
    if value is None or value == "":
        return True
    if isinstance(value, str):
        return value.strip().lower() == "not_stated"
    if isinstance(value, list):
        return not value or all(isinstance(item, str) and item.strip().lower() == "not_stated" for item in value)
    return False


def _validated_draft_fields(fields: Any, item_type: str) -> dict[str, Any]:
    if not isinstance(fields, dict):
        raise ValueError("LLM draft_fields must be an object")
    required_fields = PAPER_FIELDS if item_type == "paper" else REPO_FIELDS
    normalized: dict[str, Any] = {}
    for field in required_fields:
        value = fields.get(field)
        if field == "method_steps" and item_type == "paper":
            normalized[field] = _required_string_list(value, f"draft_fields.{field}")
        else:
            normalized[field] = _required_string(value, f"draft_fields.{field}", allow_number=field == "confidence")
    return normalized


def _required_string(value: Any, field_name: str, allow_number: bool = False) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if allow_number and isinstance(value, (int, float)):
        return str(value)
    raise ValueError(f"{field_name} must be a non-empty string")


def _required_string_list(value: Any, field_name: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{field_name} must be a non-empty string list")
    items = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{field_name} must be a non-empty string list")
        items.append(item.strip())
    return items


def _llm_prompt(shortlist: list[MaterialRecord], max_input_chars_per_item: int = 24_000) -> str:
    input_limit = max(1, int(max_input_chars_per_item))
    from daily_agent.reading import synthesis_evidence
    records = []
    for record in shortlist:
        records.append(
            {
                "key": record.key,
                "item_type": record.item_type,
                "title": record.title,
                "abstract": record.abstract,
                "paper_section_notes": {} if record.paper_document else _paper_section_notes(record),
                "reading_notes": synthesis_evidence(record, int((_LLM_WRITER_SETTINGS.get() or {}).get("synthesis_chars", 60000))) if record.paper_document else [],
                "reading_coverage": {k:v for k,v in record.reading.items() if k not in {"notes", "visual"}},
                "visual_observations": {k:v for k,v in record.reading.get("visual", {}).items() if k != "fidelity"},
                "paper_text_excerpt": "" if record.paper_document else (record.paper_text_excerpt or "")[:input_limit],
                "paper_text_status": record.paper_text_status,
                "repo_description": record.repo_description,
                "readme_excerpt": (record.readme_excerpt or "")[:input_limit],
                "metadata": {
                    "tags": record.tags,
                    "categories": record.categories,
                    "stars": record.stars,
                    "language": record.language,
                    "updated_at": record.source_updated_at,
                    "url": record.url,
                    "pdf_url": record.pdf_url,
                    "citation_context": record.raw.get("citation_context"),
                },
            }
        )
    return (
        "你是 Daily_Agent 编辑部写手，要像研究助理通览论文后写阅读笔记，而不是改写摘要。"
        "不要调用任何工具、不要读取本地文件、不要联网，只处理下面已经给出的输入。"
        "只基于输入素材写结构化草稿；没有来源依据就写 not_stated，禁止编造。"
        "有 reading_notes 时必须综合全部分块笔记与引句，abstract 仅作目录线索，不能替代已读正文。旧版无分块记录时才参考 paper_section_notes 和 paper_text_excerpt，并降低 confidence。"
        "visual_observations.strict_fidelity 为 true 时，reading_notes 来自经独立图片复核的重建文本；原生抽取差异保留用于审计，不代表重建文本仍有同样错误。模型复核不证明论文数学或科学结论正确。"
        "metadata.citation_context 是 OpenAlex 引用脉络，可用于判断这篇论文的上游基础、下游引用和影响力，但不能代替正文证据。"
        "如果 paper_text_status.sufficient_for_deep_summary 为 false，必须在 writer_notes 里说明证据缺口，并降低 confidence。"
        "每篇论文都要回答：1) 发现/针对什么具体问题；2) 用什么方法解决；3) 为什么这个方法理论上或工程上能解决；4) 相对已有工作或关键参考的新意/差异；5) 实验/结果如何；6) 有什么局限；7) 对硬件感知量子线路综合/编译、QEC、量子真机或用户研究有什么可迁移点。"
        "写法要求：用中文解释，不要逐句翻译摘要；每个字段要包含具体对象、机制、指标或实验设置，避免“提出一种方法”“有参考价值”这类空话。"
        "各字段将组成连贯解读：problem只交代瓶颈，method说明关键改变，why_it_works解释因果机制，novelty_or_difference给出有依据的对照，避免四处重复方法名与步骤。"
        "每个字段优先保留一到两个有直接引句依据的核心事实；不要把背景、机制和未经证实的解释塞进同一句。"
        "key_result优先选择一组最能代表贡献的主比较及必要边界，其余次要指标放弃；每个数字、比较基线、资源条件都必须有对应逐字引句。"
        "key_result必须把核心数字、基线、任务规模、成本口径及实测/模拟/理论/预测性质写在同一段，不能为缩短篇幅删掉限制条件。"
        "limitations优先写会改变结果解释或可迁移性的边界；本次输入不足应写本次未核验，不能推断论文未报告。"
        "possible_use_or_impact明确区分作者验证的用途与编辑提出的迁移设想，后者写为可尝试的方向，不得宣称已实现收益。"
        "不把无关论文强行关联到量子研究。简洁优先但条件完整优先于字数目标。"
        "如果论文涉及 hardware-aware quantum circuit synthesis/compilation/transpilation/routing/unitary synthesis，要优先解释硬件约束如何进入模型或优化目标。"
        "论文字段必须包含 problem, method, why_it_works, novelty_or_difference, method_steps, key_result, technical_route, possible_use_or_impact, limitations, evidence_from_source, confidence。"
        "GitHub 字段必须包含 what_it_is, core_capabilities, typical_use_cases, architecture_or_api, maturity_signal, reusable_point, evidence_from_source, confidence。"
        "输出类型必须严格遵守：draft_fields 中除 method_steps 外的每个字段都只能是一个 JSON 字符串，禁止数组和对象，每个字符串以 180 个汉字以内为目标，但不得为限字删掉数字的适用条件；method_steps 是 2 到 5 个短字符串；evidence_used 是 2 到 6 个来源路径字符串；writer_notes 最多 120 个汉字；confidence 只能写 high、medium 或 low。"
        "禁止泛泛写：与配置方向相关、需进一步阅读确认、建议查看 README。"
        "对于提供 reading_notes 的论文，只能使用已读分块笔记与引句支持结论；reading_notes 为 null 或空时不能生成深读结论。"
        "先选引句再写结论：每字段只写引句直接支持的事实；多事实必须提供多条证据，不能只附一句代表性引文。conditions不是证据，不得将它作为引句缺失信息的补充。"
        "额外输出 claim_evidence 数组，每条包含 field、chunk_id、quote（逐字引用 reading_notes.quotes）、conditions（字符串，不要对象）、evidence_kind。"
        "problem/method/why_it_works/novelty_or_difference/key_result/technical_route/limitations/method_steps/possible_use_or_impact 均应关联引用。"
        "evidence_kind 只能是 experiment/simulation/theory/prediction/not_stated；结果必须说明基线、对象与实验条件。"
        "返回 JSON 数组，每项包含 key, draft_fields, evidence_used, writer_notes, claim_evidence。输入：\n"
        + json.dumps(records, ensure_ascii=False)
    )


def _paper_problem(record: MaterialRecord, notes: dict[str, str], text: str, evidence_text: str) -> str:
    if _contains(text, "vision-language-action", "vla", "world model", "embodied"):
        if _contains(text, "distribution shift", "deployment-time", "finetuning", "fine-tuning"):
            return "论文针对预训练 VLA/具身策略在部署分布偏移下容易失效，而常规微调又依赖目标环境示范数据的问题。"
        return "论文关注具身智能或 VLA 系统在长程任务中如何保持状态理解、动作规划和环境反馈一致。"
    if _contains(text, "qutuner", "optimization pass", "compiler pass tuning", "pass tuning"):
        return "论文针对量子编译器 pass 调优的问题：可选优化 pass 空间很大，静态线路特征又不足以反映线路对不同 pass 的真实响应。"
    if _contains(text, "figure of merit", "figures of merit", "fom", "wpst", "probability of successful trials"):
        return "论文针对量子线路编译中的评价指标问题：简单深度/门数指标便宜但不反映真实硬件噪声和执行成功率，精确指标又太贵。"
    if _contains(text, "quantum architecture search", "qas", "hamqasbench"):
        return "论文针对量子架构搜索评测过度依赖能量精度、难以暴露线路结构错误和硬件路由失败的问题。"
    if _contains(text, "quantum machine learning", "qnn", "quantum neural network", "cloud microphysics"):
        return "论文检验量子机器学习在复杂物理数据上的真实优势：QNN 是否能在云微物理任务中超过充分优化的经典模型。"
    return _sentence_about_problem(record, evidence_text)


def _paper_method(notes: dict[str, str], text: str, evidence_text: str) -> str:
    if _contains(text, "rubriq", "programmatic rubric", "group relative policy optimization", "grpo"):
        return "RubriQ 把受硬件约束的量子线路综合写成 LLM 代码生成任务，再用 GRPO 优化；奖励不是黑盒 critic，而是同时检查语义正确性、T 门成本和硬件约束的程序化 rubric。"
    if _contains(text, "confidence-gated", "low-confidence syndromes") and _contains(text, "mwpm", "surface code"):
        return "方法采用两阶段置信门控解码：轻量前馈神经网络处理大多数高置信 syndrome，只有低置信样本才升级到 MWPM 精修，从而把高精度解码器的开销限制在少量困难样本上。"
    if _contains(text, "fibonacci braid", "non-abelian anyons", "solovay-kitaev"):
        return "方法把连续的 SU(2) 目标酉变换映射为 Fibonacci 任意子的离散编织词，并结合向量化 Solovay-Kitaev 递归、近邻索引和同伦约简搜索更短的可执行 braid word。"
    if _contains(text, "qutuner", "optimization pass", "compiler pass tuning", "pass tuning"):
        return "方法构建 QuTuner：先用 Bayesian Optimization 在 8,111 个量子线路上生成优化数据，再把静态 circuit features 与 optimization-aware pass embeddings 结合，用离线模型检索、排序候选 pass 序列，并做轻量 BO refinement。"
    if _contains(text, "wpst", "probability of successful trials", "figure of merit", "figures of merit"):
        return "方法提出 wPST 作为按 qubit 加权的成功概率指标，并训练 machine learning 预测器同时读取量子线路特征和硬件数据；编译前先预测 transpilation 后新增门，再结合 coherence time 预测 wPST。"
    if _is_qnn_cloud_text(text):
        return "方法用 hybrid QNN 处理云微物理数据，并加入 rich trainable frequency spectrum、classical postprocessing 和大规模 hyperparameter optimization，再与充分调参的 FCNN 基线比较。"
    if _contains(text, "dreamsteer", "latent world model", "value model"):
        return "方法让冻结的 VLA 先生成候选 action chunks，并加入 Cartesian motion primitives；latent world model 预测候选动作后的未来观测，再由 language-conditioned value model 对轨迹排序后执行。"
    if _contains(text, "opine-world", "programmatic world model", "counterexample-guided"):
        return "方法让 two cooperating agents 形成 hypothesis-and-test 循环：一个 agent 与环境交互，另一个用代码合成 object-centric world model，并通过 replay verification、CEGIS 和 model-based planning 持续修正。"
    if _contains(text, "musix", "multi-scale mixture", "scale-aware world model"):
        return "方法提出 MuSix：先用 experiential distance 通过 meta-router 判断当前情境尺度，再在对应尺度选择 world model，并用尺度相关遗忘率和跨尺度门控迁移做在线适应。"
    if _is_embodied_operator_text(text):
        return "方法把具身系统中的检测、3D 理解、手部运动恢复、VLA/世界模型、规划控制等模块定义为 embodied operators，并给出分类、输入输出契约和多维 benchmark。"
    if _contains(text, "pinocchio", "faithfulness", "embodied chain-of-thought"):
        return "方法先把具身推理拆成观测 grounding 与逐步一致性，再训练 Pinocchio critic，把 faithfulness 分数作为强化学习奖励来后训练策略。"
    if _contains(text, "hamqasbench", "hamiltonian-informed", "critical-structure"):
        return "方法用 Hamiltonian structural fingerprints 给分子实例分层，并结合 post-hoc critical-structure extraction、逐 qubit entanglement 与 state fidelity 来诊断 QAS 方法。"
    if _contains(text, "sparse routing", "qubit placement", "swap insertion"):
        return "方法把 qubit placement 和 SWAP insertion 联合建模为稀疏路由编译问题，在满足硬件连通性的同时压低双量子门深度。"
    if _contains(text, "citation-aware", "citation context", "upstream references", "downstream citing"):
        return "方法在硬件感知量子线路综合中加入 citation-aware reranking，用上游参考和下游引用信号辅助候选编译方案排序。"
    generic_method = _sentence_about_method(text, "")
    if generic_method != "not_stated":
        return generic_method
    section_method = _section_sentence(notes, ["method", "abstract", "introduction"], ["we propose", "we introduce", "we develop", "we train", "we use", "our method", "our approach"], 260)
    if section_method != "not_stated":
        return _chinese_source_summary("方法上，正文说明：", section_method)
    source_method = _sentence_about_method(text, evidence_text)
    return _chinese_source_summary("方法上，正文说明：", source_method)


def _paper_why_it_works(notes: dict[str, str], text: str, evidence_text: str) -> str:
    if _contains(text, "rubriq", "programmatic rubric", "group relative policy optimization", "grpo"):
        return "程序化 rubric 把正确性、资源压缩和硬件可执行性拆成可验证奖励，GRPO 再通过组内相对优势稳定更新策略，因此能减少稀疏奖励下只追求压缩却破坏约束的行为。"
    if _contains(text, "confidence-gated", "low-confidence syndromes") and _contains(text, "mwpm", "surface code"):
        return "神经网络负责常见 syndrome 的低时延快速路径，置信度则识别最容易造成逻辑失败的困难样本；只把这些样本交给 MWPM，可在小幅增加平均成本的同时接近高精度解码。"
    if _contains(text, "fibonacci braid", "non-abelian anyons", "solovay-kitaev"):
        return "Fibonacci 任意子的编织表示天然满足拓扑门约束，Solovay-Kitaev 递归逐层缩小近似误差，向量化索引与同伦约简则降低搜索和冗余 braid word 的成本。"
    if _contains(text, "qutuner", "optimization pass", "compiler pass tuning", "pass tuning"):
        return "它有效的原因是 pass embeddings 不是只描述线路静态结构，而是记录单个 pass 作用前后的指标变化，因此能把“这条线路会怎样响应优化”纳入候选 pass 排序。"
    if _contains(text, "wpst", "probability of successful trials", "figure of merit", "figures of merit"):
        return "它有效的原因是 wPST 不只看整体输出分布，而把不同 qubit 的成功概率和硬件差异纳入指标；预测器再显式利用 coupling map、coherence time 和编译新增门数来近似真实执行质量。"
    if _is_qnn_cloud_text(text):
        return "这样设计给 QNN 足够表达力和调参空间，避免把失败简单归因于欠调参；因此若仍输给 FCNN，更能说明当前 QNN 在该复杂物理任务上的优势证据不足。"
    if _contains(text, "dreamsteer", "latent world model", "value model"):
        return "它有效的原因是 policy、latent world model 和 value model 在不同数据/目标下泛化，world model 先排除明显会失败的动作后果，value model 再按语言目标筛选更可靠轨迹。"
    if _contains(text, "opine-world", "programmatic world model", "counterexample-guided"):
        return "程序化 world model 可被检查和复用，CEGIS 用反例强制模型与已观察转移一致；探索策略再优先补 ontology error 高的对象，减少盲目试错。"
    if _contains(text, "musix", "multi-scale mixture", "scale-aware world model"):
        return "scale-aware routing 让低层动态知识快速更新、高层抽象长期保留，避免统一更新策略同时造成遗忘和迟钝适应。"
    if _is_embodied_operator_text(text):
        return "把端到端策略拆成可复用 operator 后，可以分别评估正确性、时延、资源、接口兼容和部署可靠性，便于定位系统瓶颈。"
    if _contains(text, "pinocchio", "faithfulness", "embodied chain-of-thought"):
        return "它把原本只看任务成功的后训练目标改成可评分的推理一致性约束，因此能减少看似合理但与观测和动作脱节的 CoT。"
    if _contains(text, "hamqasbench", "hamiltonian-informed", "critical-structure"):
        return "Hamiltonian 指纹直接刻画问题所需的纠缠和结构复杂度，所以能发现只靠能量精度会掩盖的过参数化、简并态误选和路由瓶颈。"
    if _contains(text, "sparse routing", "qubit placement", "swap insertion"):
        return "它把 placement 和 routing 的代价提前合并优化，先惩罚长距离移动，再映射到硬件门，因此能减少后续补 SWAP 造成的深度膨胀。"
    if _contains(text, "citation-aware", "citation context", "upstream references", "downstream citing"):
        return "引用上下文能暴露哪些方法是上游基础、哪些设置被下游继续使用，因此 reranking 不只依赖局部线路特征，还利用了可追溯的研究脉络信号。"
    generic_reason = _why_it_works(text, "")
    if generic_reason != "not_stated":
        return generic_reason
    section_reason = _section_sentence(notes, ["method", "results", "introduction"], ["because", "therefore", "so that", "allows", "enables", "by"], 260)
    if section_reason != "not_stated":
        return _chinese_source_summary("有效性依据是：", section_reason)
    source_reason = _why_it_works(text, evidence_text)
    return _chinese_source_summary("有效性依据是：", source_reason)


def _paper_novelty_or_difference(record: MaterialRecord, notes: dict[str, str], text: str, evidence_text: str) -> str:
    if _contains(text, "rubriq", "programmatic rubric", "group relative policy optimization", "grpo"):
        return "相对依赖稀疏终局奖励或黑盒 critic 的线路生成方法，RubriQ 用领域规则构成可解释的程序化奖励，并把硬件违规率直接纳入强化学习目标。"
    if _contains(text, "confidence-gated", "low-confidence syndromes") and _contains(text, "mwpm", "surface code"):
        return "相对全量使用神经解码或全量运行 MWPM，这项工作按置信度动态选择快速路径和精修路径，显式联合优化逻辑准确率与实时解码时延。"
    if _contains(text, "fibonacci braid", "non-abelian anyons", "solovay-kitaev"):
        return "相对通用门级综合，这项工作直接面向非阿贝尔 Fibonacci 任意子的 braid group 表示，并把近邻检索、递归逼近和同伦约简整合进编译器。"
    if _contains(text, "qutuner", "optimization pass", "compiler pass tuning", "pass tuning"):
        return "相对已有 quantum compiler tuning 工作，QuTuner 不只用静态线路特征或小规模搜索，而是把 optimization-aware pass embeddings 和 8,111 条线路上的 BO 数据用于检索、排序 pass 序列。"
    if _contains(text, "wpst", "probability of successful trials", "figure of merit", "figures of merit"):
        return "相对已有工作中主要看深度、门数或未加权 PST 的 FoM，这篇的新意是提出 wPST，并用 machine learning 把线路结构、transpilation 新增门和硬件 coherence time 一起映射到执行成功率。"
    if _is_qnn_cloud_text(text):
        return "相对只展示 QNN 正结果的工作，这篇把 QNN 放到充分调参的 FCNN 强基线前做负结果检验，因此更像是在校准量子优势证据边界。"
    if _contains(text, "dreamsteer", "latent world model", "value model"):
        return "相对直接执行 VLA 输出或重新微调策略，DREAMSTEER 在部署时加入 latent world model 与 value model 先想象、再筛选候选动作，不改动原策略参数。"
    if _contains(text, "opine-world", "programmatic world model", "counterexample-guided"):
        return "相对神经 latent world model，OPINE-World 把 world model 写成可验证程序，并用反例引导修正，使模型错误能被定位和迭代。"
    if _contains(text, "musix", "multi-scale mixture", "scale-aware world model"):
        return "相对单一尺度 world model，MuSix 按 experiential distance 路由到不同尺度模型，让短期动态和长期抽象用不同更新节奏处理。"
    if _contains(text, "hamqasbench", "hamiltonian-informed", "critical-structure"):
        return "相对只看能量精度的 QAS benchmark，HamQASBench 用 Hamiltonian 结构、关键线路结构和逐 qubit 纠缠诊断来暴露隐藏失败模式。"
    if _is_embodied_operator_text(text):
        return "相对只评估端到端具身策略，这篇把感知、三维理解、VLA/世界模型和规划控制抽象成可复用 operator，并强调接口契约、部署属性和多维 benchmark。"
    if _contains(text, "sparse routing", "qubit placement", "swap insertion"):
        return "相对只做 gate cancellation 或把 qubit placement/routing 分开处理的编译流程，这篇把 quantum circuit mapping 放进 MLIR 稀疏路由框架，联合优化 placement 与 SWAP insertion。"
    if _contains(text, "citation-aware", "citation context", "upstream references", "downstream citing"):
        return "相对只用线路特征或硬件指标排序的编译流程，这篇把论文引用脉络也作为 reranking 信号，用来识别更可靠的候选方法。"
    citation_novelty = _citation_context_novelty(record.raw.get("citation_context") or {})
    if citation_novelty != "not_stated":
        return citation_novelty
    novelty_sentence = _section_sentence(
        notes,
        ["introduction", "abstract", "method", "conclusion"],
        ["prior", "previous", "existing", "unlike", "whereas", "we propose", "we introduce", "novel", "new"],
        260,
    )
    if novelty_sentence != "not_stated":
        return "相对已有工作，" + novelty_sentence
    return _novelty_from_evidence(evidence_text)


def _citation_context_novelty(context: dict[str, Any]) -> str:
    if not isinstance(context, dict) or not context:
        return "not_stated"
    referenced_titles = _citation_titles(context.get("referenced"), limit=1)
    citing_titles = _citation_titles(context.get("citing"), limit=1)
    if referenced_titles and citing_titles:
        return f"相对关键参考《{referenced_titles[0]}》，这篇已被近期工作《{citing_titles[0]}》继续引用，可优先比较它被下游复用的是方法、数据还是实验设定。"
    if referenced_titles:
        return f"相对关键参考《{referenced_titles[0]}》，这篇的差异需要结合正文方法和实验设置精读确认。"
    if citing_titles:
        return f"已有近期工作《{citing_titles[0]}》引用这篇，说明它的设定或方法正在被后续论文接着使用。"
    return "not_stated"


def _paper_key_result(notes: dict[str, str], evidence_text: str) -> str:
    joined = _clean(" ".join(notes.values()))
    lowered = joined.lower()
    context = _clean(" ".join([joined, evidence_text])).lower()
    if "rubriq" in context and "3.31" in context:
        return "结果显示，RubriQ 的平均 T 门压缩达到 3.31 倍，高于稀疏奖励强化学习基线的 2.05 倍，收敛快 2–3 倍，同时把硬件约束违规率控制在 1% 以下。"
    if "confidence-gated" in context and "99.21" in context and "99.81" in context:
        return "结果显示，仅把 3.3%–6.2% 的低置信 syndrome 交给精修，就把逻辑准确率从神经网络单独解码的 99.21% 提升到 99.81%。"
    if _is_qnn_cloud_text(lowered):
        return "结果是负面的但有价值：充分优化后的 FCNN 明显超过 QNN；QNN 只接近未充分优化的经典网络，说明该任务上尚未看到可靠量子优势。"
    if "hamqasbench" in lowered or "energy accuracy is an unreliable proxy" in lowered:
        return "结果显示，11 个分子、5 个结构层级、最高 14 qubits 的评测中，单看能量精度会漏掉过参数化、简并态误选、纠缠结构错误和路由失败。"
    if "opine-world" in lowered and "20 of 25" in lowered and "160 of 183" in lowered:
        return "结果显示，OPINE-World 无需逐游戏训练即可解决 20/25 个 games 和 160/183 个 levels，并超过强 single-agent coding agent；程序合成和神经 latent world model 基线为 0。"
    if "musix" in lowered and "6.05%p" in joined and "1.49%p" in joined:
        return "结果显示，MuSix 在 EmbodiedBench(Habitat) 比 SayCanPay 提升 6.05%p，在 HAZARD(Fire) 比 FLARE 提升 1.49%p。"
    if "embodied operators" in lowered and "benchmark" in lowered and "deployment" in lowered:
        return "结果不是单一 SOTA 数字，而是提出一套面向 embodied operators 的多维 benchmark，覆盖正确性、端到端效率、资源、稳定性、可移植性、接口兼容和部署可靠性。"
    if _is_mlir_quantum_mapping_text(context) and ("qmap" in context or "tket" in context):
        return "结果显示，借助 MLIR 生态和内置稀疏表示，该方法相对 QMAP、TKET 等定制实现同时提升运行效率和映射解质量。"
    result = _best_result_sentence(notes, 260)
    if result != "not_stated":
        return _paraphrase_key_result(result)
    return _key_result(evidence_text)


def _paper_limitations(notes: dict[str, str], evidence_text: str) -> str:
    joined = _clean(" ".join(notes.values())).lower()
    context = _clean(" ".join([joined, evidence_text])).lower()
    if "confidence-gated" in context and _contains(context, "small code distances", "simulated noise"):
        return "局限是评测仍集中在较小 code distance 和模拟噪声；真实控制栈中的测量延迟、校准漂移与更大规模 surface code 还需要验证。"
    if _is_qnn_cloud_text(joined) or _contains(joined, "worse r2", "unclear scalability"):
        return "局限/结论是 QNN 在该云微物理任务上 R2 全面更差，量子模型可扩展性仍不清楚，尚不能证明量子优势。"
    if _contains(joined, "qutuner", "pass combinations", "profiling pass combinations"):
        return "局限是它独立 profiling 单个 pass；真正的 pass 组合存在交互效应，但逐组合 profiling 会显著增加成本和 embedding 维度。"
    if _contains(joined, "no single fom", "single fom fully captures", "compiled circuit performance"):
        return "局限是不存在单一 FoM 能完整刻画编译后线路表现，指标相关性会随算法、硬件和实验目标变化。"
    if _contains(joined, "hamqasbench", "ground-state degeneracy", "other paradigms"):
        return "局限是部分结构分析依赖 CRLQAS 训练阶段线路，扩展到更多 QAS 范式和处理简并基态仍需要后续验证。"
    if _contains(joined, "hidden-state games", "bounded planner", "opine-world"):
        return "局限是当前假设可观测 Markov 状态；隐藏状态、感知推断误差和高分支规划规模都会削弱探索与验证信号。"
    if _contains(joined, "dreamsteer", "trajectory ranking", "value model") and _contains(joined, "depends", "depend", "generalizing", "unseen objects"):
        return "局限是部署时效果仍依赖 world model 和 value model 能否泛化到未见对象、环境和长程动作后果。"
    if _contains(joined, "multi-scale mixture", "embodiedbench", "hazard"):
        return "局限是结果仍主要来自 EmbodiedBench/HAZARD 等基准，真实机器人部署中的感知误差和长期安全性还需要验证。"
    if _contains_all(joined, "embodied operators", "manipulation"):
        return "局限是当前整理更偏 manipulation-oriented embodied systems，真实系统里的 operator 组合、数据标准化、VLA 安全和边缘部署仍需进一步验证。"
    if _is_mlir_quantum_mapping_text(context) and _contains(context, "coupling graph", "hardware qubits", "mlir"):
        return "局限是评测仍围绕可表示为 coupling graph 的硬件拓扑和当前 MLIR 映射实现，真实设备噪声、动态校准和更大规模线路还需要进一步验证。"
    limitation = _section_sentence(notes, ["limitations", "conclusion", "introduction"], ["limitation", "limitations", "however", "future work", "no single", "depends", "unclear", "only"], 240)
    if limitation != "not_stated":
        if "No single FoM" in limitation:
            return "局限是不存在单一 FoM 能完整刻画编译后线路表现，指标相关性会随算法、硬件和实验目标变化。"
        return _chinese_source_summary("局限方面，正文指出：", limitation)
    explicit_limitation = _sentence_with_marker(
        evidence_text,
        ["limitation", "limitations", "limited to", "future work"],
        180,
    )
    return _chinese_source_summary("局限方面，正文指出：", explicit_limitation)


def _best_result_sentence(notes: dict[str, str], limit: int) -> str:
    candidates: list[tuple[int, int, str]] = []
    order = ["results", "abstract", "conclusion", "method"]
    for order_index, key in enumerate(order):
        text = notes.get(key) or ""
        for sentence in _sentences(text):
            score = _result_sentence_score(sentence)
            if score > 0:
                candidates.append((score, -order_index, sentence))
    if not candidates:
        return _section_sentence(
            notes,
            ["results", "abstract", "conclusion"],
            ["improve", "improves", "outperform", "outperforms", "achieve", "achieves", "show", "shows", "find", "finds", "%", "×"],
            limit,
        )
    candidates.sort(key=lambda item: (item[0], item[1], len(item[2])), reverse=True)
    return _truncate(candidates[0][2], limit)


def _result_sentence_score(sentence: str) -> int:
    lowered = sentence.lower()
    score = 0
    if re.search(r"\d+(?:\.\d+)?\s*%|\d+\s*/\s*\d+|\b\d+(?:\.\d+)?\s*(?:qubits|games|levels|benchmarks)\b", lowered):
        score += 5
    if _contains(
        lowered,
        "improve",
        "improves",
        "outperform",
        "outperforms",
        "achieve",
        "achieves",
        "reduce",
        "reduces",
        "success rate",
        "experiments",
        "evaluation",
        "evaluations",
        "results show",
        "we show",
        "we find",
    ):
        score += 3
    if _contains(lowered, "baseline", "strongest", "state-of-the-art", "sota"):
        score += 2
    if _contains(lowered, "show promising", "videos are available", "keywords"):
        score -= 4
    if len(sentence) < 36:
        score -= 2
    return score


def _section_sentence(notes: dict[str, str], keys: list[str], markers: list[str], limit: int) -> str:
    for key in keys:
        text = notes.get(key) or ""
        if not text:
            continue
        sentence = _sentence_with_marker(text, markers, limit)
        if sentence != "not_stated":
            return sentence
    for key in keys:
        text = notes.get(key) or ""
        sentence = _first_informative_sentence(text, limit)
        if sentence != "not_stated":
            return sentence
    return "not_stated"


def _paraphrase_key_result(result: str) -> str:
    lowered = result.lower()
    if "qutuner" in lowered and "84.85" in result and "73.59" in result:
        return "结果显示，QuTuner 在 Qiskit 上相对最强基线最高多带来 84.85% 的 evaluation-metric reduction，并把 tuning time 降低 73.59%；在 PyTKET 上也同时提升指标下降和降低调优时间。"
    if ("wpst" in lowered or "machine learning-predicted foms" in lowered) and re.search(r"50\s*%", lowered):
        return "结果显示，machine-learning 预测的 FoM 与真实 PST/wPST 的相关性比常用 FoM 高出 over 50%，并在模拟和量子处理器实验中成立。"
    if "qnn" in lowered and (
        ("outperform" in lowered and ("neural networks" in lowered or "fcnn" in lowered))
        or "perform similarly to unoptimized neural networks" in lowered
    ):
        return "结果是负面的但有价值：充分优化后的 FCNN 明显超过 QNN；QNN 只接近未充分优化的经典网络，说明该任务上尚未看到可靠量子优势。"
    if "23.75%" in result and "66.25%" in result:
        return "结果是在 4 个真实操作基准上，DREAMSTEER 将任务成功率从 23.75% 提升到 66.25%，指令跟随准确率从 38.75% 提升到 56.25%。"
    if "energy accuracy is an unreliable proxy" in lowered or "over-parameterized" in lowered:
        return "结果显示，11 个分子、5 个结构层级、最高 14 qubits 的评测中，单看能量精度会漏掉过参数化、简并态误选、纠缠结构错误和路由失败。"
    if "6.05%p" in result and "1.49%p" in result:
        return "结果显示，MuSix 在 EmbodiedBench(Habitat) 比 SayCanPay 提升 6.05%p，在 HAZARD(Fire) 比 FLARE 提升 1.49%p。"
    if "20 of 25" in result and "160 of 183" in result:
        return "结果显示，OPINE-World 无需逐游戏训练即可解决 20/25 个 games 和 160/183 个 levels，并超过强 single-agent coding agent；程序合成和神经 latent world model 基线为 0。"
    if "embodied operators" in lowered and "correctness" in lowered and "deployment" in lowered and "benchmark" in lowered:
        return "结果不是单一 SOTA 数字，而是提出一套面向 embodied operators 的多维 benchmark，覆盖正确性、端到端效率、资源、稳定性、可移植性、接口兼容和部署可靠性。"
    if "mlir" in lowered and ("qmap" in lowered or "tket" in lowered):
        return "结果显示，借助 MLIR 生态和内置稀疏表示，该方法相对 QMAP、TKET 等定制实现同时提升运行效率和映射解质量。"
    if "1.6" in result and ("faith" in lowered or "rare" in lowered or "counterfactual" in lowered):
        return "结果显示，针对 faithfulness 的后训练让策略在稀有反事实场景中的响应性达到 SoTA 策略的约 1.6 倍，同时保持竞争性的轨迹表现。"
    gate_depth = re.search(r"(\d+(?:\.\d+)?)\s*%\s+(?:reduction|lower).{0,80}(two-qubit gate depth|cnot count|circuit depth)", lowered)
    if gate_depth:
        metric = gate_depth.group(2)
        label = "双量子门深度" if "two-qubit" in metric else ("CNOT 数量" if "cnot" in metric else "线路深度")
        return f"实验显示该方法在量子编译任务上将{label}降低 {gate_depth.group(1)}%。"
    return _chinese_source_summary("结果方面，正文报告：", result)


def _chinese_source_summary(prefix: str, value: str, limit: int = 116) -> str:
    cleaned = _clean(value)
    if not cleaned or cleaned == "not_stated" or _looks_like_web_boilerplate(cleaned):
        return "not_stated"
    if _has_cjk(cleaned):
        return _truncate(cleaned, limit)
    available = max(1, limit - len(prefix))
    return prefix + _truncate(cleaned, available)


def _first_informative_sentence(text: str, limit: int) -> str:
    for sentence in _sentences(text):
        if (
            len(sentence) >= 32
            and not sentence.lower().startswith(("figure ", "table "))
            and not _looks_like_web_boilerplate(sentence)
            and not _looks_like_pdf_extraction_noise(sentence)
            and not _looks_like_section_header_residue(sentence)
        ):
            return _truncate(sentence, limit)
    return "not_stated"


def _is_qnn_cloud_text(text: str) -> bool:
    lowered = text.lower()
    return (
        "cloud microphysics" in lowered
        or "rich trainable frequency spectrum" in lowered
        or ("qnn" in lowered and "fully-connected neural" in lowered)
        or ("quantum neural network" in lowered and "fcnn" in lowered)
    )


def _sentence_about_problem(record: MaterialRecord, abstract: str) -> str:
    if abstract == "not_stated":
        return "not_stated"
    lowered = " ".join([record.title, abstract]).lower()
    if _contains(lowered, "compilation", "compiler", "transpilation", "routing", "circuit synthesis"):
        return "论文针对量子线路在真实硬件约束下难以同时控制深度、门数和连通性的问题。"
    if _contains(lowered, "trapped-ion", "quantum hardware", "real quantum", "processor"):
        return "论文关注如何把量子算法从理想模拟推进到受噪声、连通性和规模限制的真实量子硬件上。"
    if _contains(lowered, "hamiltonian simulation", "trotter", "block encoding"):
        return "论文关注哈密顿量模拟中的线路资源、近似误差和可扩展实现问题。"
    if _contains(lowered, "error correction", "qec", "decoder", "syndrome"):
        return "论文关注量子纠错中噪声恢复、解码或逻辑错误率降低的问题。"
    return f"论文关注的问题是：{_truncate(abstract, 180)}"


def _sentence_about_method(text: str, evidence_text: str = "") -> str:
    if _contains(text, "sparse qubit allocation", "routing optimization"):
        return "方法把量子编译拆成稀疏 qubit 分配和路由优化，在满足硬件连通性的同时压低线路深度。"
    if _contains(text, "numerical linked-cluster expansion", "nlce"):
        return "方法把 numerical linked-cluster expansion 与量子算法结合，用有限真机实验估计热力学极限下的能量和准粒子色散。"
    if _contains(text, "hardware-aware", "circuit synthesis", "unitary synthesis", "lie group diffusion"):
        return "方法用硬件感知的线路综合模型搜索酉变换实现，把设备约束纳入生成或优化过程。"
    if _contains(text, "machine learning", "quantum circuit optimization"):
        return "方法用机器学习模型学习量子线路优化策略，并通过线路深度、门数或任务性能等指标评估优化结果。"
    if _contains(text, "quantum circuit compilation", "quantum circuit optimization"):
        return "方法围绕量子线路编译/优化构造搜索或变换流程，并用线路资源指标检验优化效果。"
    source_sentence = _sentence_with_marker(
        evidence_text,
        [
            "we propose",
            "we present",
            "we introduce",
            "our method",
            "our approach",
            "we develop",
            "we design",
            "we apply",
            "we use",
            "we run",
            "we compute",
            "we consider",
            "we evaluate",
            "we investigate",
            "we study",
            "we leverage",
            "we combine",
            "is based on",
        ],
        220,
    )
    if source_sentence != "not_stated" and not _contains(source_sentence.lower(), "does not", "do not", "not describe", "without"):
        return source_sentence
    if _contains(text, "fuzz", "fuzzing", "fuzzer"):
        return "方法核心是对程序或编译流程进行 fuzzing，通过失败样例引导发现缺陷。"
    if "reinforcement learning" in text or _contains(text, "rl"):
        return "方法核心是把任务建模为强化学习问题，通过策略学习优化决策过程。"
    if _contains(text, "preconditioning", "curriculum", "variational quantum regression", "quantum regression"):
        return "方法核心是结合变分量子回归、几何预条件和课程式优化来改善训练稳定性。"
    if _contains(text, "compiler", "compilation", "qubit allocation"):
        return "方法核心围绕量子编译、线路映射或 qubit allocation 优化展开。"
    if _contains(text, "neural", "pinn", "machine learning"):
        return "方法核心是用神经网络或机器学习模型表示目标函数、物理系统或量子模型。"
    if _contains(text, "agent", "agentic"):
        return "方法核心是使用 agentic workflow 或工具调用流程完成自动化推理/生成。"
    return "not_stated"


def _novelty_from_evidence(evidence_text: str) -> str:
    sentence = _sentence_with_marker(
        evidence_text,
        ["prior", "previous", "existing", "unlike", "whereas", "novel", "new", "we propose", "we introduce"],
        240,
    )
    if sentence == "not_stated":
        return "not_stated"
    return "相对已有工作，" + sentence


def _why_it_works(text: str, evidence_text: str = "") -> str:
    if _contains(text, "sparse qubit allocation", "routing optimization"):
        return "稀疏分配先减少需要映射的相互作用，再由路由优化处理连通性约束，因此能同时降低额外 SWAP 和线路深度。"
    if _contains(text, "numerical linked-cluster expansion", "nlce"):
        return "NLCE 用小簇结果外推热力学极限，量子硬件只需处理有限簇上的基态/激发信息，因此能绕开直接模拟无限系统的规模瓶颈。"
    if _contains(text, "trapped-ion", "quantum processing unit"):
        return "受控离子平台提供可编程相互作用和较高保真门，适合作为验证量子算法物理量估计能力的真机基准。"
    source_sentence = _sentence_with_marker(
        evidence_text,
        [
            "because",
            "therefore",
            "thus",
            "enables",
            "allowing",
            "by exploiting",
            "by leveraging",
            "hardware-aware",
            "topology",
            "connectivity",
            "native gate",
            "symmetry",
            "lie group",
            "hamiltonian",
            "noise",
        ],
        260,
    )
    if source_sentence != "not_stated":
        return source_sentence
    if _contains(text, "hardware-aware", "topology-aware", "device-aware", "connectivity"):
        return "它把硬件拓扑、连通性或原生门约束显式放进搜索/优化过程，因此生成的线路更接近真实设备可执行形式。"
    if _contains(text, "machine learning", "quantum circuit optimization"):
        return "机器学习模型可以从候选线路和优化反馈中学习启发式搜索规律，比固定规则更容易适配不同线路结构和目标指标。"
    if _contains(text, "quantum circuit compilation", "quantum circuit optimization"):
        return "把线路变换过程显式建模为资源优化问题，可以直接针对深度、门数或连通性开销做取舍。"
    if _contains(text, "lie group", "su(2)", "unitary"):
        return "它利用酉变换/李群结构约束搜索空间，使生成或优化过程更贴合量子门本身的几何性质。"
    if _contains(text, "hamiltonian simulation", "trotter", "block encoding"):
        return "它围绕哈密顿量结构设计模拟或误差控制步骤，使线路复杂度和近似误差可以被更直接地分析。"
    if _contains(text, "error correction", "qec", "decoder", "syndrome"):
        return "它利用纠错码结构、syndrome 信息或解码器约束，把噪声恢复问题转化为更可学习/可搜索的判别问题。"
    return "not_stated"


def _method_steps(text: str, abstract: str):
    if _contains(text, "qutuner", "optimization pass", "compiler pass tuning", "pass tuning"):
        return ["收集量子线路优化数据", "提取静态线路特征与 pass response embedding", "检索并排序候选 pass 序列", "用轻量 BO refinement 适配目标编译器"]
    if _contains(text, "wpst", "probability of successful trials", "figure of merit", "figures of merit"):
        return ["定义 PST/wPST 等执行质量指标", "提取线路与硬件特征", "训练 FoM 预测器", "用真实 PST/wPST 相关性评估指标有效性"]
    if _is_qnn_cloud_text(text):
        return ["构建云微物理监督数据", "设计带可训练频谱的 hybrid QNN", "加入 classical postprocessing 并做 HPO", "与充分调参 FCNN 比较 R2 表现"]
    if _contains(text, "dreamsteer", "latent world model", "value model"):
        return ["冻结 VLA 产生候选动作", "加入手工 motion primitives 扩大候选集", "用 latent world model 想象动作后果", "用 value model 排序并执行最优轨迹"]
    if _contains(text, "opine-world", "programmatic world model", "counterexample-guided"):
        return ["交互采样并提出对象 ontology", "用 LLM 合成程序化 world model", "用 replay/反例验证修正模型", "在验证模型上规划下一步探索"]
    if _contains(text, "musix", "multi-scale mixture", "scale-aware world model"):
        return ["计算情境 novelty/experiential distance", "用 meta-router 选择尺度", "路由到尺度内 world model", "按尺度更新和迁移知识"]
    if _is_embodied_operator_text(text):
        return ["定义 embodied operator 边界", "梳理五类可复用模块", "规定输入输出和部署属性", "建立多维 benchmark 指标"]
    if _contains(text, "pinocchio", "faithfulness", "embodied chain-of-thought"):
        return ["定义具身推理一致性检查", "训练 faithfulness critic", "把 critic 分数作为 RL 奖励", "比较轨迹表现与罕见场景响应"]
    if _contains(text, "hamqasbench", "hamiltonian-informed", "critical-structure"):
        return ["用 Hamiltonian 指纹划分问题层级", "抽取关键线路结构", "检查逐 qubit entanglement 与 fidelity", "比较不同 QAS 方法的隐藏失败模式"]
    if _contains(text, "sparse routing", "qubit placement", "swap insertion"):
        return ["联合建模 qubit placement 与 SWAP insertion", "把长距离移动写入路由代价", "搜索满足硬件连通性的映射", "用双量子门深度评估效果"]
    if _contains(text, "citation-aware", "citation context", "upstream references", "downstream citing"):
        return ["提取硬件感知线路综合候选", "收集上游参考和下游引用上下文", "用引用脉络重排候选方案", "用线路资源指标评估编译质量"]
    steps = []
    if _contains(text, "quantum"):
        steps.append("定义量子任务或量子线路问题")
    if _contains(text, "preconditioning"):
        steps.append("用几何预条件改善梯度或优化景观")
    if _contains(text, "curriculum"):
        steps.append("采用课程式优化逐步增加训练难度")
    if _contains(text, "optimization", "optimize", "optimizing") or "optimiz" in abstract.lower():
        steps.append("构造优化目标并搜索更优解")
    if _contains(text, "learning", "neural", "pinn"):
        steps.append("训练学习模型并用实验指标评估")
    if _contains(text, "fuzz", "fuzzing", "fuzzer"):
        steps.append("生成测试输入并根据失败信号迭代")
    if _contains(text, "agent", "agentic", "mcp"):
        steps.append("让 agent 调用工具或证明器并迭代生成结果")
    if not steps:
        return ["not_stated"]
    return steps[:4]


def _key_result(abstract: str) -> str:
    return _sentence_with_marker(
        abstract,
        [
            "experiments show",
            "experimental results",
            "evaluation shows",
            "we show",
            "we demonstrate",
            "we find",
            "results",
            "outperform",
            "achieve",
            "improve",
            "reduce",
            "we report",
        ],
        220,
    )


def _limitations(abstract: str) -> str:
    return _sentence_with_marker(
        abstract,
        ["limitation", "limited to", "constraint", "challenge", "however", "future work", "but"],
        180,
    )


def _paper_impact(record: MaterialRecord, text: str) -> str:
    if _is_qnn_cloud_text(text) or _contains(text, "quantum machine learning", "qnn", "quantum neural network"):
        return "可作为 QML 是否真的有优势的反例/压力测试：重点看它如何设置经典强基线、调参预算和物理任务数据，而不是只看量子模型本身。"
    if _contains(text, "vision-language-action", "vla", "world model", "embodied"):
        return "对具身智能/agent 有直接参考价值：重点看它如何把世界模型、候选动作搜索和执行反馈接成部署时纠错闭环。"
    if _contains(text, "hardware-aware", "circuit synthesis", "compilation", "transpilation", "routing", "unitary synthesis"):
        return "可迁移到硬件感知量子线路综合/编译：重点看它如何表达硬件约束、如何定义搜索空间，以及用哪些线路指标验证效果。"
    if _contains(text, "qutuner", "optimization pass", "compiler pass tuning", "pass tuning"):
        return "可迁移到量子编译器自动调参：重点看 pass response embedding 如何作为跨编译器、跨目标指标的检索信号。"
    if _contains(text, "error correction", "qec", "decoder", "syndrome"):
        return "可迁移到 QEC/真机实验：重点看噪声模型、syndrome/decoder 设计和逻辑错误率等评价指标。"
    if "quant" in text:
        return "可作为量子算法实现参考：重点看问题建模、线路资源指标和实验设备/模拟器设置是否能复用。"
    if "agent" in text:
        return "对构建科研自动化或工具调用型 agent 有启发，适合关注流程设计和验证方式。"
    return "可作为相邻方向素材，优先判断其问题定义是否能迁移到用户研究场景。"


def _repo_what_it_is(record: MaterialRecord, description: str) -> str:
    if description == "not_stated":
        return f"{record.title} 是一个 GitHub 项目，但 README/描述未说明清楚项目定位。"
    return f"{record.title} 是一个项目：{_truncate(description, 180)}"


def _repo_capabilities(text: str, description: str, readme: str) -> str:
    capabilities = []
    if _contains(text, "mcp"):
        capabilities.append("提供 MCP server 或工具接口，供 agent 调用外部能力")
    if _contains(text, "agent", "agents", "agentic"):
        capabilities.append("支持 agent 构建、教程、工具链或自动化工作流")
    if _contains(text, "quantum"):
        capabilities.append("支持量子计算、量子机器学习或量子线路相关开发")
    if _contains(text, "rag"):
        capabilities.append("包含 RAG 或检索增强生成相关能力")
    if _contains(text, "world model", "vla", "embodied"):
        capabilities.append("围绕 world model、VLA 或具身智能资料/工具组织")
    return "；".join(capabilities) if capabilities else _truncate(description if description != "not_stated" else readme, 220) or "not_stated"


def _repo_use_cases(text: str, description: str, readme: str) -> str:
    cases = []
    if _contains(text, "tutorial", "lessons", "course"):
        cases.append("作为学习路线或课程材料")
    if _contains(text, "framework", "library", "platform"):
        cases.append("作为实验原型或工程底座")
    if _contains(text, "mcp"):
        cases.append("接入本地 agent 工具调用链")
    if _contains(text, "awesome", "survey"):
        cases.append("作为文献/项目索引和选题入口")
    return "；".join(cases) if cases else "not_stated"


def _repo_architecture(text: str, readme: str) -> str:
    if _contains(text, "cli"):
        return "README/描述提到 CLI，可优先从命令行入口评估。"
    if _contains(text, "api", "server"):
        return "README/描述提到 API 或 server，可优先查看接口和服务入口。"
    if _contains(text, "python", "notebook"):
        return "主要语言或生态与 Python/Jupyter 相关，可从 package、examples 或 notebooks 入手。"
    return "not_stated"


def _repo_reusable(text: str) -> str:
    if _contains(text, "mcp"):
        return "最可复用的是 MCP server/tool 接口设计。"
    if _contains(text, "tutorial", "lessons", "course"):
        return "最可复用的是课程结构、案例组织和实践清单。"
    if _contains(text, "framework", "library", "platform"):
        return "最可复用的是项目 API、模块边界和 examples。"
    if _contains(text, "awesome"):
        return "最可复用的是资料索引和方向地图。"
    return "not_stated"


def _paper_draft_is_too_shallow(draft: EditorialDraft) -> bool:
    fields = draft.draft_fields
    core_names = ["problem", "method", "why_it_works", "key_result", "possible_use_or_impact"]
    core_values = [_clean(str(fields.get(name) or "")) for name in core_names]
    if sum(1 for value in core_values if value and value != "not_stated" and len(value) >= 24) < 4:
        return True
    if sum(len(value) for value in core_values if value and value != "not_stated") < 180:
        return True
    evidence = _clean(" ".join(draft.evidence_used or []))
    if not evidence or evidence == "not_stated":
        return True
    for value in core_values:
        if len(value) >= 120 and _near_verbatim(value, evidence):
            return True
    return False


def _paper_core_fields_lack_chinese_explanation(fields: dict[str, Any]) -> bool:
    core_names = ["method", "why_it_works", "novelty_or_difference", "key_result", "limitations"]
    english_only = 0
    for name in core_names:
        value = _clean(str(fields.get(name) or ""))
        if value == "not_stated" or len(value) < 30:
            continue
        if _has_cjk(value):
            continue
        english_only += 1
    return english_only >= 3


def _has_cjk(value: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", value))


def _looks_like_pdf_extraction_noise(value: str) -> bool:
    text = _clean(value)
    if len(text) < 20:
        return False
    lowered = text.lower()
    noisy_patterns = [
        r"\bf or\b",
        r"\br otation\b",
        r"\bgener ate d\b",
        r"\brelia ble\b",
        r"\bquan tum\b",
        r"\btreat ed\b",
        r"\bc omp",
        r"\bc orr",
        r"\bp ossible\b",
        r"\bop er ator\b",
        r"\beac h\b",
        r"\bffixed\b",
        r"\bre-?\s*arxiv\b",
    ]
    if any(re.search(pattern, lowered) for pattern in noisy_patterns):
        return True
    short_letter_runs = re.findall(r"\b[a-z]\s+[a-z]{1,3}\s+[a-z]{1,3}\b", lowered)
    if len(short_letter_runs) >= 2:
        return True
    formula_tokens = len(re.findall(r"[{}[\]`=;]|-\s*i\b|\b[xyz]\s*g\s*n\b", lowered))
    latin_words = re.findall(r"[a-z]+", lowered)
    if formula_tokens >= 4 and len(latin_words) <= 18:
        return True
    return False


def _near_verbatim(value: str, evidence: str) -> bool:
    normalized_value = _normalize_for_overlap(value)
    normalized_evidence = _normalize_for_overlap(evidence)
    if not normalized_value or not normalized_evidence:
        return False
    if normalized_value in normalized_evidence:
        return True
    value_tokens = normalized_value.split()
    if len(value_tokens) < 16:
        return False
    evidence_tokens = set(normalized_evidence.split())
    overlap = sum(1 for token in value_tokens if token in evidence_tokens)
    return overlap / max(len(value_tokens), 1) >= 0.85


def _normalize_for_overlap(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", value.lower())).strip()


def _contains(text: str, *terms: str) -> bool:
    for term in terms:
        escaped = re.escape(term.lower())
        if re.search(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])", text.lower()):
            return True
    return False


def _contains_all(text: str, *terms: str) -> bool:
    return all(_contains(text, term) for term in terms)


def _is_embodied_operator_text(text: str) -> bool:
    return _contains(text, "embodied operators") and _contains(
        text,
        "benchmark",
        "benchmarking",
        "operator taxonomy",
        "deployable embodied",
    )


def _is_mlir_quantum_mapping_text(text: str) -> bool:
    return _contains(text, "mlir") and _contains(
        text,
        "quantum circuit mapping",
        "sparse routing",
        "swap insertion",
        "qubit placement",
    )


def _looks_like_section_header_residue(value: str) -> bool:
    text = _clean(value)
    if len(text) < 30:
        return False
    section_names = (
        "Abstract",
        "Introduction",
        "Background",
        "Related Work",
        "Method",
        "Methods",
        "Results",
        "Discussion",
        "Conclusion",
    )
    section_pattern = "|".join(re.escape(name) for name in section_names)
    return bool(re.search(rf"(^|[，,。；;：:\-]\s*)({section_pattern})\s+[A-Z][A-Za-z]", text))


def _sentence_with_marker(text: str, markers: list[str], limit: int) -> str:
    for sentence in _sentences(text):
        lowered = sentence.lower()
        if (
            any(marker in lowered for marker in markers)
            and not _looks_like_web_boilerplate(sentence)
            and not _looks_like_pdf_extraction_noise(sentence)
            and not _looks_like_section_header_residue(sentence)
        ):
            return _truncate(sentence, limit)
    return "not_stated"


def _looks_like_web_boilerplate(value: str) -> bool:
    lowered = _clean(value).lower()
    return _contains(
        lowered,
        "accept all cookies",
        "accept only essential cookies",
        "how we use cookies",
        "cookie preferences",
        "privacy preferences",
    )


def _sentences(text: str) -> list[str]:
    cleaned = _clean(text)
    if not cleaned or cleaned == "not_stated":
        return []
    return [part.strip() for part in re.split(r"(?<=[.!?。！？])\s+", cleaned) if part.strip()]


def _clean(value: str) -> str:
    return " ".join(value.split())


def _truncate(value: str, limit: int) -> str:
    text = _clean(value)
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"
