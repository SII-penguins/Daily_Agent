#!/usr/bin/env python3
"""Build a disconnected, synthetic Zotero selection prototype. No network I/O."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from daily_agent.rendering.zotero_preview import write_zotero_preview


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=ROOT.parent / "prototype",
        help="New prototype directory, outside existing reports and archives",
    )
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        parser.error("output must be a new directory; existing files are never overwritten")
    if any(part.lower() in {"archive", "archives", "reports", "report_archive"} for part in output.parts):
        parser.error("this synthetic prototype must remain outside reports and archives")
    result = write_zotero_preview(output)
    print(result)
    print("Offline synthetic preview only. NOT CONNECTED. No Zotero import is implemented.")


if __name__ == "__main__":
    main()
