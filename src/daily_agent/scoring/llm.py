from __future__ import annotations

import json
import subprocess

from daily_agent.config import AppConfig, load_config
from daily_agent.editorial import _llm_writer_command, _llm_writer_settings
from daily_agent.models import DigestItem


def summarize_items(
    items: list[DigestItem],
    enabled: bool = True,
    timeout_seconds: int | None = None,
    config: AppConfig | None = None,
) -> list[DigestItem]:
    if not enabled or not items:
        return apply_rule_summaries(items)
    settings = _llm_writer_settings(config or load_config())
    timeout = int(timeout_seconds or settings["timeout_seconds"])
    prompt = _build_prompt(items)
    try:
        result = subprocess.run(
            _llm_writer_command(settings, prompt),
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        payload = _parse_llm_output(result.stdout)
        if not payload:
            return apply_rule_summaries(items)
        _apply_llm_payload(items, payload)
    except Exception:
        apply_rule_summaries(items)
    return items


def apply_rule_summaries(items: list[DigestItem]) -> list[DigestItem]:
    for item in items:
        if item.item_type == "paper":
            abstract = item.abstract or "暂无摘要。"
            item.summary_zh = item.summary_zh or _paper_summary(item, abstract)
            item.technical_route = item.technical_route or _paper_route(item, abstract)
            item.possible_use_or_impact = item.possible_use_or_impact or _paper_impact(item)
            item.recommendation_reason = item.recommendation_reason or _paper_reason(item)
        else:
            description = item.repo_description or "暂无项目描述。"
            item.summary_zh = item.summary_zh or _repo_summary(item, description)
            item.reusable_point = item.reusable_point or _repo_reusable_point(item)
            item.recommendation_reason = item.recommendation_reason or _repo_reason(item)
    return items


def _build_prompt(items: list[DigestItem]) -> str:
    compact_items = []
    for item in items:
        compact_items.append(
            {
                "id": item.id,
                "source": item.source,
                "item_type": item.item_type,
                "title": item.title,
                "abstract": item.abstract,
                "repo_description": item.repo_description,
                "categories": item.categories,
                "tags": item.all_tags,
                "stars": item.stars,
                "language": item.language,
                "url": item.url,
                "pdf_url": item.pdf_url,
            }
        )
    return (
        "你是克制、准确的研究助理。请只基于输入中的摘要、README 描述和元数据生成中文日报字段；"
        "不要调用任何工具、不要读取本地文件、不要联网。"
        "推荐理由可以结合用户研究方向：量子+AI、量子线路优化/编译、量子错误缓解/纠错、QNN、world model、VLA、具身智能、无人机导航、自动驾驶、LLM/Agent。"
        "不要输出英文摘要。返回严格 JSON 数组，每项包含 id、summary_zh、technical_route、possible_use_or_impact、reusable_point、recommendation_reason、llm_tags。"
        "如果字段不适用，用空字符串。输入：\n"
        + json.dumps(compact_items, ensure_ascii=False)
    )


def _parse_llm_output(stdout: str) -> list[dict]:
    raw = stdout.strip()
    if not raw:
        return []
    data = json.loads(raw)
    if isinstance(data, dict) and "result" in data:
        result = data["result"]
        try:
            return json.loads(result)
        except Exception:
            return []
    if isinstance(data, list):
        return data
    return []


def _apply_llm_payload(items: list[DigestItem], payload: list[dict]) -> None:
    by_id = {item.id: item for item in items}
    for raw in payload:
        item_id = str(raw.get("id", ""))
        item = by_id.get(item_id)
        if not item:
            continue
        item.summary_zh = raw.get("summary_zh") or item.summary_zh
        item.technical_route = raw.get("technical_route") or item.technical_route
        item.possible_use_or_impact = raw.get("possible_use_or_impact") or item.possible_use_or_impact
        item.reusable_point = raw.get("reusable_point") or item.reusable_point
        item.recommendation_reason = raw.get("recommendation_reason") or item.recommendation_reason
        llm_tags = raw.get("llm_tags") or []
        if isinstance(llm_tags, list):
            item.llm_tags = [str(tag) for tag in llm_tags if str(tag).strip()][:5]
    apply_rule_summaries(items)


def _paper_summary(item: DigestItem, abstract: str) -> str:
    lead = "这篇论文"
    if _has_quantum_signal(item):
        lead += "围绕量子计算或量子机器学习问题展开"
    elif "agent" in _text(item):
        lead += "关注 agentic AI 或自动化科研流程"
    else:
        lead += "与配置的研究关键词或 arXiv 分类相关"
    return f"{lead}。摘要要点：{_truncate(abstract, 180)}"


def _paper_route(item: DigestItem, abstract: str) -> str:
    text = _text(item) + " " + abstract.lower()
    if "reinforcement learning" in text or "rl" in text:
        return "主要技术路线包含强化学习建模或策略优化，需要进一步阅读 PDF 核对状态、动作、奖励和实验设置。"
    if "compiler" in text or "compilation" in text or "qubit allocation" in text:
        return "主要技术路线与量子编译、线路映射或资源分配相关，需要重点核对问题定义和优化目标。"
    if "neural" in text or "pinn" in text or "machine learning" in text:
        return "主要技术路线与神经网络或机器学习建模相关，需要核对模型结构、训练数据和评价指标。"
    return "主要技术路线需结合 PDF 进一步确认；当前可从标题、摘要和分类判断其与配置方向相关。"


def _paper_impact(item: DigestItem) -> str:
    if _has_quantum_signal(item):
        return "可作为量子线路设计、优化、编译或量子机器学习方向的阅读线索，适合优先核对方法细节。"
    if "agent" in _text(item):
        return "可为科研自动化、agent 工作流或工具调用设计提供参考。"
    return "可作为相邻研究方向的启发材料，建议先阅读摘要和实验部分判断精读价值。"


def _paper_reason(item: DigestItem) -> str:
    if item.is_historical_supplement:
        return "该条目作为历史补充进入日报，说明其相关性或影响力高于当天普通候选。"
    return "该条目命中了配置的研究关键词或 arXiv 分类，并在当前候选中排序靠前。"


def _repo_summary(item: DigestItem, description: str) -> str:
    if _has_quantum_signal(item):
        return f"这是一个量子计算或量子机器学习相关项目。项目描述：{_truncate(description, 150)}"
    if "agent" in _text(item) or "mcp" in _text(item):
        return f"这是一个 agent、MCP 或自动化工具相关项目。项目描述：{_truncate(description, 150)}"
    return f"这是一个与配置探索方向相关的 GitHub 项目。项目描述：{_truncate(description, 150)}"


def _repo_reusable_point(item: DigestItem) -> str:
    text = _text(item)
    if "framework" in text or "library" in text or "platform" in text:
        return "优先评估其 API、示例和模块边界，判断能否复用为实验或原型开发底座。"
    if "mcp" in text:
        return "优先评估其 MCP server/工具接口设计，判断是否能接入自己的 agent 工作流。"
    if "tutorial" in text or "lessons" in text:
        return "优先复用其案例组织方式和实践清单，而不是直接复用代码。"
    return "优先查看 README、examples 和核心模块，判断是否存在可直接迁移的实现模式。"


def _repo_reason(item: DigestItem) -> str:
    if item.update_label == "major_update":
        return "该项目因检测到重大更新重新进入日报。"
    if item.is_historical_supplement:
        return "该项目作为历史补充进入日报，说明其相关性或成熟度高于普通候选。"
    return "该项目命中了配置的 GitHub 查询词、主题或 Trending 信号，并在当前候选中排序靠前。"


def _has_quantum_signal(item: DigestItem) -> bool:
    text = _text(item)
    return "quant" in text or any("quant" in category.lower() for category in item.categories)


def _text(item: DigestItem) -> str:
    return " ".join(
        [
            item.title,
            item.abstract or "",
            item.repo_description or "",
            " ".join(item.categories),
            " ".join(item.all_tags),
        ]
    ).lower()


def _truncate(value: str, limit: int) -> str:
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"
