import hashlib
import json
from pathlib import Path
import shutil
import zipfile

import pytest

from daily_agent import durable_recovery as recovery
from daily_agent.cloud_workflow import init_profile, prepare_handoff, record_transition, read_handoff
from daily_agent.parent_writer import claim, import_response, request, retry_expired
from daily_agent.state_archive import snapshot, restore
from daily_agent.workflow_state import StateCorrupt, atomic_json
from datetime import date


def review_for(status, registry):
    outcomes={'verify_previous_executor_stopped':'stopped','reconcile_claim_owners':'resolved',
              'inspect_unresolved_delivery':'preserved_blocked','verify_site_archive_owner':'diagnostic_no_site',
              'revalidate_absolute_paths':'relocated_revalidated'}
    return {'receipts':registry,'reviewer':'operator',
            'evidence':{key:{'outcome':outcome,'reference':'diagnostic fixture observation'} for key,outcome in outcomes.items()},
            'claims':[{**{k:claim[k] for k in ('job_id','worker_id','generation')},
                       'outcome':'owner_stopped','reference':'diagnostic old executor stopped'} for claim in status['claims']]}


@pytest.fixture
def prepared(tmp_path):
    source = Path(__file__).resolve().parents[1]
    state = tmp_path/'original'
    init_profile(source, state, 'codex')
    atomic_json(state/'data/state/budget.json', {'used':77, 'remaining':12, 'attempts':3, 'deadline':'2026-10-09T09:40:00Z'})
    atomic_json(state/'data/writer-queue/example.claim.json', {'job_id':'example', 'worker_id':'original-worker', 'token':'fixture-token', 'generation':2, 'expires_at':'2026-10-09T08:00:00Z'})
    atomic_json(state/'data/writer-queue/example.answer.json', {'response':'preserved', 'worker_id':'original-worker'})
    (state/'data/assets').mkdir(parents=True)
    (state/'data/assets/paper.png').write_bytes(b'exact-original-asset')
    atomic_json(state/'data/state/cache.json', {'path':str(state/'data/assets/paper.png')})
    prepare_handoff(state, date(2026,10,9), 'verified', failure='diagnostic only')
    attempt = record_transition(state, date(2026,10,9), 'begin')
    record_transition(state, date(2026,10,9), 'accepted', attempt_id=attempt['attempt_id'], message_id='diagnostic-message')
    saved = snapshot(state, tmp_path/'state.zip')
    code = recovery.source_snapshot(source, tmp_path/'source.zip')
    def receipt(saved, kind):
        return {'library_file_id':'libfile_fixture_'+kind, 'file_id':'fixture_'+kind,
                'version':0, 'sha256':saved['sha256'], 'size':saved['size']}
    registry = {'source':receipt(code,'source'), 'state':receipt(saved,'state')}
    return state, Path(code['archive']), Path(saved['archive']), registry


def test_empty_workdir_restore_preserves_every_byte_and_fences(prepared, tmp_path):
    state, code, archive, registry = prepared
    baseline = {str(p.relative_to(state)):p.read_bytes() for p in state.rglob('*') if p.is_file() and p.suffix!='.lock'}
    shutil.rmtree(state)  # Isolated fault injection, never the production backup.
    result = recovery.bootstrap(code, archive, tmp_path/'empty/restored', registry, registry)
    restored = Path(result['state'])
    assert result['mutation_blocked']
    assert all((restored/name).read_bytes() == value for name,value in baseline.items())
    assert read_handoff(restored,date(2026,10,9))['state']=='accepted'
    with pytest.raises(StateCorrupt, match='fenced'):
        record_transition(restored,date(2026,10,9),'begin')
    with pytest.raises(StateCorrupt, match='fenced'):
        atomic_json(restored/'data/state/budget.json', {'remaining':9999})
    observed = recovery.inspect_recovery(restored)
    assert observed['auto_resume'] is False
    assert observed['claims'][0]['worker_id']=='original-worker'
    assert observed['unresolved_delivery'][0]['message_id']=='diagnostic-message'
    assert observed['path_revalidation'][0]['original_root']==str(state)


@pytest.mark.parametrize('function,args', [(claim,('0'*64,'new-worker')), (import_response,('0'*64,{},'new-worker')),
                                          (request,('hello',10)), (retry_expired,('0'*64,date(2026,10,9),'retry'))])
def test_queue_entrypoints_fail_before_admission(prepared,tmp_path,function,args):
    state,code,archive,registry=prepared
    result=recovery.bootstrap(code,archive,tmp_path/'recovered',registry,registry)
    with pytest.raises(StateCorrupt,match='fenced'):
        function(Path(result['state']),*args)


def test_generation_entrypoints_fail_before_work(prepared,tmp_path):
    from daily_agent.cloud_workflow import run_generation, generate
    from daily_agent.production_revision import run_generation as revision_run, reserve_generation, generate as revision_generate
    _,code,archive,registry=prepared
    result=recovery.bootstrap(code,archive,tmp_path/'recovered',registry,registry)
    root=Path(result['state'])
    for call in [lambda:run_generation(root,date(2026,10,9),900), lambda:generate(root,date(2026,10,9)),
                 lambda:revision_run(root,date(2026,10,9),'old'), lambda:reserve_generation(root,date(2026,10,9),'old'),
                 lambda:revision_generate(root,date(2026,10,9),'old')]:
        with pytest.raises(StateCorrupt,match='fenced'): call()


def test_guard_resolves_alias_symlink(prepared,tmp_path):
    _,code,archive,registry=prepared
    result=recovery.bootstrap(code,archive,tmp_path/'recovered',registry,registry)
    alias=tmp_path/'alias';alias.symlink_to(result['state'])
    with pytest.raises(StateCorrupt,match='fenced'):atomic_json(alias/'new.json',{})


@pytest.mark.parametrize('field,value',[('version',True),('version',-1),('sha256','0'*64),('file_id','changed')])
def test_restore_rejects_stale_head_or_invalid_receipt(prepared,tmp_path,field,value):
    _,code,archive,registry=prepared
    current=json.loads(json.dumps(registry));current['state'][field]=value
    with pytest.raises(recovery.RecoveryBlocked): recovery.bootstrap(code,archive,tmp_path/'no',registry,current)
    assert not (tmp_path/'no').exists()


def test_restore_refuses_existing_destination(prepared,tmp_path):
    _,code,archive,registry=prepared
    target=tmp_path/'occupied';target.mkdir();(target/'keep').write_text('keep')
    with pytest.raises(recovery.RecoveryBlocked):recovery.bootstrap(code,archive,target,registry,registry)
    assert (target/'keep').read_text()=='keep'


def test_corrupt_bytes_or_missing_asset_never_install(prepared,tmp_path):
    _,code,archive,registry=prepared
    archive.write_bytes(archive.read_bytes()[:-5])
    with pytest.raises(recovery.RecoveryBlocked):recovery.bootstrap(code,archive,tmp_path/'no',registry,registry)
    assert not (tmp_path/'no').exists()


def test_source_manifest_rejects_missing_asset_even_with_new_outer_hash(prepared,tmp_path):
    _,code,archive,registry=prepared
    bad=tmp_path/'bad.zip'
    with zipfile.ZipFile(archive) as old, zipfile.ZipFile(bad,'w') as new:
        for name in old.namelist():
            if name!='data/assets/paper.png':new.writestr(name,old.read(name))
    registry['state'].update(sha256=recovery.sha(bad),size=bad.stat().st_size)
    with pytest.raises(recovery.RecoveryBlocked):recovery.bootstrap(code,bad,tmp_path/'no',registry,registry)
    assert not (tmp_path/'no').exists()


def test_complete_review_unfences_without_clearing_receipts_or_budgets(prepared,tmp_path):
    _,code,archive,registry=prepared
    result=recovery.bootstrap(code,archive,tmp_path/'recovered',registry,registry)
    root=Path(result['state']);status=recovery.inspect_recovery(root)
    with pytest.raises(recovery.RecoveryBlocked):recovery.release_recovery(root,{'receipts':registry,'reviewer':'operator'})
    budget=(root/'data/state/budget.json').read_bytes()
    review=review_for(status,registry)
    outcome=recovery.release_recovery(root,review)
    assert outcome['authorization_granted'] is False
    assert (root/'data/state/budget.json').read_bytes()==budget
    with pytest.raises(ValueError,match='Resend blocked'):record_transition(root,date(2026,10,9),'begin')


def test_active_revision_cannot_be_unfenced(prepared,tmp_path):
    _,code,archive,registry=prepared
    result=recovery.bootstrap(code,archive,tmp_path/'recovered',registry,registry)
    root=Path(result['state']); path=root/'data/state/active.json'
    path.write_text(json.dumps({'active_attempt_id':'saved-owner'})) # fault injection
    status=recovery.inspect_recovery(root)
    review=review_for(status,registry)
    with pytest.raises(recovery.RecoveryBlocked,match='Retained active'):recovery.release_recovery(root,review)
    assert (root/recovery.FENCE).exists()


def test_snapshot_refuses_active_revision_and_manifest_is_not_rearchived(prepared,tmp_path):
    root,_,_,_=prepared
    (root/'STATE-MANIFEST.json').write_text('{}')
    saved=snapshot(root,tmp_path/'again.zip')
    with zipfile.ZipFile(saved['archive']) as z:
        assert z.namelist().count('STATE-MANIFEST.json')==1
    p=root/'data/revisions/old';p.mkdir(parents=True)
    (p/'budget.json').write_text(json.dumps({'active_attempt_id':'owner'}))
    with pytest.raises(StateCorrupt,match='Active revision'):snapshot(root,tmp_path/'no.zip')


def test_standalone_bootstrap_needs_only_stdlib(prepared,tmp_path):
    import subprocess, sys
    _,code,archive,registry=prepared
    data=tmp_path/'metadata.json';data.write_text(json.dumps(registry))
    result=subprocess.run([sys.executable,'-I',str(Path(recovery.__file__)),'bootstrap',
        '--source-archive',str(code),'--state-archive',str(archive),'--registry',str(data),
        '--current',str(data),'--destination',str(tmp_path/'stdlib-restored')],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)['mutation_blocked']


@pytest.mark.parametrize('outcome',['unknown','live','expired'])
def test_unknown_live_or_merely_expired_worker_is_not_released(prepared,tmp_path,outcome):
    _,code,archive,registry=prepared
    root=Path(recovery.bootstrap(code,archive,tmp_path/'recovered',registry,registry)['state'])
    review=review_for(recovery.inspect_recovery(root),registry)
    review['claims'][0]['outcome']=outcome
    with pytest.raises(recovery.RecoveryBlocked,match='Unknown or live'):recovery.release_recovery(root,review)
    assert (root/recovery.FENCE).exists()


def test_nested_active_schedule_blocked(prepared,tmp_path):
    _,code,archive,registry=prepared
    root=Path(recovery.bootstrap(code,archive,tmp_path/'recovered',registry,registry)['state'])
    (root/'data/state/schedule.json').write_text(json.dumps({'stages':{'review':{'status':'cleanup_pending'}}}))
    status=recovery.inspect_recovery(root)
    with pytest.raises(recovery.RecoveryBlocked,match='Retained active'):recovery.release_recovery(root,review_for(status,registry))


def test_cloud_layout_preserves_original_absolute_paths(prepared,tmp_path):
    _,code,archive,registry=prepared
    result=recovery.bootstrap(code,archive,tmp_path/'runtime',registry,registry,state_directory='data/cloud')
    assert result['state']==str(tmp_path/'runtime/data/cloud')
    assert (Path(result['state'])/recovery.FENCE).is_file()


def test_guard_blocks_outbound_symlink_replace(prepared,tmp_path):
    _,code,archive,registry=prepared
    root=Path(recovery.bootstrap(code,archive,tmp_path/'recovered',registry,registry)['state'])
    external=tmp_path/'external.json';external.write_text('{}')
    alias=root/'outbound.json';alias.symlink_to(external)
    with pytest.raises(StateCorrupt,match='fenced'):atomic_json(alias,{'bypass':True})
    assert alias.is_symlink()


def test_destructive_and_retention_entrypoints_block_before_effects(prepared,tmp_path):
    from daily_agent.scheduling import repair_schedule_state,run_scheduled_stage
    from daily_agent.production_revision import recover_generation
    from daily_agent.storage import cleanup_retention,write_daily_report
    from daily_agent.cloud_workflow import _config
    _,code,archive,registry=prepared
    root=Path(recovery.bootstrap(code,archive,tmp_path/'recovered',registry,registry)['state'])
    config=_config(root)
    for call in [lambda:repair_schedule_state(root,date(2026,10,9)),lambda:run_scheduled_stage(root,'review',date(2026,10,9)),
                 lambda:recover_generation(root,date(2026,10,9),'old'),lambda:cleanup_retention(config),
                 lambda:write_daily_report(config,date(2026,10,9),'must not write')]:
        with pytest.raises(StateCorrupt,match='fenced'):call()


def test_legacy_outbox_uncertainty_is_visible(prepared,tmp_path):
    _,code,archive,registry=prepared
    root=Path(recovery.bootstrap(code,archive,tmp_path/'recovered',registry,registry)['state'])
    (root/'data/state/legacy.outbox.json').write_text(json.dumps({'status':'uncertain','attempt_id':'original'}))
    assert any(row['status']=='uncertain' for row in recovery.inspect_recovery(root)['unresolved_delivery'])


def test_embedded_status_and_migration_history_are_not_live_owners():
    assert not recovery.active_state_record(Path('budget.json'), {'migration_accounting':{'active_attempt_id':'old'}})
    assert not recovery.active_state_record(Path('source-cache.json'), {'payload':{'status':{'sources':[]}}})
    assert recovery.active_state_record(Path('schedule.json'), {'stages':{'review':{'status':'cleanup_pending'}}})
    assert recovery.active_state_record(Path('batch-execution/key/journal.json'), {'payload':{'reservations':{'kept':{}}}})


def test_pre_fence_source_is_rejected_even_with_valid_outer_receipt(prepared,tmp_path):
    _,code,archive,registry=prepared
    bad=tmp_path/'old-source.zip'
    with zipfile.ZipFile(code) as old,zipfile.ZipFile(bad,'w') as new:
        manifest=json.loads(old.read('SOURCE-MANIFEST.json'))
        manifest['files'].pop('src/daily_agent/durable_recovery.py')
        for name in old.namelist():
            if name not in {'SOURCE-MANIFEST.json','src/daily_agent/durable_recovery.py'}:new.writestr(name,old.read(name))
        new.writestr('SOURCE-MANIFEST.json',json.dumps(manifest))
    registry['source'].update(sha256=recovery.sha(bad),size=bad.stat().st_size)
    with pytest.raises(recovery.RecoveryBlocked,match='predates fenced recovery'):
        recovery.bootstrap(bad,archive,tmp_path/'no',registry,registry)


def test_missing_cached_asset_repair_cannot_mutate_fenced_root(prepared,tmp_path):
    from daily_agent.cloud_workflow import _config
    from daily_agent.deferred_review_cache import _repair_assets,_root
    _,code,archive,registry=prepared
    root=Path(recovery.bootstrap(code,archive,tmp_path/'recovered',registry,registry)['state'])
    config=_config(root);content=b'original';sha=hashlib.sha256(content).hexdigest()
    blob=_root(config)/'blobs'/sha;blob.parent.mkdir(parents=True,exist_ok=True);blob.write_bytes(content)
    target=root/'data/assets/missing.png'
    with pytest.raises(StateCorrupt,match='fenced'):_repair_assets(config,{str(target):sha})
    assert not target.exists()
