#!/usr/bin/env python3
"""Read-only corpus audit: inclusion is not full quality acceptance."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from daily_agent.paper_document import atomic_json


from daily_agent.reading import audit_reading as audit


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = audit(json.loads(args.input.read_text()))
    atomic_json(args.output, result)
    print(json.dumps({k: v for k, v in result.items() if k != 'papers'}))
