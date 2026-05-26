from __future__ import annotations

import json
import re
import subprocess
from datetime import date
from typing import Any

from daily_agent.config import AppConfig
from daily_agent.connectors.github import enrich_github_readmes
from daily_agent.models import ApprovedItem, EditorialDraft, EditorialReview, MaterialRecord

GENERIC_PHRASES = [
    "需进一步阅读确认",
    "与配置方向相关",
    "可作为后续阅读线索",
    "建议查看 README",
    "值得关注",
]

PAPER_FIELDS = [
    "problem",
    "method",
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


def build_shortlist(config: AppConfig, library: dict[str, MaterialRecord], run_date: date) -> list[MaterialRecord]:
    max_items = int(config.quota.get("max_items", 10))
    records = [record for record in library.values() if record.quality_status not in {"rejected", "archived"}]
    records.sort(key=lambda item: item.score, reverse=True)

    selected: list[MaterialRecord] = []
    selected_keys: set[str] = set()
    paper_target = int(config.quota.get("paper_target", 7))
    github_target = int(config.quota.get("github_target", 3))

    def take(item_type: str, limit: int) -> None:
        for record in records:
            if len([item for item in selected if item.item_type == item_type]) >= limit:
                return
            if len(selected) >= max_items:
                return
            if record.key not in selected_keys and record.item_type == item_type:
                selected.append(record)
                selected_keys.add(record.key)

    take("paper", paper_target)
    take("repo", github_target)
    for record in records:
        if len(selected) >= max_items:
            break
        if record.key not in selected_keys:
            selected.append(record)
            selected_keys.add(record.key)

    return enrich_github_readmes(selected)


def draft_report_items(config: AppConfig, shortlist: list[MaterialRecord], use_llm: bool = False) -> list[EditorialDraft]:
    if use_llm:
        llm_drafts = _draft_with_llm(shortlist)
        if llm_drafts:
            return llm_drafts
    return [_rule_draft(record) for record in shortlist]


def review_draft(config: AppConfig, drafts: list[EditorialDraft], use_llm: bool = False) -> list[EditorialReview]:
    reviews = [_rule_review(draft) for draft in drafts]
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
                approval_notes="总编终审通过：字段完整，证据可追溯，读者可判断是否精读。",
            )
        )
    return approved


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
        writer_notes="规则写手草稿：仅基于摘要、README 片段和元数据生成；未说明处标记 not_stated。",
    )


def _paper_fields(record: MaterialRecord) -> dict[str, Any]:
    abstract = _clean(_best_paper_abstract(record) or "not_stated")
    metadata_evidence = _paper_metadata_evidence(record)
    text = " ".join([record.title, abstract, " ".join(record.categories), metadata_evidence]).lower()
    problem = _sentence_about_problem(record, abstract)
    method = _sentence_about_method(text)
    method_steps = _method_steps(text, abstract)
    key_result = _key_result(abstract)
    limitations = _limitations(abstract)
    return {
        "problem": problem,
        "method": method,
        "method_steps": method_steps,
        "key_result": key_result,
        "technical_route": f"{method} 关键路径：{'; '.join(method_steps) if isinstance(method_steps, list) else method_steps}",
        "possible_use_or_impact": _paper_impact(record, text),
        "limitations": limitations,
        "evidence_from_source": _truncate(" ".join(_paper_evidence_used(record)), 520),
        "confidence": "medium" if abstract != "not_stated" else "low",
    }


def _paper_evidence_used(record: MaterialRecord) -> list[str]:
    evidence = []
    abstract = _best_paper_abstract(record)
    if abstract:
        evidence.append(_clean(abstract))
    metadata = _paper_metadata_evidence(record)
    if metadata:
        evidence.append(metadata)
    return evidence or ["not_stated"]


def _best_paper_abstract(record: MaterialRecord) -> str | None:
    if record.abstract:
        return record.abstract
    sources = (record.evidence or {}).get("sources", {}) or {}
    for source in ["arxiv", "openreview", "pmlr", "neurips", "openalex", "semantic_scholar", "ieee", "crossref"]:
        abstract = (sources.get(source) or {}).get("abstract")
        if abstract:
            return str(abstract)
    return None


def _paper_metadata_evidence(record: MaterialRecord) -> str:
    sources = (record.evidence or {}).get("sources", {}) or {}
    source_names = [source for source in ["arxiv", "openreview", "pmlr", "neurips", "openalex", "semantic_scholar", "crossref", "ieee"] if source in sources or source in record.source_aliases]
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
    return "；".join(parts)


def _first_source_value(sources: dict[str, Any], field: str) -> Any:
    for source in ["arxiv", "openreview", "pmlr", "neurips", "openalex", "semantic_scholar", "crossref", "ieee"]:
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
    if draft.item_type == "paper":
        if fields.get("method") == "not_stated":
            issues.append("论文 method 未说明")
            required_changes.append("补充论文 method；如果摘要确实未说明方法，本条不登刊")
            critical_issue = True
        if not fields.get("evidence_from_source"):
            issues.append("论文缺少来源证据")
            required_changes.append("补充 evidence_from_source")
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
    prompt = _llm_prompt(shortlist)
    try:
        result = subprocess.run(
            ["claude", "-p", prompt, "--output-format", "json"],
            check=True,
            capture_output=True,
            text=True,
            timeout=240,
        )
        payload = _load_llm_payload(result.stdout)
        return _validated_llm_drafts(payload, {record.key: record for record in shortlist})
    except Exception:
        return []


def _load_llm_payload(output: str) -> list[Any]:
    payload = json.loads(output.strip())
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
                writer_notes=writer_notes,
            )
        )
        seen.add(key)
    if seen != set(by_key):
        raise ValueError("LLM draft output must match shortlisted item keys")
    return drafts


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


def _llm_prompt(shortlist: list[MaterialRecord]) -> str:
    records = []
    for record in shortlist:
        records.append(
            {
                "key": record.key,
                "item_type": record.item_type,
                "title": record.title,
                "abstract": record.abstract,
                "repo_description": record.repo_description,
                "readme_excerpt": record.readme_excerpt,
                "metadata": {
                    "tags": record.tags,
                    "categories": record.categories,
                    "stars": record.stars,
                    "language": record.language,
                    "updated_at": record.source_updated_at,
                    "url": record.url,
                    "pdf_url": record.pdf_url,
                },
            }
        )
    return (
        "你是 Daily_Agent 编辑部写手。只基于输入素材写结构化草稿；没有来源依据就写 not_stated。"
        "论文字段必须包含 problem, method, method_steps, key_result, technical_route, possible_use_or_impact, limitations, evidence_from_source, confidence。"
        "GitHub 字段必须包含 what_it_is, core_capabilities, typical_use_cases, architecture_or_api, maturity_signal, reusable_point, evidence_from_source, confidence。"
        "禁止泛泛写：与配置方向相关、需进一步阅读确认、建议查看 README。"
        "返回 JSON 数组，每项包含 key, draft_fields, evidence_used, writer_notes。输入：\n"
        + json.dumps(records, ensure_ascii=False)
    )


def _sentence_about_problem(record: MaterialRecord, abstract: str) -> str:
    if abstract == "not_stated":
        return "not_stated"
    return f"论文关注的问题是：{_truncate(abstract, 180)}"


def _sentence_about_method(text: str) -> str:
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


def _method_steps(text: str, abstract: str):
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
    if "quant" in text:
        return "对用户的量子线路优化、量子编译或量子机器学习方向有参考价值，可优先查看方法定义和实验设置。"
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


def _contains(text: str, *terms: str) -> bool:
    for term in terms:
        escaped = re.escape(term.lower())
        if re.search(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])", text.lower()):
            return True
    return False


def _sentence_with_marker(text: str, markers: list[str], limit: int) -> str:
    for sentence in _sentences(text):
        lowered = sentence.lower()
        if any(marker in lowered for marker in markers):
            return _truncate(sentence, limit)
    return "not_stated"


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
