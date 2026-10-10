"""Evidence-only quarantine of one expired, unanswered restored writer job.

An expired lease says nothing about whether its old owner is still running. This
module preserves that uncertainty and every original queue byte. Its sole state
change is an immutable sidecar; it cannot release a recovery fence, renew a job,
refund a budget, import an answer, or authorize a replacement writer generation.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import date, datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time
from zoneinfo import ZoneInfo

from daily_agent.durable_recovery import FENCE, RecoveryBlocked, _receipt, decode
from daily_agent.workflow_state import StateCorrupt

DIRECTORY = 'data/recovery/quarantined-writer-jobs'
_QUEUE = 'data/writer-queue'
_SCOPE_KEYS = {'issue_date', 'deadline', 'current_owner', 'current_executor_ref',
               'authorization_ref', 'old_owner_status'}
_TIMESTAMP = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})\Z')
_MAX_BYTES = 8 * 1024 * 1024
_SHANGHAI = ZoneInfo('Asia/Shanghai')


def _identity(job_id):
    if not isinstance(job_id, str) or re.fullmatch(r'[0-9a-f]{64}', job_id) is None:
        raise RecoveryBlocked('Invalid quarantined writer job identity')
    return job_id


def _stamp(value):
    if not isinstance(value, str) or _TIMESTAMP.fullmatch(value) is None:
        raise RecoveryBlocked('Timezone-aware writer deadline/timestamp required')
    try:
        result = datetime.fromisoformat(value)
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError('Naive timestamp')
        return result
    except (ValueError, OverflowError) as exc:
        raise RecoveryBlocked('Invalid writer deadline/timestamp') from exc


def validate_scope(scope, *, require_future=True):
    """Validate immutable host-supplied authorization and executor references.

    References are evidence supplied by the host, not an ownership oracle. The
    caller remains responsible for resolving them against the authorized task.
    """
    if not isinstance(scope, dict) or set(scope) != _SCOPE_KEYS:
        raise RecoveryBlocked('Exact new-issue recovery scope required')
    if scope['old_owner_status'] != 'unknown':
        raise RecoveryBlocked('Quarantine preserves an unknown owner; never a live/stopped assertion')
    for key in ('current_owner', 'current_executor_ref', 'authorization_ref'):
        value = scope[key]
        if (not isinstance(value, str) or not value.strip() or len(value) > 4096
                or any(ord(c) < 32 for c in value)):
            raise RecoveryBlocked('Current executor and user authorization evidence required')
    try:
        day = date.fromisoformat(scope['issue_date'])
        if not isinstance(scope['issue_date'], str) or day.isoformat() != scope['issue_date']:
            raise ValueError('Noncanonical issue date')
    except (TypeError, ValueError) as exc:
        raise RecoveryBlocked('Canonical new issue date required') from exc
    deadline = _stamp(scope['deadline'])
    if require_future and deadline <= datetime.now(timezone.utc):
        raise RecoveryBlocked('New issue authorization deadline must still be in the future')
    return dict(scope)


def _root(root):
    path = Path(root).absolute()
    if '..' in path.parts:
        raise RecoveryBlocked('Recovery root must not contain traversal')
    # Do not hide an alias before checking its ancestors.
    for ancestor in (path, *path.parents):
        if ancestor.is_symlink():
            raise RecoveryBlocked('Recovery evidence refuses symlink paths')
    if not path.is_dir():
        raise RecoveryBlocked('Restored root is missing')
    return path


@contextmanager
def _directory(root, relative='', *, create=False):
    """Open descendants by descriptor, refusing symlink substitution at each hop."""
    parts = Path(relative).parts if relative else ()
    if (Path(relative).is_absolute() or '..' in parts or '\\' in relative
            or (relative and Path(relative).as_posix() != relative)):
        raise RecoveryBlocked('Unsafe quarantine evidence path')
    descriptor = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        # Root ancestors are traversed by descriptor too: checking Path.resolve
        # alone would leave an ancestor-symlink substitution race before writing.
        components = [(part, False) for part in root.parts[1:]]
        components.extend((part, create) for part in parts)
        for part, may_create in components:
            if may_create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=descriptor)
                    os.fsync(descriptor)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                            dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    except OSError as exc:
        raise RecoveryBlocked('Unsafe or unavailable quarantine directory') from exc
    finally:
        os.close(descriptor)


def _read(root, relative):
    path = Path(relative)
    if (not isinstance(relative, str) or not relative or path.is_absolute()
            or '..' in path.parts or '\\' in relative or path.as_posix() != relative):
        raise RecoveryBlocked('Unsafe quarantine evidence path')
    try:
        with _directory(root, path.parent.as_posix() if path.parent != Path('.') else '') as directory:
            descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=directory)
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 < info.st_size <= _MAX_BYTES:
                    raise RecoveryBlocked('Invalid or aliased quarantine evidence file')
                with os.fdopen(os.dup(descriptor), 'rb') as handle:
                    content = handle.read(_MAX_BYTES + 1)
                if len(content) != info.st_size:
                    raise RecoveryBlocked('Quarantine evidence changed while reading')
                return content
            finally:
                os.close(descriptor)
    except OSError as exc:
        raise RecoveryBlocked('Missing or unsafe quarantine evidence') from exc


def _present(root, relative):
    """lstat semantics: malformed files, directories and dangling links all exist."""
    path = root/relative
    for ancestor in (path, *path.parents):
        if ancestor == root:
            break
        if ancestor.is_symlink():
            return True
        if ancestor != path and os.path.lexists(ancestor) and not ancestor.is_dir():
            return True
    return os.path.lexists(path)


def _bound(relative, content):
    return {'path': relative, 'sha256': hashlib.sha256(content).hexdigest(), 'size': len(content)}


def _validate_fence(content):
    value = decode(content)
    if (not isinstance(value, dict) or type(value.get('schema_version')) is not int
            or value['schema_version'] != 1 or value.get('status') != 'blocked'
            or value.get('reason') != 'workspace_loss_restore'
            or value.get('budgets_attempts_claims_answers_history_preserved') is not True
            or not isinstance(value.get('receipts'), dict)
            or set(value['receipts']) != {'source', 'state'}
            or not isinstance(value.get('requires'), list) or not value['requires']
            or not all(isinstance(item, str) for item in value['requires'])):
        raise RecoveryBlocked('A valid blocked restored-root fence is required')
    for receipt in value['receipts'].values():
        _receipt(receipt)
    _stamp(value.get('restored_at'))
    return value


def _fence(root):
    content = _read(root, FENCE)
    return content, _validate_fence(content)


@contextmanager
def _queue_lock(root):
    # Use the exact mutex and flock inode used by parent_writer, with no-follow
    # open rather than its general-purpose append-mode filesystem helper.
    from daily_agent.parent_writer import _QUEUE_THREADS
    if not _QUEUE_THREADS.acquire(timeout=10):
        raise RecoveryBlocked('Writer queue is busy')
    try:
        with _directory(root, _QUEUE) as directory:
            descriptor = os.open('queue.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 0o600, dir_fd=directory)
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise RecoveryBlocked('Unsafe writer queue lock')
                limit = time.monotonic() + 10
                while True:
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= limit:
                            raise RecoveryBlocked('Writer queue is busy')
                        time.sleep(.02)
                try:
                    yield
                finally:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
    except OSError as exc:
        raise RecoveryBlocked('Unsafe writer queue lock') from exc
    finally:
        _QUEUE_THREADS.release()


def _reviewed_fence_retained(root, fence):
    """Recognize an original reviewed fence across later restore/release cycles."""
    candidates = []
    current = '.daily-agent-recovery-reviewed.json'
    if _present(root, current):
        candidates.append(_read(root, current))
    history = 'data/recovery/reviewed-fences'
    if _present(root, history):
        with _directory(root, history) as directory:
            names = sorted(os.listdir(directory))
        for name in names:
            if not name.endswith('.json'):
                continue
            content = _read(root, f'{history}/{name}')
            if name != hashlib.sha256(content).hexdigest() + '.json':
                raise RecoveryBlocked('Retained reviewed-fence filename/hash mismatch')
            candidates.append(content)
    matched = False
    for content in candidates:
        reviewed = decode(content)
        if (not isinstance(reviewed, dict) or reviewed.get('status') != 'reviewed'
                or not isinstance(reviewed.get('review'), dict)):
            raise RecoveryBlocked('Malformed retained recovery review')
        _stamp(reviewed.get('reviewed_at'))
        if (_encoded({key: reviewed.get(key) for key in fence if key != 'status'})
                == _encoded({key: value for key, value in fence.items() if key != 'status'})):
            matched = True
    if not matched:
        raise RecoveryBlocked('Original source/state recovery evidence changed after release')


def _assert_no_accounted_retry(root, job_id):
    # retry_expired spends the budget before writing its job and active pointer.
    # Even that interrupted lineage remains binding. Restored archives refuse
    # symlinks; descriptor-based traversal also refuses their later introduction.
    pending = ['']
    while pending:
        relative = pending.pop()
        with _directory(root, relative) as directory:
            for name in os.listdir(directory):
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                child = f'{relative}/{name}' if relative else name
                if stat.S_ISLNK(info.st_mode):
                    raise RecoveryBlocked('Symlink prevents complete retained retry-lineage inspection')
                if stat.S_ISDIR(info.st_mode):
                    pending.append(child)
                elif relative.endswith('/cloud-generation-budgets') and name.endswith('.json'):
                    value = decode(_read(root, child))
                    if not isinstance(value, dict):
                        raise RecoveryBlocked('Unverifiable accounted writer retry lineage')
                    rows = value.get('writer_job_retries', [])
                    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                        raise RecoveryBlocked('Unverifiable accounted writer retry lineage')
                    if any(any(row.get(key) == job_id for key in ('base_job_id', 'previous_job_id', 'job_id'))
                           for row in rows):
                        raise RecoveryBlocked('Accounted retry lineage forbids quarantine')


def _queue_evidence(root, job_id, scope):
    from daily_agent.parent_writer import validate_job
    job_path = f'{_QUEUE}/{job_id}.job.json'
    claim_path = f'{_QUEUE}/{job_id}.claim.json'
    job_bytes, claim_bytes = _read(root, job_path), _read(root, claim_path)
    job, claim = decode(job_bytes), decode(claim_bytes)
    if (not isinstance(job, dict) or type(job.get('schema_version')) is not int
            or job['schema_version'] != 1 or job.get('transport') != 'parent_assisted'
            or job.get('job_id') != job_id or job.get('input_sha256') != job_id
            or not isinstance(job.get('prompt'), str) or len(job['prompt']) > 500000
            or not isinstance(job.get('images'), list)
            or not isinstance(job.get('stage'), str) or not job['stage']
            or 'retry' in job or type(job.get('call_timeout_seconds')) not in (int, float)
            or not math.isfinite(job['call_timeout_seconds']) or job['call_timeout_seconds'] <= 0):
        raise RecoveryBlocked('Invalid or retry-lineage writer job')
    for image in job['images']:
        if (not isinstance(image, dict) or set(image) != {'path', 'sha256'}
                or not isinstance(image['path'], str) or not image['path']
                or not isinstance(image['sha256'], str)
                or re.fullmatch(r'[0-9a-f]{64}', image['sha256']) is None):
            raise RecoveryBlocked('Invalid writer image evidence')
        content = _read(root, image['path'])
        if hashlib.sha256(content).hexdigest() != image['sha256']:
            raise RecoveryBlocked('Writer image evidence changed')
    try:
        validate_job(root, job, check_images=False)
    except (StateCorrupt, KeyError, TypeError, ValueError) as exc:
        raise RecoveryBlocked('Writer job contract/hash mismatch') from exc
    if (not isinstance(claim, dict) or claim.get('job_id') != job_id
            or not isinstance(claim.get('worker_id'), str) or not claim['worker_id'].strip()
            or not isinstance(claim.get('token'), str) or not claim['token'].strip()
            or type(claim.get('generation')) is not int or claim['generation'] < 1):
        raise RecoveryBlocked('Writer claim job/worker/generation identity mismatch')
    created = _stamp(job.get('created_at'))
    job_deadline, claim_deadline = _stamp(job.get('expires_at')), _stamp(claim.get('expires_at'))
    now = datetime.now(timezone.utc)
    if not created < job_deadline <= now or not created < claim_deadline <= job_deadline:
        raise RecoveryBlocked('Both original job and claim deadlines must be expired and consistent')
    old_day = created.astimezone(_SHANGHAI).date()
    if date.fromisoformat(scope['issue_date']) <= old_day:
        raise RecoveryBlocked('New issue must be distinct and later than the original Shanghai issue date')
    if _present(root, f'{_QUEUE}/{job_id}.answer.json'):
        raise RecoveryBlocked('Answered or ambiguous-answer writer jobs cannot be quarantined')
    if _present(root, f'{_QUEUE}/{job_id}.active.json'):
        raise RecoveryBlocked('Active-generation pointer forbids quarantine')
    # A descendant retry can exist even if its active pointer was never published.
    # Read every candidate strictly rather than interpreting corrupt lineage as absent.
    with _directory(root, _QUEUE) as directory:
        names = sorted(os.listdir(directory))
    for name in names:
        if name.endswith('.active.json'):
            pointer = decode(_read(root, f'{_QUEUE}/{name}'))
            if not isinstance(pointer, dict):
                raise RecoveryBlocked('Unverifiable active-generation pointer')
            if pointer.get('base_job_id') == job_id or pointer.get('job_id') == job_id:
                raise RecoveryBlocked('Active-generation pointer forbids quarantine')
        elif name.endswith('.job.json') and name != f'{job_id}.job.json':
            other = decode(_read(root, f'{_QUEUE}/{name}'))
            if not isinstance(other, dict):
                raise RecoveryBlocked('Unverifiable writer retry lineage')
            retry = other.get('retry')
            if retry is not None and not isinstance(retry, dict):
                raise RecoveryBlocked('Unverifiable writer retry lineage')
            if retry and any(retry.get(key) == job_id for key in ('base_job_id', 'previous_job_id')):
                raise RecoveryBlocked('Existing retry lineage forbids quarantine')
    _assert_no_accounted_retry(root, job_id)
    return {'job_id': job_id, 'worker_id': claim['worker_id'], 'generation': claim['generation'],
            'old_issue_date': old_day.isoformat(), 'job_created_at': job['created_at'],
            'job_expires_at': job['expires_at'], 'claim_expires_at': claim['expires_at'],
            'job_evidence': _bound(job_path, job_bytes), 'claim_evidence': _bound(claim_path, claim_bytes)}


def _record(root, job_id, scope):
    content, fence = _fence(root)
    return {'schema_version': 1, 'kind': 'expired_unanswered_writer_quarantine_v1',
            'status': 'quarantined', 'old_owner_status': 'unknown',
            'process_death_claimed': False, 'authorization_granted': False, 'budgets_reset': False,
            **_queue_evidence(root, job_id, scope), 'scope': scope,
            'recovery_evidence': {**_bound(FENCE, content),
                                  'content_base64': base64.b64encode(content).decode('ascii')},
            'source_state_receipts': fence['receipts']}


def _encoded(record):
    return json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2,
                      allow_nan=False).encode('utf-8') + b'\n'


def _write_immutable(root, relative, content):
    """One exclusive creation, never replace: a torn file stays fail-closed."""
    path = Path(relative)
    with _directory(root, path.parent.as_posix(), create=True) as directory:
        try:
            descriptor = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o400, dir_fd=directory)
        except FileExistsError:
            if _read(root, relative) != content:
                raise RecoveryBlocked('Immutable quarantine conflicts with retained bytes or scope')
            descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.fsync(directory)
            return
        try:
            with os.fdopen(descriptor, 'wb') as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.fsync(directory)
        except OSError as exc:
            raise RecoveryBlocked('Quarantine persistence failed; preserve partial evidence') from exc


def create_quarantine(root, job_id, scope, *, expected_job_sha256, expected_claim_sha256):
    """Only add an immutable evidence sidecar while the restored root is fenced."""
    root, job_id, scope = _root(root), _identity(job_id), validate_scope(scope)
    for expected in (expected_job_sha256, expected_claim_sha256):
        if not isinstance(expected, str) or re.fullmatch(r'[0-9a-f]{64}', expected) is None:
            raise RecoveryBlocked('Exact previously reviewed job and claim hashes are required')
    # Establish the fence before acquiring/creating the normal queue lock.
    _fence(root)
    with _queue_lock(root):
        record = _record(root, job_id, scope)
        if (record['job_evidence']['sha256'] != expected_job_sha256
                or record['claim_evidence']['sha256'] != expected_claim_sha256):
            raise RecoveryBlocked('Writer job or claim changed from the reviewed hash baseline')
        validate_scope(scope)  # Queue contention must not outlive the authorization deadline.
        _write_immutable(root, f'{DIRECTORY}/{job_id}.json', _encoded(record))
        return record


def assert_not_quarantined(root, job_id):
    """Presence alone blocks reuse; malformed sidecars never restore any rights."""
    try:
        root, job_id = _root(root), _identity(job_id)
        if _present(root, f'{DIRECTORY}/{job_id}.json'):
            raise StateCorrupt('Quarantined writer job is permanently unavailable for claim/import/retry/request')
    except RecoveryBlocked as exc:
        raise StateCorrupt('Cannot establish that writer job is not quarantined') from exc


def validate_quarantine(root, job_id, scope, *, require_future=True):
    """Recheck every bound byte and negative evidence, before or after release."""
    root, job_id = _root(root), _identity(job_id)
    scope = validate_scope(scope, require_future=require_future)
    with _queue_lock(root):
        content = _read(root, f'{DIRECTORY}/{job_id}.json')
        record = decode(content)
        if not isinstance(record, dict) or record.get('scope') != scope:
            raise RecoveryBlocked('Quarantine scope differs from the authorized new issue')
        try:
            retained = record['recovery_evidence']
            fence_bytes = base64.b64decode(retained['content_base64'], validate=True)
            fence = _validate_fence(fence_bytes)
            if (type(retained.get('size')) is not int
                    or retained != {**_bound(FENCE, fence_bytes),
                                    'content_base64': base64.b64encode(fence_bytes).decode('ascii')}
                    or record.get('source_state_receipts') != fence['receipts']):
                raise RecoveryBlocked('Quarantine recovery evidence identity mismatch')
            current = _fence(root)[0] if _present(root, FENCE) else None
            if current != fence_bytes:
                # A subsequent verified restore installs a new fence. Its state
                # archive must retain the previous reviewed receipt, allowing
                # validation of this immutable original quarantine across restarts.
                _reviewed_fence_retained(root, fence)
            expected = {'schema_version': 1, 'kind': 'expired_unanswered_writer_quarantine_v1',
                        'status': 'quarantined', 'old_owner_status': 'unknown',
                        'process_death_claimed': False, 'authorization_granted': False, 'budgets_reset': False,
                        **_queue_evidence(root, job_id, scope), 'scope': scope,
                        'recovery_evidence': retained, 'source_state_receipts': fence['receipts']}
            # Canonical bytes make types (notably true versus 1) and unknown fields
            # significant, in addition to duplicate-member rejection by decode.
            if content != _encoded(expected):
                raise RecoveryBlocked('Immutable quarantine identity or retained evidence changed')
        except (KeyError, TypeError, ValueError, UnicodeError) as exc:
            raise RecoveryBlocked('Invalid immutable quarantine record') from exc
        return record
