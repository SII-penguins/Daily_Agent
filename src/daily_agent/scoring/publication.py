"""Publication evidence, not retrieval-channel or topic-tag prestige."""
from __future__ import annotations
import re


def publication_evidence(item):
    raw = item.raw
    sources = (raw.get('evidence') or {}).get('sources', {})
    records = [(item.source, {**raw, 'doi': item.doi})] + list(sources.items())
    formal = []
    preprint_found = item.source == 'arxiv' or bool(item.arxiv_id) or 'arxiv' in sources
    for source, evidence in records:
        venue = str((evidence.get('journal_ref') if source == 'arxiv' else None) or evidence.get('venue') or '')
        identifiers = ' '.join(str(evidence.get(k) or '') for k in ('doi', 'url', 'pdf_url'))
        repository = bool(re.search(r'zenodo|open collections|institutional repository|\brepository\b', venue, re.I))
        arxiv = bool(re.search(r'arxiv\.org|10\.48550/arxiv\.', identifiers, re.I))
        preprint_found |= arxiv or bool(re.search(r'arxiv|biorxiv|medrxiv|preprint', venue, re.I))
        status = str(evidence.get('publication_status') or '').lower()
        types = evidence.get('publication_types') or [evidence.get('publication_type') or '']
        if isinstance(types, str): types = [types]
        types = {str(t).lower() for t in types}
        preprint = bool(re.search(r'arxiv|biorxiv|medrxiv|preprint|submitted|submission|under review|withdrawn|rejected', venue, re.I))
        preprint_found |= 'preprint' in types or status == 'preprint'
        if repository or preprint or status in {'submitted', 'rejected', 'withdrawn', 'preprint'}:
            continue
        confirmed = source in {'pmlr', 'neurips'}
        if source == 'openreview':
            confirmed = status == 'accepted'
        elif source in {'crossref', 'openalex', 'semantic_scholar', 'dblp', 'ieee'}:
            confirmed |= bool(venue and types & {'journal-article','proceedings-article','journalarticle','conference','article','review'})
        elif source == 'arxiv':
            # journal_ref is an explicit metadata assertion; DOI alone is not.
            confirmed = bool(evidence.get('journal_ref'))
            venue = evidence.get('journal_ref') or venue
        if confirmed:
            formal.append({'source': source, 'venue': venue, 'basis': 'publication_metadata'})
    if formal:
        return {'status': 'published', 'records': formal}
    return {'status': 'preprint_only' if preprint_found else 'unconfirmed', 'records': []}


def publication_scores(item, settings=None):
    cfg = settings or {}
    evidence = publication_evidence(item)
    item.raw['publication_evidence'] = evidence
    if evidence['status'] != 'published':
        return 0.0, float(cfg.get('preprint_only_penalty', -10)) if evidence['status']=='preprint_only' else 0.0
    preferred = cfg.get('preferred_venue_patterns', [r'\b(?:ICLR|ICML|NeurIPS|NIPS)\b', r'^Nature(?:\s|$)', r'^npj\s'])
    top = any(any(re.search(pattern, r['venue'], re.I) for pattern in preferred) for r in evidence['records'])
    venue_score = float(cfg.get('preferred_venue_bonus', 14)) if top else float(cfg.get('proceedings_bonus', 8)) if any(r['source']=='pmlr' for r in evidence['records']) else 0.0
    return venue_score, float(cfg.get('published_bonus', 6))
