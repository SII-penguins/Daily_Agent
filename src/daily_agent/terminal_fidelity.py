"""Negative-only stop evidence for the strict incremental cloud workflow.

This is a derived, freshly checked observation, never an acceptance cache or an
operation/completion/publication right. The original page protocols stay intact.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from daily_agent import visual_fidelity as fidelity
from daily_agent.paper_document import digest, evidence_settings, load_json, version_identity
from daily_agent.workflow_state import StateCorrupt, read_json


class TerminalFidelityRejected(Exception):
    """Only a fully evidenced, finite independent rejection may yield a sibling."""

    def __init__(self, material, evidence):
        self.material, self.evidence = material, evidence
        super().__init__('Independent page fidelity rejected after both finite attempts')


def _scope(config, execution):
    from daily_agent.cloud_workflow import PROFILE
    from daily_agent.incremental_issue import PROTOCOL
    cloud = getattr(config, 'delivery', {}).get('cloud', {})
    reading = config.sources.get('reading', {})
    return (execution is not None and execution.contract['protocol'] == PROTOCOL
            and cloud.get('profile') == PROFILE
            and reading.get('fidelity_enabled') is True
            and reading.get('visual_enabled', True) is True
            and config.sources.get('llm_writer', {}).get('provider') == 'parent_queue')


def _receipt(config, execution, operation, role):
    """Read the admitted, still-active answer without changing/retiring its job."""
    from daily_agent import parent_writer as queue
    from daily_agent.batch_execution import digest as execution_digest, ExecutionConflict
    op = execution.existing_operation(operation)
    if op is None:
        return None
    job_id = op.get('queue_job_id')
    if not isinstance(job_id, str):
        raise ExecutionConflict('Fidelity receipt has no bound queue job')
    folder = queue._folder(config.root)
    job = read_json(folder / f'{job_id}.job.json')
    queue.validate_job(config.root, job)
    try:
        queue._assert_active(folder, job)
    except ValueError as exc:
        raise ExecutionConflict('Fidelity receipt generation is retired') from exc
    if (job['job_id'] != job_id or execution_digest(queue._contract(job)) != op['input_sha256']
            or op.get('queue_role') != role or job['stage'] != role
            or op.get('retry_generation') is not None):
        raise ExecutionConflict('Fidelity receipt operation binding changed')
    answer = read_json(folder / f'{job_id}.answer.json')
    if answer is None:
        if datetime.now(timezone.utc) >= datetime.fromisoformat(job['expires_at']):
            raise queue.ExpiredResponse(job_id)
        raise queue.PendingResponse(job_id)
    if (not isinstance(answer, dict) or answer.get('job_id') != job_id
            or answer.get('input_sha256') != job_id
            or answer.get('response_sha256') != queue.digest(answer.get('response'))
            or not isinstance(answer.get('worker_id'), str) or not answer['worker_id']
            or not isinstance(answer.get('model'), str) or not answer['model']):
        raise StateCorrupt('Fidelity answer identity changed')
    lease = read_json(folder / f'{job_id}.claim.json')
    try:
        received = datetime.fromisoformat(answer['received_at'])
        if (received.tzinfo is None or not datetime.fromisoformat(job['created_at'])
                <= received <= datetime.fromisoformat(job['expires_at'])):
            raise ValueError('answer import time')
        if lease is not None:
            if (not isinstance(lease, dict) or lease.get('job_id') != job_id
                    or lease.get('worker_id') != answer['worker_id']
                    or not isinstance(lease.get('token'), str) or not lease['token']
                    or type(lease.get('generation')) is not int or lease['generation'] < 1
                    or received >= datetime.fromisoformat(lease['expires_at'])):
                raise ValueError('claim import time')
        elif job.get('retry'):
            raise ValueError('retry claim missing')
        else:
            # Initial unclaimed imports are valid in the original transport.
            # Their missing claim cannot prove this stronger negative shortcut.
            return None
    except (KeyError, TypeError, ValueError) as exc:
        raise StateCorrupt('Fidelity answer claim provenance changed') from exc
    return job, answer


def _attempt(config, execution, record, page, key, ordinal, previous):
    """Require the exact candidate AND independently imported reviewer answer."""
    saved = load_json(config.root / 'data/reading/fidelity' / f'{key}.attempt-{ordinal+1}.json')
    if (not isinstance(saved, dict) or saved.get('fingerprint') != key
            or saved.get('page') != page['page'] or saved.get('version') != fidelity.VERSION
            or saved.get('basis') != 'separate_model_image_review'
            or saved.get('image_hash') != page['image_hash']
            or saved.get('native_text_hash') != digest(page['text'])
            or not fidelity.valid_candidate(saved.get('candidate'), page)
            or saved.get('candidate_hash') != digest(saved['candidate'])
            or not fidelity.valid_review(saved.get('review'), saved['candidate'])
            or saved.get('passed') is not fidelity.accepted(saved['candidate'], saved['review'])):
        return None
    transcribe = _receipt(config, execution, (record.key, 'fidelity', 'transcribe:'+str(page['page']), ordinal), 'draft')
    review = _receipt(config, execution, (record.key, 'fidelity', 'review:'+str(page['page']), ordinal), 'review')
    if transcribe is None or review is None:
        return None
    tj, ta = transcribe; rj, ra = review
    if (ta['response'] != saved['candidate'] or ra['response'] != saved['review']
            or ta['worker_id'] == ra['worker_id'] or tj['job_id'] == rj['job_id']):
        return None
    try:
        data = json.loads(tj['prompt'][len(fidelity.TRANSCRIBE):])
        detail = data['detail_images']
        expected = {'page': page['page'], 'native_text': page['text'],
                    'previous_review': previous['review'] if previous else None,
                    'previous_candidate': previous['candidate'] if previous else None,
                    'detail_images': detail}
        image = {'path': str(Path(page['image_path']).resolve().relative_to(config.root.resolve())),
                 'sha256': page['image_hash']}
        if (tj['prompt'] != fidelity.TRANSCRIBE + json.dumps(expected, ensure_ascii=False)
                or rj['prompt'] != fidelity.REVIEW + json.dumps({**saved['candidate'], 'detail_images': detail}, ensure_ascii=False)
                or not tj['images'] or tj['images'][0] != image or tj['images'] != rj['images']
                or saved.get('detail_hash') != (digest(detail) if detail else None)):
            return None
        if detail is None:
            if len(tj['images']) != 1:
                return None
        elif (ordinal != 1 or not config.sources['reading'].get('fidelity_detail_crops', True)
                or not isinstance(detail, dict) or set(detail) != {'order', 'focus', 'hashes'}
                or detail['hashes'] != [i['sha256'] for i in tj['images'][1:]]
                or len(detail['hashes']) not in {4, 8}):
            return None
    except (KeyError, TypeError, ValueError):
        return None
    return saved, {'attempt': ordinal+1, 'candidate_sha256': saved['candidate_hash'],
                   'review_sha256': digest(saved['review']), 'transcribe_job_id': tj['job_id'],
                   'review_job_id': rj['job_id'], 'transcriber': ta['worker_id'], 'reviewer': ra['worker_id']}


def terminal_rejection(record, config, execution):
    """Missing/partial/uncertain evidence is NOT a terminal scientific rejection."""
    if not _scope(config, execution):
        return None
    state = execution.snapshot()
    if state['reservations']:
        return None
    native = record.paper_document.get('native_document', record.paper_document)
    frozen = next((c for c in execution.contract['candidates'] if c['key'] == record.key), None)
    if (not frozen or version_identity(record) != frozen['version']
            or native != frozen['input'].get('paper_document')
            or native.get('source_type') != 'pdf' or native.get('document_kind') != 'full_text'
            or execution.contract['settings']['reading'] != config.sources['reading']
            or execution.contract['settings']['llm_writer'] != config.sources['llm_writer']):
        return None
    pages = native.get('pages', [])
    count = native.get('source_page_count')
    if (type(count) is not int or count < 1 or len(pages) != count
            or [p.get('page') for p in pages] != list(range(1, count+1))
            or not all(p.get('visual_required') is True for p in pages)):
        return None
    visual = record.reading.get('visual', {})
    manifest = visual.get('fidelity', {})
    results = manifest.get('pages', [])
    if (visual.get('strict_fidelity') is not False or manifest.get('passed') is not False
            or manifest.get('version') != fidelity.VERSION or manifest.get('basis') != 'separate_model_image_review'
            or manifest.get('required_pages') != count or len(results) != count
            or [r.get('page') for r in results] != list(range(1, count+1))):
        return None
    try:
        import fitz
        content = Path(native['source_pdf_path']).read_bytes()
        if hashlib.sha256(content).hexdigest() != native.get('source_pdf_sha256'):
            return None
        with fitz.open(stream=content, filetype='pdf') as pdf:
            if len(pdf) != count:
                return None
        for page in pages:
            if hashlib.sha256(Path(page['image_path']).read_bytes()).hexdigest() != page['image_hash']:
                return None
    except (OSError, KeyError, TypeError, ValueError, RuntimeError):
        return None
    from daily_agent import parent_writer as queue
    from daily_agent.batch_dispatch import _queue_observation
    operations = [op for op in state['operations'].values()
                  if op['identity'][0] == record.key and op['identity'][1] == 'fidelity']
    if not operations or not operations[0].get('queue_job_id'):
        return None
    evidence, failed, readers, reviewers = [], [], set(), set()
    with _queue_observation(queue, queue._folder(config.root), execution.batch_id, operations[0]['queue_job_id']):
        for page, result in zip(pages, results):
            key = digest([fidelity.PROTOCOL_SHA256, fidelity.VERSION, page['page'], page['image_hash'],
                          page['text'], evidence_settings(config.sources['reading']), config.sources['llm_writer']])
            attempts, previous, rejected_reviews = [], None, []
            for ordinal in range(2):
                checked = _attempt(config, execution, record, page, key, ordinal, previous)
                if checked is None:
                    return None
                saved, receipt = checked
                attempts.append(receipt); readers.add(receipt['transcriber']); reviewers.add(receipt['reviewer'])
                previous = saved
                review = saved['review']
                rejected_reviews.append(bool(review['issues'] or not review['text_supported']
                                             or not review['inventory_complete']
                                             or any(not c['supported'] for c in review['checks'])))
                if saved['passed']:
                    if result != saved:
                        return None
                    break
            if not previous['passed']:
                if (len(attempts) != 2 or result != {'page': page['page'], 'passed': False,
                        'reason': '逐页重建/独立复核未通过', 'last_review': previous['review']}):
                    return None
                # Candidate uncertainty alone is not an independent rejection.
                if not all(rejected_reviews):
                    return None
                failed.append(page['page'])
            evidence.append({'page': page['page'], 'passed': previous['passed'], 'attempts': attempts})
    if not failed or readers & reviewers:
        return None
    return {'schema': 1, 'state': 'blocked', 'reason': 'terminal_independent_fidelity_rejection',
            'scope': 'negative_observation_only', 'source_sha256': native['source_pdf_sha256'],
            'native_document_sha256': digest(native), 'fidelity_manifest_sha256': digest(manifest),
            'failed_pages': failed, 'required_pages': count, 'pages': evidence}


def stop_if_terminal(record, config, execution, *, cached_visual=None):
    evidence = terminal_rejection(record, config, execution)
    if evidence is None:
        return
    from daily_agent.reading import read_papers
    # Note shape/quote matching cannot establish document-wide reading identity.
    # Reuse only the unchanged reader's exact source/settings/protocol caches.
    # This cache-only shadow never admits a model operation or stage window.
    shadow = deepcopy(record)
    read_papers([shadow], config, None)
    visual = deepcopy(record.reading['visual'])
    if cached_visual and cached_visual.get('notes') and not visual.get('notes'):
        visual = {**deepcopy(cached_visual), **visual}
    record.reading.update(shadow.reading)
    record.reading['visual'] = visual
    chunks = record.paper_document.get('chunks', [])
    read = record.reading['read_chunk_ids']
    record.reading.update(blocked=deepcopy(evidence),
                          unread_chunk_ids=[c['id'] for c in chunks if c['id'] not in read])
    record.reading['visual']['required_pages'] = evidence['required_pages']
    record.reading['verification'] = {'status': 'limited', 'semantic_support': 'not_checked',
                                     'issues': ['independent_page_fidelity_rejected_after_two_attempts']}
    record.paper_text_status['sufficient_for_deep_summary'] = False
    raise TerminalFidelityRejected(record.key, evidence)
