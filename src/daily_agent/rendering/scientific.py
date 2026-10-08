"""Lossless reading layout over exact, independently reviewed scientific claims.

Presentation is deliberately separate from the semantic protocol in
scientific_analysis.py. Only semantic functions and prompts bind its cache;
layout changes never summarize text or request a new scientific review.
"""
from __future__ import annotations

from daily_agent.scientific_analysis import LABELS, reviewed_analysis

# A soft reading-length target, not a scientific limit. Never split a claim,
# reorder it, or join different provenance kinds to reach this target.
PARAGRAPH_TARGET = 260


def analysis_reading_blocks(item):
    """Return claim runs split at provenance changes and whole-claim boundaries.

    The first paragraph and every change of provenance carry a label. A long
    same-kind run may continue as a new paragraph without repeating that label.
    Exact claim dictionaries remain attached for source identity and inspection.
    """
    value = reviewed_analysis(item)
    if not value:
        return {}
    blocks = {}
    for section in value['paragraphs']:
        paragraphs = []
        for claim in section['claims']:
            previous = paragraphs[-1] if paragraphs else None
            same_kind = previous is not None and previous['kind'] == claim['kind']
            length = sum(len(c['text']) for c in previous['claims']) if previous else 0
            if same_kind and length + 1 + len(claim['text']) <= PARAGRAPH_TARGET:
                previous['claims'].append(claim)
            else:
                paragraphs.append({'kind': claim['kind'],
                    'label': '' if same_kind else LABELS[claim['kind']],
                    'claims': [claim]})
        blocks[section['id']] = paragraphs
    return blocks


def analysis_paragraphs(item):
    value = reviewed_analysis(item)
    if not value: return []
    return [(p['id'], ' '.join(LABELS[c['kind']] + '：' + c['text'] for c in p['claims'])) for p in value['paragraphs']]


def analysis_sources(item):
    """Compact actual page/chunk locators, without a quotation dump."""
    value = reviewed_analysis(item)
    if not value: return ''
    chunks = {c['id']: c for c in item.material.paper_document.get('chunks', [])}
    ids = {ref['chunk_id'] for p in value['paragraphs'] for c in p['claims'] for ref in c['evidence']}
    locators = []
    for cid in sorted(ids):
        page = chunks[cid].get('page')
        locators.append((f'第 {page} 页 · ' if page is not None else '') + cid)
    return '；'.join(locators)
