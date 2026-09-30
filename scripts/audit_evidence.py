#!/usr/bin/env python3
"""Read-only corpus audit: inclusion is not full quality acceptance."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from daily_agent.paper_document import atomic_json


def audit(rows):
    papers = []
    for row in rows:
        r = row.get('material', row)
        if r.get('item_type') != 'paper':
            continue
        doc, reading = r.get('paper_document', {}), r.get('reading', {})
        verification = reading.get('verification', {})
        full = doc.get('document_kind') == 'full_text'
        required = sum(bool(p.get('visual_required')) for p in doc.get('pages', []))
        visual = reading.get('visual', {})
        text = full and reading.get('complete') is True
        fidelity = (not required and doc.get('source_type') != 'pdf') or (
            visual.get('strict_fidelity') is True and visual.get('fidelity', {}).get('passed') is True)
        claims = verification.get('status') == 'located' and verification.get('semantic_support') == 'model_checked'
        papers.append({'key': r['key'], 'title': r['title'], 'document_kind': doc.get('document_kind'),
                       'extracted_chunks_read': reading.get('complete') is True,
                       'full_text_read': bool(text), 'required_visual_pages': required,
                       'visual_fidelity_passed': bool(fidelity), 'claims_passed': claims,
                       'quality_passed': bool(text and fidelity and claims),
                       'issues': verification.get('issues', [])})
    return {'papers': papers, 'paper_count': len(papers),
            'full_text_read_count': sum(p['full_text_read'] for p in papers),
            'quality_passed_count': sum(p['quality_passed'] for p in papers),
            'all_passed': bool(papers) and all(p['quality_passed'] for p in papers)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = audit(json.loads(args.input.read_text()))
    atomic_json(args.output, result)
    print(json.dumps({k: v for k, v in result.items() if k != 'papers'}))
