from daily_agent.models import DigestItem
from daily_agent.scoring.publication import publication_scores, publication_evidence
from daily_agent.scoring.dedup import deduplicate_items
from daily_agent.connectors.openreview import _note_to_item


def item(source='arxiv', **kwargs):
    return DigestItem(id=source, source=source, item_type='paper', title='Quantum circuit learning', url='https://example.org/'+source, **kwargs)


def test_preprint_doi_and_topic_tags_do_not_prove_publication():
    p=item(doi='10.48550/arxiv.1234',source_tags=['Nature','ICLR'])
    assert publication_scores(p)==(0,-10)
    p.raw={'venue':'Submitted to ICLR 2026'}
    assert publication_scores(p)==(0,-10)


def test_merged_publication_overrides_arxiv_penalty_in_both_orders():
    for reverse in [False,True]:
        a=item(arxiv_id='1234',doi='10.1234/x')
        b=item('crossref',doi='10.1234/x',raw={'venue':'Nature Physics','publication_type':'journal-article'})
        merged=deduplicate_items([b,a] if reverse else [a,b])[0]
        assert merged.arxiv_id=='1234'
        assert publication_scores(merged)==(14,6)


def test_openreview_submission_rejection_and_acceptance_are_distinct():
    for content,expected in [({},False),({'venue':{'value':'Submitted to ICLR 2026'}},False),
        ({'venueid':{'value':'ICLR.cc/2026/Conference/Rejected_Submission'}},False),
        ({'venueid':{'value':'ICLR.cc/2026/Conference'}},True)]:
        p=_note_to_item({'id':'x','content':content},'ICLR 2026')
        assert (publication_evidence(p)['status']=='published') is expected


def test_official_proceedings_and_arxiv_journal_reference():
    assert publication_scores(item('pmlr',raw={'venue':'ICML 2025'}))==(14,6)
    assert publication_scores(item(raw={'journal_ref':'Nature Communications 17 (2026)'}))==(14,6)
    assert publication_scores(item('crossref',raw={'venue':'Nature Physics','publication_type':'posted-content'}))==(0,0)


def test_material_round_trip_keeps_cross_source_publication_evidence():
    from daily_agent.models import MaterialRecord
    a=item(arxiv_id='1234')
    m=MaterialRecord.from_item(a)
    m.evidence['sources']['crossref']={'venue':'npj Quantum Information','publication_type':'journal-article'}
    assert publication_scores(m.to_digest_item())==(14,6)


def test_openreview_http_failure_is_visible(monkeypatch):
    import httpx,pytest
    from daily_agent.config import load_config
    from daily_agent.connectors.openreview import fetch_openreview
    cfg=load_config('/Users/wuzixie/Daily_Agent')
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
    cfg=load_config('/Users/wuzixie/Daily_Agent')
    cfg.domains[:]=cfg.domains[:1]
    cfg.sources['crossref']={'enabled':True,'max_queries_per_domain':1,'preferred_queries_per_domain':1,
        'preferred_journals':[{'name':'Nature','issn':'1476-4687'}]}
    calls=[]
    class Client:
        def __init__(self,**kwargs): pass
        def __enter__(self): return self
        def __exit__(self,*args): pass
        def get(self,url,**kwargs):
            calls.append((url,kwargs['params']))
            works=[{'DOI':'10.1/nature','title':['Quantum method'],'container-title':['Nature'],'type':'journal-article'},
                   {'DOI':'10.1/preprint','title':['Preprint'],'type':'posted-content'}] if '/journals/' in url else []
            return httpx.Response(200,request=httpx.Request('GET',url),json={'message':{'items':works}})
    monkeypatch.setattr('daily_agent.connectors.crossref.httpx.Client',Client)
    results=fetch_crossref(cfg)
    assert len(results)==1 and results[0].doi=='10.1/nature'
    assert calls[1][0].endswith('/journals/1476-4687/works') and 'type:journal-article' in calls[1][1]['filter']


def test_indexed_arxiv_gets_same_preprint_penalty():
    assert publication_scores(item('openalex', doi='10.48550/arxiv.2609.00001')) == (0, -10)
    assert publication_scores(item('openalex', raw={'venue': 'arXiv (Cornell University)', 'publication_type': 'article'})) == (0, -10)


def test_repository_article_is_not_formal_publication():
    for venue in ['Zenodo (CERN European Organization for Nuclear Research)', 'Open Collections']:
        assert publication_scores(item('openalex', raw={'venue': venue, 'publication_type': 'article'})) == (0, 0)
