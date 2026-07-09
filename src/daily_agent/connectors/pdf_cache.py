from __future__ import annotations

from datetime import date
import os
import re
import time
from pathlib import Path

import httpx

from daily_agent.config import AppConfig
from daily_agent.connectors.paper_text import _candidate_pdf_urls, _download_limited
from daily_agent.models import ApprovedItem


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
                continue
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
        path = _available_pdf_path(run_dir, rank, item.title)
        path.write_bytes(content)
        item.material.raw["local_pdf_path"] = str(path)
        item.material.raw["local_pdf_report_url"] = _report_relative_url(config, path)
        item.material.raw["pdf_cache_status"] = {
            "status": "cached",
            "source_url": url,
            "bytes": len(content),
        }
        return
    item.material.raw["pdf_cache_status"] = {"status": "unavailable", "reason": "no_valid_pdf", "attempts": attempts}


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
