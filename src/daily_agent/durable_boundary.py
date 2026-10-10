"""Write-ahead, whole-state batch boundary; Library I/O belongs to the host.

Arm before work, checkpoint the complete root, then pass the receipt returned by
``durable_checkpoint.commit_checkpoint`` and its independently materialized
archive to verify. Only then begin once. After all work settles, settle, checkpoint
again and verify before arming another batch. An intent/ready/executing archive
is uncertain on recovery and must remain fenced, even if no local attempt exists.

These checks establish consistency, never remote provenance or freshness. The
host must obtain an actual committed receipt, independently refresh Library and
materialize its exact version; a caller-created receipt/copy is not that evidence.
No operation here grants a budget, expires a lease or clears delivery evidence.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
import zipfile

from daily_agent import durable_checkpoint as checkpoint
from daily_agent.workflow_state import StateCorrupt, atomic_json, exclusive_lock, read_json

BOUNDARY = '.daily-agent-boundary.json'
_KEYS = {'schema_version', 'token', 'owner', 'kind', 'state_root', 'phase',
         'created_at', 'parent_library', 'intent_receipt', 'settlement_receipt'}


def _receipt(receipt, marker):
    if not isinstance(receipt, dict) or set(receipt) != {
            'manifest', 'manifest_sha256', 'library', 'write_result', 'committed_at'}:
        raise StateCorrupt('Exact committed checkpoint receipt required')
    # _manifest validates the frozen identity contract. No local journal is
    # implied: the external control journal may have been lost with the host.
    manifest = checkpoint._manifest(receipt['manifest'], Path(marker['state_root']).parent)
    if (receipt['manifest_sha256'] != checkpoint._hash(manifest)
            or manifest['state_root'] != marker['state_root']
            or manifest['boundary'] != marker['token']
            or manifest['owner'] != marker['owner']):
        raise StateCorrupt('Checkpoint does not match this boundary/source/owner')
    checkpoint._nonempty(receipt['committed_at'], 'commit time')
    identity = checkpoint._identity(receipt['library'])
    write = receipt['write_result']
    if not isinstance(write, dict) or not isinstance(write.get('attempt_id'), str) or not checkpoint._ID.fullmatch(write['attempt_id']):
        raise StateCorrupt('Missing committed upload attempt')
    checkpoint._validate_write({'manifest': manifest, 'attempts': [write['attempt_id']]}, write, identity)
    return receipt


def _root(root):
    original = Path(root)
    if original.is_symlink():
        raise StateCorrupt('Boundary root cannot be a symlink')
    root = original.resolve()
    if not root.is_dir():
        raise StateCorrupt('Boundary state root must exist')
    for name in (BOUNDARY, '.daily-agent-boundary.lock'):
        if (root/name).is_symlink():
            raise StateCorrupt('Boundary controls cannot be symlinks')
    return root


def _load(root):
    marker = read_json(root/BOUNDARY)
    if marker is None:
        return None
    if (not isinstance(marker, dict) or set(marker) != _KEYS
            or type(marker['schema_version']) is not int or marker['schema_version'] != 1
            or not isinstance(marker['token'], str) or not checkpoint._ID.fullmatch(marker['token'])
            or marker['phase'] not in {'intent', 'ready', 'executing', 'settled'}):
        raise StateCorrupt('Invalid durable batch boundary')
    for key in ('owner', 'kind', 'state_root', 'created_at'):
        checkpoint._nonempty(marker[key], key)
    if marker['state_root'] != str(root):
        raise StateCorrupt('Boundary state root changed; explicit reconciliation required')
    if marker['parent_library'] is not None:
        checkpoint._identity(marker['parent_library'])
    intent, settlement = marker['intent_receipt'], marker['settlement_receipt']
    if marker['phase'] == 'intent':
        if intent is not None or settlement is not None:
            raise StateCorrupt('Unverified intent contains checkpoint acknowledgements')
    else:
        _receipt(intent, marker)
        if marker['parent_library'] is not None and intent['manifest']['parent'] != marker['parent_library']:
            raise StateCorrupt('Intent checkpoint does not descend from settled batch')
    if settlement is not None:
        _receipt(settlement, marker)
        if marker['phase'] != 'settled' or settlement['manifest']['parent'] != intent['library']:
            raise StateCorrupt('Settlement checkpoint ancestry mismatch')
    return marker


def boundary_status(root):
    """Inspect local boundary evidence; no remote freshness claim."""
    root = _root(root)
    with exclusive_lock(root/'.daily-agent-boundary.lock'):
        return _load(root)


def arm_boundary(root, kind, owner):
    from daily_agent.workflow_state import assert_scope_admission
    assert_scope_admission(Path(root))
    """Persist intent before snapshotting; absent marker enables first use only."""
    root = _root(root)
    checkpoint._nonempty(kind, 'kind')
    checkpoint._nonempty(owner, 'owner')
    with exclusive_lock(root/'.daily-agent-boundary.lock'):
        old = _load(root)
        if old is not None and (old['phase'] != 'settled' or old['settlement_receipt'] is None):
            raise StateCorrupt('Previous boundary must have a verified settlement checkpoint')
        marker = {'schema_version': 1, 'token': uuid4().hex, 'owner': owner, 'kind': kind,
                  'state_root': str(root), 'phase': 'intent',
                  'created_at': datetime.now(timezone.utc).isoformat(),
                  'parent_library': old['settlement_receipt']['library'] if old else None,
                  'intent_receipt': None, 'settlement_receipt': None}
        atomic_json(root/BOUNDARY, marker)
        return marker


def verify_boundary_checkpoint(root, receipt, materialized_archive):
    """Acknowledge only exact committed archive bytes and the identical marker.

    The host supplies the actual commit result and independently materialized
    readback. This offline function cannot establish their remote provenance.
    """
    root = _root(root)
    with exclusive_lock(root/'.daily-agent-boundary.lock'):
        marker = _load(root)
        if marker is None or marker['phase'] not in {'intent', 'settled'}:
            raise StateCorrupt('No boundary awaiting a checkpoint')
        if marker['phase'] == 'settled' and marker['settlement_receipt'] is not None:
            raise StateCorrupt('Settlement checkpoint already acknowledged')
        _receipt(receipt, marker)
        parent = marker['parent_library'] if marker['phase'] == 'intent' else marker['intent_receipt']['library']
        if parent is not None and receipt['manifest']['parent'] != parent:
            raise StateCorrupt('Boundary checkpoint ancestry mismatch')
        archive = Path(materialized_archive)
        if archive.resolve().is_relative_to(root):
            raise StateCorrupt('Materialized checkpoint must be outside state root')
        manifest = checkpoint._bytes(archive, receipt['library'], verify_archive=True)
        if (manifest.get('source_root') != marker['state_root']
                or len(manifest['files']) != receipt['manifest']['archive']['file_count']
                or BOUNDARY not in manifest['files']):
            raise StateCorrupt('Checkpoint source or boundary marker mismatch')
        from daily_agent.durable_recovery import decode
        with zipfile.ZipFile(archive) as source:
            archived = decode(source.read(BOUNDARY))
        if archived != marker:
            raise StateCorrupt('Committed archive does not contain identical boundary intent/state')
        # Recheck after extracting the marker to detect changed materialization.
        checkpoint._bytes(archive, receipt['library'], verify_archive=True)
        if marker['phase'] == 'intent':
            marker['phase'], marker['intent_receipt'] = 'ready', receipt
        else:
            marker['settlement_receipt'] = receipt
        atomic_json(root/BOUNDARY, marker)
        return marker


def begin_boundary(root, kind):
    from daily_agent.workflow_state import assert_scope_admission
    assert_scope_admission(Path(root))
    """Consume one verified intent. Return its token; a retry cannot consume twice."""
    root = _root(root)
    with exclusive_lock(root/'.daily-agent-boundary.lock'):
        marker = _load(root)
        if marker is None or marker['phase'] != 'ready' or marker['kind'] != kind:
            raise StateCorrupt('A matching verified boundary is required before work')
        marker['phase'] = 'executing'
        atomic_json(root/BOUNDARY, marker)
        return marker['token']


def settle_boundary(root, token):
    """Record finished bounded work, preserving budgets/claims/delivery verbatim.

    Host must first stop/join all batch writers; this scan is not a process oracle.
    Active attempt/reservation evidence blocks settlement rather than being reset.
    """
    root = _root(root)
    with exclusive_lock(root/'.daily-agent-boundary.lock'):
        marker = _load(root)
        if marker is None or marker['phase'] != 'executing' or marker['token'] != token:
            raise StateCorrupt('No matching executing boundary')
        from daily_agent.durable_recovery import active_state_record, retired_evidence
        retired = retired_evidence(root)
        for path in sorted(root.rglob('*.json')):
            if path != root/BOUNDARY and str(path) not in retired and active_state_record(path, read_json(path)):
                raise StateCorrupt('Active attempt or reservation blocks boundary settlement')
        marker['phase'] = 'settled'
        atomic_json(root/BOUNDARY, marker)
        return marker
