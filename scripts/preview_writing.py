#!/usr/bin/env python3
"""Render an existing approval snapshot locally, without fetching or model calls."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import date
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from daily_agent.config import load_config
from daily_agent.models import ApprovedItem, MaterialRecord, RunStatus
from daily_agent.rendering.composition import featured_keys, paper_paragraphs
from daily_agent.rendering.html import render_daily_html
from daily_agent.rendering.markdown import render_daily_markdown
from daily_agent.rendering.notes import write_reading_notes
from daily_agent.storage import write_daily_html_report, write_daily_report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="Existing approval.json")
    parser.add_argument("--output", type=Path, required=True, help="New directory under project tmp/")
    parser.add_argument("--date", type=date.fromisoformat, default=date.today())
    args = parser.parse_args()
    output = args.output.resolve()
    if ROOT / "tmp" not in output.parents or output.exists():
        parser.error("output must be a new directory under project tmp/")
    raw = args.input.read_bytes()
    payload = json.loads(raw)
    items = [ApprovedItem(**{**row, "material": MaterialRecord.from_dict(row["material"])}) for row in payload]
    before = [json.dumps(item.final_fields, ensure_ascii=False, sort_keys=True) for item in items]
    config = load_config(ROOT)
    object.__setattr__(config, "root", output)
    output.mkdir(parents=True)
    (output / "input-approval.json").write_bytes(raw)
    write_reading_notes(config, items, args.date, dry_run=True)
    settings = config.sources.get("report_writing", {})
    insights = config.sources.get("insights", {})
    status = RunStatus(fallback="固定已批准素材的写作预览；未重新采集或调用模型；沿用原阅读验收状态。")
    md = write_daily_report(config, args.date, render_daily_markdown(items, args.date, status, insights, settings))
    html = write_daily_html_report(config, args.date, render_daily_html(items, args.date, status, insights, settings))
    featured = featured_keys(items, settings)
    audit = {"input": str(args.input.resolve()), "sha256": hashlib.sha256(raw).hexdigest(),
             "scope": "offline composition preview, not new reading acceptance", "items": []}
    for item, original in zip(items, before):
        assert json.dumps(item.final_fields, ensure_ascii=False, sort_keys=True) == original
        if item.item_type != "paper":
            continue
        for field in ("reading_note_url", "reading_note_html_url"):
            assert (config.reports_dir / item.material.raw[field]).is_file()
        audit["items"].append({"key": item.key, "featured": item.key in featured,
                               "paragraphs": [asdict(p) for p in paper_paragraphs(item, item.key in featured)]})
    (output / "composition-audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2))
    print(md)
    print(html)


if __name__ == "__main__":
    main()
