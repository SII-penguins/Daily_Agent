"""Stdlib-only empty-workspace recovery; Library I/O belongs to the host.

This file can be executed directly after the host obtains it from a hash-verified
source bundle. It never downloads arbitrary URLs, claims to authenticate Library,
starts work, resets state, or restores a Site. Receipts must be normalized from
real current Library metadata and the matching materialized version by the host.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import tempfile
import zipfile

FENCE = '.daily-agent-recovery.json'
MAX_BYTES = 2 * 1024 ** 3


class RecoveryBlocked(RuntimeError):
    pass


def _pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise RecoveryBlocked('Duplicate JSON member')
        value[key] = item
    return value


def _nonfinite(value):
    raise RecoveryBlocked('Non-finite JSON value')


def _float(value):
    number = float(value)
    if not math.isfinite(number):
        raise RecoveryBlocked('Non-finite JSON value')
    return number


def decode(content):
    try:
        return json.loads(content, object_pairs_hook=_pairs,
                          parse_constant=_nonfinite, parse_float=_float)
    except (ValueError, UnicodeError) as exc:
        raise RecoveryBlocked('Invalid recovery JSON') from exc


def sha(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def _hash(value):
    return isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def _receipt(value):
    if (not isinstance(value, dict) or not isinstance(value.get('library_file_id'), str)
            or not value['library_file_id'].startswith('libfile_')
            or not isinstance(value.get('file_id'), str) or not value['file_id']
            or type(value.get('version')) is not int or value['version'] < 0
            or not _hash(value.get('sha256')) or type(value.get('size')) is not int
            or not 0 < value['size'] <= MAX_BYTES):
        raise RecoveryBlocked('Exact current Library identity/version/hash/size required')
    return {k: value[k] for k in ('library_file_id', 'file_id', 'version', 'sha256', 'size')}


def _bound_archive(path, expected, observed):
    expected, observed = _receipt(expected), _receipt(observed)
    if expected != observed:
        raise RecoveryBlocked('Library head differs from recovery registry; reconcile before restore')
    path = Path(path)
    if not path.is_file() or path.stat().st_size != expected['size'] or sha(path) != expected['sha256']:
        raise RecoveryBlocked('Materialized version does not match the trusted receipt')
    return expected


def _safe_name(name):
    path = Path(name)
    if (not name or path.is_absolute() or '..' in path.parts or '\\' in name
            or name.endswith('/') or path.as_posix() != name):
        raise RecoveryBlocked('Unsafe archive member')


def _members(source):
    infos = source.infolist()
    names = [row.filename for row in infos]
    if len(names) != len(set(names)) or sum(row.file_size for row in infos) > MAX_BYTES:
        raise RecoveryBlocked('Duplicate or oversized archive')
    for info in infos:
        _safe_name(info.filename)
        if (info.external_attr >> 16) & 0o170000 == 0o120000:
            raise RecoveryBlocked('Symlink archive member')
    members = set(names)
    for name in names:
        if any(p.as_posix() in members for p in Path(name).parents if p.as_posix() != '.'):
            raise RecoveryBlocked('Archive file/directory collision')
    return members


def _verified_files(source, files, prefix=''):
    if not isinstance(files, dict) or not files:
        raise RecoveryBlocked('Missing file manifest')
    result = {}
    for name, meta in files.items():
        _safe_name(name)
        if not isinstance(meta, dict) or type(meta.get('size')) is not int or not _hash(meta.get('sha256')):
            raise RecoveryBlocked('Invalid file manifest')
        try:
            content = source.read(prefix + name)
        except KeyError as exc:
            raise RecoveryBlocked('Missing archive member') from exc
        if len(content) != meta['size'] or hashlib.sha256(content).hexdigest() != meta['sha256']:
            raise RecoveryBlocked('Archive file identity mismatch')
        result[name] = prefix + name
    return result


def _source_files(source):
    names = _members(source)
    if 'SOURCE-MANIFEST.json' in names:
        manifest = decode(source.read('SOURCE-MANIFEST.json'))
        if manifest.get('schema_version') != 1 or manifest.get('profile') != 'daily-agent-source-v1' or manifest.get('recovery_protocol') != 1:
            raise RecoveryBlocked('Invalid source manifest')
        files = _verified_files(source, manifest.get('files'))
        if names != set(files) | {'SOURCE-MANIFEST.json'}:
            raise RecoveryBlocked('Unlisted source members')
    else:
        # Compatibility with the saved Oct 9 repair bundle, including its audits.
        manifests = [name for name in names if name.endswith('/source-sha256.json')]
        if len(manifests) != 1:
            raise RecoveryBlocked('No unique source manifest in repair bundle')
        prefix = manifests[0].rsplit('/', 1)[0] + '/source/'
        hashes = decode(source.read(manifests[0]))
        if not isinstance(hashes, dict):
            raise RecoveryBlocked('Invalid legacy source manifest')
        files = _verified_files(source, {name: {'size': source.getinfo(prefix+name).file_size,
                                               'sha256': digest} for name, digest in hashes.items()}, prefix)
        if {n for n in names if n.startswith(prefix)} != set(files.values()):
            raise RecoveryBlocked('Unlisted repair-source members')
    if ('pyproject.toml' not in files or 'src/daily_agent/cloud_workflow.py' not in files
            or 'src/daily_agent/durable_recovery.py' not in files
            or 'src/daily_agent/workflow_state.py' not in files):
        raise RecoveryBlocked('Source predates fenced recovery; restore for inspection only, never execute it')
    # Capability compatibility is checked in addition to archive integrity.
    guard_source = source.read(files['src/daily_agent/workflow_state.py'])
    if b'def assert_mutation_allowed(' not in guard_source or b'assert_mutation_allowed(path)' not in guard_source:
        raise RecoveryBlocked('Source does not implement the required mutation fence')
    return files


def _state_files(source):
    names = _members(source)
    if 'STATE-MANIFEST.json' not in names:
        raise RecoveryBlocked('Missing state manifest')
    manifest = decode(source.read('STATE-MANIFEST.json'))
    if manifest.get('schema_version') != 1 or manifest.get('profile') != 'cloud-public-chatgpt-v1':
        raise RecoveryBlocked('Invalid state manifest')
    files = _verified_files(source, manifest.get('files'))
    if names != set(files) | {'STATE-MANIFEST.json'}:
        raise RecoveryBlocked('Unlisted state members')
    if 'config/delivery.yaml' not in files or 'cloud-profile.json' not in files:
        raise RecoveryBlocked('Incomplete cloud profile')
    return files, manifest


def _write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode())
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(temporary, path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def source_snapshot(root, output):
    root, output = Path(root).resolve(), Path(output).resolve()
    if output.is_relative_to(root) or output.exists():
        raise RecoveryBlocked('Source archive requires a new path outside the source tree')
    files = {}
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root)
        if any(p in {'.git', '.venv', '__pycache__', '.pytest_cache'} for p in relative.parts):
            continue
        if path.is_symlink():
            raise RecoveryBlocked('Source snapshot refuses symlinks')
        if not path.is_file():
            continue
        name = path.name.lower()
        if name in {'auth.json', 'secrets.toml', 'credentials.json', '.env'} or name.startswith('.env.') or name.endswith(('.key', '.pem')):
            raise RecoveryBlocked('Credential-like source file')
        files[relative.as_posix()] = {'size': path.stat().st_size, 'sha256': sha(path)}
    if not files or sum(row['size'] for row in files.values()) > MAX_BYTES:
        raise RecoveryBlocked('Missing or oversized source')
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + '.temporary')
    try:
        with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED) as archive:
            for name in files:
                archive.write(root/name, name)
            archive.writestr('SOURCE-MANIFEST.json', json.dumps({'schema_version': 1,
                'profile': 'daily-agent-source-v1', 'recovery_protocol': 1, 'files': files}, indent=2))
        with zipfile.ZipFile(temporary) as archive:
            _source_files(archive)
        if any(sha(root/name) != row['sha256'] for name, row in files.items()):
            raise RecoveryBlocked('Source changed during snapshot')
        os.rename(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return {'archive': str(output), 'size': output.stat().st_size, 'sha256': sha(output), 'files': len(files)}


def bootstrap(source_archive, state_archive, destination, registry, current, *, state_directory="state"):
    """Restore both archives atomically to a NEW directory, always fenced.

    registry/current each contain source and state receipt mappings. `current`
    must come from newly resolved Library metadata and its materialized bytes;
    copying registry into current is not a remote verification.
    """
    if state_directory not in {'state', 'data/cloud'}:
        raise RecoveryBlocked('Unsupported state layout')
    if not isinstance(registry, dict) or not isinstance(current, dict):
        raise RecoveryBlocked('Recovery registry and fresh observations required')
    receipts = {key: _bound_archive(path, registry[key], current[key])
                for key, path in (('source', source_archive), ('state', state_archive))}
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise RecoveryBlocked('Never overwrite an existing recovery destination')
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.recovery-staging-', dir=destination.parent))
    try:
        with zipfile.ZipFile(source_archive) as source, zipfile.ZipFile(state_archive) as state:
            source_files = _source_files(source)
            state_files, manifest = _state_files(state)
            for subdir, archive, files in (('source', source, source_files), (state_directory, state, state_files)):
                for relative, member in files.items():
                    path = staging/subdir/relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(archive.read(member))
        fence = {'schema_version': 1, 'status': 'blocked', 'reason': 'workspace_loss_restore',
                 'receipts': receipts, 'original_state_root': manifest.get('source_root'),
                 'restored_at': datetime.now(timezone.utc).isoformat(),
                 'requires': ['verify_previous_executor_stopped', 'reconcile_claim_owners',
                              'inspect_unresolved_delivery', 'verify_site_archive_owner',
                              'revalidate_absolute_paths'],
                 'budgets_attempts_claims_answers_history_preserved': True}
        _write_json(staging/state_directory/FENCE, fence)
        _write_json(staging/'RECOVERY-ORIGIN.json', fence)
        # A sibling staging directory prevents partially restored active roots.
        if destination.exists() or destination.is_symlink():
            raise RecoveryBlocked('Recovery destination appeared while restoring')
        os.rename(staging, destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    return {'root': str(destination), 'source': str(destination/'source'),
            'state': str(destination/state_directory), 'mutation_blocked': True,
            'source_files': len(source_files), 'state_files': len(state_files),
            'original_state_root': manifest.get('source_root')}


def _canonical(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False, separators=(',', ':')).encode()).hexdigest()


def retired_evidence(root):
    """Only exact permanently fenced migration snapshots exempt old activity.

    This does not claim that an old process died. The replacement must retain
    its existing immutable completion receipt; old mutable journals are never
    reset or replayed. Changed or unsealed retirement evidence earns no exemption.
    """
    root = Path(root).resolve()
    frozen = {}
    for marker in root.rglob('retired.json'):
        try:
            folder = marker.parent
            retired = decode(marker.read_bytes())
            envelope = decode((folder/'migration-snapshot.json').read_bytes())
            snap = envelope['payload']
            old_id, new_id = folder.name, retired['replacement_id']
            replacement = folder.parent/new_id
            complete = decode((replacement/'migration-complete.json').read_bytes())
            contract = decode((replacement/'contract.json').read_bytes())
            if (retired.get('permanent') is not True or retired.get('process_death_claimed') is not False
                    or retired.get('old_revision_id') != old_id or not _hash(new_id)
                    or _canonical(snap) != envelope['sha256'] or retired['snapshot_sha256'] != envelope['sha256']
                    or _canonical(snap['evidence']) != snap['expected_evidence_sha']
                    or _canonical(snap['identity']) != new_id or snap['replacement_id'] != new_id
                    or snap['identity'].get('old_revision_id') != old_id
                    or snap['identity'].get('kind') != 'production_revision_retirement_v1'
                    or snap['evidence'].get('revision_id') != old_id
                    or complete != {'snapshot_sha256':envelope['sha256'], 'revision_id':new_id}
                    or _canonical(contract['payload']) != contract['sha256']
                    or contract['payload'].get('revision_id') != new_id):
                continue
            for name, evidence in snap['evidence']['files'].items():
                _safe_name(name)
                path = folder/name
                if (path.is_file() and not path.is_symlink() and type(evidence.get('size')) is int
                        and path.stat().st_size == evidence['size'] and sha(path) == evidence.get('sha256')):
                    frozen[str(path)] = {'retired_revision': old_id, 'replacement_revision': new_id,
                                         'snapshot_sha256': envelope['sha256']}
        except (KeyError, TypeError, ValueError, OSError, RecoveryBlocked):
            continue
    return frozen


def active_state_record(path, value):
    """Interpret live controller fields, never embedded history as an owner."""
    path = Path(path)
    def live(node):
        if not isinstance(node, dict):
            return False
        status = node.get('status')
        return bool(node.get('running') is True or node.get('active_attempt_id')
                    or node.get('cleanup_pending') is True
                    or isinstance(status, str) and status in {'running','cleanup_pending'})
    if not isinstance(value, dict):
        return False
    if live(value):
        return True
    if path.name == '.daily-agent-boundary.json':
        return value.get('phase') in {'intent','ready','executing'}
    stages = value.get('stages')
    if isinstance(stages, dict) and any(live(stage) for stage in stages.values()):
        return True
    if path.name == 'journal.json' and 'batch-execution' in path.parts:
        payload = value.get('payload', value)
        return live(payload) or bool(isinstance(payload, dict) and payload.get('reservations'))
    return False


def inspect_recovery(root):
    """Read evidence only. No stale lease, elapsed window or send is reset."""
    root = Path(root).resolve()
    marker = root/FENCE
    if not marker.is_file():
        raise RecoveryBlocked('Recovery fence missing; this is not a verified restored root')
    fence = decode(marker.read_bytes())
    active, claims, delivery, absolute_paths = [], [], [], []
    frozen_retirements = retired_evidence(root)
    for path in sorted(root.rglob('*.json')):
        if path.name == FENCE:
            continue
        try:
            value = decode(path.read_bytes())
        except (RecoveryBlocked, OSError):
            active.append({'path': str(path.relative_to(root)), 'reason': 'unreadable_json'})
            continue
        relative = str(path.relative_to(root))
        if isinstance(value, dict):
            if active_state_record(path, value) and str(path) not in frozen_retirements:
                active.append({'path': relative, 'reason': 'retained_active_attempt_or_reservation'})
            if path.name.endswith('.claim.json'):
                claims.append({'path': relative, **{k: value.get(k) for k in ('job_id','worker_id','generation','expires_at')}})
            if (isinstance(value.get('state'), str) and value['state'] in {'sending','uncertain','accepted'}) or (path.name.endswith('.outbox.json') and isinstance(value.get('status'), str) and value['status'] in {'sending','uncertain'}):
                delivery.append({'path': relative, **{k: value.get(k) for k in ('state','status','attempt_id','message_id','conversation')}})
    old_root = fence.get('original_state_root')
    if old_root and str(root) != old_root:
        absolute_paths.append({'original_root': old_root, 'restored_root': str(root),
                               'resolution': 'Use original runtime path or explicitly revalidate path-bound evidence'})
    return {'fence': fence, 'active_evidence': active, 'claims': claims,
            'unresolved_delivery': delivery, 'path_revalidation': absolute_paths,
            'validated_retired_files': len(frozen_retirements),
            'auto_resume': False}


def _completed_claim_evidence(root, claim):
    """A scoped recovery accepts completed responses, never a guessed stop."""
    path = Path(root)/claim['path']
    if not path.name.endswith('.claim.json'):
        raise RecoveryBlocked('Invalid claim evidence path')
    job_path = path.with_name(path.name.replace('.claim.json','.job.json'))
    answer_path = path.with_name(path.name.replace('.claim.json','.answer.json'))
    try:
        job, answer = decode(job_path.read_bytes()), decode(answer_path.read_bytes())
        contract = {key:job[key] for key in ('schema_version','transport','prompt','images','stage')}
        if job.get('retry'): contract['retry'] = job['retry']
        def queue_hash(value):
            return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest()
        if (queue_hash(contract) != claim['job_id'] or job.get('job_id') != claim['job_id']
                or job.get('input_sha256') != claim['job_id']
                or answer.get('job_id') != claim['job_id'] or answer.get('input_sha256') != claim['job_id']
                or answer.get('worker_id') != claim['worker_id']
                or queue_hash(answer.get('response')) != answer.get('response_sha256')):
            raise RecoveryBlocked('Completed claim is not backed by its exact committed response')
    except (KeyError, TypeError, ValueError, OSError) as exc:
        raise RecoveryBlocked('Completed claim lacks valid immutable job/response evidence') from exc


def release_recovery(root, review):
    """Host evidence boundary, not an ownership oracle or user authorization.

    Owner checks must use the live executor/task system, never PID absence in a
    fresh namespace. Uncertain delivery records retain their ordinary send block.
    Review does not expire jobs, refund budgets, authorize new work or clear state.
    """
    root = Path(root).resolve()
    status = inspect_recovery(root)
    fence = status['fence']
    if (not isinstance(review, dict) or review.get('receipts') != fence['receipts']
            or not isinstance(review.get('reviewer'), str) or not review['reviewer'].strip()):
        raise RecoveryBlocked('Review is not bound to this recovered source/state')
    scope = review.get('new_issue_scope')
    if scope is not None:
        from daily_agent.claim_quarantine import validate_scope
        scope = validate_scope(scope)
    evidence = review.get('evidence', {})
    outcomes = {'verify_library_source_and_state': {'verified'},
                'verify_previous_executor_stopped': ({'current_executor_verified_old_owner_unknown_quarantined'} if scope is not None else {'stopped'}),
                'reconcile_claim_owners': {'resolved'},
                'inspect_unresolved_delivery': {'preserved_blocked', 'verified_no_uncertain_sends'},
                'verify_site_archive_owner': {'verified', 'diagnostic_no_site'},
                'revalidate_absolute_paths': {'original_root_verified', 'relocated_revalidated'}}
    for requirement in fence['requires']:
        record = evidence.get(requirement)
        if (not isinstance(record, dict) or record.get('outcome') not in outcomes[requirement]
                or not isinstance(record.get('reference'), str) or not record['reference'].strip()):
            raise RecoveryBlocked('Missing resolved owner/claim/delivery/archive/path evidence')
    if (status['path_revalidation']
            and evidence['revalidate_absolute_paths']['outcome'] != 'relocated_revalidated'):
        raise RecoveryBlocked('Restored root moved; original-path claim cannot release the fence')
    if (status['unresolved_delivery']
            and evidence['inspect_unresolved_delivery']['outcome'] != 'preserved_blocked'):
        raise RecoveryBlocked('Uncertain delivery cannot be declared absent')
    claims = review.get('claims')
    expected = [{k: claim[k] for k in ('job_id', 'worker_id', 'generation')} for claim in status['claims']]
    if not isinstance(claims, list) or len(claims) != len(expected):
        raise RecoveryBlocked('Every retained claim requires a matched disposition')
    seen = []
    quarantined_jobs = []
    for claim in claims:
        if (not isinstance(claim, dict) or claim.get('outcome') not in ({'worker_completed','quarantined_expired_unknown'} if scope is not None else {'worker_completed','owner_stopped'})
                or not isinstance(claim.get('reference'), str) or not claim['reference'].strip()):
            raise RecoveryBlocked('Unknown or live claim owner cannot be released')
        identity = {k: claim.get(k) for k in ('job_id', 'worker_id', 'generation')}
        if identity not in expected or identity in seen:
            raise RecoveryBlocked('Claim disposition identity mismatch')
        seen.append(identity)
        if scope is not None:
            matching = next(row for row in status['claims'] if all(row[k] == identity[k] for k in identity))
            if claim['outcome'] == 'quarantined_expired_unknown':
                from daily_agent.claim_quarantine import validate_quarantine
                validate_quarantine(root, claim['job_id'], scope)
                quarantined_jobs.append(claim['job_id'])
            else:
                _completed_claim_evidence(root, matching)
    if scope is not None and not quarantined_jobs:
        raise RecoveryBlocked('Scoped unknown-owner recovery requires verified preserved quarantine')
    # Active process journals need their existing supported recovery/migration
    # path. Do not unlock a cross-namespace attempt merely on a prose assertion.
    if status['active_evidence']:
        raise RecoveryBlocked('Retained active or corrupt evidence requires explicit supported reconciliation; never reset it')
    if scope is not None:
        scoped_path = root/'.daily-agent-recovery-scope.json'
        scoped_record = {'schema_version':1, 'scope':scope, 'quarantined_jobs':sorted(quarantined_jobs),
                         'old_owner_status':'unknown', 'process_death_claimed':False,
                         'historical_issues_released':False, 'receipts':fence['receipts']}
        if scoped_path.exists():
            retained_scope = decode(scoped_path.read_bytes())
            # The scope's receipts identify its original release, while the new
            # fence identifies this restored head. Never rewrite the old proof.
            if not isinstance(retained_scope, dict) or set(retained_scope.get('receipts', {})) != {'source','state'}:
                raise RecoveryBlocked('Corrupt retained issue scope')
            comparable = {key:value for key,value in retained_scope.items() if key != 'receipts'}
            if comparable != {key:value for key,value in scoped_record.items() if key != 'receipts'}:
                raise RecoveryBlocked('Conflicting immutable fresh-issue recovery scope')
            for value in retained_scope.get('receipts', {}).values(): _receipt(value)
        else:
            _write_json(scoped_path, scoped_record)
    reviewed_path = root/'.daily-agent-recovery-reviewed.json'
    if reviewed_path.exists():
        previous = reviewed_path.read_bytes()
        history = root/'data/recovery/reviewed-fences'/(hashlib.sha256(previous).hexdigest()+'.json')
        if history.exists() and history.read_bytes() != previous:
            raise RecoveryBlocked('Retained recovery review history changed')
        if not history.exists():
            history.parent.mkdir(parents=True,exist_ok=True)
            with history.open('xb') as handle:
                handle.write(previous); handle.flush(); os.fsync(handle.fileno())
            descriptor = os.open(history.parent, os.O_RDONLY)
            try: os.fsync(descriptor)
            finally: os.close(descriptor)
    record = {**fence, 'status': 'reviewed', 'review': review,
              'reviewed_at': datetime.now(timezone.utc).isoformat()}
    _write_json(reviewed_path, record)
    (root/FENCE).unlink()
    descriptor = os.open(root, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return {'reviewed': True, 'authorization_granted': False, 'budgets_reset': False,
            'warning': 'Existing deadlines, claims, generation fencing and uncertain-delivery blocks remain binding'}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['bootstrap','source-snapshot','inspect','release'])
    p.add_argument('--source-archive'); p.add_argument('--state-archive'); p.add_argument('--destination')
    p.add_argument('--state-directory',choices=['state','data/cloud'],default='state')
    p.add_argument('--registry'); p.add_argument('--current'); p.add_argument('--root'); p.add_argument('--output'); p.add_argument('--review')
    a = p.parse_args(argv)
    if a.action == 'bootstrap':
        result = bootstrap(a.source_archive, a.state_archive, a.destination,
                           decode(Path(a.registry).read_bytes()), decode(Path(a.current).read_bytes()), state_directory=a.state_directory)
    elif a.action == 'source-snapshot': result = source_snapshot(a.root, a.output)
    elif a.action == 'inspect': result = inspect_recovery(a.root)
    else: result = release_recovery(a.root, decode(Path(a.review).read_bytes()))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
