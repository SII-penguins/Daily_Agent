"""Failure injection: no network, model calls, or remote messages."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
import yaml

import daily_agent.scheduling as schedule
from daily_agent.config import load_config
from daily_agent.models import ApprovedItem, DeliveryStatus, MaterialRecord
from daily_agent.workflow_state import StateCorrupt, WorkflowBusy, atomic_json, exclusive_lock, read_json
from daily_agent.workflow_runtime import run_process

DAY = date(2026, 10, 8)


@pytest.fixture(autouse=True)
def isolated_delivery_preflight(monkeypatch):
    # These tests replace the remote adapter; local capability checks are another
    # adapter boundary. Their real behavior is covered in test_recovery_contracts.
    monkeypatch.setattr("daily_agent.delivery.feishu.preflight_delivery", lambda *args: None)


def sandbox(root, **recovery):
    (root / 'config').mkdir(exist_ok=True)
    settings = {'report': {'timezone': 'Asia/Shanghai'}, 'delivery': {'default': 'cc-connect'},
                'schedule': {'recovery': {'base_backoff_seconds': 0, 'max_attempts': 3, **recovery}}}
    (root / 'config/delivery.yaml').write_text(yaml.safe_dump(settings))
    return load_config(root)


def sealed(root, **recovery):
    cfg = sandbox(root, **recovery)
    record = MaterialRecord(key='arxiv:1234', source='arxiv', item_type='paper', title='Quantum circuit',
                            url='https://arxiv.org/abs/1234')
    item = ApprovedItem(key=record.key, item_type=record.item_type, title=record.title, source=record.source,
                        url=record.url, final_fields={'problem': 'Verified claim'}, material=record)
    cfg.reports_dir.mkdir()
    (cfg.reports_dir / f'daily-agent-{DAY}.md').write_text(f'# Daily {DAY}\nQuantum circuit')
    folder = root / 'data/editorial' / str(DAY)
    folder.mkdir(parents=True)
    atomic_json(folder / 'approval.json', [item.to_dict()])
    schedule.seal_ready_report(cfg, DAY)
    return cfg


def state_file(root):
    return root / 'data/state/schedule-stages' / f'{DAY}.json'


def test_transient_retries_are_bounded_across_scheduler_triggers(tmp_path):
    sandbox(tmp_path)
    calls = []
    def failed(command, root):
        calls.append(command)
        return 75
    for _ in range(2):
        with pytest.raises(schedule.StageFailure):
            schedule.run_scheduled_stage(tmp_path, 'overnight', DAY, executor=failed)
    assert len(calls) == 3
    entry = read_json(state_file(tmp_path))['stages']['overnight']
    assert entry['attempts'] == 3 and entry['circuit_open'] is True
    assert schedule.repair_schedule_state(tmp_path, DAY)['blocked']
    assert read_json(state_file(tmp_path))['stages']['overnight']['attempts'] == 3


def test_revision_change_reopens_permanent_failure_without_blind_retries(tmp_path):
    sandbox(tmp_path)
    with pytest.raises(schedule.StageFailure):
        schedule.run_scheduled_stage(tmp_path, 'overnight', DAY, executor=lambda *args: 1)
    with pytest.raises(schedule.StageFailure, match='circuit is open'):
        schedule.run_scheduled_stage(tmp_path, 'overnight', DAY, executor=lambda *args: 0)
    (tmp_path / 'config/fixed.yaml').write_text('repaired: true\n')
    result = schedule.run_scheduled_stage(tmp_path, 'overnight', DAY, executor=lambda *args: 0)
    assert result.completed_stages == ['overnight']
    state = read_json(state_file(tmp_path))
    assert state['stages']['overnight']['attempts'] == 1
    assert any(event['kind'] == 'revision_changed' for event in state['events'])


def test_review_resumes_only_failed_command_and_preserves_advisory_warning(tmp_path):
    sandbox(tmp_path)
    counts = {'run': 0, 'feedback': 0, 'quality': 0, 'source': 0}
    def executor(command, root):
        name = command[3]
        counts[name] += 1
        if name == 'feedback':
            return 1
        if name == 'source' and counts[name] == 1:
            return 75
        return 0
    result = schedule.run_scheduled_stage(tmp_path, 'review', DAY, executor=executor)
    assert counts == {'run': 1, 'feedback': 1, 'quality': 1, 'source': 2}
    assert len(result.warnings) == 1
    assert read_json(state_file(tmp_path))['stages']['review']['attempts'] == 2


def test_missing_output_cannot_mark_native_generation_completed(tmp_path, monkeypatch):
    sandbox(tmp_path)
    monkeypatch.setattr(schedule, '_now', lambda: datetime(2026, 10, 7, 16, 11, tzinfo=timezone.utc))
    monkeypatch.setattr(schedule, '_run_stage_command', lambda *args, **kwargs: 0)
    with pytest.raises(schedule.StageFailure):
        schedule.run_scheduled_stage(tmp_path, 'overnight', DAY)
    entry = read_json(state_file(tmp_path))['stages']['overnight']
    assert entry['completed'] is False and entry['failed'] is True


def test_watchdog_does_not_report_future_jobs_as_missed(tmp_path, monkeypatch):
    sandbox(tmp_path)
    monkeypatch.setattr(schedule, '_now', lambda: datetime(2026, 10, 7, 16, 0, tzinfo=timezone.utc))
    result = schedule.inspect_schedule_state(tmp_path, DAY)
    assert result['ok'] and result['problems'] == []
    assert result['readiness']['ready'] is False
    assert not state_file(tmp_path).exists()


def test_expired_window_never_starts_a_command(tmp_path, monkeypatch):
    sandbox(tmp_path)
    monkeypatch.setattr(schedule, '_now', lambda: datetime(2026, 10, 8, 4, 0, tzinfo=timezone.utc))
    calls = []
    monkeypatch.setattr(schedule, '_run_stage_command', lambda *a, **kw: calls.append(a))
    with pytest.raises(schedule.StageFailure, match='execution window expired'):
        schedule.run_scheduled_stage(tmp_path, 'overnight', DAY)
    assert calls == []
    assert read_json(state_file(tmp_path))['stages']['overnight']['failure_kind'] == 'deadline'


def test_repair_cannot_overwrite_an_active_lease(tmp_path):
    sandbox(tmp_path)
    with exclusive_lock(schedule._stage_lock(tmp_path)):
        with pytest.raises(WorkflowBusy):
            schedule.repair_schedule_state(tmp_path, DAY)
        with pytest.raises(WorkflowBusy):
            schedule.run_scheduled_stage(tmp_path, 'delivery', DAY, executor=lambda *args: 0)
    assert not state_file(tmp_path).exists()


def test_corrupt_state_restores_backup_only_in_explicit_locked_repair(tmp_path):
    sandbox(tmp_path)
    with pytest.raises(schedule.StageFailure):
        schedule.run_scheduled_stage(tmp_path, 'overnight', DAY, executor=lambda *args: 1)
    path = state_file(tmp_path)
    backup = path.with_suffix('.bak').read_bytes()
    path.write_text('{broken')
    before = list(path.parent.iterdir())
    assert schedule.inspect_schedule_state(tmp_path, DAY)['ok'] is False
    assert path.read_text() == '{broken' and list(path.parent.iterdir()) == before
    schedule.repair_schedule_state(tmp_path, DAY, reset_budget=True)
    state = read_json(path)
    assert any(event['kind'] == 'restore_backup' for event in state['events'])
    assert next(path.parent.glob('*.corrupt.*')).read_text() == '{broken'
    assert backup  # original checkpoint existed and wasn't fabricated


def test_corrupt_state_without_backup_blocks_instead_of_restarting(tmp_path):
    sandbox(tmp_path)
    path = state_file(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text('null')
    with pytest.raises(StateCorrupt):
        schedule.repair_schedule_state(tmp_path, DAY, reset_budget=True)
    assert path.read_text() == 'null'


def test_approval_tampering_and_report_tampering_block_remote_send(tmp_path, monkeypatch):
    cfg = sealed(tmp_path)
    monkeypatch.setattr('daily_agent.delivery.feishu.deliver_weekly_report', lambda *a, **kw: pytest.fail('remote called'))
    path = schedule._ready_path(cfg, DAY)
    payload = read_json(path)
    payload['approval'][0]['final_fields']['problem'] = 'Invented claim'
    atomic_json(path, payload)
    with pytest.raises(schedule.StageFailure, match='Approval identity mismatch'):
        schedule.deliver_ready_report(cfg, DAY)
    payload['approval_sha256'] = schedule._approval_hash(payload['approval'])
    atomic_json(path, payload)
    Path(payload['report']).write_text('corrupted')
    with pytest.raises(schedule.StageFailure, match='identity mismatch'):
        schedule.deliver_ready_report(cfg, DAY)


def test_repo_quota_uses_repo_type_and_insufficient_issue_never_seals(tmp_path):
    cfg = sealed(tmp_path)
    cfg.delivery['schedule']['recovery']['require_target_counts'] = True
    cfg.quota.update(paper_target=1, github_target=1)
    with pytest.raises(schedule.StageFailure, match='repo count'):
        schedule.validate_ready_report(cfg, DAY)
    cfg.quota['github_target'] = 0
    assert schedule.validate_ready_report(cfg, DAY)


def test_lost_ack_is_not_retried_or_cleared_by_repair(tmp_path, monkeypatch):
    cfg = sealed(tmp_path)
    calls = []
    def lost_ack(*args, **kwargs):
        calls.append(args)
        raise TimeoutError('remote accepted but acknowledgement was lost')
    monkeypatch.setattr('daily_agent.delivery.feishu.deliver_weekly_report', lost_ack)
    for _ in range(2):
        with pytest.raises(schedule.StageFailure, match='uncertain'):
            schedule.deliver_ready_report(cfg, DAY)
    assert len(calls) == 1
    assert schedule.repair_schedule_state(tmp_path, DAY, reset_budget=True)['blocked']
    assert schedule.inspect_schedule_state(tmp_path, DAY)['readiness']['delivered'] is False
    schedule.resolve_delivery_outcome(cfg, DAY, 'sent', 'Checked the remote message identifier')
    schedule.deliver_ready_report(cfg, DAY)
    assert len(calls) == 1
    receipt = read_json(schedule._ready_path(cfg, DAY).with_suffix('.delivered.json'))
    assert receipt['publication_reconciled'] and receipt['operator_confirmed']
    with pytest.raises(schedule.StageFailure, match='cannot be downgraded'):
        schedule.resolve_delivery_outcome(cfg, DAY, 'not-sent', 'wrong operator decision')


def test_corrupt_receipt_never_becomes_permission_to_resend(tmp_path, monkeypatch):
    cfg = sealed(tmp_path)
    receipt = schedule._ready_path(cfg, DAY).with_suffix('.delivered.json')
    receipt.write_text('null')
    monkeypatch.setattr('daily_agent.delivery.feishu.deliver_weekly_report', lambda *a, **kw: pytest.fail('remote called'))
    with pytest.raises(StateCorrupt):
        schedule.deliver_ready_report(cfg, DAY)
    assert receipt.read_text() == 'null'


def test_publication_failure_retries_local_commit_without_remote_resend(tmp_path, monkeypatch):
    cfg = sealed(tmp_path)
    calls = []
    monkeypatch.setattr('daily_agent.delivery.feishu.deliver_weekly_report', lambda *a, **kw: calls.append(a) or DeliveryStatus(requested_mode='cc-connect', final_mode='cc-connect'))
    import daily_agent.storage as storage
    original = storage.write_selected
    def fail(*args, **kwargs):
        raise OSError('simulated local disk write failure')
    monkeypatch.setattr(storage, 'write_selected', fail)
    with pytest.raises(OSError):
        schedule.deliver_ready_report(cfg, DAY)
    receipt = read_json(schedule._ready_path(cfg, DAY).with_suffix('.delivered.json'))
    assert receipt['status']['ok'] is True and not receipt['publication_reconciled']
    monkeypatch.setattr(storage, 'write_selected', original)
    schedule.deliver_ready_report(cfg, DAY)
    assert len(calls) == 1
    assert storage.load_material_library(cfg)['arxiv:1234'].published_dates == [str(DAY)]


def test_sent_issue_cannot_be_replaced_by_later_generation(tmp_path, monkeypatch):
    cfg = sealed(tmp_path)
    calls = []
    monkeypatch.setattr('daily_agent.delivery.feishu.deliver_weekly_report', lambda *a, **kw: calls.append(a) or DeliveryStatus(requested_mode='cc-connect', final_mode='cc-connect'))
    schedule.deliver_ready_report(cfg, DAY)
    first = read_json(schedule._ready_path(cfg, DAY))
    (cfg.reports_dir / f'daily-agent-{DAY}.md').write_text('different later issue')
    schedule.seal_ready_report(cfg, DAY)
    assert read_json(schedule._ready_path(cfg, DAY)) == first
    schedule.deliver_ready_report(cfg, DAY)
    assert len(calls) == 1


def test_manual_generation_cannot_race_with_publication_writes(tmp_path):
    cfg = sandbox(tmp_path)
    from daily_agent.pipeline import run_pipeline
    with exclusive_lock(cfg.state_dir / 'pipeline.lock'):
        with pytest.raises(WorkflowBusy):
            run_pipeline(root=tmp_path, run_date=DAY, use_llm=False)


@pytest.mark.parametrize('record', ['data/materials/library.json', 'data/state/history_index.json',
                                    'data/state/published_index.json', 'data/state/feishu_delivery.json'])
def test_corrupt_durable_records_fail_closed_without_erasing_evidence(tmp_path, record):
    cfg = sandbox(tmp_path)
    path = tmp_path / record
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{broken')
    from daily_agent import storage
    operation = {'library.json': lambda: storage.load_material_library(cfg),
                 'history_index.json': lambda: storage.load_history(cfg, DAY),
                 'published_index.json': lambda: storage.load_published_index(cfg),
                 'feishu_delivery.json': lambda: storage.load_feishu_delivery_state(cfg)}[path.name]
    with pytest.raises(StateCorrupt):
        operation()
    assert path.read_text() == '{broken'


def alive_not_zombie(pid):
    response = subprocess.run(['ps', '-p', str(pid), '-o', 'stat='], capture_output=True, text=True)
    return bool(response.stdout.strip()) and not response.stdout.strip().startswith('Z')


def child_script(pidfile):
    return ("import subprocess,sys,time; "
            "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
            f"open({str(pidfile)!r},'w').write(str(child.pid)); ")


def test_timeout_cleans_up_real_grandchild_and_emits_heartbeats(tmp_path):
    pidfile = tmp_path / 'child.pid'
    ticks = []
    code = run_process([sys.executable, '-c', child_script(pidfile) + 'time.sleep(30)'], tmp_path, .5,
                       on_tick=lambda: ticks.append(time.monotonic()), heartbeat_seconds=.05)
    assert code == 124 and len(ticks) >= 2
    assert not alive_not_zombie(int(pidfile.read_text()))


def test_successful_parent_also_cleans_up_detached_work_in_its_group(tmp_path):
    pidfile = tmp_path / 'child.pid'
    assert run_process([sys.executable, '-c', child_script(pidfile)], tmp_path, 3) == 0
    assert not alive_not_zombie(int(pidfile.read_text()))


def test_pid_reuse_cannot_kill_an_unrelated_process(monkeypatch):
    from daily_agent import workflow_runtime as runtime
    monkeypatch.setattr(runtime, 'process_identity', lambda pid: {'pid': pid, 'pgid': pid, 'description': 'new birth'})
    monkeypatch.setattr(runtime, 'stop_group', lambda *args, **kw: pytest.fail('unrelated PID signalled'))
    assert runtime.stop_verified_orphan({'pid': 99999, 'pgid': 99999, 'description': 'old birth'}) is False


def test_sigterm_to_supervisor_terminates_the_active_child(tmp_path):
    pidfile = tmp_path / 'child.pid'
    child = child_script(pidfile) + 'time.sleep(30)'
    code = ('from pathlib import Path; from daily_agent.workflow_runtime import run_process; '
            f'run_process([{sys.executable!r}, "-c", {child!r}], Path({str(tmp_path)!r}), 30)')
    env = {**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')}
    supervisor = subprocess.Popen([sys.executable, '-c', code], env=env, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 5
        while not pidfile.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        assert pidfile.exists()
        supervisor.send_signal(signal.SIGTERM)
        supervisor.wait(timeout=8)
        assert not alive_not_zombie(int(pidfile.read_text()))
    finally:
        if supervisor.poll() is None:
            supervisor.kill()
            supervisor.wait(timeout=5)


@pytest.mark.parametrize('succeeds, publication_fails', [(False, False), (True, False), (True, True)])
def test_formal_pipeline_waits_for_receipt_before_publication(tmp_path, monkeypatch, succeeds, publication_fails):
    cfg = sandbox(tmp_path)
    import daily_agent.pipeline as pipeline
    import daily_agent.storage as storage
    record = MaterialRecord(key='arxiv:integration', source='arxiv', item_type='paper',
                            title='Quantum integration', url='https://arxiv.org/abs/integration')
    item = ApprovedItem(key=record.key, item_type=record.item_type, source=record.source, title=record.title,
                        url=record.url, final_fields={}, material=record)
    monkeypatch.setattr(pipeline, '_fetch_windowed_sources', lambda *a, **kw: [])
    monkeypatch.setattr(pipeline, 'load_material_library', lambda *a: {record.key: record})
    monkeypatch.setattr(pipeline, '_build_shortlist_from_library', lambda config, library, today: [record] if record.key in library else [])
    for name in ('enrich_open_access_links', 'enrich_unpaywall_links', 'enrich_paper_texts', 'enrich_citation_contexts', 'cache_selected_pdfs'):
        monkeypatch.setattr(pipeline, name, lambda values, *a: values)
    monkeypatch.setattr(pipeline, 'draft_report_items', lambda *a, **kw: [])
    monkeypatch.setattr(pipeline, 'review_draft', lambda *a, **kw: [])
    monkeypatch.setattr(pipeline, 'approve_publication', lambda *a: [item])
    calls = []
    def delivery(*args, **kwargs):
        # The remote call must observe no formal publication, including history.
        assert storage.load_material_library(cfg)[record.key].published_dates == []
        assert storage.load_history(cfg, DAY) == {}
        assert not (cfg.selected_dir / f'selected-{DAY}.json').exists()
        calls.append(args)
        if not succeeds:
            raise TimeoutError('acknowledgement lost')
        return DeliveryStatus(requested_mode='cc-connect', final_mode='cc-connect')
    monkeypatch.setattr('daily_agent.delivery.feishu.deliver_weekly_report', delivery)
    if publication_fails:
        original = storage.write_selected
        def fail_commit(config, items, day, dry_run=False):
            if not dry_run:
                raise OSError('formal history commit failed')
            return original(config, items, day, dry_run=dry_run)
        monkeypatch.setattr(storage, 'write_selected', fail_commit)
    result = pipeline.run_pipeline(tmp_path, DAY, dry_run=False, use_llm=False, delivery_mode='cc-connect')
    assert len(calls) == 1 and result.status.delivery.ok == succeeds
    assert bool(storage.load_history(cfg, DAY)) == (succeeds and not publication_fails)
    assert bool(storage.load_material_library(cfg)[record.key].published_dates) == succeeds
    assert result.health['current']['signals']['delivery']['ok'] == succeeds
    if publication_fails:
        assert result.health['current']['overall'] == 'failed'
        assert any(check['id'] == 'local_publication_pending' and check['status'] == 'fail'
                   for check in result.health['current']['checks'])
        import daily_agent.cli as cli
        from types import SimpleNamespace
        monkeypatch.setattr(cli, 'run_pipeline', lambda **kwargs: result)
        monkeypatch.setattr(cli, 'enforce_full_profile', lambda *a, **kw: SimpleNamespace(changed=False))
        args = SimpleNamespace(root=str(tmp_path), date=str(DAY), dry_run=False, send='cc-connect',
                               require_full=False, allow_degraded=True, llm=False)
        assert cli._run_with_runtime(args) == 5


def test_adding_missing_credential_reopens_circuit_without_recording_the_secret(tmp_path, monkeypatch):
    sandbox(tmp_path)
    import daily_agent.secrets as secrets
    configured = False
    monkeypatch.setattr(secrets, 'credential_present', lambda name: configured if name == 'CORE_API_KEY' else False)
    with pytest.raises(schedule.StageFailure):
        schedule.run_scheduled_stage(tmp_path, 'overnight', DAY, executor=lambda *args: 2)
    configured = True
    result = schedule.run_scheduled_stage(tmp_path, 'overnight', DAY, executor=lambda *args: 0)
    assert result.completed_stages == ['overnight']
    assert 'CORE_API_KEY' not in state_file(tmp_path).read_text()


def test_backpressure_limits_reading_batch_and_reserves_project_slots(tmp_path):
    from daily_agent.pipeline import _bounded_editorial_batch
    cfg = sandbox(tmp_path)
    cfg.sources['selection'] = {'editorial_batch_size': 8}
    cfg.quota.update(paper_target=8, github_target=2)
    papers = [MaterialRecord(key=f'p:{i}', source='arxiv', item_type='paper', title=f'Paper {i}', url='https://example.org') for i in range(32)]
    repos = [MaterialRecord(key=f'r:{i}', source='github', item_type='repo', title=f'Repo {i}', url='https://example.org') for i in range(2)]
    records = papers + repos
    batch = _bounded_editorial_batch(cfg, records, [])
    assert len(batch) == 8 and sum(item.item_type == 'paper' for item in batch) == 6
    assert batch[-2:] == repos and records == papers + repos
    already = [ApprovedItem(key=record.key, title=record.title, source=record.source, item_type=record.item_type,
                            url=record.url, final_fields={}, material=record) for record in papers[:8]]
    assert _bounded_editorial_batch(cfg, records, already) == repos


def test_pipeline_keeps_refilling_projects_after_paper_quota_is_met(tmp_path, monkeypatch):
    cfg = sandbox(tmp_path)
    cfg.quota.update(max_items=2, paper_target=1, github_target=1)
    cfg.sources['selection'] = {'editorial_batch_size': 2, 'max_review_batches': 3}
    import daily_agent.pipeline as pipeline
    paper = MaterialRecord(key='p:1', source='arxiv', item_type='paper', title='Quantum paper', url='https://example.org/p')
    repos = [MaterialRecord(key=f'r:{i}', source='github', item_type='repo', title=f'Quantum repo {i}', url='https://example.org/r') for i in range(2)]
    library = {record.key: record for record in [paper] + repos}
    monkeypatch.setattr(pipeline, 'load_config', lambda root: cfg)
    monkeypatch.setattr(pipeline, '_fetch_windowed_sources', lambda *a, **kw: [])
    monkeypatch.setattr(pipeline, 'load_material_library', lambda *a: dict(library))
    monkeypatch.setattr(pipeline, '_build_shortlist_from_library', lambda config, library, today: list(library.values()))
    for name in ('enrich_open_access_links', 'enrich_unpaywall_links', 'enrich_paper_texts', 'enrich_citation_contexts', 'cache_selected_pdfs'):
        monkeypatch.setattr(pipeline, name, lambda values, *a: values)
    batches = []
    def draft(config, records, **kwargs):
        batches.append([record.key for record in records])
        return []
    monkeypatch.setattr(pipeline, 'draft_report_items', draft)
    monkeypatch.setattr(pipeline, 'review_draft', lambda *a, **kw: [])
    def approve(config, records, *args):
        # The first repo failed evidence review. The replacement must still be tried.
        return [ApprovedItem(key=record.key, title=record.title, item_type=record.item_type, source=record.source,
                             url=record.url, final_fields={}, material=record) for record in records if record.key != 'r:0']
    monkeypatch.setattr(pipeline, 'approve_publication', approve)
    result = pipeline.run_pipeline(tmp_path, DAY, use_llm=False)
    assert batches == [['p:1', 'r:0'], ['r:1']]
    assert [item.key for item in result.items] == ['p:1', 'r:1']


def test_morning_checkpoint_can_recover_missing_overnight_with_remaining_attempts(tmp_path, monkeypatch):
    cfg = sealed(tmp_path)
    schedule._ready_path(cfg, DAY).unlink()
    path = state_file(tmp_path)
    atomic_json(path, {'date': str(DAY), 'stages': {'overnight': {
        'attempts': 1, 'failed': True, 'circuit_open': True, 'failure_kind': 'deadline',
        'revision': schedule._revision(tmp_path), 'commands': {}}}})
    monkeypatch.setattr(schedule, '_now', lambda: datetime(2026, 10, 7, 21, 30, tzinfo=timezone.utc))
    calls = []
    def execute(command, root, **kwargs):
        calls.append((command, kwargs['timeout']))
        if 'run' in command:
            assert kwargs['timeout'] > 7000  # recovery lasts until delivery, not expired 05:28
            schedule.seal_ready_report(cfg, DAY)
        return 0
    monkeypatch.setattr(schedule, '_run_stage_command', execute)
    result = schedule.run_scheduled_stage(tmp_path, 'review', DAY)
    assert result.completed_stages == ['overnight', 'review']
    assert len(calls) == 4
    assert read_json(path)['stages']['overnight']['attempts'] == 2
    assert not schedule._ready_path(cfg, DAY).with_suffix('.outbox.json').exists()
