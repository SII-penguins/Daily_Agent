"""Explicit source-evidence qualification, separate from immutable reading notes.

The native policy certifies complete extracted-text reading and claim-scoped
pixel review. It NEVER certifies a whole-PDF transcription or pixel inventory.
Legacy reading.py stays byte-identical so its exact source/note protocol remains
reusable. Reuse grants no review, quota, completion or publication entitlement.
"""
from __future__ import annotations

from copy import copy, deepcopy
import hashlib
from pathlib import Path
import re

from daily_agent.paper_document import digest, valid_document, version_identity

STRICT = 'strict_fidelity_v1'
NATIVE = 'native_claim_evidence_v1'
SCHEMA = 2
PROTOCOL_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
_BAD_TEXT = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\ufffd]')


def configured_policy(config):
    value = config.sources.get('reading', {}).get('source_evidence_policy', STRICT)
    if value not in (STRICT, NATIVE):
        raise ValueError('Unknown source evidence policy')
    if value == NATIVE and config.sources.get('reading', {}).get('require_scientific_analysis', True) is not True:
        raise ValueError('Native daily selection requires scientific analysis')
    return value


def native_qualification_contract():
    return {'schema_version': 1, 'require_scientific_analysis': True}


def valid_native_qualification_contract(contract):
    # JSON booleans and integers must never compare equal at this boundary.
    return (isinstance(contract, dict) and set(contract) == {'schema_version', 'require_scientific_analysis'}
            and type(contract.get('schema_version')) is int and contract['schema_version'] == 1
            and contract.get('require_scientific_analysis') is True)


def _qualification_valid(record):
    marker = record.reading.get('source_evidence', {})
    return (isinstance(marker, dict) and type(marker.get('schema_version')) is int
            and marker['schema_version'] == SCHEMA
            and valid_native_qualification_contract(marker.get('qualification_contract')))


def expected_record_policy(config, record):
    """Native PDF mode leaves non-PDF text sources on their existing contract."""
    mode = configured_policy(config)
    doc = record.get('paper_document', {}) if isinstance(record, dict) else record.paper_document
    return NATIVE if mode == NATIVE and doc.get('source_type') == 'pdf' else STRICT


def record_policy(record):
    reading = record.get('reading', {}) if isinstance(record, dict) else record.reading
    marker = reading.get('source_evidence')
    if marker is None:
        verification = reading.get('verification', {})
        # Deleting the policy marker cannot downgrade a native review into
        # legacy flag-only qualification, even if old strict flags are forged.
        if ('native_visual_evidence' in reading or 'native_claim_review' in reading
                or (isinstance(verification, dict) and (
                    verification.get('source_evidence_policy') == NATIVE
                    or 'native_claim_review' in verification))):
            return 'invalid'
        return STRICT
    if not isinstance(marker, dict) or marker.get('policy') != NATIVE:
        return 'invalid'
    return NATIVE


def native_reading_config(config):
    """Keep unchanged raw-note protocol/settings; policy is frozen separately.

    Only this orchestration/qualification selector is excluded. All actual
    reading settings, writer settings, source/chunk hashes and budgets remain.
    Never give this view to issue enrollment, review, completion or selection.
    """
    if configured_policy(config) != NATIVE:
        return config
    view = copy(config)
    sources = dict(config.sources)
    sources['reading'] = {k: v for k, v in config.sources.get('reading', {}).items()
                          if k not in {'source_evidence_policy', 'require_scientific_analysis'}}
    object.__setattr__(view, 'sources', sources)
    return view


def source_text_gaps(record):
    """Local extraction observations, not automatic scientific rejection.

    Enough characters and no replacement glyphs do not prove correct column
    order or table association. Independent retained-claim pixels are mandatory.
    """
    gaps = []
    for page in record.paper_document.get('pages', []):
        if _BAD_TEXT.search(page.get('text', '')):
            gaps.append({'page': page.get('page'), 'kind': 'unreadable_text_glyphs',
                         'scope': 'native_text_only'})
        if page.get('ocr_status') in {'needed', 'failed'}:
            gaps.append({'page': page.get('page'), 'kind': 'source_text_extraction_incomplete',
                         'scope': 'native_text_only'})
    return gaps


def _binding(record):
    doc, reading = record.paper_document, record.reading
    return {'schema_version': SCHEMA, 'policy': NATIVE,
            'qualification_contract': native_qualification_contract(),
            'protocol_sha256': PROTOCOL_SHA256,
            'source_identity': version_identity(record),
            'source_pdf_sha256': doc.get('source_pdf_sha256'),
            'document_sha256': digest(doc),
            'chunks_sha256': digest(doc.get('chunks', [])),
            'reading_fingerprint': reading.get('fingerprint'),
            'notes_sha256': digest(reading.get('notes', [])),
            'scope': 'complete_native_text_reading_with_claim_scoped_pixels',
            'source_text_gaps': source_text_gaps(record)}


def bind_source_evidence(record, config):
    if configured_policy(config) != NATIVE:
        return
    record.reading['source_evidence'] = _binding(record)
    # Explicitly leave the old all-content visual certificate absent/false.
    if record.reading.get('visual', {}).get('strict_fidelity') is True:
        raise ValueError('Native policy cannot relabel a strict reconstruction')
    record.paper_text_status['sufficient_for_deep_summary'] = native_text_complete(record)


def _source_valid(record):
    doc = record.paper_document
    nested = doc.get('native_document')
    if nested is not None and nested != {k: v for k, v in doc.items() if k != 'native_document'}:
        return False
    if (doc.get('source_type') != 'pdf' or doc.get('evidence_basis') == 'image_transcription_reviewed'
            or not valid_document(doc, version_identity(record))
            or doc.get('document_kind') != 'full_text' or doc.get('title_match') is not True):
        return False
    try:
        path = Path(doc.get('source_pdf_path') or '')
        if not path.is_file() or path.stat().st_size > 64_000_000:
            return False
        data = path.read_bytes()
        expected = doc.get('source_pdf_sha256')
        if not isinstance(expected, str) or hashlib.sha256(data).hexdigest() != expected:
            return False
        import fitz
        with fitz.open(stream=data, filetype='pdf') as pdf:
            count = len(pdf)
        if (type(doc.get('source_page_count')) is not int or doc['source_page_count'] != count
                or [p.get('page') for p in doc.get('pages', [])] != list(range(1, count + 1))):
            return False
        return count > 0
    except (OSError, ValueError, TypeError, KeyError, RuntimeError):
        return False


def native_text_complete(record):
    """Complete exact native chunks, NOT every character/pixel trustworthy."""
    from daily_agent.reading import valid_note
    try:
        doc, reading = record.paper_document, record.reading
        if (record_policy(record) != NATIVE or not _qualification_valid(record)
                or reading.get('source_evidence') != _binding(record)
                or not _source_valid(record) or reading.get('complete') is not True
                or reading.get('failures') or reading.get('synthesis_error')):
            return False
        chunks, notes = doc.get('chunks', []), reading.get('notes', [])
        ids = [c['id'] for c in chunks]
        if (not ids or len(notes) != len(ids) or reading.get('read_chunk_ids') != ids
                or reading.get('total_chunks') != len(ids) or reading.get('coverage') != 1.0
                or [n.get('chunk_id') for n in notes] != ids):
            return False
        return all(valid_note(note, chunk, {}) for note, chunk in zip(notes, chunks))
    except (TypeError, ValueError, KeyError, AttributeError):
        return False


def verify_draft(draft, record):
    """Retain every original quote/number/field rule; branch only source policy."""
    from daily_agent.reading import verify_draft as legacy_verify
    # Legacy verification writes this exact missing-value sentinel as a list,
    # then interprets it as a new asserted string on replay. Normalize only the
    # sentinel for the unchanged mechanical check; it writes the same list back.
    if record_policy(record) == NATIVE and draft.draft_fields.get('method_steps') == ['not_stated']:
        draft.draft_fields['method_steps'] = 'not_stated'
    legacy_verify(draft, record)
    policy = record_policy(record)
    if policy == STRICT:
        return
    issues = []
    if policy != NATIVE or not native_text_complete(record):
        issues.append('原生全文阅读或来源绑定未完成')
    # Local bad glyphs remain visible in source_text_gaps. They confer no
    # support, but do not veto unrelated text or a claim independently checked
    # against the original source pixels. Quotes themselves remain unmodified.
    if issues:
        draft.verification.update(status='limited', label='证据不足，已降级展示')
        draft.verification.setdefault('issues', []).extend(issues)
        draft.draft_fields['confidence'] = 'low'
    draft.verification['source_evidence_policy'] = policy
    record.reading['verification'] = draft.verification


def source_ready(record):
    policy = record_policy(record)
    if policy == NATIVE:
        from daily_agent.native_visual_evidence import verified_native_visual_evidence
        return native_text_complete(record) and verified_native_visual_evidence(record)
    if policy != STRICT:
        return False
    visual = record.reading.get('visual', {})
    return record.paper_document.get('source_type') != 'pdf' or bool(
        visual.get('strict_fidelity') is True and visual.get('fidelity', {}).get('passed') is True)


def native_quality(record, draft):
    from daily_agent.native_claim_review import native_claim_support_valid
    if (record_policy(record) != NATIVE or not source_ready(record) or draft.key != record.key
            or draft.verification.get('status') != 'located'
            or draft.verification.get('semantic_support') != 'model_checked'
            or draft.verification.get('source_evidence_policy') != NATIVE
            or draft.draft_fields.get('confidence') == 'low'):
        return False
    # Read-only replay of all existing mechanical scientific support rules.
    checked, material = deepcopy(draft), deepcopy(record)
    verify_draft(checked, material)
    if (checked.verification.get('status') != 'located'
            or checked.verification.get('valid_fields') != draft.verification.get('valid_fields')
            or checked.draft_fields != draft.draft_fields
            or checked.claim_evidence != draft.claim_evidence):
        return False
    return native_claim_support_valid(record, draft)


def native_review_supported(record, draft):
    if not native_quality(record, draft):
        return False
    # New native contracts require deep analysis. Neither an absent field nor
    # a mutable completion_scope label can opt out of exact scientific proof.
    from daily_agent.scientific_analysis import scientific_analysis_valid
    return scientific_analysis_valid(record, draft)


def audit_reading(rows):
    """Shared policy-aware acceptance for cloud, quota, reuse and offline audit."""
    from daily_agent.reading import audit_reading as legacy_audit
    from daily_agent.models import MaterialRecord, EditorialDraft
    papers = []
    for row in rows:
        raw = row.get('material', row)
        if raw.get('item_type') != 'paper':
            continue
        if record_policy(raw) == STRICT:
            papers.extend(legacy_audit([row])['papers'])
            continue
        good = False
        text = False
        claims = False
        core = False
        gaps = []
        try:
            record = MaterialRecord.from_dict(raw)
            fields = row.get('final_fields', record.detail)
            # Actual published text is authoritative; stale material.detail must
            # never carry independent approval over different final_fields.
            if not isinstance(fields, dict) or ('final_fields' in row and fields != record.detail):
                raise ValueError('Published fields differ from reviewed material')
            draft = EditorialDraft(record.key, record.item_type, record.title, fields,
                                   claim_evidence=record.reading.get('claim_evidence', []),
                                   verification=record.reading.get('verification', {}))
            text = native_text_complete(record)
            gaps = source_text_gaps(record)
            good = native_review_supported(record, draft)
            core = good or native_quality(record, draft)
            claims = core
        except (KeyError, TypeError, ValueError, AttributeError, OSError):
            pass
        verification = raw.get('reading', {}).get('verification', {})
        papers.append({'key': raw['key'], 'title': raw.get('title', ''),
                       'document_kind': raw.get('paper_document', {}).get('document_kind'),
                       'extracted_chunks_read': raw.get('reading', {}).get('complete') is True,
                       'full_text_read': text, 'required_visual_pages': 0,
                       'visual_fidelity_passed': False,
                       'source_evidence_policy': record_policy(raw),
                       'claim_scoped_evidence_passed': good, 'source_text_gaps': gaps,
                       'claims_passed': claims, 'core_quality_passed': core,
                       'scientific_analysis_passed': good, 'require_scientific_analysis': True,
                       'completion_scope': 'core_and_scientific_analysis' if good else 'core_only' if core else 'incomplete',
                       'quality_passed': good,
                       'issues': verification.get('issues', [])})
    return {'papers': papers, 'paper_count': len(papers),
            'full_text_read_count': sum(p['full_text_read'] for p in papers),
            'quality_passed_count': sum(p['quality_passed'] for p in papers),
            'all_passed': bool(papers) and all(p['quality_passed'] for p in papers)}
