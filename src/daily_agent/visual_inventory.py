"""Reuse independently reviewed page assets for bounded editorial nomination.

This is a new derived inventory, never a native-text/image-match certificate.
The four lower-stage protocols and their cache files remain authoritative.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from daily_agent.paper_document import digest, evidence_settings
from daily_agent import visual_fidelity as fidelity_protocol

SCHEMA = 1
BASIS = 'independently_reviewed_page_asset_inventory'
SCOPE = ('用于原图选页的完整资产目录；不是原生文本与图片匹配证书，'
         '不证明数学或科学结论正确，不代替选图时的原页像素检查')


def _inventory(fidelity, source_contract):
    """Pure projection; only accepted candidates may enter the new schema."""
    if (not isinstance(fidelity, dict) or fidelity.get('passed') is not True
            or fidelity.get('basis') != 'separate_model_image_review'
            or fidelity.get('version') != fidelity_protocol.VERSION):
        return None
    results = fidelity.get('pages')
    if (not isinstance(results, list) or not results
            or type(fidelity.get('required_pages')) is not int
            or len(results) != fidelity['required_pages']):
        return None
    pages = []
    seen = set()
    for result in results:
        if not isinstance(result, dict):
            return None
        n = result.get('page')
        candidate, review = result.get('candidate'), result.get('review')
        if (type(n) is not int or n < 1 or n in seen
                or not fidelity_protocol.valid_candidate(candidate, {'page': n})
                or result.get('passed') is not True
                or result.get('basis') != 'separate_model_image_review'
                or result.get('version') != fidelity_protocol.VERSION
                or result.get('candidate_hash') != digest(candidate)
                or not fidelity_protocol.accepted(candidate, review)):
            return None
        for name in ('fingerprint', 'image_hash', 'native_text_hash'):
            value = result.get(name)
            if not isinstance(value, str) or len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
                return None
        seen.add(n)
        pages.append({'page': n, 'image_sha256': result['image_hash'],
                      'native_text_sha256': result['native_text_hash'],
                      'candidate_sha256': result['candidate_hash'],
                      'review_sha256': digest(review),
                      'fidelity_fingerprint': result['fingerprint'],
                      'asset_refs': [{'id': a['id'], 'kind': a['kind'], 'sha256': digest(a)}
                                     for a in candidate['assets']]})
    if (not isinstance(source_contract, dict)
            or set(source_contract) != {'source_type', 'source_pdf_sha256', 'source_page_count', 'native_document_sha256', 'page_numbers', 'required_pages'}
            or not isinstance(source_contract.get('native_document_sha256'), str)
            or len(source_contract['native_document_sha256']) != 64
            or any(c not in '0123456789abcdef' for c in source_contract['native_document_sha256'])
            or source_contract.get('page_numbers') != [p['page'] for p in pages]
            or source_contract.get('required_pages') != len(pages)):
        return None
    if source_contract.get('source_type') == 'pdf':
        count = source_contract.get('source_page_count')
        pdf_hash = source_contract.get('source_pdf_sha256')
        if (type(count) is not int or count < 1 or len(pages) != count
                or seen != set(range(1, count + 1))
                or not isinstance(pdf_hash, str) or len(pdf_hash) != 64
                or any(c not in '0123456789abcdef' for c in pdf_hash)):
            return None
    return {'schema_version': SCHEMA, 'basis': BASIS, 'scope': SCOPE,
            'source_contract': deepcopy(source_contract),
            'source_contract_sha256': digest(source_contract),
            'fidelity_protocol_sha256': fidelity_protocol.PROTOCOL_SHA256,
            'fidelity_manifest_sha256': digest(fidelity),
            'required_pages': len(pages), 'complete': True, 'pages': pages}


def verified_inventory(visual):
    """Consumers must recheck the exact projection, not trust completeness labels."""
    if not isinstance(visual, dict) or visual.get('strict_fidelity') is not True:
        return None
    actual = visual.get('asset_inventory')
    expected = _inventory(visual.get('fidelity'), actual.get('source_contract') if isinstance(actual, dict) else None)
    return actual if (expected is not None and actual == expected
                      and visual.get('required_pages') == expected['required_pages']) else None


def inventory_assets(visual, page):
    """Resolve compact references to the unchanged, independently reviewed assets."""
    inventory = verified_inventory(visual)
    if inventory is None or page not in inventory['pages']:
        return []
    result = next(r for r in visual['fidelity']['pages'] if r['page'] == page['page'])
    return deepcopy(result['candidate']['assets'])


def writer_visual_observations(visual):
    """Coverage metadata is not a second copy of already supplied reading text."""
    result = {k: deepcopy(v) for k, v in visual.items() if k not in {'fidelity', 'asset_inventory'}}
    inventory = verified_inventory(visual)
    if inventory:
        result['asset_inventory'] = {key: inventory[key] for key in
            ('schema_version', 'basis', 'scope', 'required_pages', 'complete', 'fidelity_manifest_sha256')}
    return result


def _source_contract(native):
    return {'source_type': native.get('source_type'),
            'source_pdf_sha256': native.get('source_pdf_sha256'),
            'source_page_count': native.get('source_page_count'),
            'native_document_sha256': digest(native),
            'page_numbers': [p.get('page') for p in native.get('pages', [])],
            'required_pages': sum(bool(p.get('visual_required')) for p in native.get('pages', []))}


def _incomplete_pdf(native):
    if native.get('source_type') != 'pdf':
        return False
    count = native.get('source_page_count')
    pages = native.get('pages', [])
    return (type(count) is not int or count < 1 or len(pages) != count
            or {p.get('page') for p in pages} != set(range(1, count + 1))
            or not native.get('source_pdf_sha256'))


def derive_inventory(record, config):
    """Called only after the original repair function revalidates every page."""
    visual = record.reading.get('visual', {})
    manifest = visual.get('fidelity')
    doc = record.paper_document
    native = doc.get('native_document')
    source_contract = _source_contract(native) if isinstance(native, dict) else None
    inventory = _inventory(manifest, source_contract)
    if (inventory is None or visual.get('strict_fidelity') is not True
            or doc.get('evidence_basis') != 'image_transcription_reviewed'
            or doc.get('fidelity_manifest_hash') != digest(manifest)
            or not isinstance(native, dict)):
        return None
    pages = native.get('pages', [])
    repaired = doc.get('pages', [])
    results = manifest['pages']
    if (len(pages) != len(results) or len(repaired) != len(results)
            or not all(p.get('visual_required') for p in pages)
            or [p.get('page') for p in pages] != [r['page'] for r in results]
            or [p.get('page') for p in repaired] != [r['page'] for r in results]):
        return None
    cfg = config.sources.get('reading', {})
    try:
        for page, rebuilt, result in zip(pages, repaired, results):
            actual = hashlib.sha256(Path(page.get('image_path') or '').read_bytes()).hexdigest()
            expected_key = digest([fidelity_protocol.PROTOCOL_SHA256, fidelity_protocol.VERSION,
                                   page['page'], actual, page['text'], evidence_settings(cfg),
                                   config.sources.get('llm_writer', {})])
            if (actual != page.get('image_hash') or actual != result['image_hash']
                    or result['native_text_hash'] != digest(page['text'])
                    or result['fingerprint'] != expected_key
                    or rebuilt.get('fidelity_hash') != result['candidate_hash']
                    or rebuilt.get('text_source') != 'image_transcription_reviewed'
                    or rebuilt.get('text') != fidelity_protocol.evidence_text(result['candidate'])):
                return None
    except (OSError, ValueError, TypeError, KeyError):
        return None
    return inventory


def _legacy_complete(visual, pages):
    from daily_agent.visual_reading import valid_visual
    if not isinstance(visual, dict) or visual.get('complete') is not True:
        return False
    notes = visual.get('notes')
    if (visual.get('required_pages') != len(pages) or not pages
            or not isinstance(notes, list) or len(notes) != len(pages)
            or visual.get('failures') != []):
        return False
    if any(not valid_visual(note, page) for note, page in zip(notes, pages)):
        return False
    mismatch = [n['page'] for n in notes if not n['text_matches_image'] or n['issues']]
    return visual.get('mismatch_pages') == mismatch and visual.get('passed') is (not mismatch)


def _assert_bound_jobs(record, config, execution):
    """Never hide an admitted pending/retired job behind newly reusable evidence."""
    from daily_agent.batch_execution import ExecutionConflict, digest as execution_digest
    from daily_agent import parent_writer as queue
    from daily_agent.batch_dispatch import _queue_observation
    from daily_agent.parent_writer import (_folder, validate_job, _contract,
                                          _assert_active, digest as queue_digest,
                                          PendingResponse, ExpiredResponse)
    from daily_agent.workflow_state import read_json
    native = record.paper_document.get('native_document', record.paper_document)
    bound = []
    folder = _folder(config.root)
    for page in native.get('pages', []):
        if not page.get('visual_required'):
            continue
        identity = (record.key, 'visual', 'page:' + str(page['page']), 0)
        operation = execution.existing_operation(identity)
        if operation is None:
            continue
        job_id = operation.get('queue_job_id')
        if not isinstance(job_id, str):
            raise ExecutionConflict('Bound visual operation has no verifiable queue job')
        with _queue_observation(queue, folder, execution.batch_id, job_id):
            job = read_json(folder / (job_id + '.job.json'))
            if not isinstance(job, dict):
                raise ExecutionConflict('Bound visual job is missing; preserve its original binding')
            validate_job(config.root, job)
            try:
                _assert_active(folder, job)
            except ValueError as exc:
                raise ExecutionConflict('Bound visual generation is retired; explicit reconciliation required') from exc
            if (job.get('job_id') != job_id or queue_digest(_contract(job)) != job_id
                    or execution_digest(_contract(job)) != operation.get('input_sha256')
                    or operation.get('queue_role') != job.get('stage')
                    or operation.get('retry_generation') is not None):
                raise ExecutionConflict('Bound visual queue provenance changed')
            try:
                payload = json.loads(job['prompt'].split('输入：\n', 1)[1])
                expected_image = {'path': str(Path(page['image_path']).resolve().relative_to(config.root.resolve())),
                                  'sha256': page['image_hash']}
                if (not job['prompt'].startswith('核对附带的论文页面图片和抽取文本。')
                        or payload != {'page': page['page'], 'extracted_text': page['text']}
                        or job['images'] != [expected_image] or job['stage'] != 'review'):
                    raise ValueError('source contract')
            except (KeyError, TypeError, ValueError, IndexError) as exc:
                raise ExecutionConflict('Bound visual job no longer matches its native source page') from exc
            answer = read_json(folder / (job_id + '.answer.json'))
            if answer is None:
                if datetime.now(timezone.utc) >= datetime.fromisoformat(job['expires_at']):
                    raise ExpiredResponse(job_id)
                raise PendingResponse(job_id)
            if (not isinstance(answer, dict) or answer.get('job_id') != job_id
                    or answer.get('input_sha256') != job_id
                    or answer.get('response_sha256') != queue_digest(answer.get('response'))
                    or not answer.get('worker_id') or not answer.get('model')):
                raise ExecutionConflict('Bound visual answer identity changed')
            lease = read_json(folder / (job_id + '.claim.json'))
            try:
                received = datetime.fromisoformat(answer['received_at'])
                created = datetime.fromisoformat(job['created_at'])
                expires = datetime.fromisoformat(job['expires_at'])
                if received.tzinfo is None or not created <= received <= expires:
                    raise ValueError('answer validity at import')
                if lease is not None:
                    if (not isinstance(lease, dict) or lease.get('job_id') != job_id
                            or lease.get('worker_id') != answer['worker_id']
                            or not isinstance(lease.get('token'), str) or not lease['token']
                            or type(lease.get('generation')) is not int or lease['generation'] < 1
                            or received >= datetime.fromisoformat(lease['expires_at'])):
                        raise ValueError('claim validity at import')
                elif job.get('retry'):
                    raise ValueError('retry claim missing')
            except (KeyError, TypeError, ValueError) as exc:
                raise ExecutionConflict('Bound visual answer claim provenance changed') from exc
        bound.append(page)
    return bound


def _read_visual_evidence(records, config, invoke=None, *, execution=None):
    """Keep legacy behavior unless explicitly using the fidelity-first workflow."""
    from daily_agent.visual_reading import read_visuals
    if execution is None or not config.sources.get('reading', {}).get('fidelity_enabled', False):
        kwargs = {'execution': execution} if execution is not None else {}
        read_visuals(records, config, invoke, **kwargs)
        fidelity_protocol.repair_visuals(records, config, invoke, **kwargs)
        return
    for record in records:
        native = record.paper_document.get('native_document', record.paper_document)
        required = [p for p in native.get('pages', []) if p.get('visual_required')]
        bound = _assert_bound_jobs(record, config, execution)
        # Process admitted original requests before reconstruction changes text.
        # The subset creates no new visual slots for unbound pages.
        if bound:
            shadow = deepcopy(record)
            shadow.paper_document = {**deepcopy(native), 'pages': deepcopy(bound)}
            read_visuals([shadow], config, invoke, execution=execution)
        # Reuse only cache entries validated by the unchanged native protocol.
        # This cache-only pass runs on a shadow and cannot downgrade the record.
        shadow = deepcopy(record)
        shadow.paper_document = deepcopy(native)
        read_visuals([shadow], config, None)
        cached = shadow.reading.get('visual', {})
        previous = record.reading.get('visual', {})
        if _legacy_complete(cached, required):
            # Preserve an identical, already attached evidence object verbatim.
            keep = previous if _legacy_complete(previous, required) and previous.get('notes') == cached.get('notes') else cached
            record.reading['visual'] = deepcopy(keep)
        else:
            record.reading['visual'] = {}
        fidelity_protocol.repair_visuals([record], config, invoke, execution=execution)
        visual = record.reading.setdefault('visual', {})
        inventory = derive_inventory(record, config)
        if inventory is not None:
            visual['required_pages'] = len(required)
            visual['asset_inventory'] = inventory
            continue
        incomplete_source = _incomplete_pdf(native)
        if visual.get('strict_fidelity') is True and not incomplete_source:
            from daily_agent.workflow_state import StateCorrupt
            raise StateCorrupt('Reviewed visual inventory does not match the complete repaired source')
        from daily_agent.terminal_fidelity import stop_if_terminal
        stop_if_terminal(record, config, execution, cached_visual=cached)
        # Fallback overwrites visual in the original function; preserve its
        # current notes while restoring the actual failed fidelity evidence.
        failure = deepcopy(visual.get('fidelity'))
        shadow = deepcopy(record)
        shadow.paper_document = deepcopy(native)
        try:
            read_visuals([shadow], config, invoke, execution=execution)
        finally:
            current = deepcopy(shadow.reading.get('visual', {}))
            record.reading['visual'] = current
            current.pop('asset_inventory', None)
            if failure is not None:
                current['fidelity'] = failure
            current['strict_fidelity'] = False
            if incomplete_source:
                current['inventory_failure'] = {'basis': BASIS, 'reason': 'source_page_coverage_incomplete'}
            if required:
                record.paper_text_status['sufficient_for_deep_summary'] = False


class _CancelledInvocation(BaseException):
    """Carry real cancellation across unchanged lower-stage Exception handlers."""

    def __init__(self, original):
        self.original = original


def read_visual_evidence(records, config, invoke=None, *, execution=None):
    """Propagate cancellation without turning it into a model/evidence failure."""
    from daily_agent.workflow_runtime import WorkflowCancelled
    cancelled = []

    def guarded(*args, **kwargs):
        if cancelled:
            raise _CancelledInvocation(cancelled[0])
        try:
            return invoke(*args, **kwargs)
        except WorkflowCancelled as exc:
            if not cancelled:
                cancelled.append(exc)
            raise _CancelledInvocation(cancelled[0]) from exc

    try:
        return _read_visual_evidence(records, config, guarded if invoke is not None else None, execution=execution)
    except _CancelledInvocation as exc:
        raise exc.original

# Native-caption nomination is deliberately separate from the strict,
# independently pixel-reviewed inventory above. These hints cannot establish
# either that a visual is present or that its contents support a claim.
NATIVE_NOMINATION_VERSION = 1
NATIVE_CAPTION_SCOPE = ('Heuristic caption candidates from native text across supplied pages; '
                        'not a complete visual inventory, pixel review, or claim evidence')


def native_caption_inventory(native):
    """Scan every native page for caption starts, retaining original text spans.

    In-body references ("see Figure 4", "Table 7, ...", "Figure 3 shows ...")
    are not captions. Missing/unrecognized captions remain unknown, not absent.
    No page images are loaded or rendered here.
    """
    import re
    label = re.compile(r'^[ \t]*(?P<kind>figure|fig\.?|table|图|表)[ \t]*'
                       r'(?P<number>(?:[A-Za-z][ \t]*)?\d+(?:[.-]\d+)*|[IVXLCDM]+)'
                       r'[ \t]*[.:：、][ \t]*(?=\S)', re.I | re.M)
    patterns = {
        'qualitative_result': r'qualitative|定性|trajectory overlays?|(?:prediction|predicted).{0,100}(?:ground.truth|\bGT\b)|(?:ground.truth|\bGT\b).{0,100}(?:prediction|predicted)|轨迹.*(?:预测|真值)',
        'descriptive_figure': r'statistical distribution|(?:dataset|tool).{0,35}statistics|数据集.*(?:统计|分布)',
        'framework': r'framework|architecture|system overview|pipeline|action module|框架|架构|流程',
        'quantitative_plot': r'performance|accuracy|success.rate|ablation|convergence|scaling|robustness|learning curve|training curve|benchmark results|实验结果|消融|曲线|准确率|性能',
    }
    pages = sorted((p for p in native.get('pages', []) if isinstance(p, dict)
                    and type(p.get('page')) is int and p['page'] > 0), key=lambda p: p['page'])
    numbers = [p['page'] for p in pages]
    if len(numbers) != len(set(numbers)):
        raise ValueError('Duplicate native source page')
    captions, math_pages = [], []
    for page in pages:
        text = page.get('text', '')
        if not isinstance(text, str):
            raise ValueError('Native page text must be a string')
        matches = list(label.finditer(text))
        for index, match in enumerate(matches):
            # A bounded context prevents an entire results section being passed
            # off as one caption. The original exact span is retained for audit.
            stop = min(len(text), match.start() + 1200,
                       matches[index + 1].start() if index + 1 < len(matches) else len(text))
            ends = [m.end() for m in re.finditer(r'\n', text[match.start():stop])]
            if len(ends) >= 5:
                stop = match.start() + ends[4]
            caption = text[match.start():stop].rstrip()
            stop = match.start() + len(caption)
            kind = 'table' if match['kind'].lower() in {'table', '表'} else 'figure'
            role_text = ' '.join(caption.split())
            if kind == 'table':
                role = ('ablation_table' if re.search(r'ablation|消融', role_text, re.I) else
                        'result_table' if re.search(patterns['quantitative_plot'] + r'|efficiency comparisons?|comparison of simulation|comparison with and without|correlation analysis|比较', role_text, re.I)
                        else 'table')
            else:
                role = next((name for name, pattern in patterns.items()
                             if re.search(pattern, role_text, re.I)), 'figure')
            captions.append({'id': f"p{page['page']}:{kind}:{match['number']}:{match.start()}",
                'page': page['page'], 'kind': kind, 'number': match['number'],
                'role_hint': role, 'caption': caption, 'text_span': [match.start(), stop],
                'native_text_sha256': hashlib.sha256(text.encode()).hexdigest(),
                'basis': 'native_caption_candidate_not_pixel_verified'})
        # Numbered mathematics is only a nomination hint. Merely mentioning an
        # objective, equation, or table is insufficient to fabricate a formula.
        if (re.search(r'[=∑∫∂≤≥]', text)
                and re.search(r'\(\s*\d+(?:[a-z])?\s*\)\s*(?:\n|$)', text, re.I)):
            math_pages.append(page['page'])
    count = native.get('source_page_count')
    return {'schema_version': NATIVE_NOMINATION_VERSION,
            'basis': 'native_caption_candidates', 'scope': NATIVE_CAPTION_SCOPE,
            'source_pdf_sha256': native.get('source_pdf_sha256'),
            'source_page_count': count, 'scanned_native_pages': numbers,
            'complete_native_page_scan': type(count) is int and numbers == list(range(1, count + 1)),
            'visual_inventory_complete': False, 'captions': captions,
            'numbered_math_candidate_pages': math_pages}


def native_nomination_plan(native, notes=(), chunks=(), *, limit=8):
    """Deterministic role diversity before duplicate tables or loose references.

    One page per available role is reserved before remaining caption pages are
    filled. Quantitative plots and qualitative results occupy distinct roles.
    The cap applies to pixels, not the full native text/caption scan.
    """
    if type(limit) is not int or not 0 <= limit <= 8:
        raise ValueError('Native nomination exceeds original page budget')
    inventory = native_caption_inventory(native)
    roles = ('quantitative_plot', 'qualitative_result', 'framework', 'result_table',
             'ablation_table', 'numbered_math', 'descriptive_figure', 'figure', 'table')
    queues = {role: [] for role in roles}
    reasons = {}
    for caption in inventory['captions']:
        n, role = caption['page'], caption['role_hint']
        if n not in queues[role]: queues[role].append(n)
        reasons.setdefault(n, []).append(caption['id'])
    queues['numbered_math'] = list(inventory['numbered_math_candidate_pages'])
    chosen = []
    # A page may already cover multiple roles; do not allocate another slot to
    # one of those roles before giving a still-unrepresented role its turn.
    for role in roles:
        if queues[role] and not any(n in chosen for n in queues[role]) and len(chosen) < limit:
            chosen.append(queues[role].pop(0))
    # Once roles are represented, fill deterministically without wasting turns
    # on duplicate pages shared by several caption roles.
    while any(queues.values()) and len(chosen) < limit:
        for role in roles:
            while queues[role] and queues[role][0] in chosen:
                queues[role].pop(0)
            if queues[role] and len(chosen) < limit:
                chosen.append(queues[role].pop(0))
    # Notes remain a lower-confidence fallback only after caption candidates.
    import re
    by_id = {c.get('id'): c for c in chunks if isinstance(c, dict)}
    note_pages = set()
    for note in notes:
        if not isinstance(note, dict): continue
        page = by_id.get(note.get('chunk_id'), {}).get('page')
        if (type(page) is int and page in inventory['scanned_native_pages']
                and re.search(r'\b(?:fig(?:ure)?\.?|table|equation|eq\.)\s*\d|图\s*\d|表\s*\d|公式',
                              json.dumps(note, ensure_ascii=False), re.I)):
            note_pages.add(page)
    for page in sorted(note_pages):
        if page not in chosen and len(chosen) < limit: chosen.append(page)
    candidates = []
    for page in chosen:
        hints = [role for role in roles if (role == 'numbered_math' and page in inventory['numbered_math_candidate_pages'])
                 or any(c['page'] == page and c['role_hint'] == role for c in inventory['captions'])]
        candidates.append({'page': page, 'role_hints': hints or ['reading_note_reference'],
                           'caption_ids': reasons.get(page, []), 'pixels_reviewed': False})
    omitted = [{**caption, 'nomination_status': 'not_nominated_page_budget', 'pixels_reviewed': False}
               for caption in inventory['captions'] if caption['page'] not in chosen]
    return {'schema_version': NATIVE_NOMINATION_VERSION, 'inventory': inventory,
            'page_budget': limit, 'candidate_pages': chosen, 'candidates': candidates,
            'not_nominated_captions': omitted,
            'absence_status': 'unknown_until_supplied_pixels_reviewed',
            'scope': 'Omitted and undetected visuals are unreviewed; neither means absent from the paper'}
