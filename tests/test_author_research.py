import json
from dataclasses import replace
from pathlib import Path
import pytest
from daily_agent.config import load_config
from daily_agent.models import MaterialRecord
from daily_agent.author_context import build_author_context, update_watchlist
from daily_agent.author_research import (explicit_pdf_evidence,frontmatter,enrich_selected_author_contexts,
    watchlist_discovery_queries,_validated_sources)
from daily_agent.parent_writer import PendingResponse,digest


def setup(tmp_path):
    config=replace(load_config(),root=tmp_path,sources={'llm_writer':{'provider':'parent_queue'},'author_context':{}})
    r=MaterialRecord(key='pmlr:new',source='pmlr',item_type='paper',title='New Quantum Learning',url='https://proceedings.mlr.press/new.html',authors=['Alice Example','Bob Example'])
    r.paper_document={'title_match':True,'source_type':'pdf','source_url':'https://proceedings.mlr.press/new.pdf','content_hash':'pdf123','pages':[{'page':1,'text':'New Quantum Learning\nAlice Example, Bob Example\nCorrespondence to: Bob Example\nInstitute X'}]}
    return config,r


def source(r):
    return {'title':r.title,'source_url':r.paper_document['source_url'],'source_kind':'paper_pdf','checked_at':'2026-10-08T00:00:00Z','review_status':'verified','author_order_complete':True,'page':1,'excerpt':r.paper_document['pages'][0]['text'],'authors':[{'name':'Alice Example','institutions':['Institute X']},{'name':'Bob Example','roles':['corresponding_author']}],'claims':[{'kind':'author','subject':'Alice Example','quote':'Alice Example'},{'kind':'author','subject':'Bob Example','quote':'Bob Example'},{'kind':'institution','subject':'Institute X','quote':'Institute X'},{'kind':'role','subject':'corresponding_author','quote':'Correspondence to: Bob Example'}]}


def test_direct_pdf_role_is_not_author_order(tmp_path):
    _,r=setup(tmp_path)
    ev=explicit_pdf_evidence(r)
    ctx=build_author_context(r.to_digest_item(),ev)
    bob=next(a for a in ctx['authors'] if a['name']=='Bob Example')
    assert bob['roles']==['corresponding_author']
    r.paper_document['title_match']=False
    assert not explicit_pdf_evidence(r)


def test_generic_research_and_independent_review_then_cache(tmp_path,monkeypatch):
    config,r=setup(tmp_path); calls=[]
    def request(root,prompt,timeout,**kw):
        calls.append(kw['stage'])
        if kw['stage']=='author_research':return {'papers':[{'key':r.key,'sources':[source(r)],'uncertainties':['具体课题组尚未核实']}]}
        payload=json.loads(prompt.split('\nREVIEW_INPUT:',1)[1])
        return {'input_sha256':digest(payload),'approved_source_hashes':[digest(source(r))],'uncertainties':{}}
    monkeypatch.setattr('daily_agent.author_research.request',request)
    enrich_selected_author_contexts([r],config)
    assert calls==['author_research','review']
    assert r.raw['author_research_sources']
    assert not r.raw['research_context']['labs']
    assert r.raw['research_context']['institutions'][0]['name']=='Institute X'
    enrich_selected_author_contexts([r],config)
    assert len(calls)==2
    assert watchlist_discovery_queries(config,1)==['"Alice Example" quantum']


def test_prompt_is_frozen_across_resume(tmp_path,monkeypatch):
    config,r=setup(tmp_path); prompts=[]
    def request(root,prompt,timeout,**kw):
        prompts.append(prompt);raise PendingResponse('job')
    monkeypatch.setattr('daily_agent.author_research.request',request)
    with pytest.raises(PendingResponse):enrich_selected_author_contexts([r],config)
    r.raw['research_context']['checked_at']='changed'
    with pytest.raises(PendingResponse):enrich_selected_author_contexts([r],config)
    assert prompts[0]==prompts[1]


def test_reject_fabricated_pdf_excerpt_missing_claim_and_wrong_paper(tmp_path):
    _,r=setup(tmp_path);s=source(r)
    assert _validated_sources(r,[s])
    assert not _validated_sources(r,[{**s,'excerpt':'Invented statement'}])
    assert not _validated_sources(r,[{**s,'title':'Wrong identity'}])
    assert not _validated_sources(r,[{**s,'claims':[]}])


def test_review_cannot_approve_unknown_sources(tmp_path,monkeypatch):
    config,r=setup(tmp_path)
    def request(root,prompt,timeout,**kw):
        if kw['stage']=='author_research':return {'papers':[{'key':r.key,'sources':[source(r)]}]}
        payload=json.loads(prompt.split('\nREVIEW_INPUT:',1)[1])
        return {'input_sha256':digest(payload),'approved_source_hashes':['invented']}
    monkeypatch.setattr('daily_agent.author_research.request',request)
    with pytest.raises(ValueError,match='unknown author source'):enrich_selected_author_contexts([r],config)


def test_watchlist_candidates_do_not_assert_homonym_identity(tmp_path):
    config,r=setup(tmp_path)
    r.raw['research_context']=build_author_context(r.to_digest_item())
    update_watchlist(config.root/'data/author_context/watchlist.json',[r.to_digest_item()])
    assert watchlist_discovery_queries(config)==[]


def test_real_queue_resume_and_independent_review_are_stable(tmp_path):
    from daily_agent.parent_writer import pending,import_response
    config,r=setup(tmp_path)
    with pytest.raises(PendingResponse):enrich_selected_author_contexts([r],config)
    jobs=pending(tmp_path);assert len(jobs)==1 and jobs[0]['stage']=='author_research'
    import_response(tmp_path,jobs[0]['job_id'],{'papers':[{'key':r.key,'sources':[source(r)],'uncertainties':[]}]},'author-research-worker')
    with pytest.raises(PendingResponse):enrich_selected_author_contexts([r],config)
    jobs=pending(tmp_path);assert len(jobs)==1 and jobs[0]['stage']=='review'
    payload=json.loads(jobs[0]['prompt'].split('\nREVIEW_INPUT:',1)[1])
    answer={'input_sha256':digest(payload),'approved_source_hashes':[digest(source(r))],'uncertainties':{}}
    with pytest.raises(ValueError,match='Independent review'):
        import_response(tmp_path,jobs[0]['job_id'],answer,'author-research-worker')
    import_response(tmp_path,jobs[0]['job_id'],answer,'author-review-worker')
    enrich_selected_author_contexts([r],config)
    assert not pending(tmp_path)
    assert r.raw['author_research_sources']
    enrich_selected_author_contexts([r],config)
    assert len(list((tmp_path/'data/writer-queue').glob('*.job.json')))==2


def test_metadata_refresh_does_not_erase_verified_watchlist(tmp_path):
    config,r=setup(tmp_path)
    p=r.to_digest_item();p.raw['research_context']=build_author_context(p,[source(r)])
    path=tmp_path/'data/author_context/watchlist.json'
    update_watchlist(path,[p])
    p.raw['research_context']=build_author_context(p)
    update_watchlist(path,[p])
    assert watchlist_discovery_queries(config,1)==['"Alice Example" quantum']


def test_crossref_watchlist_replaces_existing_slots_and_keeps_window(tmp_path,monkeypatch):
    import httpx
    from datetime import datetime,timezone
    from daily_agent.connectors.crossref import fetch_crossref
    config,r=setup(tmp_path)
    config=replace(config,domains=config.domains[:1],sources={'crossref':{'enabled':True,'max_queries_per_domain':3}})
    p=r.to_digest_item();p.raw['research_context']=build_author_context(p,[source(r)])
    update_watchlist(tmp_path/'data/author_context/watchlist.json',[p])
    calls=[]
    def handler(request):
        calls.append(dict(request.url.params))
        return httpx.Response(200,json={'message':{'items':[]}})
    real=httpx.Client
    monkeypatch.setattr('daily_agent.connectors.crossref.httpx.Client',lambda **kw:real(transport=httpx.MockTransport(handler),**kw))
    fetch_crossref(config,datetime(2026,10,8,tzinfo=timezone.utc),window_days=92)
    assert len(calls)==3
    assert any('Alice Example' in c['query.bibliographic'] for c in calls)
    assert all('from-pub-date:2026-07-08' in c['filter'] for c in calls)
    assert 'Alice Example' not in calls[0]['query.bibliographic']


def test_correspondence_does_not_match_name_substrings(tmp_path):
    _,r=setup(tmp_path)
    r.authors=['Ali','Alice Example']
    r.paper_document['pages'][0]['text']='New Quantum Learning\nCorrespondence to: Alice Example'
    ev=explicit_pdf_evidence(r)
    assert [a['name'] for a in ev[0]['authors']]==['Alice Example']
