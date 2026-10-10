"""Separate official publication records from unverified bibliographic assertions.

A journal reference supplied to arXiv, a DOI, or indexer metadata is a discovery
lead, not a verified publisher/proceedings record. This module performs no I/O:
primary_source_record means provenance from a recognized official collector,
not that a publisher page was re-fetched during scoring or that its body was read.
"""
from __future__ import annotations
import re
from urllib.parse import urlparse, parse_qs


def _primary_url(source, evidence):
    url = str(evidence.get('url') or evidence.get(f'{source}_url') or '')
    parsed = urlparse(url)
    if parsed.scheme != 'https' or parsed.username or parsed.password:
        return None
    host = (parsed.hostname or '').lower()
    if source == 'pmlr' and host == 'proceedings.mlr.press' and re.fullmatch(r'/v\d+/[^/]+\.html', parsed.path):
        return url
    if source == 'neurips' and host in {'papers.nips.cc', 'proceedings.neurips.cc'} and re.search(r'/(?:paper_files/paper|paper)/\d+/(?:hash|file)/[^/]+Abstract(?:-[A-Za-z]+)?\.html$', parsed.path):
        return url
    if source == 'openreview' and host == 'openreview.net' and parsed.path == '/forum' and parse_qs(parsed.query).get('id'):
        return url
    if source == 'nature' and host == 'www.nature.com' and re.fullmatch(r'/articles/[A-Za-z0-9-]+', parsed.path):
        return url
    if source == 'ieee' and host == 'ieeexplore.ieee.org' and re.fullmatch(r'/document/\d+/?', parsed.path):
        return url
    return None


def _same_title(left, right):
    def normalize(value):
        return re.sub(r"[^\w]+", " ", str(value or "").casefold()).strip()
    title = normalize(left)
    return bool(title and title != "untitled" and title == normalize(right))


def non_arxiv_doi(value):
    """Exact publication identifier only; repository DOI is a separate alias."""
    value = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "",
                   str(value or "").strip(), flags=re.I).casefold()
    return value if value.startswith("10.") and "/" in value and not value.startswith("10.48550/arxiv.") else None


def publication_dois(item):
    sources = (item.raw.get('evidence') or {}).get('sources', {})
    values = [item.doi, (item.raw.get('primary_verification') or {}).get('doi')]
    for evidence in sources.values():
        if isinstance(evidence, dict):
            values.extend([evidence.get('doi'), (evidence.get('primary_verification') or {}).get('doi')])
    return {doi for value in values if (doi := non_arxiv_doi(value))}


def publication_evidence(item):
    raw = item.raw
    sources = (raw.get('evidence') or {}).get('sources', {})
    records = [(item.source, {**raw, 'doi': item.doi, 'url': item.url, 'title': item.title})] + list(sources.items())
    formal, metadata = [], []
    conflicting_dois = sorted(publication_dois(item))
    identity_conflict = len(conflicting_dois) > 1
    preprint_found = item.source == 'arxiv' or bool(item.arxiv_id) or 'arxiv' in sources
    for source, evidence in records:
        if not isinstance(evidence, dict):
            continue
        venue = str((evidence.get('journal_ref') if source == 'arxiv' else None) or evidence.get('venue') or '')
        identifiers = ' '.join(str(evidence.get(k) or '') for k in ('doi', 'url', 'pdf_url'))
        repository = bool(re.search(r'zenodo|open collections|institutional repository|\brepository\b', venue, re.I))
        arxiv = bool(re.search(r'arxiv\.org|10\.48550/arxiv\.', identifiers, re.I))
        preprint_found |= arxiv or bool(re.search(r'arxiv|biorxiv|medrxiv|preprint', venue, re.I))
        status = str(evidence.get('publication_status') or '').lower()
        types = evidence.get('publication_types') or [evidence.get('publication_type') or '']
        if isinstance(types, str):
            types = [types]
        types = {str(t).lower() for t in types}
        preprint = bool(re.search(r'arxiv|biorxiv|medrxiv|preprint|submitted|submission|under review|withdrawn|rejected', venue, re.I))
        preprint_found |= 'preprint' in types or status == 'preprint'
        if repository or preprint or status in {'submitted', 'rejected', 'withdrawn', 'preprint'}:
            continue
        official_url = _primary_url(source, evidence)
        proof = evidence.get('primary_verification') or {}
        nature_verified = (source == 'nature' and evidence.get('primary_landing_verified') is True
            and proof.get('verified') is True and proof.get('method') == 'publisher_landing_citation_metadata'
            and proof.get('url') == official_url and _same_title(proof.get('title'), item.title)
            and proof.get('venue') == venue and bool(venue)
            and status == 'published')
        confirmed = bool(not identity_conflict and official_url and _same_title(evidence.get('title'), item.title) and (
            nature_verified or source in {'pmlr', 'neurips'}
            or source == 'openreview' and status == 'accepted'
            or source == 'ieee' and venue and types & {'journal-article', 'proceedings-article', 'journalarticle', 'conference', 'article', 'review'}))
        record = {'source': source, 'venue': venue, 'url': str(evidence.get('url') or '')}
        if confirmed:
            record.update(url=official_url, basis='primary_source_record')
            if record not in formal:
                formal.append(record)
        elif (source == 'arxiv' and evidence.get('journal_ref')
              or source in {'crossref', 'openalex', 'semantic_scholar', 'dblp', 'ieee'}
              and venue and types & {'journal-article', 'proceedings-article', 'journalarticle', 'conference', 'article', 'review'}
              or source in {'pmlr', 'neurips', 'nature'} or source == 'openreview' and status == 'accepted'):
            record['basis'] = 'unverified_publication_metadata'
            if identity_conflict:
                record['reason'] = 'conflicting_publication_doi'
            if record not in metadata:
                metadata.append(record)
    if formal:
        status, verification = 'published', 'primary_source_record'
    elif metadata:
        status, verification = 'metadata_only', 'unverified_metadata'
    else:
        status, verification = ('preprint_only' if preprint_found else 'unconfirmed'), 'unverified'
    result = {'status': status, 'verification': verification, 'records': formal,
              'metadata_records': metadata, 'has_preprint': preprint_found}
    if identity_conflict:
        result['identity_conflicts'] = {'reason': 'conflicting_publication_doi', 'dois': conflicting_dois}
    return result


def publication_scores(item, settings=None):
    cfg = settings or {}
    evidence = publication_evidence(item)
    item.raw['publication_evidence'] = evidence
    if evidence['status'] != 'published':
        return 0.0, float(cfg.get('preprint_only_penalty', -10)) if evidence['has_preprint'] else 0.0
    preferred = cfg.get('preferred_venue_patterns', [r'\b(?:ICLR|ICML|NeurIPS|NIPS)\b', r'^Nature(?:\s|$)', r'^npj\s'])
    top = any(any(re.search(pattern, r['venue'], re.I) for pattern in preferred) for r in evidence['records'])
    venue_score = float(cfg.get('preferred_venue_bonus', 14)) if top else float(cfg.get('proceedings_bonus', 8)) if any(r['source']=='pmlr' for r in evidence['records']) else 0.0
    return venue_score, float(cfg.get('published_bonus', 6))
