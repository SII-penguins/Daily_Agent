"""Offline issue enrollment, immutable planning, and publication-lock tests."""
from copy import deepcopy
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from daily_agent import incremental_issue as issue
from daily_agent.batch_execution import ExecutionConflict
from daily_agent.models import MaterialRecord
from daily_agent.workflow_state import StateCorrupt, WorkflowBusy, atomic_json, exclusive_lock

DAY = date(2026, 10, 10)


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setattr(issue, 'source_version', lambda: 'source-A')
    return SimpleNamespace(root=tmp_path, state_dir=tmp_path/'data/state',
        reports_dir=tmp_path/'data/reports', selected_dir=tmp_path/'data/selected', logs_dir=tmp_path/'data/logs',
        sources={'selection': {'max_review_batches': 6, 'editorial_batch_size': 8},
                 'llm_writer': {'provider': 'parent_queue'}}, domains=[], quota={'paper_target': 8, 'github_target': 2},
        delivery={'cloud': {'profile': 'cloud-v1'}})


def material(key='paper:A'):
    return MaterialRecord(key=key, item_type='paper', source='arxiv', title=key, url='https://example.test/'+key)


def test_only_explicit_new_enrollment_creates_plan(config):
    assert issue.load_plan(config, DAY) is None
    assert issue.can_enroll(config, DAY)
    plan = issue.enroll(config, DAY)
    assert plan.state['batches'] == []
    with pytest.raises(ExecutionConflict):
        issue.enroll(config, DAY)


@pytest.mark.parametrize('kind', ['cloud', 'pilot', 'provider'])
def test_legacy_and_non_parent_profiles_never_enroll(config, kind):
    if kind == 'cloud': config.delivery['cloud'] = {}
    elif kind == 'pilot': config.delivery['cloud']['pilot'] = True
    else: config.sources['llm_writer']['provider'] = 'codex'
    assert not issue.can_enroll(config, DAY)


@pytest.mark.parametrize('artifact', ['checkpoint', 'editorial', 'report', 'selected', 'log'])
def test_existing_issue_activity_is_never_enrolled(config, artifact):
    paths = {'checkpoint': config.root/'data/cloud-checkpoints'/str(DAY)/'old-code.json',
             'editorial': config.root/'data/editorial'/str(DAY)/'shortlist.json',
             'report': config.reports_dir/f'daily-agent-{DAY}.md',
             'selected': config.selected_dir/f'selected-{DAY}.json',
             'log': config.logs_dir/f'run-{DAY}.log'}
    path=paths[artifact]; path.parent.mkdir(parents=True); path.write_text('{}')
    assert not issue.can_enroll(config, DAY)
    assert issue.load_plan(config, DAY) is None


@pytest.mark.parametrize('state', ['prepared', 'sending', 'accepted', 'uncertain', 'confirmed'])
def test_delivery_owned_issues_are_read_only(config, state):
    path=config.state_dir/'cloud-delivery'/f'{DAY}.json'
    atomic_json(path, {'state': state, 'publication_reconciled': state == 'confirmed'})
    before=path.read_bytes()
    assert not issue.can_enroll(config, DAY)
    with pytest.raises(ExecutionConflict): issue.assert_generation_allowed(config, DAY)
    assert path.read_bytes()==before
    assert not issue.issue_dir(config, DAY).exists()


@pytest.mark.parametrize('suffix', ['.json', '.outbox.json', '.delivered.json'])
def test_seals_and_local_receipts_block_without_rewrite(config, suffix):
    path=(config.state_dir/'ready-reports'/str(DAY)).with_suffix(suffix)
    atomic_json(path, {'state': 'sealed'})
    before=path.read_bytes()
    with pytest.raises(ExecutionConflict): issue.assert_generation_allowed(config, DAY)
    assert path.read_bytes()==before


def test_corrupt_publication_record_is_not_missing(config):
    path=config.state_dir/'cloud-delivery'/f'{DAY}.json'
    path.parent.mkdir(parents=True);path.write_text('{')
    with pytest.raises(StateCorrupt): issue.can_enroll(config, DAY)
    assert path.read_text()=='{'


def test_source_version_change_rejects_same_stable_path(config, monkeypatch):
    plan=issue.enroll(config, DAY); before=plan.path.read_bytes()
    monkeypatch.setattr(issue, 'source_version', lambda: 'source-B')
    with pytest.raises(ExecutionConflict): issue.load_plan(config, DAY)
    assert plan.path.read_bytes()==before
    assert list(plan.folder.parent.iterdir())==[plan.folder]


@pytest.mark.parametrize('change', ['quota', 'source', 'selection'])
def test_settings_change_rejects_original_plan(config, change):
    plan=issue.enroll(config, DAY); before=plan.path.read_bytes()
    if change=='quota': config.quota['paper_target']=1
    elif change=='source': config.sources['reading']={'run_budget_seconds': 99999}
    else: config.sources['selection']['max_review_batches']=5
    with pytest.raises(ExecutionConflict): issue.load_plan(config, DAY)
    assert plan.path.read_bytes()==before


@pytest.mark.parametrize('filename', ['initialized.json', 'plan.json'])
def test_missing_planner_component_never_restores_budget(config, filename):
    plan=issue.enroll(config, DAY); (plan.folder/filename).unlink()
    with pytest.raises(StateCorrupt): issue.load_plan(config, DAY)
    assert not (plan.folder/filename).exists()


def test_original_batch_order_and_enrichment_are_frozen(config):
    plan=issue.enroll(config, DAY)
    a,b=material(),material('paper:B')
    plan.freeze_batch(0,[a,b]); plan.freeze_enriched(0,[a,b])
    restored=issue.load_plan(config, DAY)
    assert restored.batch(0)['id']==f'{DAY}:batch:0'
    assert [r['key'] for r in restored.batch(0)['enriched']]==[a.key,b.key]
    a.title='changed source'
    with pytest.raises(ExecutionConflict): restored.freeze_enriched(0,[a,b])
    with pytest.raises(ExecutionConflict): restored.freeze_batch(1,[b])
    with pytest.raises(ExecutionConflict): restored.batch(2)


def test_six_times_eight_candidate_ceiling(config):
    plan=issue.enroll(config, DAY)
    for batch in range(6):
        plan.freeze_batch(batch,[material(f'repo:{batch}:{n}') for n in range(8)])
    assert sum(len(b['selected']) for b in plan.state['batches'])==48
    with pytest.raises(ExecutionConflict):plan.batch(6)
    assert len(issue.load_plan(config, DAY).state['batches'])==6


@pytest.mark.parametrize('field,value', [('max_review_batches',7),('editorial_batch_size',9),('editorial_batch_size',0)])
def test_invalid_capacity_cannot_enroll(config,field,value):
    config.sources['selection'][field]=value
    with pytest.raises(ExecutionConflict):issue.enroll(config,DAY)
    assert not issue.issue_dir(config,DAY).exists()


def test_discovery_is_immutable_and_defensively_copied(config):
    plan=issue.enroll(config,DAY)
    snapshot={'raw_items':[{'key':'A'}]}
    plan.freeze_discovery(snapshot); snapshot['raw_items'].clear()
    assert plan.discovery()['raw_items']==[{'key':'A'}]
    returned=plan.discovery();returned.clear()
    assert plan.discovery()
    with pytest.raises(ExecutionConflict):plan.freeze_discovery({})



def active_attempt(config,monkeypatch):
    import os
    from datetime import datetime,timezone
    from daily_agent import cloud_workflow, workflow_runtime
    monkeypatch.setattr(workflow_runtime, 'process_namespace', lambda: 'fixture-namespace')
    now=datetime(2026,10,10,0,0,tzinfo=timezone.utc)
    monkeypatch.setattr(cloud_workflow,'_clock',lambda:now)
    budget={'schema_version':1,'date':str(DAY),'issue_started_at':now.isoformat(),
            'deadline':'2026-10-10T09:00:00+00:00','runtime_seconds':0.,'failures':0,'resumes':1,
            'max_runtime_seconds':25800.,'max_failures':3,'max_resumes':200,
            'active_started_at':now.isoformat(),'active_attempt_id':'fixture-active',
            'active_namespace':'fixture-namespace','active_timeout_seconds':900}
    state={'date':str(DAY),'running':True,'attempt_id':'fixture-active','namespace':'fixture-namespace',
           'child_identity':{'pid':os.getpid(),'pgid':os.getpgid(0),'description':'offline-test'}}
    atomic_json(config.state_dir/'cloud-generation-budgets'/f'{DAY}.json',budget)
    atomic_json(config.state_dir/'cloud-generation.json',state)
    return budget,state

def test_generate_holds_publication_locks_through_pipeline(config,monkeypatch):
    active_attempt(config,monkeypatch)
    from daily_agent import cloud_workflow,pipeline
    monkeypatch.setattr(cloud_workflow,'_config',lambda root:config)
    calls=[]
    def fake_pipeline(*args,**kwargs):
        for path in [config.state_dir/'cloud-delivery'/f'{DAY}.lock',
                     config.state_dir/'ready-reports'/f'{DAY}.lock', config.state_dir/'pipeline.lock']:
            with pytest.raises(WorkflowBusy):
                with exclusive_lock(path): pass
        assert kwargs['_ready_lock_held'] is True
        assert kwargs['_issue_plan'] is None
        calls.append(1)
        return 'offline-result'
    monkeypatch.setattr(pipeline,'_run_pipeline_unlocked',fake_pipeline)
    assert cloud_workflow.generate(config.root,DAY,expected_attempt='fixture-active',expected_namespace='fixture-namespace')=='offline-result'
    assert calls==[1]


def test_generate_existing_confirmed_issue_never_calls_pipeline(config,monkeypatch):
    from daily_agent import cloud_workflow,pipeline
    monkeypatch.setattr(cloud_workflow,'_config',lambda root:config)
    monkeypatch.setattr(pipeline,'_run_pipeline_unlocked',lambda *a,**k:pytest.fail('must not generate'))
    path=config.state_dir/'cloud-delivery'/f'{DAY}.json'
    atomic_json(path,{'state':'confirmed','publication_reconciled':True})
    before=path.read_bytes()
    with pytest.raises(ExecutionConflict):cloud_workflow.generate(config.root,DAY,expected_attempt='fixture-active',expected_namespace='fixture-namespace')
    assert path.read_bytes()==before


def test_missing_entire_planner_cannot_fall_back_to_legacy(config):
    from shutil import rmtree
    plan=issue.enroll(config,DAY)
    budget_path=config.state_dir/'cloud-generation-budgets'/f'{DAY}.json'
    atomic_json(budget_path,{'execution_protocol':issue.PROTOCOL})
    rmtree(plan.folder)
    with pytest.raises(StateCorrupt):issue.load_plan(config,DAY)
    assert not plan.folder.exists()


def test_unknown_budget_protocol_is_not_legacy(config):
    atomic_json(config.state_dir/'cloud-generation-budgets'/f'{DAY}.json',{'execution_protocol':'unknown'})
    with pytest.raises(ExecutionConflict):issue.load_plan(config,DAY)


def test_runtime_plan_requires_authoritative_enrollment(config):
    issue.enroll(config,DAY)
    with pytest.raises(StateCorrupt):issue.load_plan(config,DAY,require_budget=True)


def runtime(config,monkeypatch):
    from datetime import datetime,timezone
    from daily_agent import cloud_workflow,workflow_runtime
    config.delivery.update(schedule={'delivery_time':'09:00','recovery':{}},report={'timezone':'UTC'})
    monkeypatch.setattr(cloud_workflow,'_config',lambda root:config)
    monkeypatch.setattr(cloud_workflow,'_clock',lambda:datetime(2026,10,10,0,0,tzinfo=timezone.utc))
    calls=[]
    def launch(*args,**kwargs):
        calls.append(args)
        kwargs['on_start']({'pid':999999,'pgid':999999,'start_time':'fixture'})
        return 75
    monkeypatch.setattr(workflow_runtime,'run_process',launch)
    return cloud_workflow,calls


def test_supervisor_enrolls_once_and_preserves_budget_namespace(config,monkeypatch):
    from daily_agent.workflow_state import read_json
    workflow,calls=runtime(config,monkeypatch)
    assert workflow.run_generation(config.root,DAY,900)==75
    budget_path=config.state_dir/'cloud-generation-budgets'/f'{DAY}.json'
    first=read_json(budget_path)
    assert first['execution_protocol']==issue.PROTOCOL
    plan=issue.load_plan(config,DAY,require_budget=True)
    before=plan.path.read_bytes()
    assert workflow.run_generation(config.root,DAY,900)==75
    assert read_json(budget_path)['resumes']==2
    assert plan.path.read_bytes()==before and len(calls)==2


def test_supervisor_unknown_old_budget_does_not_enroll(config,monkeypatch):
    from daily_agent.workflow_state import read_json
    workflow,calls=runtime(config,monkeypatch)
    # Mark preexisting date-scoped activity before the first supervised launch.
    old=config.root/'data/cloud-checkpoints'/str(DAY)/'source-old-hash.json'
    atomic_json(old,{'legacy':True})
    with pytest.raises(StateCorrupt):workflow.run_generation(config.root,DAY,900)
    assert not calls
    assert not (config.state_dir/'cloud-generation-budgets'/f'{DAY}.json').exists()
    assert issue.load_plan(config,DAY) is None


def test_supervisor_confirmed_issue_does_not_spend_or_launch(config,monkeypatch):
    workflow,calls=runtime(config,monkeypatch)
    manifest=config.state_dir/'cloud-delivery'/f'{DAY}.json'
    atomic_json(manifest,{'state':'confirmed','publication_reconciled':True})
    before=manifest.read_bytes()
    with pytest.raises(ExecutionConflict):workflow.run_generation(config.root,DAY,900)
    assert calls==[] and manifest.read_bytes()==before
    assert not (config.state_dir/'cloud-generation-budgets'/f'{DAY}.json').exists()


def test_supervisor_missing_enrolled_budget_does_not_reinitialize(config,monkeypatch):
    workflow,calls=runtime(config,monkeypatch)
    plan=issue.enroll(config,DAY);before=plan.path.read_bytes()
    with pytest.raises(StateCorrupt):workflow.run_generation(config.root,DAY,900)
    assert not calls and plan.path.read_bytes()==before


def test_old_authoritative_budget_continues_legacy_without_enrollment(config,monkeypatch):
    from daily_agent.workflow_state import read_json
    workflow,calls=runtime(config,monkeypatch)
    config.sources['llm_writer']['provider']='codex'
    assert workflow.run_generation(config.root,DAY,900)==75
    config.sources['llm_writer']['provider']='parent_queue'
    assert workflow.run_generation(config.root,DAY,900)==75
    budget=read_json(config.state_dir/'cloud-generation-budgets'/f'{DAY}.json')
    assert budget['resumes']==2 and 'execution_protocol' not in budget
    assert issue.load_plan(config,DAY) is None


@pytest.mark.parametrize('winner',['manual','cloud'])
def test_real_concurrent_manual_and_cloud_entries_reject_before_any_inner_write(config,monkeypatch,winner):
    active_attempt(config,monkeypatch)
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from daily_agent import pipeline,cloud_workflow
    monkeypatch.setattr(pipeline,'load_config',lambda root:config)
    monkeypatch.setattr(cloud_workflow,'_config',lambda root:config)
    entered=threading.Event();release=threading.Event();writes=[]
    def inner(*args,**kwargs):
        assert kwargs['_ready_lock_held'] is True
        writes.append('inner')
        entered.set()
        assert release.wait(5)
        return 'finished'
    monkeypatch.setattr(pipeline,'_run_pipeline_unlocked',inner)
    def manual():return pipeline.run_pipeline(config.root,DAY,dry_run=False,delivery_mode='local',
        expected_attempt='fixture-active',expected_namespace='fixture-namespace')
    def cloud():return cloud_workflow.generate(config.root,DAY,expected_attempt='fixture-active',expected_namespace='fixture-namespace')
    first,second=(manual,cloud) if winner=='manual' else (cloud,manual)
    with ThreadPoolExecutor(max_workers=1) as workers:
        future=workers.submit(first)
        try:
            assert entered.wait(5)
            with pytest.raises(WorkflowBusy):second()
            assert writes==['inner']
        finally:release.set()
        assert future.result(timeout=5)=='finished'
    assert second()=='finished'
    assert writes==['inner','inner']


def test_manual_cloud_run_respects_confirmed_seal_before_inner_work(config,monkeypatch):
    from daily_agent import pipeline
    monkeypatch.setattr(pipeline,'load_config',lambda root:config)
    monkeypatch.setattr(pipeline,'_run_pipeline_unlocked',lambda *a,**k:pytest.fail('no work on confirmed issue'))
    path=config.state_dir/'cloud-delivery'/f'{DAY}.json'
    atomic_json(path,{'state':'confirmed','publication_reconciled':True})
    before=path.read_bytes()
    with pytest.raises(ExecutionConflict):pipeline.run_pipeline(config.root,DAY,dry_run=False,
        expected_attempt='fixture-active',expected_namespace='fixture-namespace')
    assert path.read_bytes()==before


@pytest.mark.parametrize('entry',['manual','cloud'])
@pytest.mark.parametrize('failure',['missing','idle','wrong_attempt','other_process','exhausted'])
def test_public_cloud_entry_cannot_bypass_supervisor_budget(config,monkeypatch,entry,failure):
    from daily_agent import pipeline,cloud_workflow
    monkeypatch.setattr(pipeline,'load_config',lambda root:config)
    monkeypatch.setattr(cloud_workflow,'_config',lambda root:config)
    monkeypatch.setattr(pipeline,'_run_pipeline_unlocked',lambda *a,**k:pytest.fail('unauthorized work'))
    if failure!='missing':
        budget,state=active_attempt(config,monkeypatch)
        if failure=='idle':budget.pop('active_started_at');budget.pop('active_attempt_id')
        elif failure=='wrong_attempt':state['attempt_id']='different'
        elif failure=='other_process':state['child_identity']['pid']+=1
        else:budget['runtime_seconds']=budget['max_runtime_seconds']
        atomic_json(config.state_dir/'cloud-generation-budgets'/f'{DAY}.json',budget)
        atomic_json(config.state_dir/'cloud-generation.json',state)
    with pytest.raises((ExecutionConflict,StateCorrupt)):
        if entry=='manual':pipeline.run_pipeline(config.root,DAY,dry_run=False,
        expected_attempt='fixture-active',expected_namespace='fixture-namespace')
        else:cloud_workflow.generate(config.root,DAY,expected_attempt='fixture-active',expected_namespace='fixture-namespace')
