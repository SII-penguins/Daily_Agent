"""Per-material commit/replay and projection recovery, using offline queue jobs."""
from copy import deepcopy
from datetime import date
from types import SimpleNamespace
import json

import pytest

from daily_agent import pipeline, parent_writer, batch_execution
from daily_agent.models import MaterialRecord, EditorialDraft
from daily_agent.paper_document import version_identity
from daily_agent.reading import CORE, verify_draft
from daily_agent.editorial import PAPER_FIELDS, review_draft
from daily_agent.workflow_state import atomic_json, read_json, StateCorrupt

DAY=date(2026,10,10)
QUOTE='Compared with the baseline, circuit depth falls on simulated quantum circuits.'
TEXT='本文研究量子线路编译中的搜索空间，通过保留门之间的依赖关系构建候选方案，再在固定模拟条件下比较线路深度与资源代价，结论仅适用于测试中的设置。'


def record(key):
    r=MaterialRecord(key='arxiv:'+key,item_type='paper',source='arxiv',title='Quantum Circuit Search '+key,
                     url='https://arxiv.org/abs/'+key+'v1')
    r.paper_document={'identity':version_identity(r),'content_hash':'fixture-source',
        'document_kind':'full_text','source_type':'html',
        'chunks':[{'id':'c1','text':QUOTE}], 'pages':[{'page':1,'visual_required':True,'text':QUOTE}]}
    return r


def qualify(r):
    cid=r.paper_document['chunks'][0]['id']
    r.reading={'complete':True,'read_chunk_ids':[cid],'notes':[{'chunk_id':cid,'quotes':[QUOTE]}],
               'visual':{'required_pages':1,'strict_fidelity':True,'fidelity':{'passed':True}}}
    r.paper_text_status['sufficient_for_deep_summary']=True
    fields={name:TEXT for name in PAPER_FIELDS};fields['method_steps']=[TEXT];fields['confidence']='medium'
    draft=EditorialDraft(r.key,'paper',r.title,fields,evidence_used=[QUOTE],claim_evidence=[
        {'field':name,'chunk_id':cid,'quote':QUOTE,'conditions':'simulation baseline','evidence_kind':'simulation'} for name in CORE])
    verify_draft(draft,r)
    draft.verification['semantic_support']='model_checked'
    draft.verification['semantic_checks']=[{'field':f,'supported':True,'reason':'Fixture independent response'} for f in draft.verification['valid_fields']]
    r.reading['verification']=deepcopy(draft.verification)
    assert review_draft(None,[draft])[0].verdict=='PASS'
    return draft


@pytest.fixture
def setup(tmp_path,monkeypatch):
    config=SimpleNamespace(root=tmp_path,sources={
        'reading':{'run_budget_seconds':1800,'visual_budget_seconds':600,'fidelity_budget_seconds':7200,'max_chunks_per_paper':80},
        'llm_writer':{'run_budget_seconds':14400,'provider':'parent_queue'},
        'author_context':{'max_research_papers_per_batch':12}},
        quota={'paper_target':8,'github_target':2,'max_items':10}, delivery={'cloud':{'profile':'cloud-v1'}})
    plan=SimpleNamespace(folder=tmp_path/'issue')
    clock=SimpleNamespace(now=100.)
    original_execution=batch_execution.Execution
    def execution(*args,**kwargs):return original_execution(*args,**kwargs,clock=lambda:clock.now)
    monkeypatch.setattr(batch_execution,'Execution',execution)
    from daily_agent import author_context,scientific_analysis
    monkeypatch.setattr(author_context,'enrich_selected_author_contexts',lambda rows,*a,**k:rows)
    monkeypatch.setattr(scientific_analysis,'analyze_papers',lambda *a,**k:None)
    projections={'library':{},'editorial':{}}
    def library(config,value):
        projections['library']={k:r.to_dict() for k,r in value.items()}
        atomic_json(tmp_path/'library.json',projections['library'])
    def editorial(config,day,rows,drafts,reviews,approved):
        projections['editorial']={'keys':[r.key for r in rows],'approved':[r.key for r in approved]}
        atomic_json(tmp_path/'editorial.json',projections['editorial'])
    monkeypatch.setattr(pipeline,'write_material_library',library)
    monkeypatch.setattr(pipeline,'write_editorial_artifacts',editorial)
    return SimpleNamespace(config=config,plan=plan,clock=clock,projections=projections,
                           batch=[record('2609.00001'),record('2609.00002')],calls=[],library={})


def run(s):
    return pipeline._process_incremental_batch(s.config,DAY,deepcopy(s.batch),s.plan,'original-batch',
                                               s.library,[],[],[],use_llm=True)


def answer(s,job_id):
    job=read_json(s.config.root/'data/writer-queue'/f'{job_id}.job.json')
    worker='offline-test-reviewer' if job['stage']=='review' else 'offline-test-writer'
    claim=parent_writer.claim(s.config.root,job_id,worker)
    parent_writer.import_response(s.config.root,job_id,{'fixture':True},worker,claim_token=claim['token'])


STEPS=[('primary_writer','author_research',0,'author_research'),
       ('primary_writer','author_review',0,'review'),('native','chunk:c1',0,'reading'),('visual','page:1',0,'review'),
       ('fidelity','transcribe:1',0,'draft'),('fidelity','review:1',0,'review'),('repaired','chunk:cR',0,'reading'),
       ('primary_writer','draft',0,'draft'),('primary_writer','semantic',0,'review'),
       ('primary_writer','rewrite',0,'draft'),('primary_writer','semantic',1,'review'),
       ('primary_writer','presentation',0,'review'),('primary_writer','scientific_writer',0,'draft'),
       ('primary_writer','scientific_review',0,'review')]


def install_writer(s,monkeypatch):
    def draft(config,rows,*,use_llm,execution):
        r=rows[0]
        execution.bind_author_candidates([c['key'] for c in execution.contract['candidates']])
        for phase,substep,ordinal,role in STEPS:
            if phase=='repaired':
                native=deepcopy(r.paper_document)
                r.paper_document={**native,'native_document':native,'evidence_basis':'image_transcription_reviewed',
                                  'content_hash':'fixture-reviewed-derivative','chunks':[{'id':'cR','text':QUOTE}]}
                execution.bind_repaired(r)
            s.calls.append((r.key,phase,substep,ordinal))
            with execution.stage(phase):
                s.clock.now+=.01
                parent_writer.request(config.root,json.dumps([r.key,substep,ordinal]),120,
                    stage=role,execution=execution,operation=(r.key,phase,substep,ordinal))
        return [qualify(r)]
    monkeypatch.setattr(pipeline,'draft_report_items',draft)


def test_a_complete_b_each_stage_pending_resume_never_reexecutes_a(setup,monkeypatch):
    s=setup;install_writer(s,monkeypatch)
    completed_a=False; pending_b=[]; stable_jobs={}
    for _ in range(2*len(STEPS)+1):
        before=len(s.calls)
        try:
            records,drafts,reviews=run(s)
            break
        except parent_writer.PendingResponse as pending:
            job=read_json(s.config.root/'data/writer-queue'/f'{pending.job_id}.job.json')
            key,substep,ordinal=json.loads(job['prompt'])
            identity=(key,substep,ordinal)
            assert stable_jobs.setdefault(identity,pending.job_id)==pending.job_id
            if key==s.batch[1].key:
                completed_a=True;pending_b.append((substep,ordinal))
                # All later replays start directly at B, not even calling the A transport.
                if len(pending_b)>1:assert all(call[0]!=s.batch[0].key for call in s.calls[before:])
                journal=read_json(next((s.plan.folder/'batch-execution').glob('*/journal.json')))['payload']
                assert s.batch[0].key in journal['completed']
                assert not journal['reservations']
            answer(s,pending.job_id)
            s.clock.now+=3600 # parent waiting is excluded from stage budget
    else:pytest.fail('finite queue sequence did not finish')
    assert completed_a and len(pending_b)==len(STEPS)
    assert [r.key for r in records]==[r.key for r in s.batch]
    assert s.projections['editorial']['approved']==[r.key for r in s.batch]
    assert len(list((s.config.root/'data/writer-queue').glob('*.job.json')))==2*len(STEPS)
    before=len(s.calls);run(s);assert len(s.calls)==before
    journal=read_json(next((s.plan.folder/'batch-execution').glob('*/journal.json')))['payload']
    assert all(pool['charged']<10 for pool in journal['pools'].values())


@pytest.mark.parametrize('failed_view',['library','editorial'])
def test_projection_crash_after_commit_rebuilds_without_model(setup,monkeypatch,failed_view):
    s=setup
    def draft(config,rows,**kwargs):s.calls.append(rows[0].key);return [qualify(rows[0])]
    monkeypatch.setattr(pipeline,'draft_report_items',draft)
    function='write_material_library' if failed_view=='library' else 'write_editorial_artifacts'
    original=getattr(pipeline,function)
    def crash(*args,**kwargs):raise OSError('simulated projection write failure')
    monkeypatch.setattr(pipeline,function,crash)
    with pytest.raises(OSError):run(s)
    assert s.calls==[s.batch[0].key]
    monkeypatch.setattr(pipeline,function,original)
    run(s)
    assert s.calls==[s.batch[0].key,s.batch[1].key]
    assert s.projections['editorial']['approved']==[r.key for r in s.batch]


def test_completed_result_still_requires_mechanical_evidence_on_resume(setup,monkeypatch):
    s=setup
    monkeypatch.setattr(pipeline,'draft_report_items',lambda config,rows,**kwargs:[qualify(rows[0])])
    run(s)
    from daily_agent import deferred_review_cache
    monkeypatch.setattr(deferred_review_cache,'_supported',lambda *a:False)
    with pytest.raises(StateCorrupt):run(s)


def test_partial_papers_do_not_gain_completion_or_full_quota(setup,monkeypatch):
    s=setup
    def partial(config,rows,**kwargs):
        r=rows[0];draft=qualify(r)
        r.reading['complete']=False
        r.paper_text_status['sufficient_for_deep_summary']=False
        draft.verification['status']='limited'
        return [draft]
    monkeypatch.setattr(pipeline,'draft_report_items',partial)
    run(s)
    journal=read_json(next((s.plan.folder/'batch-execution').glob('*/journal.json')))['payload']
    assert journal['completed']=={}
    assert s.projections['editorial']['approved']==[]


def test_full_pipeline_freezes_enrichment_and_bounds_six_original_batches(tmp_path,monkeypatch):
    from daily_agent import incremental_issue,author_context,material_pool
    from daily_agent.models import EditorialReview
    config=SimpleNamespace(root=tmp_path,state_dir=tmp_path/'state',reports_dir=tmp_path/'reports',
        selected_dir=tmp_path/'selected',logs_dir=tmp_path/'logs',domains=[],
        sources={'selection':{'max_review_batches':6,'editorial_batch_size':8},
                 'llm_writer':{'provider':'parent_queue'},'arxiv':{},'github':{}},
        quota={'paper_target':8,'github_target':2,'max_items':10},delivery={'cloud':{'profile':'cloud-v1'}})
    monkeypatch.setattr(incremental_issue,'source_version',lambda:'fixture-v1')
    plan=incremental_issue.enroll(config,DAY)
    library={r.key:r for r in [record(f'2609.{n:05d}') for n in range(50)]}
    calls={'fetch':0,'enrich':[],'batches':[],'upserts':[]}; suspended={'once':True}
    monkeypatch.setattr(pipeline,'load_config',lambda root:config)
    monkeypatch.setattr(pipeline,'load_history',lambda *a:{})
    monkeypatch.setattr(pipeline,'load_material_library',lambda *a:deepcopy(library))
    monkeypatch.setattr(pipeline,'write_material_library',lambda *a:None)
    monkeypatch.setattr(pipeline,'_fallback_window_steps',lambda *a:[(1,1)])
    def fetch(*args):calls['fetch']+=1;return []
    monkeypatch.setattr(pipeline,'_fetch_windowed_sources',fetch)
    monkeypatch.setattr(pipeline,'_prepare_selected_items',lambda *a:[next(iter(library.values())).to_digest_item()])
    monkeypatch.setattr(pipeline,'upsert_materials',lambda config,items,day:calls['upserts'].append([r.id for r in items]))
    monkeypatch.setattr(pipeline,'_build_shortlist_from_library',lambda config,lib,day:list(lib.values()))
    monkeypatch.setattr(pipeline,'select_library_candidates',lambda config,lib,day:list(lib.values()))
    for name in ['enrich_open_access_links','enrich_unpaywall_links','enrich_citation_contexts']:
        monkeypatch.setattr(pipeline,name,lambda rows,config:rows)
    def enrich(rows,config):calls['enrich'].append([r.key for r in rows]);return rows
    monkeypatch.setattr(pipeline,'enrich_paper_texts',enrich)
    monkeypatch.setattr(author_context,'enrich_author_contexts',lambda rows,config:rows)
    monkeypatch.setattr(pipeline,'write_editorial_artifacts',lambda *a:None)
    monkeypatch.setattr(material_pool,'retain_quota_deferred',lambda *a:None)
    def process(config,day,batch,plan,batch_id,*args,**kwargs):
        calls['batches'].append((batch_id,[r.key for r in batch]))
        if batch_id.endswith(':1') and suspended['once']:
            suspended['once']=False
            raise parent_writer.PendingResponse('fixture-pending')
        return batch,[EditorialDraft(r.key,'paper',r.title,{}) for r in batch],[EditorialReview(r.key,'FAIL') for r in batch]
    monkeypatch.setattr(pipeline,'_process_incremental_batch',process)
    class Finished(Exception):pass
    def finish(*args):raise Finished()
    monkeypatch.setattr(pipeline,'cache_selected_pdfs',finish)
    with pytest.raises(parent_writer.PendingResponse):
        pipeline._run_pipeline_unlocked(tmp_path,DAY,dry_run=False,_issue_plan=plan,defer_delivery=True)
    restored=incremental_issue.load_plan(config,DAY)
    with pytest.raises(Finished):
        pipeline._run_pipeline_unlocked(tmp_path,DAY,dry_run=False,_issue_plan=restored,defer_delivery=True)
    assert calls['fetch']==1 and len(calls['upserts'])==1
    assert len(calls['enrich'])==6
    assert [keys for _,keys in calls['batches'][:2]]==[keys for _,keys in calls['batches'][2:4]]
    batches=incremental_issue.load_plan(config,DAY).state['batches']
    assert len(batches)==6 and sum(len(b['selected']) for b in batches)==48
    assert all(len(b['selected'])==8 for b in batches)


def test_completed_replay_cannot_restore_publication_eligibility(setup,monkeypatch):
    s=setup
    monkeypatch.setattr(pipeline,'draft_report_items',lambda config,rows,**kwargs:[qualify(rows[0])])
    run(s)
    a,b=s.batch
    s.library[a.key].published_dates=['2026-10-11']
    s.library[a.key].raw['published_paper_identity']=version_identity(s.library[a.key])
    monkeypatch.setattr(pipeline,'select_library_candidates',lambda config,lib,day:[r for r in lib.values() if not r.published_dates])
    eligible=pipeline._replay_eligible_keys(s.config,DAY,deepcopy(s.batch),s.library)
    assert eligible=={b.key}
    monkeypatch.setattr(pipeline,'draft_report_items',lambda *a,**k:pytest.fail('completed result does not call model'))
    journal=next((s.plan.folder/'batch-execution').glob('*/journal.json'))
    budgets=read_json(journal)['payload']['pools']
    rows,_,_=pipeline._process_incremental_batch(s.config,DAY,deepcopy(s.batch),s.plan,'original-batch',
        s.library,[],[],[],use_llm=True,eligible_keys=eligible)
    assert [r.key for r in rows]==[b.key]
    assert s.library[a.key].published_dates==['2026-10-11']
    assert read_json(journal)['payload']['pools']==budgets
    assert set(read_json(journal)['payload']['completed'])=={a.key,b.key}


@pytest.mark.parametrize('change',['version','source_bytes'])
def test_resume_changed_actual_source_rejects_without_new_budget(setup,monkeypatch,change):
    s=setup
    library={r.key:deepcopy(r) for r in s.batch}
    if change=='version':library[s.batch[0].key].url='https://arxiv.org/abs/2609.00001v2'
    else:library[s.batch[0].key].paper_document['chunks'][0]['text']+=' new source bytes'
    monkeypatch.setattr(pipeline,'select_library_candidates',lambda config,lib,day:list(lib.values()))
    with pytest.raises(batch_execution.ExecutionConflict):
        pipeline._replay_eligible_keys(s.config,DAY,s.batch,library)
    assert not s.plan.folder.exists()


def test_resume_fallback_upserts_only_new_window_not_old_source_snapshot(tmp_path,monkeypatch):
    from daily_agent import incremental_issue,author_context
    from daily_agent.models import RunStatus
    config=SimpleNamespace(root=tmp_path,state_dir=tmp_path/'state',reports_dir=tmp_path/'reports',
        selected_dir=tmp_path/'selected',logs_dir=tmp_path/'logs',domains=[],
        sources={'selection':{'max_review_batches':6,'editorial_batch_size':8},
                 'llm_writer':{'provider':'parent_queue'}},
        quota={'paper_target':8,'github_target':2,'max_items':10},delivery={'cloud':{'profile':'cloud-v1'}})
    old,new=record('2609.00001'),record('2609.00002')
    current=deepcopy(old);current.url='https://arxiv.org/abs/2609.00001v2'
    library={old.key:current};upserts=[]
    monkeypatch.setattr(incremental_issue,'source_version',lambda:'fixture-v1')
    plan=incremental_issue.enroll(config,DAY)
    plan.freeze_discovery({'raw_items':[old.to_digest_item().to_dict()],
        'selected_items':[old.to_digest_item().to_dict()],'window_index':0,'status':RunStatus().to_dict()})
    monkeypatch.setattr(pipeline,'load_config',lambda root:config)
    monkeypatch.setattr(pipeline,'load_history',lambda *a:{})
    monkeypatch.setattr(pipeline,'load_material_library',lambda *a:deepcopy(library))
    monkeypatch.setattr(pipeline,'_fallback_window_steps',lambda *a:[(1,1),(3,3)])
    monkeypatch.setattr(pipeline,'_fetch_windowed_sources',lambda *a:[new.to_digest_item()])
    monkeypatch.setattr(pipeline,'_prepare_selected_items',lambda rows,*a:rows)
    def upsert(config,items,day):
        upserts.append([item.canonical_key() for item in items])
        library[new.key]=deepcopy(new)
    monkeypatch.setattr(pipeline,'upsert_materials',upsert)
    monkeypatch.setattr(pipeline,'_build_shortlist_from_library',lambda config,lib,day:[lib[new.key]] if new.key in lib else [])
    monkeypatch.setattr(pipeline,'select_library_candidates',lambda config,lib,day:[lib[new.key]] if new.key in lib else [])
    monkeypatch.setattr(pipeline,'write_material_library',lambda *a:None)
    for name in ['enrich_open_access_links','enrich_unpaywall_links','enrich_paper_texts','enrich_citation_contexts']:
        monkeypatch.setattr(pipeline,name,lambda rows,config:rows)
    monkeypatch.setattr(author_context,'enrich_author_contexts',lambda rows,config:rows)
    class ReachedBatch(Exception):pass
    def process(*a,**k):raise ReachedBatch()
    monkeypatch.setattr(pipeline,'_process_incremental_batch',process)
    with pytest.raises(ReachedBatch):
        pipeline._run_pipeline_unlocked(tmp_path,DAY,dry_run=False,defer_delivery=True,_issue_plan=plan)
    assert upserts==[[new.key]]
    assert library[old.key].url.endswith('v2')
