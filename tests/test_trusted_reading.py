from datetime import date
from pathlib import Path
import json

from daily_agent.config import load_config
from daily_agent.models import MaterialRecord, EditorialDraft, ApprovedItem, RunStatus
from daily_agent.paper_document import build_document, attach_document, version_identity, extract_document
from daily_agent.reading import read_papers, verify_draft, semantic_review, synthesis_evidence
from daily_agent.rendering.notes import write_reading_notes, must_read
from daily_agent.rendering.markdown import render_daily_markdown
from daily_agent.rendering.html import render_daily_html
from daily_agent.storage import select_library_candidates
from daily_agent.insights import build_daily_insights


def test_numbers_preserve_quantities_across_standard_thousands_separators():
    from daily_agent.reading import numbers
    assert numbers('1,000 QPUs and 48.6 GWh') == numbers('1000 QPUs and 48.6 GWh')
    assert numbers('1,234,567.8 %') == {'1234567.8%'}
    assert numbers('1,2 and 12,34') == {'1', '2', '12', '34'}
    assert not numbers('1001 QPUs') <= numbers('1,000 QPUs')


def config(tmp_path):
    cfg=load_config(Path(__file__).resolve().parents[1]); object.__setattr__(cfg,'root',tmp_path)
    cfg.sources['reading']={'run_budget_seconds':60,'max_chunks_per_paper':80}
    return cfg


def paper():
    return MaterialRecord(key='arxiv:2601.00001',source='arxiv',item_type='paper',title='Quantum Routing Study',
        url='https://arxiv.org/abs/2601.00001v1',pdf_url='https://arxiv.org/pdf/2601.00001v1',abstract='Metadata abstract')


def document(record):
    text='Quantum Routing Study\nAbstract\nWe study quantum routing.\nMethods\n'+('A constrained routing search reduces noise. '*80)+'\nResults\nCompared with baseline A, depth falls 22% on 12 simulated qubits.\nDiscussion\nLimits include small circuits.\nReferences\nFinal reference tail evidence.'
    return build_document(record,[{'page':1,'text':text[:1800]},{'page':2,'text':text[1800:]}],record.pdf_url,'pdf',{'chunk_chars':1000})


def responder(prompt, timeout):
    chunk=json.loads(prompt.split('输入：\n',1)[1])
    return {'chunk_id':chunk['id'],'summary':'本块说明量子线路研究及其证据。','quotes':[chunk['text'].strip()[:200]],'conditions':'12 simulated qubits; baseline A','evidence_kind':'simulation'}


def draft(record):
    fields={name:'原文讨论在硬件连通性约束下搜索量子线路的技术方法，并通过模拟实验比较基线，限定结论的适用范围。' for name in ['problem','method','why_it_works','novelty_or_difference','technical_route','limitations','possible_use_or_impact']}
    fields.update(key_result='在12比特模拟上相对基线降低22%，该结果仅限于文中描述的线路与噪声条件，不能直接外推至其他硬件平台。',method_steps=['执行约束搜索'],confidence='medium',evidence_from_source='chunks')
    result=next(c for c in record.paper_document['chunks'] if 'depth falls 22%' in c['text'])
    quote='Compared with baseline A, depth falls 22% on 12 simulated qubits.'
    claims=[{'field':f,'chunk_id':result['id'],'quote':quote,'conditions':'baseline A; 12 simulated qubits','evidence_kind':'simulation'} for f in fields if f not in {'confidence','evidence_from_source'}]
    return EditorialDraft(record.key,'paper',record.title,fields,claim_evidence=claims)


def test_document_preserves_tail_and_pages_without_keyword_false_positive():
    r=paper(); doc=document(r)
    assert doc['document_kind']=='full_text'
    assert 'Final reference tail' in doc['chunks'][-1]['text']
    assert {c['page'] for c in doc['chunks']}=={1,2}
    assert 'limitations' not in doc['coverage']['sections_found']  # not mandatory header
    false=build_document(r,[{'page':None,'text':'Quantum Routing Study: our method results show limitations. '*100}],r.url,'html')
    assert false['document_kind']=='abstract_only'
    assert not false['sections']


def test_partial_parse_and_wrong_identity_cannot_be_full():
    r=paper(); doc=document(r)
    partial=build_document(r,doc['pages'],r.pdf_url,'pdf',{'max_raw_text_chars':100})
    assert partial['document_kind']=='partial_text'
    empty=build_document(r,doc['pages']+[{'page':3,'text':''}],r.pdf_url,'pdf')
    assert empty['document_kind']=='partial_text'
    r.title='Completely Unrelated Biology Manuscript'
    wrong=build_document(r,doc['pages'],r.pdf_url,'pdf')
    assert not wrong['title_match'] and wrong['document_kind']=='partial_text'


def test_reading_covers_tail_and_cache_invalidates(tmp_path):
    cfg=config(tmp_path); r=paper(); attach_document(r,document(r)); calls=[]
    def invoke(prompt,timeout): calls.append(prompt); return responder(prompt,timeout)
    read_papers([r],cfg,invoke)
    assert r.reading['complete'] and r.paper_text_status['sufficient_for_deep_summary']
    assert 'Final reference tail' in calls[-1]
    first=len(calls); read_papers([r],cfg,invoke); assert len(calls)==first
    r.paper_document['content_hash']='new content'
    read_papers([r],cfg,invoke); assert len(calls)==2*first
    r.url=r.url.replace('v1','v2'); r.pdf_url=r.pdf_url.replace('v1','v2')
    assert version_identity(r)!=r.paper_document['identity']


def test_failures_and_fabricated_quotes_do_not_count_as_read(tmp_path):
    cfg=config(tmp_path); r=paper(); attach_document(r,document(r))
    def bad(prompt,timeout):
        n=responder(prompt,timeout); n['quotes']=['this quote is entirely fabricated']; return n
    read_papers([r],cfg,bad)
    assert r.reading['coverage']==0 and not r.paper_text_status['sufficient_for_deep_summary']
    read_papers([r],cfg,None)
    assert not r.reading['complete']


def test_numbers_missing_conditions_and_fake_locations_are_removed(tmp_path):
    cfg=config(tmp_path); r=paper(); attach_document(r,document(r)); read_papers([r],cfg,responder)
    d=draft(r); d.draft_fields['key_result']='提高99%'
    verify_draft(d,r)
    assert d.draft_fields['key_result']=='not_stated' and d.verification['status']=='limited'
    d=draft(r)
    for c in d.claim_evidence: c['chunk_id']='nonexistent'
    verify_draft(d,r)
    assert not d.claim_evidence and d.draft_fields['method']=='not_stated'
    d=draft(r)
    for c in d.claim_evidence: c['conditions']='not_stated'
    verify_draft(d,r)
    assert d.draft_fields['key_result']=='not_stated'


def test_semantic_review_rejects_numeric_coincidence(tmp_path):
    cfg=config(tmp_path); r=paper(); attach_document(r,document(r)); read_papers([r],cfg,responder)
    d=draft(r); verify_draft(d,r)
    def reject(prompt,timeout):
        return {'checks':[{'field':f,'supported':f!='key_result','reason':'错误基线' if f=='key_result' else '支持'} for f in d.verification['valid_fields']]}
    semantic_review(d,r,reject,10)
    assert d.draft_fields['key_result']=='not_stated' and d.verification['status']=='limited'


def test_notes_cards_and_coverage_are_consistent(tmp_path):
    cfg=config(tmp_path); r=paper(); attach_document(r,document(r)); read_papers([r],cfg,None)
    d=draft(r); verify_draft(d,r)
    r.reading['claim_evidence']=d.claim_evidence
    item=ApprovedItem(r.key,'paper',r.title,r.source,r.url,d.draft_fields,r)
    write_reading_notes(cfg,[item],date(2026,9,28),True)
    md=render_daily_markdown([item],date(2026,9,28),RunStatus())
    html=render_daily_html([item],date(2026,9,28),RunStatus())
    assert not must_read(item)
    assert r.title not in md.split('## 今日洞察')[0]
    assert '未完成阅读' in md and '未完成阅读' in html
    assert '为什么有效' not in md and '阅读状态' in md
    for field in ['reading_note_url','reading_note_html_url']:
        path=cfg.reports_dir/r.raw[field]
        assert path.exists() and '分块阅读与覆盖' in path.read_text()
    assert '-dry-run/' in r.raw['reading_note_url']


def test_unchanged_papers_stay_suppressed_and_sources_are_not_topics(tmp_path):
    cfg=config(tmp_path); r=paper(); r.published_dates=['2026-01-01']; r.raw['published_paper_identity']=version_identity(r)
    assert select_library_candidates(cfg,{r.key:r},date(2026,9,28))==[]
    r.url=r.url.replace('v1','v2'); r.pdf_url=r.pdf_url.replace('v1','v2'); r.update_label='version_update'
    assert select_library_candidates(cfg,{r.key:r},date(2026,9,28))==[r]
    r.tags=['openalex','openalex']; r2=paper(); r2.key='other'; r2.tags=['openalex']
    items=[ApprovedItem(x.key,'paper',x.title,x.source,x.url,{},x) for x in [r,r2]]
    assert 'openalex' not in ' '.join(build_daily_insights(items))


def test_synthesis_budget_is_explicit(tmp_path):
    cfg=config(tmp_path);r=paper();attach_document(r,document(r));read_papers([r],cfg,responder)
    assert synthesis_evidence(r,1) is None
    assert 'synthesis_error' in r.reading


def test_editorial_end_to_end_uses_chunk_notes_and_reuses_verified_draft(tmp_path, monkeypatch):
    from daily_agent.editorial import draft_report_items, review_draft, approve_publication
    cfg=config(tmp_path); r=paper(); attach_document(r,document(r)); calls=[]
    def fake_run(args,**kwargs):
        prompt=args[-1]; calls.append(prompt)
        if prompt.startswith('阅读论文的一个原文块'):
            output=responder(prompt,kwargs['timeout'])
        elif prompt.startswith('你是独立证据核验员'):
            value=json.loads(prompt.split('输入：\n',1)[1])
            output={'checks':[{'field':f,'supported':True,'reason':'受引句支持'} for f in value['fields']]}
        else:
            assert 'reading_notes' in prompt and 'Final reference tail' in prompt
            d=draft(r)
            output=[{'key':r.key,'draft_fields':d.draft_fields,'claim_evidence':d.claim_evidence,
                     'evidence_used':['chunks'],'writer_notes':'分块阅读完成'}]
        return type('Result',(),{'stdout':json.dumps(output,ensure_ascii=False)})()
    monkeypatch.setattr('daily_agent.editorial.subprocess.run',fake_run)
    drafts=draft_report_items(cfg,[r],True)
    assert drafts[0].verification['semantic_support']=='model_checked'
    approved=approve_publication(cfg,[r],drafts,review_draft(cfg,drafts,True))
    assert len(approved)==1
    count=len(calls)
    again=draft_report_items(cfg,[r],True)
    assert len(calls)==count and again[0].verification==drafts[0].verification
    # Content/prompt fingerprints invalidate both chunk and final draft caches.
    cfg.sources['reading']['prompt_version']=2
    draft_report_items(cfg,[r],True)
    assert len(calls)>count


def test_empty_evidence_fallback_is_not_published(tmp_path):
    from daily_agent.editorial import draft_report_items, review_draft, approve_publication
    cfg=config(tmp_path);r=paper();attach_document(r,document(r))
    drafts=draft_report_items(cfg,[r],False)
    assert approve_publication(cfg,[r],drafts,review_draft(cfg,drafts))==[]


def test_real_pdf_extractor_preserves_page_numbers():
    import pytest
    fitz=pytest.importorskip('fitz')
    r=paper()
    with fitz.open() as pdf:
        for text in ['Quantum Routing Study\nMethods\nWe use constrained routing.','Results\nDepth falls 22%.\nDiscussion\nLimited simulations.\nReferences\nTail evidence.']:
            page=pdf.new_page();page.insert_text((50,50),text)
        doc=extract_document(pdf.tobytes(),r,r.pdf_url,{'min_body_chars':50})
    assert doc['coverage']['total_pages']==2
    assert doc['chunks'][-1]['page']==2 and 'Tail evidence' in doc['chunks'][-1]['text']


def test_corrupt_document_cache_and_oversized_synthesis_are_not_trusted(tmp_path):
    from daily_agent.paper_document import valid_document
    r=paper(); doc=document(r)
    assert valid_document(doc,version_identity(r))
    doc['pages'][0]['text']='tampered'
    assert not valid_document(doc,version_identity(r))
    cfg=config(tmp_path);attach_document(r,document(r));read_papers([r],cfg,responder)
    synthesis_evidence(r,1)
    d=draft(r);verify_draft(d,r)
    assert d.verification['status']=='limited'


def test_numbered_technical_headings_and_ordinary_sentences():
    r=paper()
    text='Quantum Routing Study\n1 Introduction\nBackground.\n2 Architecture\n'+('Quantum constrained search. '*130)+'\n3 Performance\nSimulation results.\n4 Conclusion and future directions\nLimits.\nReferences\nReferences list.'
    doc=build_document(r,[{'page':1,'text':text}],r.pdf_url,'pdf')
    assert doc['document_kind']=='full_text'
    assert {'method','results','conclusion'} <= set(doc['coverage']['sections_found'])


def test_pdf_candidates_preserve_explicit_arxiv_version():
    from daily_agent.connectors.paper_text import _candidate_pdf_urls
    r=paper()
    assert _candidate_pdf_urls(r)[0].endswith('2601.00001v1')


def test_equivalent_conditions_and_escaped_linebreaks_preserve_quote_checks():
    from daily_agent.reading import normalize_note, valid_note
    chunk={'id':'c1','text':'This is original evidence.\nIt uses twelve qubits.'}
    note={'chunk_id':'c1','summary':'原文说明实验条件。','quotes':['This is original evidence.\\nIt uses twelve qubits.'],
          'conditions':{'sample':'twelve qubits'},'evidence_kind':'simulation'}
    normalized=normalize_note(note)
    assert valid_note(normalized,chunk,{})
    assert json.loads(normalized['conditions'])==note['conditions']
    normalized['quotes']=['This is fabricated evidence with twelve qubits.']
    assert not valid_note(normalized,chunk,{})


def test_parallel_reading_keeps_order_and_validates_every_chunk(tmp_path):
    cfg=config(tmp_path);cfg.sources['reading']['concurrent_reads']=3
    r=paper();attach_document(r,document(r))
    read_papers([r],cfg,responder)
    assert r.reading['complete']
    assert r.reading['read_chunk_ids']==[c['id'] for c in r.paper_document['chunks']]
    assert not r.reading['failures']


def test_fenced_model_json_parses_but_prose_is_not_silently_accepted():
    import pytest
    from daily_agent.editorial import _decode_model_json, _load_llm_payload
    assert _decode_model_json('```json\n{"ok":true}\n```')=={'ok':True}
    assert _load_llm_payload('```\n[]\n```')==[]
    with pytest.raises(ValueError): _decode_model_json('Here is JSON: {"ok":true}')


def test_invalid_quote_gets_one_bounded_repair_request(tmp_path):
    cfg=config(tmp_path);r=paper();attach_document(r,document(r));calls={}
    def invoke(prompt,timeout):
        chunk=json.JSONDecoder().raw_decode(prompt.split('输入：\n',1)[1])[0]
        cid=chunk['id'];calls[cid]=calls.get(cid,0)+1
        return {'chunk_id':cid,'summary':'读取该段原文。','quotes':['Fabricated quote rejected by validation' if calls[cid]==1 else chunk['text'].strip()[:100]],
                'conditions':'not_stated','evidence_kind':'not_stated'}
    read_papers([r],cfg,invoke)
    assert r.reading['complete'] and all(n==2 for n in calls.values())
    assert list((tmp_path/'data/reading').glob('*/*.invalid.json'))


def test_visual_notes_link_hash_verified_local_page(tmp_path):
    import hashlib
    cfg=config(tmp_path);r=paper();attach_document(r,document(r));d=draft(r)
    path=tmp_path/'page.png';path.write_bytes(b'page evidence')
    r.paper_document['pages'][0].update(image_path=str(path),image_hash=hashlib.sha256(path.read_bytes()).hexdigest())
    r.reading['visual']={'notes':[{'page':1,'summary':'页面转写','figures':[],'tables':[],'formulas':['x^2'],'issues':[]}]}
    item=ApprovedItem(r.key,'paper',r.title,r.source,r.url,d.draft_fields,r)
    write_reading_notes(cfg,[item],date(2026,9,28),True)
    html=(cfg.reports_dir/r.raw['reading_note_html_url']).read_text()
    assert '<a href="assets/' in html
    assets=list((cfg.reports_dir/'notes/2026-09-28-dry-run/assets').glob('*.png'))
    assert len(assets)==1 and assets[0].read_bytes()==b'page evidence'


def test_rejected_semantics_gets_one_rewrite_and_fresh_review(tmp_path,monkeypatch):
    from daily_agent.editorial import draft_report_items
    cfg=config(tmp_path);r=paper();attach_document(r,document(r));calls={'write':0,'review':0}
    def run(args,**kwargs):
        prompt=args[-1]
        if prompt.startswith('阅读论文'):
            output=responder(prompt,1)
        elif prompt.startswith('你是独立证据核验员'):
            calls['review']+=1
            value=json.loads(prompt.split('输入：\n',1)[1])
            output={'checks':[{'field':f,'supported':not(f=='method' and calls['review']==1),'reason':'需缩小结论范围'} for f in value['fields']]}
        else:
            calls['write']+=1
            if calls['write']==2: assert 'rejected_claims' in prompt
            d=draft(r);output=[{'key':r.key,'draft_fields':d.draft_fields,'claim_evidence':d.claim_evidence,'evidence_used':['chunks'],'writer_notes':'有依据'}]
        return type('R',(),{'stdout':json.dumps(output,ensure_ascii=False)})()
    monkeypatch.setattr('daily_agent.editorial.subprocess.run',run)
    out=draft_report_items(cfg,[r],True)
    assert calls=={'write':2,'review':2}
    assert 'method' in out[0].verification['valid_fields']
    assert out[0].verification['semantic_support']=='model_checked'
    assert r.reading['semantic_rewrite_attempted']


def test_reading_budget_changes_reuse_validated_notes(tmp_path):
    cfg = config(tmp_path); r = paper(); attach_document(r, document(r))
    read_papers([r], cfg, responder)
    identity = r.reading['fingerprint']
    cfg.sources['reading'].update(run_budget_seconds=0, timeout_seconds=1, concurrent_reads=3)
    def forbidden(*args):
        raise AssertionError('Validated notes must survive a scheduling change')
    read_papers([r], cfg, forbidden)
    assert r.reading['complete'] and r.reading['fingerprint'] == identity
    cfg.sources['reading']['prompt_version'] = 99
    read_papers([r], cfg, forbidden)
    assert not r.reading['complete'] and r.reading['fingerprint'] != identity
