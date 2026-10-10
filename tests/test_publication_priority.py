from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

from daily_agent.models import DigestItem
from daily_agent.scoring.publication import publication_scores, publication_evidence
from daily_agent.scoring.dedup import deduplicate_items
from daily_agent.connectors.openreview import _note_to_item


def item(source='arxiv', **kwargs):
    return DigestItem(id=source, source=source, item_type='paper', title='Quantum circuit learning', url=kwargs.pop('url', 'https://example.org/'+source), **kwargs)


def test_preprint_doi_and_topic_tags_do_not_prove_publication():
    p=item(doi='10.48550/arxiv.1234',source_tags=['Nature','ICLR'])
    assert publication_scores(p)==(0,-10)
    p.raw={'venue':'Submitted to ICLR 2026'}
    assert publication_scores(p)==(0,-10)


def test_merged_publication_overrides_arxiv_penalty_in_both_orders():
    for reverse in [False,True]:
        a=item(arxiv_id='1234',doi='10.1234/x')
        b=item('pmlr',doi='10.1234/x',url='https://proceedings.mlr.press/v267/example25a.html',raw={'venue':'ICML 2025'})
        merged=deduplicate_items([b,a] if reverse else [a,b])[0]
        assert merged.arxiv_id=='1234'
        assert publication_scores(merged)==(14,6)


def test_openreview_submission_rejection_and_acceptance_are_distinct():
    for content,expected in [({},False),({'venue':{'value':'Submitted to ICLR 2026'}},False),
        ({'venueid':{'value':'ICLR.cc/2026/Conference/Rejected_Submission'}},False),
        ({'venueid':{'value':'ICLR.cc/2026/Conference'}},True)]:
        p=_note_to_item({'id':'x','content':{'title':{'value':'Quantum circuit learning'}, **content}},'ICLR 2026')
        assert (publication_evidence(p)['status']=='published') is expected


def test_official_proceedings_and_arxiv_journal_reference():
    assert publication_scores(item('pmlr',url='https://proceedings.mlr.press/v267/example25a.html',raw={'venue':'ICML 2025'}))==(14,6)
    assert publication_scores(item(raw={'journal_ref':'Nature Communications 17 (2026)'}))==(0,-10)
    assert publication_scores(item('crossref',raw={'venue':'Nature Physics','publication_type':'posted-content'}))==(0,0)


def test_material_round_trip_keeps_cross_source_publication_evidence():
    from daily_agent.models import MaterialRecord
    a=item(arxiv_id='1234')
    m=MaterialRecord.from_item(a)
    m.evidence['sources']['crossref']={'venue':'npj Quantum Information','publication_type':'journal-article'}
    assert publication_scores(m.to_digest_item())==(0,-10)
    proof=publication_evidence(m.to_digest_item())
    assert proof['status']=='metadata_only'
    assert proof['metadata_records'][0]['venue']=='npj Quantum Information'


def test_openreview_http_failure_is_visible(monkeypatch):
    import httpx,pytest
    from daily_agent.config import load_config
    from daily_agent.connectors.openreview import fetch_openreview
    cfg=load_config(PROJECT_ROOT)
    class Client:
        def __init__(self,**kwargs): pass
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def get(self,url,**kwargs): return httpx.Response(403,request=httpx.Request('GET',url))
    monkeypatch.setattr('daily_agent.connectors.openreview.httpx.Client',Client)
    with pytest.raises(httpx.HTTPStatusError): fetch_openreview(cfg)


def test_crossref_targets_configured_journal_and_filters_preprints(monkeypatch):
    import httpx
    from daily_agent.config import load_config
    from daily_agent.connectors.crossref import fetch_crossref
    cfg=load_config(PROJECT_ROOT)
    cfg.domains[:]=cfg.domains[:1]
    cfg.sources['crossref']={'enabled':True,'max_queries_per_domain':1,'preferred_queries_per_domain':1,
        'preferred_journals':[{'name':'Nature','issn':'1476-4687'}]}
    calls=[]
    def handler(request):
        calls.append(request.url)
        works=[{'DOI':'10.1/nature','title':['Quantum method'],'container-title':['Nature'],'type':'journal-article'},
               {'DOI':'10.1/preprint','title':['Preprint'],'type':'posted-content'}] if '/journals/' in request.url.path else []
        return httpx.Response(200,json={'message':{'items':works}})
    real=httpx.Client
    monkeypatch.setattr('daily_agent.connectors.crossref.httpx.Client',lambda **kwargs: real(transport=httpx.MockTransport(handler),**kwargs))
    results=fetch_crossref(cfg)
    assert len(results)==1 and results[0].doi=='10.1/nature'
    assert calls[0].path.endswith('/journals/1476-4687/works') and 'type:journal-article' in calls[0].params['filter']
    assert calls[1].path=='/works'


def test_indexed_arxiv_gets_same_preprint_penalty():
    assert publication_scores(item('openalex', doi='10.48550/arxiv.2609.00001')) == (0, -10)
    assert publication_scores(item('openalex', raw={'venue': 'arXiv (Cornell University)', 'publication_type': 'article'})) == (0, -10)


def test_repository_article_is_not_formal_publication():
    for venue in ['Zenodo (CERN European Organization for Nuclear Research)', 'Open Collections']:
        assert publication_scores(item('openalex', raw={'venue': venue, 'publication_type': 'article'})) == (0, 0)


def test_journal_reference_and_index_metadata_are_explicitly_unverified():
    for p in [item(raw={'journal_ref':'Nature 999 (2099)'}),
              item('crossref', raw={'venue':'Nature','publication_type':'journal-article'}),
              item('openalex', raw={'venue':'Nature','publication_type':'article'})]:
        proof=publication_evidence(p)
        assert proof['status']=='metadata_only'
        assert proof['verification']=='unverified_metadata'
        assert proof['records']==[]
        assert proof['metadata_records']
        assert publication_scores(p)[0]==0


def test_official_source_label_requires_canonical_official_paper_link():
    for url in ['https://example.org/paper.html',
                'https://proceedings.mlr.press.evil.example/v267/x.html',
                'https://proceedings.mlr.press/v267/',
                'https://evil@proceedings.mlr.press/v267/x.html']:
        proof=publication_evidence(item('pmlr',url=url,raw={'venue':'ICML 2025'}))
        assert proof['status']!='published'
    p=item('neurips',url='https://papers.nips.cc/paper_files/paper/2025/hash/abc-Abstract-Conference.html',raw={'venue':'NeurIPS 2025'})
    # Current proceedings include track suffixes in the official URL.
    assert publication_evidence(p)['verification']=='primary_source_record'


def test_openreview_partial_failures_preserve_items_and_each_failed_venue(monkeypatch):
    import httpx, pytest
    from daily_agent.config import load_config
    from daily_agent.connectors.openreview import fetch_openreview, OpenReviewPartialError
    cfg=load_config(PROJECT_ROOT)
    cfg.sources['openreview']={'enabled':True,'venues':[
        {'venueid':'denied401'},{'venueid':'allowed'},{'venueid':'denied403'}]}
    class Client:
        def __init__(self,**kwargs): pass
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def get(self,url,**kwargs):
            venue=kwargs['params']['content.venueid']
            status=401 if venue=='denied401' else 403 if venue=='denied403' else 200
            return httpx.Response(status,request=httpx.Request('GET',url),json={'notes':[
                {'id':'paper','content':{'title':{'value':'Quantum circuit learning'}}}]})
    monkeypatch.setattr('daily_agent.connectors.openreview.httpx.Client',Client)
    with pytest.raises(OpenReviewPartialError) as caught:
        fetch_openreview(cfg)
    exc=caught.value
    assert len(exc.partial_items)==1
    assert [row['status_code'] for row in exc.failures]==[401,403]
    assert 'denied401' in str(exc) and 'denied403' in str(exc)
    from daily_agent.models import RunStatus
    from daily_agent.pipeline import _fetch_source
    status=RunStatus()
    calls=[]
    def fetch():
        calls.append(1)
        raise exc
    result=_fetch_source('OpenReview',fetch,status)
    assert len(result)==1 and len(calls)==1
    assert status.sources[0].ok is False and status.sources[0].item_count==1
    assert status.errors and '403' in status.errors[0]


def test_openreview_all_failed_retains_http_error_and_all_venue_failures(monkeypatch):
    import httpx, pytest
    from daily_agent.config import load_config
    from daily_agent.connectors.openreview import fetch_openreview
    cfg=load_config(PROJECT_ROOT)
    cfg.sources['openreview']={'enabled':True,'venues':[{'venueid':'one'},{'venueid':'two'}]}
    class Client:
        def __init__(self,**kwargs): pass
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def get(self,url,**kwargs):
            return httpx.Response(403,request=httpx.Request('GET',url))
    monkeypatch.setattr('daily_agent.connectors.openreview.httpx.Client',Client)
    with pytest.raises(httpx.HTTPStatusError) as caught:
        fetch_openreview(cfg)
    assert len(caught.value.failures)==2
    assert 'one' in str(caught.value) and 'two' in str(caught.value)


def test_source_check_partial_result_is_not_healthy(monkeypatch):
    from datetime import datetime, timezone
    from daily_agent.config import load_config
    from daily_agent.connectors.openreview import OpenReviewPartialError
    from daily_agent import source_check
    cfg=load_config(PROJECT_ROOT)
    cfg.sources['openreview']={'enabled':True}
    failure=OpenReviewPartialError([item('openreview')],[{'venue':'denied','status_code':403,'message':'HTTP 403'}])
    def fetch(*args, **kwargs):
        raise failure
    monkeypatch.setattr(source_check,'_source_specs',lambda:[{'key':'openreview','name':'OpenReview','fetcher':fetch}])
    result=source_check.run_source_check(cfg, datetime.now(timezone.utc))[0]
    assert result.ok is False and result.skipped is False
    assert result.item_count==1 and result.samples==['Quantum circuit learning']
    assert '403' in result.error


def test_openreview_forum_link_and_venue_label_without_acceptance_are_not_publication():
    for status in ['', 'submitted', 'rejected', 'withdrawn', 'unconfirmed']:
        p=item('openreview',url='https://openreview.net/forum?id=x',
               raw={'venue':'ICLR 2026','publication_status':status})
        assert publication_evidence(p)['status']!='published'
    p=item('openreview',url='https://openreview.net.evil.example/forum?id=x',
           raw={'venue':'ICLR 2026','publication_status':'accepted'})
    assert publication_evidence(p)['status']!='published'


def test_primary_record_must_match_paper_title_after_merging():
    p=item(arxiv_id='2601.00001',raw={'evidence':{'sources':{
        'pmlr':{'venue':'ICML 2025','url':'https://proceedings.mlr.press/v267/x.html',
                'title':'An unrelated paper'}}}})
    assert publication_evidence(p)['status']!='published'
    p.raw['evidence']['sources']['pmlr']['title']=p.title
    assert publication_evidence(p)['verification']=='primary_source_record'


def test_generic_publisher_landing_page_never_verifies_journal_reference():
    p=item(url='https://www.nature.com/',raw={'journal_ref':'Nature 999 (2099)'})
    proof=publication_evidence(p)
    assert proof['status']=='metadata_only' and proof['records']==[]
