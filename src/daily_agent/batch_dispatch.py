"""Bounded, sequential cross-material dispatch; queue observations grant no rights.

The caller retains the one issue/pipeline/Execution lease. Each frozen material
is visited once per launch. This module never claims, answers, retries or cancels
jobs, and its rebuildable wait-set cannot authorize an operation or publication.
"""
from datetime import datetime, timezone
from contextlib import contextmanager, nullcontext
from copy import deepcopy
import json
import re
from daily_agent.batch_execution import digest, ExecutionConflict
from daily_agent.parent_writer import PendingResponse
from daily_agent.workflow_state import atomic_json, read_json, StateCorrupt


class PendingBatchResponse(PendingResponse):
    def __init__(self, batch_id, jobs):
        if not jobs:
            raise ValueError('A suspended batch needs an observed queue job')
        self.batch_id = batch_id
        self.jobs = jobs
        self.expired = any(row['status'] == 'expired' for row in jobs)
        primary = next((row for row in jobs if row['status'] == 'expired'), jobs[0])
        super().__init__(primary['job_id'])


def response_payload(exc):
    """Keep legacy output stable while exposing all bounded sibling jobs."""
    result = {'state': 'expired_parent_writer' if getattr(exc, 'expired', False)
              else 'awaiting_parent_writer', 'job_id': exc.job_id}
    if getattr(exc, 'expired', False):
        result.update(auto_resume=False, recovery_required='expired_requires_authorized_recovery')
    if isinstance(exc, PendingBatchResponse):
        result.update(batch_id=exc.batch_id, jobs=exc.jobs)
    if isinstance(exc, QueueObservationBusy):
        result.update(batch_id=exc.batch_id, reason='queue_observation_busy')
    if getattr(exc, 'queue_capacity', False):
        result.update(reason='queue_capacity', queue_limit=200)
    return result


def publish_wait_set(config, execution, suspended):
    """Rebuild one observation per actual admitted job under the queue lock.

    Include all threaded page/chunk requests admitted before a stage suspended,
    not just the first BaseException returned by its executor. An answer arriving
    during this scan is reported as answer_available; the next launch still
    validates it through the original transport and scientific stages.
    """
    from daily_agent import parent_writer as queue
    state = execution.snapshot()
    if state['reservations']:
        raise ExecutionConflict('Wait-set publication requires settled stage windows')
    operations = state['operations']
    for key, suspension in suspended.items():
        job_id = suspension['job_id']
        if not any(op['identity'][0] == key and op['queue_job_id'] == job_id
                   for op in operations.values()):
            raise ExecutionConflict('Suspension is not bound to an admitted material/job')
    folder = queue._folder(config.root)
    jobs = []
    seen = {}
    # The Execution lease is already held: preserve execution -> queue ordering.
    admitted_jobs = [op['queue_job_id'] for op in operations.values() if op['queue_job_id']]
    known_expired = next((row['job_id'] for row in suspended.values() if row['expired']), None)
    context = (_queue_observation(queue, folder, execution.batch_id, known_expired or admitted_jobs[0], bool(known_expired))
               if admitted_jobs else nullcontext())
    with context:
        now = datetime.now(timezone.utc)
        job_cache = {}  # One queue-lock observation only; no cached approval across resumes.
        for material in (row['key'] for row in execution.contract['candidates']):
            for op in operations.values():
                job_id = op['queue_job_id']
                if op['identity'][0] != material:
                    continue
                if not job_id:
                    raise ExecutionConflict('Dispatch requires an actual queue job identity')
                if job_id not in job_cache:
                    job_cache[job_id] = read_json(folder / f'{job_id}.job.json')
                job = job_cache[job_id]
                queue.validate_job(config.root, job)
                queue._assert_active(folder, job)
                if digest(queue._contract(job)) != op['input_sha256'] or job['stage'] != op['queue_role']:
                    raise StateCorrupt('Wait-set queue evidence differs from admitted operation')
                binding = {'material': material, 'operation': op['identity']}
                if job_id in seen:
                    seen[job_id]['bindings'].append(binding)
                    continue
                answer = read_json(folder / f'{job_id}.answer.json')
                if answer is not None:
                    if (answer.get('job_id') != job_id or answer.get('input_sha256') != job_id
                            or queue.digest(answer.get('response')) != answer.get('response_sha256')):
                        raise StateCorrupt('Wait-set answer integrity mismatch')
                    if job_id != suspended.get(material, {}).get('job_id'):
                        continue
                    status = 'answer_available'
                else:
                    status = 'expired' if now >= datetime.fromisoformat(job['expires_at']) else 'pending'
                lease = read_json(folder / f'{job_id}.claim.json')
                claim = None
                if lease is not None:
                    if (lease.get('job_id') != job_id or not isinstance(lease.get('worker_id'), str)
                            or not isinstance(lease.get('token'), str) or type(lease.get('generation')) is not int):
                        raise StateCorrupt('Wait-set claim identity mismatch')
                    try:
                        live = now < datetime.fromisoformat(lease['expires_at'])
                    except (KeyError, ValueError, TypeError) as exc:
                        raise StateCorrupt('Wait-set claim deadline invalid') from exc
                    claim = {'worker_id': lease['worker_id'], 'generation': lease['generation'],
                             'expires_at': lease['expires_at'], 'active': live}
                row = {'material': material, 'job_id': job_id, 'status': status,
                             'operation': op['identity'], 'role': op['queue_role'],
                             'expires_at': job['expires_at'], 'claim': claim, 'bindings': [binding]}
                jobs.append(row)
                seen[job_id] = row
    if len(jobs) > MAX_WAIT_JOBS:
        raise ExecutionConflict('Bounded wait-set observation limit exceeded')
    payload = {'schema': 1, 'batch_id': execution.batch_id,
               'contract_sha256': digest(execution.contract), 'journal_sha256': digest(state),
               'jobs': jobs, 'committed_materials': list(state['completed']),
               'scope': 'rebuildable_observation_only',
               'auto_resume': not any(row['status'] == 'expired' for row in jobs),
               'recovery_required': ('expired_requires_authorized_recovery'
                    if any(row['status'] == 'expired' for row in jobs) else None)}
    atomic_json(execution.folder / 'waiting.json', {'payload': payload, 'sha256': digest(payload)})
    return jobs


MAX_WAIT_JOBS = 512
MAX_CHECKPOINT_BYTES = 512 * 1024


class QueueObservationBusy(PendingResponse):
    """A contended derived observation is suspension, not a backend failure."""
    def __init__(self, batch_id, job_id, expired=False):
        self.expired = expired
        self.batch_id = batch_id
        super().__init__(job_id)


@contextmanager
def _queue_observation(queue, folder, batch_id, job_id, expired=False):
    manager = queue.queue_lock(folder / 'queue.lock', timeout=1, strict_io=True)
    try:
        manager.__enter__()
    except TimeoutError as exc:
        raise QueueObservationBusy(batch_id, job_id, expired) from exc
    try:
        yield
    finally:
        manager.__exit__(None, None, None)


def validate_response_payload(value, day):
    """Validate bounded stdout diagnostics; never turn them into admission rights."""
    try:
        if not isinstance(value, dict) or value.get('state') not in {'awaiting_parent_writer', 'expired_parent_writer'}:
            return None
        if len(json.dumps(value).encode()) > MAX_CHECKPOINT_BYTES:
            return None
        def text(v, limit=1024): return isinstance(v, str) and 0 < len(v) <= limit
        def job_id(v): return isinstance(v, str) and re.fullmatch(r'[0-9a-f]{64}', v) is not None
        def timestamp(v):
            return isinstance(v, str) and datetime.fromisoformat(v).utcoffset() is not None
        capacity = value.get('reason') == 'queue_capacity'
        if capacity and (value.get('queue_limit') != 200 or value['state'] != 'awaiting_parent_writer'):
            return None
        no_admission = capacity and value.get('job_id') is None and 'jobs' not in value
        if (not no_admission and not job_id(value.get('job_id'))) or value.get('issue_date', str(day)) != str(day):
            return None
        result = {key:value[key] for key in ('state', 'job_id')}
        result['issue_date'] = str(day)
        if value['state'] == 'expired_parent_writer':
            if value.get('auto_resume', False) is not False or value.get('recovery_required', 'expired_requires_authorized_recovery') != 'expired_requires_authorized_recovery':
                return None
            result.update(auto_resume=False, recovery_required='expired_requires_authorized_recovery')
        elif 'auto_resume' in value or 'recovery_required' in value:
            return None
        if 'batch_id' in value:
            if not text(value['batch_id'], 256): return None
            result['batch_id'] = value['batch_id']
        if 'reason' in value:
            if capacity:
                result.update(reason='queue_capacity', queue_limit=200)
            elif value['reason'] == 'queue_observation_busy' and 'batch_id' in result:
                result['reason'] = value['reason']
            else: return None
        if 'jobs' not in value:
            return result
        jobs = value['jobs']
        if ('batch_id' not in result or not isinstance(jobs, list) or not 1 <= len(jobs) <= MAX_WAIT_JOBS
):
            return None
        seen = set()
        for row in jobs:
            if (not isinstance(row, dict) or set(row) != {'material','job_id','status','operation','role','expires_at','claim','bindings'}
                    or not job_id(row['job_id']) or row['job_id'] in seen
                    or not text(row['material']) or row['status'] not in {'pending','expired','answer_available'}
                    or not text(row['role'], 128) or not timestamp(row['expires_at'])):
                return None
            seen.add(row['job_id'])
            def operation(op, material):
                return (isinstance(op, list) and len(op) == 4 and op[0] == material
                        and all(text(x, 1024) for x in op[:3])
                        and type(op[3]) is int and 0 <= op[3] <= 1)
            if not operation(row['operation'], row['material']): return None
            bindings = row['bindings']
            if not isinstance(bindings, list) or not 1 <= len(bindings) <= 128: return None
            for binding in bindings:
                if (not isinstance(binding, dict) or set(binding) != {'material','operation'}
                        or not text(binding['material']) or not operation(binding['operation'], binding['material'])):
                    return None
            if bindings[0] != {'material':row['material'], 'operation':row['operation']}: return None
            claim = row['claim']
            if claim is not None and (not isinstance(claim, dict)
                    or set(claim) != {'worker_id','generation','expires_at','active'}
                    or not text(claim['worker_id']) or type(claim['generation']) is not int
                    or claim['generation'] < 1 or not timestamp(claim['expires_at'])
                    or type(claim['active']) is not bool): return None
        primary = next((row for row in jobs if row['status'] == 'expired'), jobs[0])
        if value['job_id'] != primary['job_id']: return None
        if (value['state'] == 'expired_parent_writer') != any(r['status'] == 'expired' for r in jobs):
            return None
        result['jobs'] = deepcopy(jobs)
        return result
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
        return None


def read_response_checkpoint(path, offset, day, *, revision_id=None):
    """Read only this launch's bounded stdout tail and validate its diagnostics."""
    if not path.exists() or path.stat().st_size <= offset:
        return None
    with path.open('rb') as handle:
        handle.seek(max(offset, path.stat().st_size-MAX_CHECKPOINT_BYTES-1))
        lines = handle.read().decode('utf-8', errors='replace').splitlines()
    for line in reversed(lines):
        try: value = json.loads(line)
        except (ValueError, RecursionError): continue
        if revision_id is not None and (not isinstance(value, dict) or value.get('revision_id') != revision_id):
            continue
        checkpoint = validate_response_payload(value, day)
        if checkpoint is not None:
            return checkpoint
    return None
