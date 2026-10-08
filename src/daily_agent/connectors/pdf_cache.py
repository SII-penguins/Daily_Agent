from __future__ import annotations

from datetime import date
import os
import hashlib
import re
import time
from pathlib import Path

import httpx

from daily_agent.config import AppConfig
from daily_agent.connectors.paper_text import _candidate_pdf_urls, _download_limited
from daily_agent.models import ApprovedItem
from daily_agent.workflow_state import atomic_bytes


def cache_selected_pdfs(items: list[ApprovedItem], config: AppConfig, run_date: date) -> list[ApprovedItem]:
    cache_config = config.sources.get("pdf_cache", {}) or {}
    if not cache_config.get("enabled", False):
        return items
    max_papers = int(cache_config.get("max_papers_per_run", 10))
    if max_papers <= 0:
        return items
    max_bytes = int(cache_config.get("max_pdf_bytes", 30_000_000))
    max_urls_per_paper = int(cache_config.get("max_urls_per_paper", 4))
    timeout = float(cache_config.get("timeout_seconds", 15))
    run_budget_seconds = float(cache_config.get("run_budget_seconds", 0) or 0)
    output_dir = str(cache_config.get("output_dir") or "data/pdfs")
    pdf_root = config.root / output_dir
    run_dir = pdf_root / run_date.isoformat()
    run_dir.mkdir(parents=True, exist_ok=True)

    deadline = time.monotonic() + run_budget_seconds if run_budget_seconds > 0 else None
    processed = 0
    with httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": "Daily-Agent/0.1"}) as client:
        for rank, item in enumerate(items, start=1):
            material = item.material
            if material.item_type != "paper":
                continue
            if material.raw.get("local_pdf_path"):
                cached = _read_verified_pdf(config, material.raw["local_pdf_path"], material, max_bytes)
                expected = (material.raw.get("pdf_cache_status") or {}).get("sha256")
                if cached is not None and (not expected or hashlib.sha256(cached).hexdigest() == expected):
                    material.raw.setdefault("pdf_cache_status", {}).update(
                        status="cached", sha256=hashlib.sha256(cached).hexdigest(), bytes=len(cached))
                    continue
                # Do not render a link to missing, corrupt, or different-version evidence.
                material.raw.pop("local_pdf_path", None)
                material.raw.pop("local_pdf_report_url", None)
                material.raw.pop("local_pdf_relative_path", None)
            if processed >= max_papers:
                material.raw["pdf_cache_status"] = {"status": "skipped", "reason": "max_papers_exceeded"}
                continue
            if _deadline_exceeded(deadline):
                material.raw["pdf_cache_status"] = {"status": "skipped", "reason": "run_budget_exceeded"}
                continue
            processed += 1
            _cache_one_pdf(
                item=item,
                config=config,
                client=client,
                run_dir=run_dir,
                rank=rank,
                max_bytes=max_bytes,
                max_urls_per_paper=max_urls_per_paper,
                deadline=deadline,
            )
    return items


def _cache_one_pdf(
    item: ApprovedItem,
    config: AppConfig,
    client: httpx.Client,
    run_dir: Path,
    rank: int,
    max_bytes: int,
    max_urls_per_paper: int,
    deadline: float | None,
) -> None:
    document = item.material.paper_document or {}
    source_path = document.get("source_pdf_path")
    if source_path and document.get("source_pdf_sha256"):
        content = _read_verified_pdf(config, source_path, item.material, max_bytes)
        if content is not None:
            _save_pdf(item, config, run_dir, rank, content, document.get("source_url"))
            return
    urls = _candidate_pdf_urls(item.material)
    if not urls:
        item.material.raw["pdf_cache_status"] = {"status": "unavailable", "reason": "no_pdf_url"}
        return
    attempts = 0
    for url in urls:
        if max_urls_per_paper > 0 and attempts >= max_urls_per_paper:
            break
        if _deadline_exceeded(deadline):
            item.material.raw["pdf_cache_status"] = {"status": "skipped", "reason": "run_budget_exceeded"}
            return
        attempts += 1
        content = _download_limited(client, url, max_bytes=max_bytes, deadline=deadline)
        if not content or not content.startswith(b"%PDF"):
            continue
        expected = document.get("source_pdf_sha256")
        if expected and hashlib.sha256(content).hexdigest() != expected:
            continue
        _save_pdf(item, config, run_dir, rank, content, url)
        return
    item.material.raw["pdf_cache_status"] = {"status": "unavailable", "reason": "no_valid_pdf", "attempts": attempts}


def _read_verified_pdf(config, value, material, max_bytes):
    """Read only bounded local assets; reviewed PDF identity takes precedence."""
    try:
        path = Path(value)
        path = (path if path.is_absolute() else config.root / path).resolve()
        if not path.is_relative_to(config.root.resolve()) or not path.is_file() or path.stat().st_size > max_bytes:
            return None
        with path.open("rb") as handle:
            content = handle.read(max_bytes + 1)
        expected = (material.paper_document or {}).get("source_pdf_sha256")
        if (len(content) > max_bytes or not content.startswith(b"%PDF")
                or expected and hashlib.sha256(content).hexdigest() != expected):
            return None
        return content
    except (OSError, TypeError, ValueError):
        return None


def _save_pdf(item, config, run_dir, rank, content, url):
    path = _available_pdf_path(run_dir, rank, item.title)
    atomic_bytes(path, content)
    # Keep the legacy absolute-path field; add a portable identity for new consumers.
    item.material.raw["local_pdf_path"] = str(path)
    item.material.raw["local_pdf_relative_path"] = path.relative_to(config.root).as_posix()
    item.material.raw["local_pdf_report_url"] = _report_relative_url(config, path)
    item.material.raw["pdf_cache_status"] = {
        "status": "cached", "source_url": url, "bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
        "matches_reviewed_pdf": bool((item.material.paper_document or {}).get("source_pdf_sha256")),
    }


def _available_pdf_path(run_dir: Path, rank: int, title: str) -> Path:
    base = f"{rank:02d}-{_slug(title)}"
    path = run_dir / f"{base}.pdf"
    counter = 2
    while path.exists():
        path = run_dir / f"{base}-{counter}.pdf"
        counter += 1
    return path


def _slug(title: str) -> str:
    words = re.findall(r"[a-z0-9]+", title.lower())
    slug = "-".join(words[:10])
    return slug[:90] or "paper"


def _report_relative_url(config: AppConfig, path: Path) -> str:
    return Path(os.path.relpath(path, config.reports_dir)).as_posix()


def _deadline_exceeded(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() >= deadline
