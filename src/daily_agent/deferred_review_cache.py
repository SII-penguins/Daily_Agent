"""Content-addressed review reuse, independent of issue date and selection policy.

A hit is an exact evidence/protocol match, not a stored PASS label. Files are
kept by hash outside the date-scoped report retention tree. Publication and
reservation eligibility remain the caller's responsibility and are never
restored from this cache.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path
import tempfile

from daily_agent.models import EditorialDraft
from daily_agent.paper_document import atomic_json, digest, evidence_settings, load_json, version_identity

SCHEMA = 1
# Changes to these evidence/review implementations invalidate aggregate reuse.
PROTOCOL_FILES = ('reading.py', 'visual_reading.py', 'visual_fidelity.py',
                  'paper_document.py', 'editorial.py', 'rendering/composition.py')


def _root(config):
    return config.root / 'data' / 'deferred-reviews'


def _protocol():
    root = Path(__file__).parent
    return digest({name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                   for name in PROTOCOL_FILES})


def _settings(config):
    return {name: evidence_settings(config.sources.get(name, {}) or {})
            for name in ('reading', 'llm_writer', 'paper_text')}


def _identity(config, record):
    # Deliberately exclude score, topic weights, tags, quotas, issue date,
    # publication history and scheduling budgets. Bind actual evidence, not
    # merely a caller-supplied content_hash.
    return digest([SCHEMA, _protocol(), _settings(config), version_identity(record),
                   record.title, record.abstract, record.authors, record.doi,
                   record.paper_document, record.raw.get('citation_context')])


def _enrichment_identity(config, record):
    root = Path(__file__).parent
    parser = {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
              for name in ('paper_document.py', 'page_evidence.py', 'connectors/paper_text.py')}
    return digest([SCHEMA, parser, evidence_settings(config.sources.get('paper_text', {}) or {}),
                   version_identity(record), record.title, record.abstract, record.paper_document])


def _local(config, value):
    if not isinstance(value, str) or not value or '://' in value:
        return None
    path = Path(value)
    if not path.is_absolute():
        path = config.root / path
    path = path.resolve()
    return path if path.is_relative_to(config.root.resolve()) else None


def _paths(config, value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key == 'path' or key.endswith('_path'):
                path = _local(config, child)
                if path is not None:
                    yield path
            yield from _paths(config, child)
    elif isinstance(value, list):
        for child in value:
            yield from _paths(config, child)


def _assets(config, record, *, save=False):
    """Require advertised hashes; snapshot all local evidence dependencies."""
    document = record.paper_document
    for doc in [document, document.get('native_document', {})]:
        if not doc:
            continue
        source_path = doc.get('source_pdf_path')
        native = document.get('native_document', {})
        if (not source_path and doc.get('source_pdf_sha256')
                and doc.get('source_pdf_sha256') == native.get('source_pdf_sha256')):
            source_path = native.get('source_pdf_path')
        expected_paths = [(source_path, doc.get('source_pdf_sha256'))]
        expected_paths += [(p.get('image_path'), p.get('image_hash')) for p in doc.get('pages', [])]
        if doc.get('source_type') == 'pdf' and not source_path:
            return None
        for name, expected in expected_paths:
            if not name:
                if expected:
                    return None
                continue
            path = _local(config, name)
            if path is None or not path.is_file() or not expected:
                return None
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                return None
    manifest = {}
    for path in set(_paths(config, {'document': document, 'reading': record.reading})):
        if not path.is_file():
            return None
        content = path.read_bytes()
        sha = hashlib.sha256(content).hexdigest()
        manifest[str(path)] = sha
        if save:
            blob = _root(config) / 'blobs' / sha
            if not blob.is_file() or hashlib.sha256(blob.read_bytes()).hexdigest() != sha:
                blob.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(dir=blob.parent, delete=False) as handle:
                    handle.write(content)
                    temporary = Path(handle.name)
                temporary.replace(blob)
    return manifest


def _repair_assets(config, manifest):
    if not isinstance(manifest, dict):
        return False
    for name, sha in manifest.items():
        path = _local(config, name)
        if path is None or not isinstance(sha, str) or len(sha) != 64 or any(c not in '0123456789abcdef' for c in sha):
            return False
        if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == sha:
            continue
        blob = _root(config) / 'blobs' / sha
        if not blob.is_file() or hashlib.sha256(blob.read_bytes()).hexdigest() != sha:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            handle.write(blob.read_bytes())
            temporary = Path(handle.name)
        temporary.replace(path)
    return True


def _supported(record, draft):
    """Re-run mechanical checks before trusting the exact independent review."""
    from daily_agent.reading import audit_reading, verify_draft
    if record.paper_document.get('identity') != version_identity(record):
        return False
    verification = draft.verification
    checks = verification.get('semantic_checks', [])
    fields = verification.get('valid_fields', [])
    if (verification.get('status') != 'located'
            or verification.get('semantic_support') != 'model_checked'
            or not fields or not isinstance(checks, list) or len(checks) != len(fields)
            or {c.get('field') for c in checks if isinstance(c, dict)} != set(fields)
            or any(not isinstance(c, dict) or c.get('supported') is not True
                   or not isinstance(c.get('reason'), str) for c in checks)):
        return False
    candidate, material = deepcopy(draft), deepcopy(record)
    verify_draft(candidate, material)
    if (candidate.verification.get('status') != 'located'
            or candidate.verification.get('valid_fields') != fields
            or candidate.draft_fields != draft.draft_fields
            or candidate.claim_evidence != draft.claim_evidence):
        return False
    material.reading['verification'] = deepcopy(verification)
    return audit_reading([material.to_dict()])['all_passed']


def _load(config, record):
    if record.item_type != 'paper' or not record.paper_document:
        return None
    key = _identity(config, record)
    envelope = load_json(_root(config) / 'reviews' / (key + '.json'))
    if not isinstance(envelope, dict):
        return None
    payload = envelope.get('payload')
    if (not isinstance(payload, dict) or digest(payload) != envelope.get('sha256')
            or payload.get('schema_version') != SCHEMA or key not in payload.get('input_keys', [])):
        return None
    try:
        candidate = deepcopy(record)
        candidate.paper_document = payload['paper_document']
        candidate.reading = payload['reading']
        candidate.paper_text_status = payload['paper_text_status']
        draft = EditorialDraft.from_dict(payload['draft'])
        if (draft.key != record.key or not _supported(candidate, draft)
                or not _repair_assets(config, payload['assets']) or _assets(config, candidate) is None):
            return None
        return candidate, draft
    except (KeyError, TypeError, ValueError, OSError, AttributeError):
        return None


def reusable_enrichment(config, record):
    """Reuse immutable extraction independently of reading/model/draft changes."""
    if record.item_type != 'paper' or not record.paper_document:
        return False
    key = _enrichment_identity(config, record)
    envelope = load_json(_root(config) / 'enrichment' / (key + '.json'))
    if not isinstance(envelope, dict):
        return False
    payload = envelope.get('payload')
    if (not isinstance(payload, dict) or digest(payload) != envelope.get('sha256')
            or payload.get('schema_version') != SCHEMA or key not in payload.get('enrichment_keys', [])):
        return False
    try:
        candidate = deepcopy(record)
        candidate.paper_document = payload['paper_document']
        candidate.reading = payload['reading']
        return (candidate.paper_document.get('identity') == version_identity(record)
                and candidate.paper_document.get('document_kind') == 'full_text'
                and _repair_assets(config, payload['assets']) and _assets(config, candidate) is not None)
    except (KeyError, TypeError, ValueError, OSError, AttributeError):
        return False


def draft_report_items(config, shortlist, use_llm, original, *, execution=None):
    kwargs = {"execution": execution} if execution is not None else {}
    if not use_llm:
        return original(config, shortlist, use_llm=use_llm, **kwargs)
    ready, pending, input_keys, enrichment_keys = {}, [], {}, {}
    for record in shortlist:
        cached = _load(config, record)
        if cached:
            candidate, ready[record.key] = cached
            # Later rendering may have added validated crops after this snapshot.
            newer_assets = record.reading.get('paper_visual_assets')
            record.paper_document = candidate.paper_document
            record.paper_text_status = candidate.paper_text_status
            record.reading = candidate.reading
            if newer_assets is not None:
                record.reading['paper_visual_assets'] = newer_assets
        else:
            pending.append(record)
            input_keys[record.key] = _identity(config, record)
            enrichment_keys[record.key] = _enrichment_identity(config, record)
    computed = original(config, pending, use_llm=use_llm, **kwargs) if pending else []
    by_key = {r.key: r for r in pending}
    for draft in computed:
        record = by_key.get(draft.key)
        if record is None or record.item_type != 'paper' or not _supported(record, draft):
            continue
        assets = _assets(config, record, save=True)
        if assets is None:
            continue
        keys = sorted({input_keys[record.key], _identity(config, record)})
        enrichment = sorted({enrichment_keys[record.key], _enrichment_identity(config, record)})
        payload = {'schema_version': SCHEMA, 'input_keys': keys, 'enrichment_keys': enrichment,
                   'paper_document': record.paper_document, 'reading': record.reading,
                   'paper_text_status': record.paper_text_status, 'draft': draft.to_dict(), 'assets': assets}
        envelope = {'sha256': digest(payload), 'payload': payload}
        for key in keys:
            atomic_json(_root(config) / 'reviews' / (key + '.json'), envelope)
        for key in enrichment:
            atomic_json(_root(config) / 'enrichment' / (key + '.json'), envelope)
    ready.update({draft.key: draft for draft in computed})
    return [ready[r.key] for r in shortlist if r.key in ready]


def protected_artifacts(config):
    """Retention may drop issue reports, never evidence of quota-deferred work."""
    from daily_agent.storage import load_material_library
    protected = set()
    for record in load_material_library(config).values():
        if record.raw.get('pool_deferral', {}).get('status') not in {'quota_deferred', 'screening_deferred'}:
            continue
        protected.update(_paths(config, record.to_dict()))
    return protected
