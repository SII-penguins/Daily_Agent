"""Offline transport counts and recovery for the fidelity-first orchestrator."""
from copy import deepcopy
from collections import Counter
import hashlib
import json
import pytest

from daily_agent import editorial, parent_writer, reading
from daily_agent.batch_execution import Execution, candidate, digest
from daily_agent.paper_document import build_document, attach_document
from daily_agent.parent_writer import PendingResponse
from test_stage_execution import stage_config, stage_record, StageClock


class EvidenceReady(BaseException):
    """Stop before the unrelated writer; no synthesized PASS is installed."""


def fixture(root, page_count=2):
    root.mkdir(parents=True, exist_ok=True)
    cfg = stage_config(root, run_budget_seconds=100, visual_budget_seconds=100,
                       fidelity_budget_seconds=100, max_chunks_per_paper=80)
    cfg.sources['paper_text'] = {'chunk_chars':512, 'min_body_chars':300}
    r = stage_record()
    texts = [r.title+'\nMethods\n'+('We evaluate a fixed routing procedure with formula x 2 under a fixed baseline. '*14),
             'Results\n'+('The simulated routing outcome uses a fixed baseline and formula x 2. '*14)+'\nConclusion\nLimits remain.']
    texts = [texts[i % 2] for i in range(page_count)]
    pages=[]
    for n,text in enumerate(texts,1):
        image=root/f'page-{n}.png';image.write_bytes(f'fake page {n}'.encode())
        pages.append({'page':n,'text':text,'visual_required':True,'image_path':str(image),
                      'image_hash':hashlib.sha256(image.read_bytes()).hexdigest()})
    attach_document(r,build_document(r,pages,r.url,'html',cfg.sources['paper_text']))
    assert r.paper_document['document_kind']=='full_text'
    return cfg,r


class Transport:
    def __init__(self, clock, *, fail_review=False, pending=None):
        self.clock=clock;self.fail_review=fail_review;self.pending=pending;self.calls=[];self.blocked=False;self.fidelity_started=False
    def __call__(self, root, prompt, timeout, image=None, *, execution=None, operation=None):
        data=json.loads(prompt.split('输入：\n',1)[1])
        if prompt.startswith('阅读论文'):
            phase=operation[1] if operation else ('repaired' if self.fidelity_started else 'native')
            result={'chunk_id':data['id'],'summary':'该块提供限定条件下的原文证据。','quotes':[data['text'][:100]],
                    'conditions':'fixed baseline','evidence_kind':'simulation'}
        elif prompt.startswith('核对附带'):
            phase='visual';result={'page':data['page'],'summary':'完整原页核对','figures':[], 'tables':[],
                                   'formulas':[],'text_matches_image':True,'issues':[]}
        elif prompt.startswith('你是论文页面转写员'):
            self.fidelity_started=True;phase='transcribe';result={'page':data['page'],'text':data['native_text'].replace('x 2','x^2'),
                                      'assets':[],'unresolved':[]}
        elif prompt.startswith('你是独立的页面保真复核员'):
            phase='review';result={'page':data['page'],'text_supported':not self.fail_review,
                'inventory_complete':True,'checks':[],'issues':['not supported'] if self.fail_review else []}
        else: pytest.fail('unexpected model operation')
        if execution is not None:
            execution.admit(*operation, {'prompt':prompt,'images':image},
                            queue_job_id=digest([prompt,image]),retry_generation=0,queue_role=phase)
        self.calls.append((phase,deepcopy(operation),digest([prompt,image])))
        self.clock.advance(1)
        if phase==self.pending and not self.blocked:
            self.blocked=True;raise PendingResponse('offline-pending')
        return result


def run(monkeypatch, cfg, record, transport, execution=None):
    # Isolate the fidelity-first reorder; grouped transport has separate integration tests.
    monkeypatch.setattr('daily_agent.reading_batches.read_papers',reading.read_papers)
    monkeypatch.setattr(parent_writer,'request',transport)
    def ready(*args,**kwargs): raise EvidenceReady()
    monkeypatch.setattr(editorial,'_draft_with_llm',ready)
    with pytest.raises(EvidenceReady):
        editorial._draft_report_items_uncached(cfg,[record],execution=execution)


def ledger(cfg, original, clock):
    return Execution(cfg.root/'issue','batch',[candidate(original)],cfg,protocol='fidelity-first-test',clock=clock)


@pytest.mark.parametrize('page_count',[2,8])
def test_exact_native_and_visual_savings_with_all_independent_reviews_retained(tmp_path,monkeypatch,page_count):
    old_cfg,old=fixture(tmp_path/'legacy',page_count); new_cfg,new=fixture(tmp_path/'new',page_count)
    old_clock=StageClock();old_transport=Transport(old_clock)
    run(monkeypatch,old_cfg,old,old_transport)
    clock=StageClock();transport=Transport(clock);original=deepcopy(new)
    with ledger(new_cfg,original,clock) as execution:
        run(monkeypatch,new_cfg,new,transport,execution)
        state=execution.snapshot()
    old_counts=Counter(p for p,_,_ in old_transport.calls); counts=Counter(p for p,_,_ in transport.calls)
    assert old_counts['native']==len(original.paper_document['chunks'])>0
    assert counts['native']==0
    assert counts['visual']==0 and old_counts['visual']==page_count
    for phase in ['transcribe','review','repaired']:
        assert counts[phase]==old_counts[phase]>0
    assert len(old_transport.calls)-len(transport.calls)==len(original.paper_document['chunks'])+page_count
    assert old.reading['fingerprint']==new.reading['fingerprint']
    print(json.dumps({'pages':page_count,'legacy_calls':dict(old_counts),'fidelity_first_calls':dict(counts),
                      'saved_fake_transport_calls':len(old_transport.calls)-len(transport.calls)},sort_keys=True))
    assert old.reading['notes']==new.reading['notes']
    assert old.paper_document['content_hash']==new.paper_document['content_hash']
    assert new.reading['complete'] and new.reading['visual']['strict_fidelity']
    assert state['pools']['native']['remaining']==100 and state['pools']['native']['charged']==0
    assert all(pool['limit']==100 for name,pool in state['pools'].items() if name in {'native','repaired','visual','fidelity'})
    audit=reading.audit_reading([new.to_dict()])
    assert audit['full_text_read_count']==1 and audit['quality_passed_count']==0  # no claim review yet


@pytest.mark.parametrize('phase',['transcribe','review','repaired'])
def test_pending_resume_never_admits_native_and_reuses_exact_job_identity(tmp_path,monkeypatch,phase):
    cfg,original=fixture(tmp_path);clock=StageClock();transport=Transport(clock,pending=phase)
    with ledger(cfg,original,clock) as execution:
        with pytest.raises(PendingResponse):
            run(monkeypatch,cfg,deepcopy(original),transport,execution)
        first=execution.snapshot()
        assert not first['reservations']
    resumed=deepcopy(original)
    with ledger(cfg,original,clock) as execution:
        run(monkeypatch,cfg,resumed,transport,execution)
        final=execution.snapshot()
    assert not any(p=='native' for p,_,_ in transport.calls)
    for slot,op in first['operations'].items(): assert final['operations'][slot]==op
    assert resumed.reading['complete'] and resumed.reading['visual']['fidelity']['passed']
    # A complete replay runs all evidence gates against cache, with zero transport.
    transport.calls.clear()
    with ledger(cfg,original,clock) as execution:
        run(monkeypatch,cfg,deepcopy(original),transport,execution)
    assert transport.calls==[]


def test_fidelity_failure_retains_native_fallback_and_blocks_full_readiness(tmp_path,monkeypatch):
    cfg,r=fixture(tmp_path);original=deepcopy(r);clock=StageClock();transport=Transport(clock,fail_review=True)
    with ledger(cfg,original,clock) as execution:
        run(monkeypatch,cfg,r,transport,execution)
    counts=Counter(p for p,_,_ in transport.calls)
    assert counts['native']==len(original.paper_document['chunks']) and counts['repaired']==0
    assert counts['review']==4  # original two finite attempts for each of two pages
    assert r.reading['complete'] and not r.reading['visual']['strict_fidelity']
    assert r.paper_text_status['sufficient_for_deep_summary'] is False


def test_disabled_fidelity_keeps_original_native_path(tmp_path,monkeypatch):
    cfg,r=fixture(tmp_path);cfg.sources['reading']['fidelity_enabled']=False
    original=deepcopy(r);clock=StageClock();transport=Transport(clock)
    with ledger(cfg,original,clock) as execution:
        run(monkeypatch,cfg,r,transport,execution)
    assert transport.calls[0][0]=='native'
    assert not any(p in {'transcribe','review','repaired'} for p,_,_ in transport.calls)


def test_exhausted_fidelity_does_not_borrow_unused_native_budget(tmp_path,monkeypatch):
    cfg,r=fixture(tmp_path);cfg.sources['reading']['fidelity_budget_seconds']=0
    original=deepcopy(r);clock=StageClock();transport=Transport(clock)
    with ledger(cfg,original,clock) as execution:
        run(monkeypatch,cfg,r,transport,execution)
        state=execution.snapshot()
    assert not any(p in {'transcribe','review','repaired'} for p,_,_ in transport.calls)
    assert any(p=='native' for p,_,_ in transport.calls)
    assert state['pools']['fidelity']['remaining']==0
    assert not r.paper_text_status['sufficient_for_deep_summary']


def test_exact_lower_stage_caches_reused_without_new_transport(tmp_path,monkeypatch):
    import shutil
    cfg,old=fixture(tmp_path/'old');clock=StageClock();transport=Transport(clock)
    run(monkeypatch,cfg,old,transport)  # populate original stage protocols through legacy order
    new_cfg,new=fixture(tmp_path/'new');original=deepcopy(new)
    shutil.copytree(cfg.root/'data'/'reading',new_cfg.root/'data'/'reading')
    cached=Transport(clock)
    with ledger(new_cfg,original,clock) as execution:
        run(monkeypatch,new_cfg,new,cached,execution)
    assert cached.calls==[]
    assert new.reading['fingerprint']==old.reading['fingerprint']
    assert new.reading['complete'] and new.reading['visual']['strict_fidelity']


def test_nonvisual_document_retains_native_full_read(tmp_path,monkeypatch):
    cfg,r=fixture(tmp_path)
    for page in r.paper_document['pages']: page['visual_required']=False
    original=deepcopy(r);clock=StageClock();transport=Transport(clock)
    with ledger(cfg,original,clock) as execution:
        run(monkeypatch,cfg,r,transport,execution)
    assert {p for p,_,_ in transport.calls}=={'native'}
    assert r.reading['complete']
