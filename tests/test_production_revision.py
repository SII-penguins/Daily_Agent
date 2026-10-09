"""Offline-only revision fixtures; no real authorization or publication proof."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import subprocess
import pytest
from test_cloud_html_handoff import report, DAY
from test_cloud_site_delivery import site
from daily_agent import production_revision as revision
from daily_agent.cloud_site_delivery import bind_site
from daily_agent.cloud_workflow import record_transition, read_handoff, _config, reserved_delivery_identities
from daily_agent.report_archive import export_archive
from daily_agent.workflow_state import StateCorrupt, read_json


@pytest.fixture
def parent(site):
    root=site['root']; bind_site(day=DAY,**site)
    m=record_transition(root,DAY,'begin')
    m=record_transition(root,DAY,'accepted',attempt_id=m['attempt_id'],message_id='parent_message')
    m=record_transition(root,DAY,'confirmed',attempt_id=m['attempt_id'],message_id=m['message_id'],conversation=m['conversation'],body=m['body'])
    return root,m


def args(parent):
    root,m=parent
    return dict(parent_identity=m['identity'], authorization={'source':'offline-fixture:user-request','text':'Republish today with more reviewed papers','authorized_at':datetime.now(timezone.utc).isoformat()},
                policy={'deadline':(datetime.now(timezone.utc)+timedelta(hours=6)).isoformat(),
                        'max_runtime_seconds':21600,'max_failures':3,'max_resumes':100,'per_launch_seconds':900})


@pytest.fixture
def authorized(parent):
    a=args(parent)
    c=revision.authorize_revision(parent[0],DAY,**a)
    return parent[0],c,a


def prep(authorized):
    root,c,_=authorized; rid=c['revision_id']
    revision.seal_ready(root,DAY,rid,revision.carry_forward_rows(root,DAY,rid))
    return revision.prepare_handoff(root,DAY,rid)


def bind_revision(authorized,tmp_path):
    root,c,_=authorized; rid=c['revision_id']; m=prep(authorized)
    output=tmp_path/'revision-export'
    export_archive([root],output,include_current={'root':str(root),'date':str(DAY),'identity':m['identity'],'revision_id':rid})
    repo=tmp_path/'revision-site';(repo/'.openai').mkdir(parents=True)
    shutil.copytree(output,repo/'dist',ignore=shutil.ignore_patterns('.export.lock'))
    (repo/'.openai/hosting.json').write_text(json.dumps({'static':{'directory':'dist'},'project_id':'appgprj_fixture'}))
    def git(*a):return subprocess.run(['git','-C',str(repo),*a],capture_output=True,check=True).stdout.decode().strip()
    git('init');git('add','.');git('-c','user.name=Test','-c','user.email=test@example.org','commit','-m','Offline revision fixture')
    deployment={'isError':False,'structuredContent':{'id':'appgdep_revision','project_id':'appgprj_fixture',
        'version_id':'appgprj_fixture~appgver_revision','type':'publish','status':'succeeded',
        'failure_message':None,'url':'https://fixture.chatgpt.site','updated_at':'2026-10-08T10:00:00Z'}}
    version={'isError':False,'structuredContent':{'id':'appgprj_fixture~appgver_revision','project_id':'appgprj_fixture',
        'deployment_id':'appgdep_revision','source':{'commit_sha':git('rev-parse','HEAD')}}}
    dep=tmp_path/'revision-deployment.json';dep.write_text(json.dumps(deployment))
    ver=tmp_path/'revision-version.json';ver.write_text(json.dumps(version))
    return revision.bind_site(root,DAY,rid,archive_dir=output,deployment_file=dep,site_version_file=ver,site_repo=repo)


def test_authorization_once_preserves_original_budget_and_receipts(authorized):
    root,c,a=authorized
    old=(root/'data/state/cloud-delivery'/f'{DAY}.json').read_bytes()
    bpath=root/'data/state/cloud-generation-budgets'/f'{DAY}.json';bpath.parent.mkdir(exist_ok=True);bpath.write_text('{"old":"budget"}')
    launch=revision.reserve_generation(root,DAY,c['revision_id'],60)
    revision.settle_generation(root,DAY,c['revision_id'],launch['attempt_id'],75,10,expected_namespace=launch['namespace'])
    assert revision.authorize_revision(root,DAY,**a)==c
    assert revision._read_budget(root,DAY,c['revision_id'])['resumes']==1
    assert bpath.read_text()=='{"old":"budget"}'
    assert (root/'data/state/cloud-delivery'/f'{DAY}.json').read_bytes()==old
    changed=deepcopy(a);changed['policy']['max_runtime_seconds']=30000
    with pytest.raises(ValueError,match='already authorized'):revision.authorize_revision(root,DAY,**changed)


def test_missing_budget_never_resets(authorized):
    root,c,a=authorized;folder=revision.issue_dir(root,DAY,c['revision_id']);(folder/'budget.json').unlink()
    with pytest.raises(StateCorrupt):revision.authorize_revision(root,DAY,**a)
    assert not (folder/'budget.json').exists()


def test_mutated_frozen_budget_rejected(authorized):
    root,c,a=authorized;folder=revision.issue_dir(root,DAY,c['revision_id']);p=folder/'budget.json'
    b=json.loads(p.read_text());b['max_runtime_seconds']+=1;p.write_text(json.dumps(b))
    with pytest.raises(StateCorrupt):revision.reserve_generation(root,DAY,c['revision_id'])


def test_carry_is_exact_parent_row(authorized):
    root,c,_=authorized;rid=c['revision_id'];rows=revision.carry_forward_rows(root,DAY,rid)
    rows[0]['final_fields']['what_it_is']='An altered description'
    with pytest.raises(ValueError,match='Carry-forward'):revision.seal_ready(root,DAY,rid,rows)
    m=prep(authorized)
    assert m['parent_identity']==read_handoff(root,DAY)['identity']
    assert m['identity']!=m['parent_identity']
    assert m['approval']==read_handoff(root,DAY)['approval']
    with pytest.raises(ValueError,match='Sealed'):revision.reserve_generation(root,DAY,rid)


def test_same_day_reservation_and_append_only_confirmed_history(authorized,tmp_path):
    root,c,_=authorized;rid=c['revision_id'];legacy_index=root/'data/materials/published_index.json'
    # Existing dates/ranks remain byte-for-byte; revisions get independent events.
    original_files={p:p.read_bytes() for p in (root/'data/state/cloud-delivery').glob('*.json')}
    indexes={p:p.read_bytes() for p in root.rglob('published_index.json')}
    m=bind_revision(authorized,tmp_path)
    assert '修订版 r1' in m['body'] and f'-r1-{m["identity"][:12]}/index.html' in m['body']
    m=revision.record_transition(root,DAY,rid,'begin')
    assert 'github:test/repo' in reserved_delivery_identities(_config(root),DAY)
    m=revision.record_transition(root,DAY,rid,'accepted',attempt_id=m['attempt_id'],message_id='revision_message')
    with pytest.raises(ValueError,match='body'):
        revision.record_transition(root,DAY,rid,'confirmed',attempt_id=m['attempt_id'],message_id=m['message_id'],conversation=m['conversation'],body='wrong')
    m=revision.record_transition(root,DAY,rid,'confirmed',attempt_id=m['attempt_id'],message_id=m['message_id'],conversation=m['conversation'],body=m['body'])
    assert m['publication_reconciled']
    event=root/'data/state/revision-publications'/f'{rid}.json';saved=event.read_bytes()
    revision.reconcile_publication(root,DAY,rid)
    assert event.read_bytes()==saved
    assert all(p.read_bytes()==v for p,v in {**original_files,**indexes}.items())
    assert 'github:test/repo' not in revision.reservation_keys(root)
    with pytest.raises(ValueError,match='resend'):revision.record_transition(root,DAY,rid,'begin')


def test_scoped_config_keeps_original_root_and_history(authorized):
    from daily_agent.storage import editorial_dir, materials_dir
    root,c,_=authorized;rid=c['revision_id'];cfg=revision.scoped_config(root,DAY,rid)
    assert cfg.root==root
    assert materials_dir(cfg)==root/'data/materials'
    assert cfg.reports_dir.is_relative_to(revision.issue_dir(root,DAY,rid))
    assert editorial_dir(cfg,DAY)==revision.issue_dir(root,DAY,rid)/'editorial'
    assert editorial_dir(_config(root),DAY)==root/'data/editorial'/str(DAY)


def test_artifact_and_contract_tampering_rejected(authorized):
    root,c,_=authorized;m=prep(authorized)
    (root/m['html_report']).write_bytes(b'tampered')
    with pytest.raises(StateCorrupt,match='artifact'):revision.read_handoff(root,DAY,c['revision_id'])


def test_budget_active_launch_not_refunded_and_settlement_idempotent(authorized):
    root,c,_=authorized;rid=c['revision_id'];launch=revision.reserve_generation(root,DAY,rid,20)
    assert launch['timeout_seconds']==20
    with pytest.raises(StateCorrupt,match='Unreconciled'):revision.reserve_generation(root,DAY,rid)
    b=revision.settle_generation(root,DAY,rid,launch['attempt_id'],1,15,expected_namespace=launch['namespace'])
    assert b['failures']==1 and b['runtime_seconds']>=15
    assert revision.settle_generation(root,DAY,rid,launch['attempt_id'],1,15,expected_namespace=launch['namespace'])==b


def make_repo(key):
    from daily_agent.models import MaterialRecord, EditorialDraft, EditorialReview
    from daily_agent.editorial import REPO_FIELDS
    r=MaterialRecord(key=key,source='github',item_type='repo',title='Quantum simulation fixture tool',url='https://github.com/'+key.split(':',1)[1],
                     readme_excerpt='Concrete quantum simulation capability from an offline fixture README')
    d=EditorialDraft(r.key,'repo',r.title,{f:'Concrete capability from the fixture README' for f in REPO_FIELDS})
    return r,d,EditorialReview(r.key,'PASS')


def test_worker_scopes_outputs_and_keeps_original_seals(parent,monkeypatch):
    from daily_agent import pipeline
    from daily_agent.storage import load_material_library, write_material_library
    root,m=parent;cfg=_config(root)
    record,draft,review=make_repo('github:fixture/new')
    library=load_material_library(cfg);library[record.key]=record;write_material_library(cfg,library)
    monkeypatch.setattr('daily_agent.editorial.passes_topic_gate',lambda *a:True)
    monkeypatch.setattr('daily_agent.editorial.build_shortlist',lambda config,records,day:list(records.values()))
    monkeypatch.setattr(pipeline,'build_shortlist',lambda config,records,day:list(records.values()))
    c=revision.authorize_revision(root,DAY,**args(parent));rid=c['revision_id']
    assert c['candidates']
    old=(root/'data/state/cloud-delivery'/f'{DAY}.json').read_bytes()
    original=root/'data/editorial'/str(DAY);original.mkdir(parents=True);(original/'approval.json').write_text('original')
    monkeypatch.setattr('daily_agent.cloud_cache.prepare_batch',lambda config,day,batch,enrich:batch)
    def process(config,day,batch,plan,batch_id,library,records,drafts,reviews,**kw):
        from daily_agent.storage import editorial_dir
        assert config.root==root and editorial_dir(config,day)!=original
        assert plan.folder.is_relative_to(revision.issue_dir(root,day,rid))
        return [record],[draft],[review]
    monkeypatch.setattr(pipeline,'_process_incremental_batch',process)
    launch=revision.reserve_generation(root,DAY,rid)
    fixture_supervisor(root,rid,launch)
    value=revision.generate(root,DAY,rid,expected_attempt=launch['attempt_id'],expected_namespace=launch['namespace'])
    revision.settle_generation(root,DAY,rid,launch['attempt_id'],0,1,expected_namespace=launch['namespace'])
    assert {r['key'] for r in value['approval']}=={'github:test/repo','github:fixture/new'}
    assert (original/'approval.json').read_text()=='original'
    assert (root/'data/state/cloud-delivery'/f'{DAY}.json').read_bytes()==old


def test_cutoff_recovers_only_committed_sibling_without_running_models(parent,monkeypatch):
    from daily_agent.batch_execution import Execution,candidate,existing_review_validator
    from daily_agent.incremental_issue import PROTOCOL
    from daily_agent.storage import load_material_library,write_material_library
    root,_=parent;cfg=_config(root)
    one,draft,review=make_repo('github:fixture/completed');two,_,_=make_repo('github:fixture/pending')
    library=load_material_library(cfg);library.update({one.key:one,two.key:two});write_material_library(cfg,library)
    monkeypatch.setattr('daily_agent.editorial.passes_topic_gate',lambda *a:True)
    c=revision.authorize_revision(root,DAY,**args(parent));rid=c['revision_id'];folder=revision.issue_dir(root,DAY,rid)
    config=revision.scoped_config(root,DAY,rid)
    payload=[one.to_dict(),two.to_dict()]
    revision._immutable(folder/'batches/0.enriched.json',{'payload':payload,'sha256':revision.digest(payload)})
    with Execution(folder/'incremental',f'{rid}:batch:0',[candidate(one),candidate(two)],config,protocol=PROTOCOL) as ex:
        sha=ex.prepare_completion(one.key,record=one.to_dict(),draft=draft.to_dict(),review=review.to_dict(),science={},evidence={},asset_hashes={})
        ex.commit_completion(one.key,sha,validator=existing_review_validator(config))
    monkeypatch.setattr('daily_agent.pipeline._process_incremental_batch',lambda *a,**k:pytest.fail('Cutoff must never run a model worker'))
    value=revision.freeze_completed(root,DAY,rid)
    assert {r['key'] for r in value['approval']}=={'github:test/repo',one.key}
    assert two.key not in {r['key'] for r in value['approval']}
    assert revision.freeze_completed(root,DAY,rid)==value


def test_freeze_refuses_active_child_budget(authorized):
    root,c,_=authorized;rid=c['revision_id'];revision.reserve_generation(root,DAY,rid)
    with pytest.raises(ValueError,match='active'):revision.freeze_completed(root,DAY,rid)


def test_changed_config_cannot_create_new_execution_budget(authorized):
    import yaml
    root,c,_=authorized;p=root/'config/sources.yaml';data=yaml.safe_load(p.read_text());data['reading']['run_budget_seconds']+=1;p.write_text(yaml.safe_dump(data))
    with pytest.raises(StateCorrupt,match='settings'):revision.scoped_config(root,DAY,c['revision_id'])


def test_supervisor_resumes_charge_one_frozen_revision(authorized,monkeypatch):
    root,c,_=authorized;rid=c['revision_id']
    def fake_run(argv,cwd,timeout,**kw):
        assert argv[2]=='daily_agent.production_revision' and str(root)==str(cwd)
        assert '--revision-id' in argv and rid in argv and timeout==20
        kw['on_start']({'pid':999999,'pgid':999999,'description':'offline fixture, no process'})
        return 75
    monkeypatch.setattr('daily_agent.workflow_runtime.run_process',fake_run)
    assert revision.run_generation(root,DAY,rid,20)==75
    assert revision.run_generation(root,DAY,rid,20)==75
    b=revision._read_budget(root,DAY,rid)
    assert b['resumes']==2 and b['failures']==0 and 'active_attempt_id' not in b


def fixture_supervisor(root,rid,launch):
    import os
    from daily_agent.workflow_state import atomic_json
    atomic_json(revision.issue_dir(root,DAY,rid)/'generation.json',
                {'revision_id':rid,'attempt_id':launch['attempt_id'],'namespace':launch['namespace'],'running':True,
                 'child_identity':{'pid':os.getpid(),'pgid':os.getpgid(0),'description':'offline test supervisor fixture'}})


def test_unsupervised_worker_cannot_spend_reserved_budget(authorized):
    root,c,_=authorized;rid=c['revision_id']
    launch=revision.reserve_generation(root,DAY,rid)
    with pytest.raises(StateCorrupt,match='launch identity'): revision.generate(root,DAY,rid,expected_attempt=launch['attempt_id'],expected_namespace=launch['namespace'])
    fixture_supervisor(root,rid,launch)
    revision.assert_active_generation(root,DAY,rid,expected_attempt=launch['attempt_id'],expected_namespace=launch['namespace'])


def test_revision_orphan_module_is_recognized_for_exact_root(monkeypatch,tmp_path):
    from daily_agent import workflow_runtime as runtime
    identity={'pid':56789,'pgid':56789,'description':f'Fri Oct 09 00:00:00 2026 python -m daily_agent.production_revision worker --root {tmp_path} --date 2026-10-09 --revision-id '+('a'*64)}
    monkeypatch.setattr(runtime,'process_identity',lambda pid:identity)
    monkeypatch.setattr(runtime,'_group_has_live_members',lambda pid:True)
    assert runtime.inspect_orphan(identity,expected_root=tmp_path)=='verified'
    with pytest.raises(runtime.CleanupPending):runtime.inspect_orphan(identity,expected_root=tmp_path/'other')


def test_budget_deferred_only_unfinishable_effective_document(monkeypatch,authorized):
    from daily_agent.models import MaterialRecord
    root,c,_=authorized;config=_config(root);config.sources['reading']['max_chunks_per_paper']=80
    record=MaterialRecord(key='arxiv:2610.00001',source='arxiv',item_type='paper',title='Offline long paper',url='https://arxiv.org/abs/2610.00001v1',
                          paper_document={'document_kind':'full_text','chunks':[{'id':str(i)} for i in range(114)]})
    monkeypatch.setattr('daily_agent.deferred_review_cache._load',lambda config,r:None)
    before=deepcopy(record.to_dict())
    deferred=revision.budget_deferral(config,record)
    assert deferred['status']=='budget_deferred' and deferred['chunk_count']==114
    assert deferred['scope']=='this_revision_only' and record.to_dict()==before
    monkeypatch.setattr('daily_agent.deferred_review_cache._load',lambda config,r:(r,'independently validated cached draft'))
    assert revision.budget_deferral(config,record) is None
    monkeypatch.setattr('daily_agent.deferred_review_cache._load',lambda config,r:None)
    record.paper_document={'evidence_basis':'image_transcription_reviewed','native_document':record.paper_document,
                           'document_kind':'full_text','chunks':[{'id':str(i)} for i in range(40)]}
    assert revision.budget_deferral(config,record) is None


def test_revision_projection_cannot_rewind_canonical_publication(authorized):
    from daily_agent.storage import load_material_library,write_material_library
    root,c,_=authorized;canonical=_config(root);config=revision.scoped_config(root,DAY,c['revision_id'])
    record,_,_=make_repo('github:fixture/published_elsewhere')
    library=load_material_library(canonical);library[record.key]=record;write_material_library(canonical,library)
    stale=deepcopy(library)
    library[record.key].published_dates=['2026-10-09'];library[record.key].quality_status='published'
    library[record.key].raw['published_paper_identity']='current-publication-identity'
    write_material_library(canonical,library)
    write_material_library(config,stale)
    actual=load_material_library(canonical)[record.key]
    assert actual.published_dates==['2026-10-09'] and actual.quality_status=='published'
    assert actual.raw['published_paper_identity']=='current-publication-identity'


def test_stale_source_or_rejected_candidate_cannot_be_sealed(parent,monkeypatch):
    from daily_agent.storage import load_material_library,write_material_library
    from daily_agent.models import ApprovedItem
    root,_=parent;cfg=_config(root);r,d,review=make_repo('github:fixture/source')
    library=load_material_library(cfg);library[r.key]=r;write_material_library(cfg,library)
    monkeypatch.setattr('daily_agent.editorial.passes_topic_gate',lambda *a:True)
    c=revision.authorize_revision(root,DAY,**args(parent));rid=c['revision_id']
    row=ApprovedItem(key=r.key,item_type='repo',title=r.title,source=r.source,url=r.url,final_fields=d.draft_fields,material=r).to_dict()
    library[r.key]=deepcopy(r);library[r.key].source_updated_at='2026-10-09T01:00:00Z'
    # A known old source timestamp compared with a newer pool timestamp.
    row['material']['source_updated_at']='2026-10-08T01:00:00Z'
    write_material_library(cfg,library)
    with pytest.raises(ValueError,match='source version'):revision.seal_ready(root,DAY,rid,revision.carry_forward_rows(root,DAY,rid)+[row])
    row['material']['source_updated_at']=library[r.key].source_updated_at
    library[r.key].quality_status='rejected';write_material_library(cfg,library)
    with pytest.raises(ValueError,match='stale'):revision.seal_ready(root,DAY,rid,revision.carry_forward_rows(root,DAY,rid)+[row])


def test_revision_contract_protects_shared_assets_from_retention(parent,monkeypatch):
    from daily_agent.storage import load_material_library,write_material_library,_retained_delivery_artifacts
    root,_=parent;cfg=_config(root);r,_,_=make_repo('github:fixture/asset')
    asset=root/'data/pdfs/2020-01-01/revision-only.pdf';asset.parent.mkdir(parents=True);asset.write_bytes(b'fixture')
    r.raw['local_pdf_path']=str(asset)
    library=load_material_library(cfg);library[r.key]=r;write_material_library(cfg,library)
    monkeypatch.setattr('daily_agent.editorial.passes_topic_gate',lambda *a:True)
    revision.authorize_revision(root,DAY,**args(parent))
    assert asset.resolve() in _retained_delivery_artifacts(cfg)


@pytest.mark.parametrize('corrupt_deferral',[False, True])
def test_late_parsed_over_limit_deferred_before_model_admission(parent,monkeypatch,corrupt_deferral):
    from daily_agent.models import MaterialRecord
    from daily_agent.storage import load_material_library,write_material_library
    from daily_agent import pipeline
    root,_=parent;cfg=_config(root)
    r=MaterialRecord(key='arxiv:2610.00002',source='arxiv',item_type='paper',title='Quantum offline native long document',url='https://arxiv.org/abs/2610.00002v1',raw={'published_at':str(DAY)})
    library=load_material_library(cfg);library[r.key]=r;write_material_library(cfg,library)
    monkeypatch.setattr('daily_agent.editorial.passes_topic_gate',lambda *a:True)
    monkeypatch.setattr(pipeline,'build_shortlist',lambda config,records,day:list(records.values()))
    monkeypatch.setattr('daily_agent.deferred_review_cache._load',lambda *a:None)
    c=revision.authorize_revision(root,DAY,**args(parent));rid=c['revision_id']
    def enrich(config,day,batch,callback):
        batch[0].paper_document={'document_kind':'full_text','chunks':[{'id':str(i)} for i in range(114)]}
        return batch
    monkeypatch.setattr('daily_agent.cloud_cache.prepare_batch',enrich)
    seen=[]
    def no_model(config,day,batch,plan,batch_id,library,records,drafts,reviews,**kw):
        assert kw['eligible_keys']==set();seen.append(batch[0].key)
        return [],[],[]
    monkeypatch.setattr(pipeline,'_process_incremental_batch',no_model)
    launch=revision.reserve_generation(root,DAY,rid);fixture_supervisor(root,rid,launch)
    if corrupt_deferral:
        from daily_agent.workflow_state import atomic_json
        atomic_json(revision.issue_dir(root,DAY,rid)/'budget-deferrals'/(revision.digest(r.key)+'.json'),
                    {'payload':None,'sha256':'bad'})
        with pytest.raises(StateCorrupt,match='deferral'):revision.generate(root,DAY,rid,expected_attempt=launch['attempt_id'],expected_namespace=launch['namespace'])
        assert not seen
        return
    result=revision.generate(root,DAY,rid,expected_attempt=launch['attempt_id'],expected_namespace=launch['namespace'])
    revision.settle_generation(root,DAY,rid,launch['attempt_id'],0,1,expected_namespace=launch['namespace'])
    assert seen==[r.key] and {row['key'] for row in result['approval']}=={'github:test/repo'}
    reason=read_json(revision.issue_dir(root,DAY,rid)/'budget-deferrals'/(revision.digest(r.key)+'.json'))['payload']
    assert reason['status']=='budget_deferred' and reason['chunk_count']==114
    retained=load_material_library(cfg)[r.key]
    assert len(retained.paper_document['chunks'])==114 and retained.quality_status not in {'rejected','archived'}
    assert not retained.raw.get('pool_deferral')
