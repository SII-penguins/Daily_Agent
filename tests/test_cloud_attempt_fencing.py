"""Offline generation fences: explicit attempts, executor identity and stale CAS."""
from datetime import date, datetime, timedelta, timezone
import os
from types import SimpleNamespace

import pytest

from daily_agent import cloud_workflow as cloud, incremental_issue as issue, pipeline, workflow_runtime
from daily_agent.batch_execution import ExecutionConflict
from daily_agent.workflow_state import StateCorrupt, WorkflowBusy, atomic_json, exclusive_lock, read_json

DAY = date(2026, 10, 10)
NOW = datetime(2026, 10, 10, 0, 0, tzinfo=timezone.utc)
NAMESPACE = 'boot-A:pidns-A'


@pytest.fixture
def config(tmp_path, monkeypatch):
    value = SimpleNamespace(root=tmp_path, state_dir=tmp_path/'data/state',
        reports_dir=tmp_path/'data/reports', selected_dir=tmp_path/'data/selected', logs_dir=tmp_path/'data/logs',
        sources={'selection': {'max_review_batches': 6, 'editorial_batch_size': 8},
                 'llm_writer': {'provider': 'codex'}}, domains=[], quota={'paper_target': 8, 'github_target': 2},
        delivery={'cloud': {'profile': cloud.PROFILE}, 'report': {'timezone': 'UTC'},
                  'schedule': {'delivery_time': '09:00', 'recovery': {}}})
    monkeypatch.setattr(cloud, '_config', lambda root: value)
    monkeypatch.setattr(pipeline, 'load_config', lambda root: value)
    monkeypatch.setattr(cloud, '_clock', lambda: NOW)
    monkeypatch.setattr(workflow_runtime, 'process_namespace', lambda: NAMESPACE)
    monkeypatch.setattr(issue, 'source_version', lambda: 'offline-source')
    return value


def paths(config):
    return (config.state_dir/'cloud-generation-budgets'/f'{DAY}.json',
            config.state_dir/'cloud-generation.json')


def active(config, attempt='current', namespace=NAMESPACE):
    budget = {'schema_version': 1, 'date': str(DAY), 'issue_started_at': NOW.isoformat(),
              'deadline': (NOW+timedelta(hours=9)).isoformat(), 'runtime_seconds': 0.,
              'failures': 0, 'resumes': 1, 'max_runtime_seconds': 25800., 'max_failures': 3,
              'max_resumes': 200, 'active_started_at': NOW.isoformat(), 'active_attempt_id': attempt,
              'active_namespace': namespace, 'active_timeout_seconds': 900}
    state = {'date': str(DAY), 'running': True, 'attempt_id': attempt, 'namespace': namespace,
             'child_identity': {'pid': os.getpid(), 'pgid': os.getpgid(0), 'description': 'offline'}}
    for path, value in zip(paths(config), (budget, state)):
        atomic_json(path, value)
    return budget, state


def invoke(config, entry, **kwargs):
    if entry == 'generate':
        return cloud.generate(config.root, DAY, **kwargs)
    if entry == 'pipeline':
        return pipeline.run_pipeline(config.root, DAY, dry_run=False, **kwargs)
    return issue.assert_active_generation(config, DAY, **kwargs)


@pytest.mark.parametrize('entry', ['generate', 'pipeline', 'assert'])
@pytest.mark.parametrize('tokens', [{}, {'expected_attempt': 'current'}, {'expected_namespace': NAMESPACE}])
def test_worker_cannot_infer_authority_from_current_state_or_environment(config, monkeypatch, entry, tokens):
    active(config)
    monkeypatch.setenv('DAILY_AGENT_EXPECTED_ATTEMPT', 'current')
    monkeypatch.setenv('DAILY_AGENT_EXPECTED_NAMESPACE', NAMESPACE)
    monkeypatch.setattr(pipeline, '_run_pipeline_unlocked', lambda *a, **k: pytest.fail('unbound worker ran'))
    before = [p.read_bytes() for p in paths(config)]
    with pytest.raises(ExecutionConflict, match='explicit supervisor'):
        invoke(config, entry, **tokens)
    assert [p.read_bytes() for p in paths(config)] == before


@pytest.mark.parametrize('entry', ['generate', 'pipeline', 'assert'])
@pytest.mark.parametrize('attempt,namespace,current_namespace', [
    ('obsolete', NAMESPACE, NAMESPACE),
    ('current', 'boot-A:pidns-old', 'boot-A:pidns-old'),
    ('obsolete', 'boot-A:pidns-old', 'boot-A:pidns-old'),
    ('current', NAMESPACE, 'boot-B:pidns-A'),
])
def test_same_numeric_pid_cannot_adopt_replacement_attempt(config, monkeypatch, entry,
                                                         attempt, namespace, current_namespace):
    active(config)
    monkeypatch.setattr(workflow_runtime, 'process_namespace', lambda: current_namespace)
    monkeypatch.setattr(pipeline, '_run_pipeline_unlocked', lambda *a, **k: pytest.fail('stale worker ran'))
    before = [p.read_bytes() for p in paths(config)]
    with pytest.raises(ExecutionConflict):
        invoke(config, entry, expected_attempt=attempt, expected_namespace=namespace)
    assert [p.read_bytes() for p in paths(config)] == before


def test_worker_rechecks_budget_during_start_handshake(config, monkeypatch):
    budget, state = active(config)
    state['child_identity'] = None
    atomic_json(paths(config)[1], state)
    def supersede(_):
        budget['active_attempt_id'] = 'replacement'
        atomic_json(paths(config)[0], budget)
    monkeypatch.setattr(issue.time, 'sleep', supersede)
    with pytest.raises(ExecutionConflict, match='supervisor active attempt'):
        issue.assert_active_generation(config, DAY, expected_attempt='current', expected_namespace=NAMESPACE)
    assert read_json(paths(config)[0])['active_attempt_id'] == 'replacement'


def test_worker_enforces_reserved_timeout(config, monkeypatch):
    active(config)
    monkeypatch.setattr(cloud, '_clock', lambda: NOW+timedelta(seconds=900))
    with pytest.raises(ExecutionConflict):
        issue.assert_active_generation(config, DAY, expected_attempt='current', expected_namespace=NAMESPACE)


def test_supervisor_explicit_cli_binding_and_legacy_idle_budget_work(config, monkeypatch):
    seen = []
    def run(command, root, timeout, **callbacks):
        attempt = command[command.index('--expected-attempt')+1]
        namespace = command[command.index('--expected-namespace')+1]
        assert namespace == NAMESPACE
        assert '--expected-attempt' in command
        budget, state = (read_json(p) for p in paths(config))
        assert budget['active_attempt_id'] == state['attempt_id'] == attempt
        assert budget['active_namespace'] == state['namespace'] == namespace
        assert 'execution_protocol' not in budget
        callbacks['on_start']({'pid': os.getpid(), 'pgid': os.getpgid(0), 'description': 'offline'})
        callbacks['on_tick']()
        with pytest.raises(WorkflowBusy):
            with exclusive_lock(paths(config)[1].with_suffix('.lock')):
                pass
        issue.assert_active_generation(config, DAY, expected_attempt=attempt, expected_namespace=namespace)
        seen.append(attempt)
        return 75
    monkeypatch.setattr(workflow_runtime, 'run_process', run)
    assert cloud.run_generation(config.root, DAY, 30) == 75
    assert cloud.run_generation(config.root, DAY, 30) == 75
    assert len(set(seen)) == 2
    budget, state = (read_json(p) for p in paths(config))
    assert budget['last_attempt_id'] == state['attempt_id'] == seen[-1]
    assert budget['last_namespace'] == state['namespace'] == NAMESPACE
    assert budget['resumes'] == 2 and budget['failures'] == 0
    assert 'active_attempt_id' not in budget and 'active_namespace' not in budget
    assert 'active_timeout_seconds' not in budget and state['running'] is False


@pytest.mark.parametrize('when', ['start', 'tick', 'finish', 'finished_write'])
@pytest.mark.parametrize('replacement', ['attempt', 'namespace', 'state_only', 'budget_only'])
def test_stale_callbacks_and_settlement_cannot_overwrite_replacement(config, monkeypatch, when, replacement):
    replacement_bytes = []
    def supersede():
        budget, state = (read_json(p) for p in paths(config))
        if replacement == 'namespace':
            budget['active_namespace'] = state['namespace'] = 'boot-B:pidns-B'
        elif replacement == 'state_only':
            state['attempt_id'] = 'replacement'
        elif replacement == 'budget_only':
            budget['active_attempt_id'] = 'replacement'
        else:
            budget['active_attempt_id'] = state['attempt_id'] = 'replacement'
        if not budget.get('active_started_at'):
            budget['active_started_at'] = NOW.isoformat()
            budget.setdefault('active_attempt_id', 'replacement')
            budget.setdefault('active_namespace', NAMESPACE)
        for path, value in zip(paths(config), (budget, state)):
            atomic_json(path, value)
        replacement_bytes[:] = [p.read_bytes() for p in paths(config)]
    def run(command, root, timeout, **callbacks):
        if when == 'start':
            supersede()
            callbacks['on_start']({'pid': 5, 'pgid': 5, 'description': 'obsolete'})
        callbacks['on_start']({'pid': 5, 'pgid': 5, 'description': 'first'})
        if when in {'tick', 'finish'}:
            supersede()
        if when == 'tick':
            callbacks['on_tick']()
        return 75
    monkeypatch.setattr(workflow_runtime, 'run_process', run)
    if when == 'finished_write':
        original = cloud.atomic_json
        def replace_after_settlement(path, value):
            original(path, value)
            if path == paths(config)[0] and value.get('last_returncode') == 75:
                supersede()
        monkeypatch.setattr(cloud, 'atomic_json', replace_after_settlement)
    with pytest.raises(StateCorrupt, match='superseded'):
        cloud.run_generation(config.root, DAY, 30)
    assert replacement_bytes and [p.read_bytes() for p in paths(config)] == replacement_bytes


@pytest.mark.parametrize('legacy', [True, False])
def test_unverifiable_or_foreign_namespace_never_inspects_local_pid_for_cleanup(config, monkeypatch, legacy):
    budget, state = active(config, namespace='boot-old:pidns-old')
    if legacy:
        budget.pop('active_namespace')
        state.pop('namespace')
        for path, value in zip(paths(config), (budget, state)):
            atomic_json(path, value)
    before = [p.read_bytes() for p in paths(config)]
    monkeypatch.setattr(workflow_runtime, 'stop_verified_orphan', lambda *a, **k: pytest.fail('unsafe local cleanup'))
    monkeypatch.setattr(workflow_runtime, 'run_process', lambda *a, **k: pytest.fail('replacement launched'))
    with pytest.raises(StateCorrupt, match='namespace cannot be verified'):
        cloud.run_generation(config.root, DAY, 30)
    assert [p.read_bytes() for p in paths(config)] == before


def test_cli_forwards_explicit_worker_fence(config, monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(cloud, '_local_day', lambda *a: DAY)
    monkeypatch.setattr(cloud, 'generate', lambda *a, **k: seen.append(k))
    assert cloud.main(['generate', '--root', str(config.root), '--date', str(DAY),
                      '--expected-attempt', 'worker-token', '--expected-namespace', NAMESPACE]) == 0
    assert seen == [{'expected_attempt': 'worker-token', 'expected_namespace': NAMESPACE}]
    assert '"generated": true' in capsys.readouterr().out


@pytest.mark.parametrize('cloud_profile', [True, False])
def test_offline_model_free_pipeline_remains_compatible(config, monkeypatch, cloud_profile):
    if not cloud_profile:
        config.delivery.pop('cloud')
    monkeypatch.setattr(pipeline, '_run_pipeline_unlocked', lambda *a, **k: 'offline')
    assert pipeline.run_pipeline(config.root, DAY, dry_run=True, use_llm=False) == 'offline'
    assert not paths(config)[0].exists()


def test_ordinary_noncloud_pipeline_does_not_require_cloud_fence(config, monkeypatch):
    config.delivery.pop('cloud')
    monkeypatch.setattr(pipeline, '_run_pipeline_unlocked', lambda *a, **k: 'legacy')
    assert pipeline.run_pipeline(config.root, DAY, dry_run=False, use_llm=True) == 'legacy'


def test_retained_callbacks_cannot_rewrite_settled_attempt(config, monkeypatch):
    retained = {}
    def run(*args, **callbacks):
        retained.update(callbacks)
        callbacks['on_start']({'pid': 5, 'pgid': 5, 'description': 'offline'})
        return 75
    monkeypatch.setattr(workflow_runtime, 'run_process', run)
    assert cloud.run_generation(config.root, DAY, 30) == 75
    before = [p.read_bytes() for p in paths(config)]
    with pytest.raises(StateCorrupt, match='superseded'):
        retained['on_start']({'pid': 5, 'pgid': 5, 'description': 'late'})
    with pytest.raises(StateCorrupt, match='superseded'):
        retained['on_tick']()
    assert [p.read_bytes() for p in paths(config)] == before


def test_namespace_fence_binds_boot_and_pid_namespace(monkeypatch):
    boot = ['11111111-1111-4111-8111-111111111111']
    namespace = ['pid:[12345]']
    monkeypatch.setattr(workflow_runtime.Path, 'read_text', lambda *a, **k: boot[0])
    monkeypatch.setattr(workflow_runtime.os, 'readlink', lambda *a, **k: namespace[0])
    first = workflow_runtime.process_namespace()
    assert len(first) == 64 and workflow_runtime.process_namespace() == first
    namespace[0] = 'pid:[54321]'
    assert workflow_runtime.process_namespace() != first
    namespace[0] = 'pid:[12345]'
    boot[0] = '22222222-2222-4222-8222-222222222222'
    assert workflow_runtime.process_namespace() != first


@pytest.mark.parametrize('failure', ['boot_missing', 'boot_invalid', 'namespace_missing', 'namespace_invalid'])
def test_namespace_fence_never_falls_back_when_unverifiable(monkeypatch, failure):
    def read(*a, **k):
        if failure == 'boot_missing':
            raise OSError('boot identity unavailable')
        return 'bad' if failure == 'boot_invalid' else '11111111-1111-4111-8111-111111111111'
    def link(*a, **k):
        if failure == 'namespace_missing':
            raise OSError('PID namespace unavailable')
        return 'bad' if failure == 'namespace_invalid' else 'pid:[12345]'
    monkeypatch.setattr(workflow_runtime.Path, 'read_text', read)
    monkeypatch.setattr(workflow_runtime.os, 'readlink', link)
    with pytest.raises(StateCorrupt, match='Cannot verify execution namespace'):
        workflow_runtime.process_namespace()


def test_recovery_cannot_overwrite_fence_changed_during_orphan_cleanup(config, monkeypatch):
    active(config)
    after = []
    def cleanup(*a, **k):
        active(config, attempt='replacement')
        after[:] = [p.read_bytes() for p in paths(config)]
    monkeypatch.setattr(workflow_runtime, 'stop_verified_orphan', cleanup)
    monkeypatch.setattr(workflow_runtime, 'run_process', lambda *a, **k: pytest.fail('replacement launched'))
    with pytest.raises(StateCorrupt, match='superseded'):
        cloud.run_generation(config.root, DAY, 30)
    assert after and [p.read_bytes() for p in paths(config)] == after


@pytest.mark.parametrize('persisted', [False, True], ids=['before-write', 'after-rename'])
def test_on_start_io_and_cleanup_failure_never_settle_possibly_live_child(config, monkeypatch, persisted):
    writes = []
    original = cloud.atomic_json
    def failing_identity_write(path, value):
        if path == paths(config)[1] and value.get('child_identity') is not None:
            if persisted:
                original(path, value)
            writes.append('callback-write-failed')
            raise OSError('simulated identity fsync failure')
        original(path, value)
    monkeypatch.setattr(cloud, 'atomic_json', failing_identity_write)
    # Exercise the real run_process finally path with a fake child: no process
    # is launched or signaled, while callback/fsync and cleanup failures compose.
    reaped = []
    process = SimpleNamespace(pid=5, wait=lambda **k: reaped.append(True))
    monkeypatch.setattr(workflow_runtime.subprocess, 'Popen', lambda *a, **k: process)
    def fail_cleanup(*a, **k):
        raise workflow_runtime.CleanupPending('simulated live process group remains')
    monkeypatch.setattr(workflow_runtime, 'stop_group', fail_cleanup)
    with pytest.raises(workflow_runtime.CleanupPending, match='live process group remains'):
        cloud.run_generation(config.root, DAY, 30)
    budget, state = (read_json(p) for p in paths(config))
    assert writes == ['callback-write-failed'] and reaped == [True]
    assert budget['active_attempt_id'] == state['attempt_id']
    assert budget['active_namespace'] == state['namespace'] == NAMESPACE
    assert budget['failures'] == 0 and 'last_attempt_id' not in budget
    assert state['running'] is True and 'finished_at' not in state
    assert (state['child_identity'] is not None) is persisted


@pytest.mark.parametrize('persisted', [False, True], ids=['before-write', 'after-rename'])
def test_on_start_io_failure_retains_launch_even_when_cleanup_error_is_not_wrapped(config, monkeypatch, persisted):
    original = cloud.atomic_json
    def failing_identity_write(path, value):
        if path == paths(config)[1] and value.get('child_identity') is not None:
            if persisted:
                original(path, value)
            raise OSError('simulated identity fsync failure')
        original(path, value)
    monkeypatch.setattr(cloud, 'atomic_json', failing_identity_write)
    monkeypatch.setattr(workflow_runtime, 'run_process', lambda *a, **k:
        k['on_start']({'pid': 5, 'pgid': 5, 'description': None}))
    with pytest.raises(OSError, match='identity fsync failure'):
        cloud.run_generation(config.root, DAY, 30)
    budget, state = (read_json(p) for p in paths(config))
    assert budget['active_attempt_id'] == state['attempt_id']
    assert budget['failures'] == 0 and 'last_attempt_id' not in budget
    assert state['running'] is True and 'finished_at' not in state


def test_cleanup_pending_before_callback_never_becomes_prelaunch_failure(config, monkeypatch):
    def uncertain_launch(*a, **k):
        raise workflow_runtime.CleanupPending('launch cleanup is unconfirmed')
    monkeypatch.setattr(workflow_runtime, 'run_process', uncertain_launch)
    with pytest.raises(workflow_runtime.CleanupPending, match='unconfirmed'):
        cloud.run_generation(config.root, DAY, 30)
    budget, state = (read_json(p) for p in paths(config))
    assert budget['active_attempt_id'] == state['attempt_id']
    assert budget['failures'] == 0 and 'last_attempt_id' not in budget
    assert state['running'] is True
