from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path

import pytest

from daily_agent import claim_quarantine as quarantine
from daily_agent.durable_recovery import FENCE, RecoveryBlocked
from daily_agent.parent_writer import digest
from daily_agent.workflow_state import StateCorrupt, atomic_json


NOW = datetime(2030, 10, 10, 6, tzinfo=timezone.utc)
_REVIEWED_HASHES = {}


def review_hashes(root, job_id):
    _REVIEWED_HASHES[str(root.resolve())] = {
        f'expected_{suffix}_sha256': hashlib.sha256(
            (root/f'data/writer-queue/{job_id}.{suffix}.json').read_bytes()).hexdigest()
        for suffix in ('job', 'claim')}


def create(root, job_id, scope):
    return quarantine.create_quarantine(root, job_id, scope,
                                        **_REVIEWED_HASHES[str(root.resolve())])


class Clock(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW if tz else NOW.replace(tzinfo=None)


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(quarantine, 'datetime', Clock)
    root = tmp_path/'restored'
    queue = root/'data/writer-queue'
    queue.mkdir(parents=True)
    contract = {'schema_version': 1, 'transport': 'parent_assisted', 'prompt': 'retained draft',
                'images': [], 'stage': 'draft'}
    job_id = digest(contract)
    job = {**contract, 'job_id': job_id, 'input_sha256': job_id,
           'created_at': '2030-10-09T01:00:00Z', 'expires_at': '2030-10-09T07:00:00Z',
           'call_timeout_seconds': 120}
    claim = {'job_id': job_id, 'worker_id': 'old-writer', 'generation': 2,
             'token': 'original-token', 'expires_at': '2030-10-09T01:15:00Z'}
    for suffix, value in [('job', job), ('claim', claim)]:
        (queue/f'{job_id}.{suffix}.json').write_text(json.dumps(value, indent=3))
    receipts = {kind: {'library_file_id': f'libfile_{kind}', 'file_id': f'file_{kind}',
                      'version': 0, 'size': 7, 'sha256': 'a'*64} for kind in ('source', 'state')}
    fence = {'schema_version': 1, 'status': 'blocked', 'reason': 'workspace_loss_restore',
             'receipts': receipts, 'original_state_root': '/old-root',
             'restored_at': '2030-10-10T05:00:00Z',
             'requires': ['verify_previous_executor_stopped', 'reconcile_claim_owners'],
             'budgets_attempts_claims_answers_history_preserved': True}
    (root/FENCE).write_text(json.dumps(fence, indent=4))
    other = root/'data/state/budget.json'
    other.parent.mkdir()
    other.write_text('{"failures": 2, "remaining": 0, "deadline": "2030-10-09T08:00:00Z"}')
    scope = {'issue_date': '2030-10-10', 'deadline': '2030-10-10T10:00:00Z',
             'current_owner': '/root/new-issue', 'current_executor_ref': 'executor:observed-current',
             'authorization_ref': 'user-message:approve-new-issue', 'old_owner_status': 'unknown'}
    review_hashes(root, job_id)
    return root, job_id, scope


def rewrite(root, job_id, suffix, change):
    path = root/f'data/writer-queue/{job_id}.{suffix}.json'
    value = json.loads(path.read_bytes())
    change(value)
    path.write_text(json.dumps(value))


def retained(root):
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob('*')
            if path.is_file() and path.name != 'queue.lock' and quarantine.DIRECTORY not in str(path)}


def test_quarantine_is_immutable_evidence_only_and_strictly_idempotent(evidence):
    root, job_id, scope = evidence
    before = retained(root)
    result = create(root, job_id, scope)
    path = root/quarantine.DIRECTORY/f'{job_id}.json'
    first = path.read_bytes()
    assert result['worker_id'] == 'old-writer' and result['generation'] == 2
    assert result['old_owner_status'] == 'unknown'
    assert result['process_death_claimed'] is False
    assert result['authorization_granted'] is False and result['budgets_reset'] is False
    for suffix in ('job', 'claim'):
        content = before[f'data/writer-queue/{job_id}.{suffix}.json']
        assert result[f'{suffix}_evidence']['size'] == len(content)
        assert result[f'{suffix}_evidence']['sha256'] == hashlib.sha256(content).hexdigest()
    assert create(root, job_id, scope) == result
    assert path.read_bytes() == first and retained(root) == before
    assert quarantine.validate_quarantine(root, job_id, scope) == result
    assert (root/FENCE).exists()
    with pytest.raises(StateCorrupt, match='fenced'):
        atomic_json(root/'data/state/budget.json', {'remaining': 100})
    with pytest.raises(StateCorrupt, match='Quarantined'):
        quarantine.assert_not_quarantined(root, job_id)


def test_creation_requires_original_valid_blocked_fence(evidence):
    root, job_id, scope = evidence
    fence = root/FENCE
    old = fence.read_bytes()
    for content in (b'{}', b'{"schema_version":true}', b'{"status":"reviewed"}', b'null'):
        fence.write_bytes(content)
        with pytest.raises(RecoveryBlocked):
            create(root, job_id, scope)
    fence.unlink()
    with pytest.raises(RecoveryBlocked):
        create(root, job_id, scope)
    assert not (root/quarantine.DIRECTORY).exists()
    fence.write_bytes(old)
    create(root, job_id, scope)


def test_scope_is_exact_and_requires_authorization_and_executor_evidence(evidence):
    root, job_id, scope = evidence
    variants = [{**scope, 'extra_right': 'retry'}, {k: v for k, v in scope.items() if k != 'authorization_ref'},
                *[{**scope, key: value} for key in ('current_owner', 'current_executor_ref', 'authorization_ref')
                  for value in ('', ' ', True, [], 'line\nbreak')]]
    for bad in variants:
        with pytest.raises(RecoveryBlocked):
            create(root, job_id, bad)
    assert not (root/quarantine.DIRECTORY).exists()


def test_unknown_owner_is_never_converted_into_stopped_or_live(evidence):
    root, job_id, scope = evidence
    for outcome in ('live', 'stopped', 'expired', 'worker_completed', 'UNKNOWN', None, False):
        with pytest.raises(RecoveryBlocked):
            create(root, job_id, {**scope, 'old_owner_status': outcome})


def test_scope_deadline_must_be_aware_valid_and_future(evidence):
    root, job_id, scope = evidence
    for deadline in ('2030-10-10T05:00:00Z', '2030-10-10T06:00:00Z', '2030-10-10T10:00:00',
                     '2030-10-10', '2030-10-10 10:00:00Z', True, '2030-10-10T10:00:00+25:00'):
        with pytest.raises(RecoveryBlocked):
            create(root, job_id, {**scope, 'deadline': deadline})


def test_new_issue_must_be_canonical_and_later_in_shanghai(evidence):
    root, job_id, scope = evidence
    # UTC October 9 at 17:00 was already October 10 in Shanghai.
    rewrite(root, job_id, 'job', lambda j: j.update(created_at='2030-10-09T17:00:00Z', expires_at='2030-10-09T23:00:00Z'))
    rewrite(root, job_id, 'claim', lambda c: c.update(expires_at='2030-10-09T17:15:00Z'))
    for issue in ('2030-10-09', '2030-10-10', '20301011', '2030-1-11', True):
        with pytest.raises(RecoveryBlocked):
            create(root, job_id, {**scope, 'issue_date': issue})
    review_hashes(root, job_id)
    create(root, job_id, {**scope, 'issue_date': '2030-10-11', 'deadline': '2030-10-11T10:00:00Z'})


def test_both_leases_must_be_expired_and_chronologically_consistent(evidence):
    root, job_id, scope = evidence
    queue = root/'data/writer-queue'
    originals = {suffix: (queue/f'{job_id}.{suffix}.json').read_bytes() for suffix in ('job', 'claim')}
    changes = [('job', 'expires_at', '2030-10-11T01:00:00Z'),
               ('claim', 'expires_at', '2030-10-11T01:00:00Z'),
               ('job', 'created_at', '2030-10-09T08:00:00Z'),
               ('claim', 'expires_at', '2030-10-09T08:00:00Z'),
               ('job', 'expires_at', '2030-10-09T07:00:00'),
               ('claim', 'expires_at', '2030-10-09T01:15:00')]
    for suffix, key, value in changes:
        for name, content in originals.items():
            (queue/f'{job_id}.{name}.json').write_bytes(content)
        rewrite(root, job_id, suffix, lambda item: item.update({key: value}))
        with pytest.raises(RecoveryBlocked):
            create(root, job_id, scope)


def test_claim_identity_generation_and_owner_are_strict(evidence):
    root, job_id, scope = evidence
    path = root/f'data/writer-queue/{job_id}.claim.json'
    original = path.read_bytes()
    changes = [('job_id', '0'*64), ('worker_id', ''), ('worker_id', True), ('generation', True),
               ('generation', 0), ('generation', -1), ('generation', 2.0), ('token', '')]
    for key, value in changes:
        path.write_bytes(original)
        rewrite(root, job_id, 'claim', lambda item: item.update({key: value}))
        with pytest.raises(RecoveryBlocked):
            create(root, job_id, scope)


def test_contract_hash_and_job_schema_cannot_be_spoofed(evidence):
    root, job_id, scope = evidence
    path = root/f'data/writer-queue/{job_id}.job.json'
    original = path.read_bytes()
    for key, value in [('prompt', 'changed'), ('schema_version', True), ('input_sha256', '0'*64),
                       ('job_id', '0'*64), ('call_timeout_seconds', True), ('call_timeout_seconds', float('nan')),
                       ('images', {}), ('stage', None), ('retry', {})]:
        path.write_bytes(original)
        rewrite(root, job_id, 'job', lambda item: item.update({key: value}))
        with pytest.raises(RecoveryBlocked):
            create(root, job_id, scope)


def test_duplicate_json_members_are_never_tolerated(evidence):
    root, job_id, scope = evidence
    paths = [root/FENCE, root/f'data/writer-queue/{job_id}.job.json', root/f'data/writer-queue/{job_id}.claim.json']
    for path in paths:
        original = path.read_bytes()
        path.write_bytes(b'{"duplicate":1,"duplicate":2,' + original.lstrip()[1:])
        with pytest.raises(RecoveryBlocked):
            create(root, job_id, scope)
        path.write_bytes(original)
    create(root, job_id, scope)
    path = root/quarantine.DIRECTORY/f'{job_id}.json'
    damaged = b'{"duplicate":1,"duplicate":2,' + path.read_bytes().lstrip()[1:]
    path.unlink()  # Isolated corruption injection bypasses read-only fixture file.
    path.write_bytes(damaged)
    with pytest.raises(RecoveryBlocked):
        quarantine.validate_quarantine(root, job_id, scope)


def test_any_answer_entry_including_dangling_symlink_blocks(evidence):
    root, job_id, scope = evidence
    answer = root/f'data/writer-queue/{job_id}.answer.json'
    for content in (b'{}', b'null', b'broken', b''):
        answer.write_bytes(content)
        with pytest.raises(RecoveryBlocked, match='answer'):
            create(root, job_id, scope)
    answer.unlink()
    answer.symlink_to(root/'does-not-exist')
    with pytest.raises(RecoveryBlocked, match='answer'):
        create(root, job_id, scope)


def test_active_pointer_or_unpublished_retry_lineage_blocks(evidence):
    root, job_id, scope = evidence
    queue = root/'data/writer-queue'
    for name, content in [(f'{job_id}.active.json', {}), ('other.active.json', {'job_id': job_id}),
                          ('other.job.json', {'retry': {'previous_job_id': job_id}}),
                          ('other.job.json', {'retry': {'base_job_id': job_id}})]:
        path = queue/name
        path.write_text(json.dumps(content))
        with pytest.raises(RecoveryBlocked):
            create(root, job_id, scope)
        path.unlink()
    budget = root/'data/state/cloud-generation-budgets/2030-10-09.json'
    budget.parent.mkdir()
    for field in ('base_job_id', 'previous_job_id', 'job_id'):
        budget.write_text(json.dumps({'writer_job_retries': [{field: job_id}]}))
        with pytest.raises(RecoveryBlocked, match='Accounted retry lineage'):
            create(root, job_id, scope)


def test_identical_bytes_required_even_if_json_identity_is_unchanged(evidence):
    root, job_id, scope = evidence
    create(root, job_id, scope)
    path = root/f'data/writer-queue/{job_id}.claim.json'
    path.write_bytes(path.read_bytes() + b'\n')
    for call in (create, quarantine.validate_quarantine):
        with pytest.raises(RecoveryBlocked):
            call(root, job_id, scope)


def test_scope_changes_conflict_without_replacing_sidecar(evidence):
    root, job_id, scope = evidence
    create(root, job_id, scope)
    path = root/quarantine.DIRECTORY/f'{job_id}.json'
    original = path.read_bytes()
    for key, value in [('current_owner', '/root/another'), ('current_executor_ref', 'new-ref'),
                       ('authorization_ref', 'other-user-message'), ('deadline', '2030-10-10T11:00:00Z'),
                       ('issue_date', '2030-10-11')]:
        with pytest.raises(RecoveryBlocked):
            create(root, job_id, {**scope, key: value})
        with pytest.raises(RecoveryBlocked):
            quarantine.validate_quarantine(root, job_id, {**scope, key: value})
    assert path.read_bytes() == original


def test_presence_guard_fails_closed_on_malformed_partial_and_symlink_sidecars(evidence):
    root, job_id, _ = evidence
    quarantine.assert_not_quarantined(root, job_id)
    path = root/quarantine.DIRECTORY/f'{job_id}.json'
    path.parent.mkdir(parents=True)
    for content in (b'', b'null', b'{}', b'{'):
        path.write_bytes(content)
        with pytest.raises(StateCorrupt, match='Quarantined'):
            quarantine.assert_not_quarantined(root, job_id)
    path.unlink()
    path.symlink_to(root/'absent')
    with pytest.raises(StateCorrupt, match='Quarantined'):
        quarantine.assert_not_quarantined(root, job_id)


def test_ids_cannot_select_paths_or_coerce_boolean(evidence):
    root, _, scope = evidence
    for job_id in ('../outside', 'A'*64, 'f'*63, True, None, 'f'*64 + '/job'):
        with pytest.raises(RecoveryBlocked):
            create(root, job_id, scope)
        with pytest.raises(StateCorrupt):
            quarantine.assert_not_quarantined(root, job_id)


def test_symlink_root_and_evidence_files_never_read_or_write_targets(evidence, tmp_path):
    root, job_id, scope = evidence
    alias = tmp_path/'alias'
    alias.symlink_to(root, target_is_directory=True)
    with pytest.raises(RecoveryBlocked):
        create(alias, job_id, scope)
    for path in (root/FENCE, root/f'data/writer-queue/{job_id}.job.json',
                 root/f'data/writer-queue/{job_id}.claim.json'):
        content = path.read_bytes()
        external = tmp_path/'external'
        external.write_bytes(content)
        path.unlink()
        path.symlink_to(external)
        with pytest.raises(RecoveryBlocked):
            create(root, job_id, scope)
        assert external.read_bytes() == content
        path.unlink()
        path.write_bytes(content)


def test_symlink_quarantine_directory_and_lock_cannot_escape(evidence, tmp_path):
    root, job_id, scope = evidence
    external = tmp_path/'external'
    external.mkdir()
    directory = root/quarantine.DIRECTORY
    directory.parent.mkdir(parents=True)
    directory.symlink_to(external, target_is_directory=True)
    with pytest.raises(RecoveryBlocked):
        create(root, job_id, scope)
    assert list(external.iterdir()) == []
    directory.unlink()
    lock = root/'data/writer-queue/queue.lock'
    lock.unlink(missing_ok=True)
    target = external/'lock'
    target.write_bytes(b'original')
    lock.symlink_to(target)
    with pytest.raises(RecoveryBlocked):
        create(root, job_id, scope)
    assert target.read_bytes() == b'original'


def test_revalidation_detects_late_answer_pointer_and_claim_replacement(evidence):
    root, job_id, scope = evidence
    create(root, job_id, scope)
    queue = root/'data/writer-queue'
    for suffix in ('answer', 'active'):
        path = queue/f'{job_id}.{suffix}.json'
        path.write_bytes(b'{}')
        with pytest.raises(RecoveryBlocked):
            quarantine.validate_quarantine(root, job_id, scope)
        path.unlink()
    rewrite(root, job_id, 'claim', lambda c: c.update(worker_id='replacement'))
    with pytest.raises(RecoveryBlocked):
        quarantine.validate_quarantine(root, job_id, scope)


def test_reviewed_fence_preserves_bound_source_state_and_unknown_owner(evidence):
    root, job_id, scope = evidence
    expected = create(root, job_id, scope)
    fence_path = root/FENCE
    fence = json.loads(fence_path.read_bytes())
    reviewed = root/'.daily-agent-recovery-reviewed.json'
    value = {**fence, 'status': 'reviewed', 'review': {'claims': 'test-only'}, 'reviewed_at': NOW.isoformat()}
    reviewed.write_text(json.dumps(value))
    fence_path.unlink()
    assert quarantine.validate_quarantine(root, job_id, scope) == expected
    assert expected['old_owner_status'] == 'unknown' and not expected['process_death_claimed']
    with pytest.raises(RecoveryBlocked):
        create(root, job_id, scope)
    value['receipts']['state']['version'] = 1
    reviewed.write_text(json.dumps(value))
    with pytest.raises(RecoveryBlocked):
        quarantine.validate_quarantine(root, job_id, scope)


def test_runtime_validation_can_preserve_expired_scope_without_renewal(evidence, monkeypatch):
    root, job_id, scope = evidence
    create(root, job_id, scope)
    monkeypatch.setattr(__import__(__name__), 'NOW', NOW + timedelta(days=1))
    with pytest.raises(RecoveryBlocked, match='future'):
        quarantine.validate_quarantine(root, job_id, scope)
    assert quarantine.validate_scope(scope, require_future=False) == scope
    assert quarantine.validate_quarantine(root, job_id, scope, require_future=False)['scope'] == scope
    with pytest.raises(StateCorrupt):
        quarantine.assert_not_quarantined(root, job_id)


def test_record_boolean_coercion_and_added_rights_do_not_validate(evidence):
    root, job_id, scope = evidence
    create(root, job_id, scope)
    path = root/quarantine.DIRECTORY/f'{job_id}.json'
    original = path.read_bytes()
    for change in ({'schema_version': True}, {'generation': True}, {'process_death_claimed': True},
                   {'authorization_granted': True}, {'budgets_reset': True}, {'allowed_to_retry': True}):
        value = json.loads(original)
        value.update(change)
        path.unlink()  # Isolated corruption injection, never production evidence.
        path.write_bytes(quarantine._encoded(value))
        with pytest.raises(RecoveryBlocked):
            quarantine.validate_quarantine(root, job_id, scope)
        with pytest.raises(StateCorrupt):
            quarantine.assert_not_quarantined(root, job_id)


def test_reviewed_hashes_are_mandatory_and_reject_owner_token_generation_changes(evidence):
    root, job_id, scope = evidence
    with pytest.raises(TypeError):
        quarantine.create_quarantine(root, job_id, scope)
    baseline = _REVIEWED_HASHES[str(root.resolve())]
    for bad in (None, True, '', 'A'*64):
        with pytest.raises(RecoveryBlocked):
            quarantine.create_quarantine(root, job_id, scope,
                                         **{**baseline, 'expected_claim_sha256': bad})
    path = root/f'data/writer-queue/{job_id}.claim.json'
    original = path.read_bytes()
    for field, value in [('worker_id', 'new-owner'), ('token', 'new-token'), ('generation', 3)]:
        path.write_bytes(original)
        rewrite(root, job_id, 'claim', lambda c: c.update({field: value}))
        with pytest.raises(RecoveryBlocked, match='reviewed hash'):
            create(root, job_id, scope)
        assert not (root/quarantine.DIRECTORY/f'{job_id}.json').exists()


def test_second_restore_uses_retained_exact_original_review_without_new_rights(evidence):
    root, job_id, scope = evidence
    expected = create(root, job_id, scope)
    original = json.loads((root/FENCE).read_bytes())
    reviewed = root/'.daily-agent-recovery-reviewed.json'
    reviewed.write_text(json.dumps({**original, 'status': 'reviewed',
                                    'review': {'fixture': True}, 'reviewed_at': NOW.isoformat()}))
    current = json.loads(json.dumps(original))
    current['receipts']['state']['version'] += 1
    current['restored_at'] = NOW.isoformat()
    (root/FENCE).write_text(json.dumps(current))
    assert quarantine.validate_quarantine(root, job_id, scope) == expected
    original_review_bytes = reviewed.read_bytes()
    history = root/'data/recovery/reviewed-fences'
    history.mkdir()
    preserved = history/(hashlib.sha256(original_review_bytes).hexdigest() + '.json')
    preserved.write_bytes(original_review_bytes)
    reviewed.write_text(json.dumps({**current, 'status': 'reviewed',
                                    'review': {'second_release': True}, 'reviewed_at': NOW.isoformat()}))
    # A second release has replaced the head review; the hash-bound history
    # still proves the immutable quarantine's original source/state provenance.
    assert quarantine.validate_quarantine(root, job_id, scope) == expected
    preserved.write_bytes(original_review_bytes + b'\n')
    with pytest.raises(RecoveryBlocked, match='filename/hash'):
        quarantine.validate_quarantine(root, job_id, scope)
    preserved.write_bytes(original_review_bytes)
    with pytest.raises(StateCorrupt, match='fenced'):
        atomic_json(root/'data/state/new.json', {'rights': 'none'})
    with pytest.raises(StateCorrupt, match='Quarantined'):
        quarantine.assert_not_quarantined(root, job_id)
    reviewed.unlink()
    preserved.unlink()
    with pytest.raises(RecoveryBlocked):
        quarantine.validate_quarantine(root, job_id, scope)


def test_image_evidence_refuses_path_escape_and_changed_bytes(evidence, tmp_path):
    root, old_id, scope = evidence
    queue = root/'data/writer-queue'
    job = json.loads((queue/f'{old_id}.job.json').read_bytes())
    claim = json.loads((queue/f'{old_id}.claim.json').read_bytes())
    (queue/f'{old_id}.job.json').unlink()
    (queue/f'{old_id}.claim.json').unlink()
    external = tmp_path/'image'
    external.write_bytes(b'retained-image')
    for path in ('../image', str(external), 'data/../image', 'data\\image'):
        job['images'] = [{'path': path, 'sha256': hashlib.sha256(external.read_bytes()).hexdigest()}]
        job_id = digest({k: job[k] for k in ('schema_version', 'transport', 'prompt', 'images', 'stage')})
        job.update(job_id=job_id, input_sha256=job_id)
        claim['job_id'] = job_id
        for suffix, value in [('job', job), ('claim', claim)]:
            (queue/f'{job_id}.{suffix}.json').write_text(json.dumps(value))
        review_hashes(root, job_id)
        with pytest.raises(RecoveryBlocked):
            create(root, job_id, scope)
        for suffix in ('job', 'claim'):
            (queue/f'{job_id}.{suffix}.json').unlink()
