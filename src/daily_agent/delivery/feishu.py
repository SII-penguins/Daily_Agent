from __future__ import annotations

import hashlib
import re
import tomllib
import uuid
from datetime import date
from pathlib import Path
from typing import Any

import httpx

from daily_agent.config import AppConfig
from daily_agent.delivery.cc_connect import send_via_cc_connect
from daily_agent.models import DeliveryStatus
from daily_agent.secrets import credential_value
from daily_agent.storage import load_feishu_delivery_state, write_feishu_delivery_state

BASE_URL = "https://open.feishu.cn/open-apis"


def deliver_weekly_report(
    path: Path,
    mode: str = "local",
    config: AppConfig | None = None,
    run_date: date | None = None,
    daily_markdown: str | None = None,
) -> DeliveryStatus:
    if mode == "local":
        return DeliveryStatus(requested_mode=mode, final_mode="local", ok=True)
    if mode in {"cc-connect", "cc_connect"}:
        send_via_cc_connect(path, message="Daily Agent 日报已生成，见附件。")
        return DeliveryStatus(requested_mode=mode, final_mode="cc-connect", ok=True)
    if mode == "feishu":
        if not config or not run_date or daily_markdown is None:
            return _fallback_to_cc_connect(path, config, mode, "Feishu delivery requires config, run_date, and daily_markdown")
        try:
            return _deliver_to_feishu(config, path, run_date, daily_markdown)
        except Exception as exc:
            return _fallback_to_cc_connect(path, config, mode, str(exc))
    raise ValueError(f"Unsupported delivery mode: {mode}")


def _deliver_to_feishu(config: AppConfig, path: Path, run_date: date, daily_markdown: str) -> DeliveryStatus:
    feishu_config = config.delivery.get("delivery", {}).get("feishu", {}) or {}
    if not feishu_config.get("enabled"):
        raise RuntimeError("Feishu delivery is disabled")

    cc_connect_feishu = _load_cc_connect_feishu_config()
    app_id = _env_value(feishu_config, "app_id_env") or cc_connect_feishu.get("app_id")
    app_secret = _env_value(feishu_config, "app_secret_env") or cc_connect_feishu.get("app_secret")
    if not app_id or not app_secret:
        raise RuntimeError("Feishu credentials are missing")

    timeout = float(feishu_config.get("request_timeout_seconds", 30))
    token = _tenant_access_token(app_id, app_secret, timeout)
    state = load_feishu_delivery_state(config)
    week_key = _week_key(run_date)
    title = _weekly_title(config, run_date)
    weeks = state.setdefault("weeks", {})
    week = weeks.setdefault(week_key, {"title": title, "days": {}})

    document_id = str(week.get("document_id") or _env_value(feishu_config, "doc_token_env") or "")
    if not document_id:
        folder_token = _env_value(feishu_config, "folder_token_env") or cc_connect_feishu.get("folder_token")
        created = _create_document(token, title, folder_token, timeout)
        document_id = created["document_id"]
        week["document_url"] = created.get("document_url")
    week["document_id"] = document_id
    week["title"] = title
    week.setdefault("document_url", _document_url(document_id))

    content_hash = "sha256:" + hashlib.sha256(daily_markdown.encode("utf-8")).hexdigest()
    days = week.setdefault("days", {})
    day_key = run_date.isoformat()
    current_day = days.get(day_key) or {}
    if current_day.get("content_hash") == content_hash:
        write_feishu_delivery_state(config, state)
        return DeliveryStatus(requested_mode="feishu", final_mode="feishu", ok=True, document_url=week.get("document_url"))

    old_start_index = int(current_day.get("start_index", 0) or 0)
    old_block_count = _managed_block_count(current_day)
    blocks = _markdown_to_blocks(daily_markdown)
    new_day_state: dict[str, Any] | None = None
    try:
        block_ids = _create_blocks_in_batches(token, document_id, document_id, blocks, timeout)
        new_day_state = {"block_ids": block_ids, "block_count": len(blocks), "start_index": 0}
        if old_block_count:
            _delete_managed_blocks(token, document_id, document_id, current_day, timeout, ignore_stale=True)
    except Exception:
        if new_day_state:
            _delete_managed_blocks(token, document_id, document_id, new_day_state, timeout, ignore_stale=True)
        raise
    _shift_day_start_indices(days, day_key, old_start_index, old_block_count, len(blocks))
    days[day_key] = {
        "block_ids": block_ids,
        "block_count": len(blocks),
        "start_index": 0,
        "content_hash": content_hash,
        "updated_at": _now_hint(),
    }
    write_feishu_delivery_state(config, state)
    return DeliveryStatus(requested_mode="feishu", final_mode="feishu", ok=True, document_url=week.get("document_url"))


def _fallback_to_cc_connect(path: Path, config: AppConfig | None, requested_mode: str, error: str) -> DeliveryStatus:
    if config:
        delivery = config.delivery.get("delivery", {}) or {}
        feishu_config = delivery.get("feishu", {}) or {}
        cc_config = delivery.get("cc_connect", {}) or {}
        if feishu_config.get("fallback_to_cc_connect", True) and cc_config.get("enabled", True):
            try:
                send_via_cc_connect(path, message="Daily Agent 日报已生成，飞书云文档写入失败，先发送 Markdown 附件。")
                return DeliveryStatus(requested_mode=requested_mode, final_mode="cc-connect", ok=True, fallback_used=True, error=error)
            except Exception as exc:
                return DeliveryStatus(requested_mode=requested_mode, final_mode="local", ok=False, fallback_used=True, error=f"{error}; cc-connect fallback failed: {exc}")
    return DeliveryStatus(requested_mode=requested_mode, final_mode="local", ok=False, fallback_used=False, error=error)


def _tenant_access_token(app_id: str, app_secret: str, timeout: float) -> str:
    response = httpx.post(
        f"{BASE_URL}/auth/v3/tenant_access_token/internal",
        json={"app_id": app_id, "app_secret": app_secret},
        timeout=timeout,
    )
    _raise_for_feishu_error(response)
    payload = response.json()
    token = payload.get("tenant_access_token")
    if not token:
        raise RuntimeError(f"Feishu token response missing tenant_access_token: {payload}")
    return str(token)


def _create_document(token: str, title: str, folder_token: str | None, timeout: float) -> dict[str, str]:
    body: dict[str, str] = {"title": title}
    if folder_token:
        body["folder_token"] = folder_token
    response = httpx.post(
        f"{BASE_URL}/docx/v1/documents",
        headers=_headers(token),
        json=body,
        timeout=timeout,
    )
    _raise_for_feishu_error(response)
    payload = response.json()
    document = payload.get("data", {}).get("document", {}) or payload.get("data", {})
    document_id = document.get("document_id") or document.get("document_token") or document.get("token")
    if not document_id:
        raise RuntimeError(f"Feishu create document response missing document id: {payload}")
    return {"document_id": str(document_id), "document_url": document.get("url") or _document_url(str(document_id))}


def _create_blocks_in_batches(token: str, document_id: str, parent_block_id: str, blocks: list[dict[str, Any]], timeout: float) -> list[str]:
    block_ids: list[str] = []
    for batch in reversed([blocks[index : index + 20] for index in range(0, len(blocks), 20)]):
        block_ids[:0] = _create_blocks(token, document_id, parent_block_id, batch, 0, timeout)
    return block_ids


def _create_blocks(token: str, document_id: str, parent_block_id: str, blocks: list[dict[str, Any]], index: int, timeout: float) -> list[str]:
    if not blocks:
        return []
    response = httpx.post(
        f"{BASE_URL}/docx/v1/documents/{document_id}/blocks/{parent_block_id}/children",
        headers=_headers(token),
        params={"document_revision_id": -1, "client_token": str(uuid.uuid4())},
        json={"children": blocks, "index": index},
        timeout=timeout,
    )
    _raise_for_feishu_error(response)
    payload = response.json()
    created = payload.get("data", {}).get("children", []) or payload.get("data", {}).get("blocks", [])
    block_ids = []
    for block in created:
        block_id = block.get("block_id") or block.get("id")
        if block_id:
            block_ids.append(str(block_id))
    if not block_ids:
        block_ids = [f"unknown-{index}" for index, _ in enumerate(blocks, start=1)]
    return block_ids


def _delete_managed_blocks(token: str, document_id: str, parent_block_id: str, day_state: dict[str, Any], timeout: float, ignore_stale: bool = False) -> None:
    block_count = _managed_block_count(day_state)
    if block_count <= 0:
        return
    start_index = int(day_state.get("start_index", 0) or 0)
    response = httpx.request(
        "DELETE",
        f"{BASE_URL}/docx/v1/documents/{document_id}/blocks/{parent_block_id}/children/batch_delete",
        headers=_headers(token),
        params={"document_revision_id": -1, "client_token": str(uuid.uuid4())},
        json={"start_index": start_index, "end_index": start_index + block_count},
        timeout=timeout,
    )
    try:
        _raise_for_feishu_error(response)
    except RuntimeError:
        if ignore_stale and response.status_code == 400:
            return
        raise


def _raise_for_feishu_error(response: httpx.Response) -> None:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = response.text[:1000]
        request = response.request
        raise RuntimeError(f"Feishu API {response.status_code} {request.method} {request.url}: {detail}") from exc


def _managed_block_count(day_state: dict[str, Any]) -> int:
    block_count = day_state.get("block_count")
    if isinstance(block_count, int) and block_count > 0:
        return block_count
    return len([block_id for block_id in day_state.get("block_ids", []) if block_id])


def _shift_day_start_indices(days: dict[str, Any], current_key: str, old_start_index: int, old_count: int, new_count: int) -> None:
    for key, day in days.items():
        if key == current_key or not isinstance(day, dict):
            continue
        start_index = int(day.get("start_index", 0) or 0)
        if old_count and start_index > old_start_index:
            start_index = max(start_index - old_count, 0)
        if new_count:
            start_index += new_count
        day["start_index"] = start_index


def _markdown_to_blocks(markdown: str) -> list[dict[str, Any]]:
    blocks = []
    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        field = "text"
        block_type = 2
        text = line
        if line.startswith("### "):
            field = "heading3"
            block_type = 5
            text = line[4:]
        elif line.startswith("## "):
            field = "heading2"
            block_type = 4
            text = line[3:]
        elif line.startswith("# "):
            field = "heading1"
            block_type = 3
            text = line[2:]
        elif line.startswith("- "):
            field = "bullet"
            block_type = 12
            text = line[2:]
        blocks.append({"block_type": block_type, field: _text_payload(text)})
    return blocks


def _text_payload(text: str) -> dict[str, Any]:
    return {"elements": [{"text_run": {"content": _strip_markdown_links(text)}}]}


def _strip_markdown_links(text: str) -> str:
    return re.sub(r"\[([^\]]+)\]\(([^\)]+)\)", r"\1（\2）", text)


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"}


def _env_value(config: dict[str, Any], key: str) -> str | None:
    env_name = str(config.get(key) or "")
    if not env_name:
        return None
    return credential_value(env_name)


def _load_cc_connect_feishu_config() -> dict[str, str]:
    path = Path.home() / ".cc-connect" / "config.toml"
    if not path.exists():
        return {}
    try:
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    for project in payload.get("projects", []):
        for platform in project.get("platforms", []):
            if platform.get("type") == "feishu":
                options = platform.get("options", {}) or {}
                return {key: str(options[key]) for key in ("app_id", "app_secret", "folder_token") if options.get(key)}
    return {}


def _week_key(run_date: date) -> str:
    year, week, _ = run_date.isocalendar()
    return f"{year}-W{week:02d}"


def _weekly_title(config: AppConfig, run_date: date) -> str:
    prefix = config.delivery.get("delivery", {}).get("feishu", {}).get("weekly_doc_title") or config.delivery.get("report", {}).get("title_prefix", "Daily Agent 日报")
    year, week, _ = run_date.isocalendar()
    return f"{prefix}｜{year} 第 {week:02d} 周"


def _document_url(document_id: str) -> str:
    return f"https://feishu.cn/docx/{document_id}"


def _now_hint() -> str:
    from daily_agent.models import utc_now_iso

    return utc_now_iso()
