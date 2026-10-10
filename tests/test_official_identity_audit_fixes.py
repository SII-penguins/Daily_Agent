"""Regressions for the four independently reproduced formal-discovery defects."""
from copy import deepcopy

import httpx
import pytest

from daily_agent.connectors.official_metadata import MetadataCache
from daily_agent.connectors.pmlr import fetch_pmlr
from daily_agent.connectors.nature import fetch_nature
from daily_agent.connectors.source_failures import PartialSourceError
from daily_agent.models import DigestItem
from daily_agent.scoring.dedup import deduplicate_items
from daily_agent.scoring.publication import publication_evidence
from daily_agent import formal_discovery
import test_official_metadata_repair as p
import test_formal_nature_crossref_repair as n


def revised_index(cache, title):
    entry=cache.get(p.VOLUME,kind='index')
    payload=deepcopy(entry['payload'])
    payload['items'][0]['title']=title
    payload['items'][0]['raw']['index_evidence']['title']=title
    cache.put(p.VOLUME,payload,kind='index',status='index')


@pytest.mark.parametrize('initial_kind',['verified','metadata_missing','identity_mismatch'])
def test_corrected_pmlr_title_refetches_content_cache_once(tmp_path,monkeypatch,initial_kind):
    old='Quantum circuit learning old';new='Quantum circuit learning revised'
    first=p.landing(old)
    if initial_kind=='metadata_missing':first=first.split('<div id="abstract">')[0]
    if initial_kind=='identity_mismatch':first=p.landing('Unrelated title')
    pages={p.VOLUME:(200,p.index([old])),p.VOLUME+'p0.html':(200,first)}
    calls=p.transport(monkeypatch,pages);cfg=p.config(tmp_path);cache=MetadataCache(cfg,'pmlr')
    if initial_kind=='identity_mismatch':
        with pytest.raises(PartialSourceError):fetch_pmlr(cfg,p.TARGET)
    else:fetch_pmlr(cfg,p.TARGET)
    assert cache.get(p.VOLUME+'p0.html')['status']==initial_kind
    revised_index(cache,new);pages[p.VOLUME+'p0.html']=(200,p.landing(new))
    coverage={};[record]=fetch_pmlr(cfg,p.TARGET,coverage=coverage)
    assert calls==[p.VOLUME,p.VOLUME+'p0.html',p.VOLUME+'p0.html']
    assert record.title==new and record.raw['primary_landing_verified'] is True
    assert cache.get(record.url)['status']=='verified'
    assert coverage['identity_cache_misses']==1 and coverage['new_landing_requests']==1


def test_corrected_title_without_budget_never_overwrites_positive_cache(tmp_path,monkeypatch):
    old='Quantum circuit learning old';new='Quantum circuit learning revised'
    calls=p.transport(monkeypatch,{p.VOLUME:(200,p.index([old])),p.VOLUME+'p0.html':(200,p.landing(old))})
    cfg=p.config(tmp_path);fetch_pmlr(cfg,p.TARGET);cache=MetadataCache(cfg,'pmlr')
    previous=cache.get(p.VOLUME+'p0.html');revised_index(cache,new)
    cfg.sources['pmlr']['max_detail_pages']=0
    [record]=fetch_pmlr(cfg,p.TARGET)
    assert len(calls)==2 and record.raw['abstract_status']=='enrichment_pending'
    assert not record.raw.get('primary_landing_verified')
    assert cache.get(record.url)==previous


@pytest.mark.parametrize('status',[403,429])
def test_title_change_does_not_bypass_url_access_negative(tmp_path,monkeypatch,status):
    calls=p.transport(monkeypatch,{p.VOLUME:(200,p.index(['Quantum circuit learning old'])),p.VOLUME+'p0.html':(status,'blocked')})
    cfg=p.config(tmp_path)
    with pytest.raises(PartialSourceError):fetch_pmlr(cfg,p.TARGET)
    revised_index(MetadataCache(cfg,'pmlr'),'Quantum circuit learning revised')
    with pytest.raises(PartialSourceError):fetch_pmlr(cfg,p.TARGET)
    assert calls==[p.VOLUME,p.VOLUME+'p0.html']


def pair(arxiv_doi='10.5555/different-a',formal_doi='10.5555/formal-b'):
    a=DigestItem(id='arxiv',source='arxiv',item_type='paper',title='Quantum circuit learning',
        url='https://arxiv.org/abs/2601.00001',arxiv_id='2601.00001',doi=arxiv_doi,published_at='2026-01-01')
    b=DigestItem(id='pmlr',source='pmlr',item_type='paper',title=a.title,
        url='https://proceedings.mlr.press/v306/new.html',doi=formal_doi,published_at='2026-09-29',raw={'venue':'ICML 2026'})
    return a,b


@pytest.mark.parametrize('reverse',[False,True])
def test_title_year_collision_cannot_merge_conflicting_publication_dois(reverse):
    a,b=pair();rows=deduplicate_items([b,a] if reverse else [a,b])
    assert len(rows)==2
    preprint=next(row for row in rows if row.source=='arxiv')
    assert publication_evidence(preprint)['records']==[]
    assert 'pmlr' not in preprint.raw['evidence']['sources']
    assert publication_evidence(next(row for row in rows if row.source=='pmlr'))['status']=='published'


def test_conflicting_title_clusters_do_not_accept_doi_less_bridge():
    a,b=pair()
    unknown=DigestItem(id='unknown',source='openalex',item_type='paper',title=a.title,
        url='https://openalex.org/unknown',published_at='2026-08-01')
    duplicate=deepcopy(b);duplicate.source='crossref';duplicate.id='duplicate'
    rows=deduplicate_items([a,b,unknown,duplicate])
    assert len(rows)==3
    assert len([x for x in rows if x.doi=='10.5555/formal-b'])==1
    assert next(x for x in rows if x.source=='openalex').doi is None


@pytest.mark.parametrize('arxiv_doi',['10.48550/arxiv.2601.00001','https://doi.org/10.48550/arXiv.2601.00001','https://doi.org/10.5555/formal-b'])
def test_repository_doi_and_exact_normalized_final_doi_are_compatible(arxiv_doi):
    a,b=pair(arxiv_doi=arxiv_doi)
    [merged]=deduplicate_items([a,b])
    assert merged.source=='arxiv' and publication_evidence(merged)['status']=='published'


def test_preexisting_wrong_merge_is_quarantined_at_publication_boundary():
    a,b=pair()
    a.raw['evidence']={'sources':{'pmlr':{'title':b.title,'url':b.url,'doi':b.doi,'venue':'ICML 2026'}}}
    proof=publication_evidence(a)
    assert proof['records']==[] and proof['status']!='published'
    assert proof['identity_conflicts']['reason']=='conflicting_publication_doi'
    assert proof['metadata_records'][0]['reason']=='conflicting_publication_doi'


@pytest.mark.parametrize('article_type',['Editorial','Author Correction','News & Views'])
def test_nature_nonresearch_negative_cache_skips_repeated_seeds_and_pool(tmp_path,monkeypatch,article_type):
    cfg=n.config(tmp_path,metadata_cache_enabled=True,max_detail_pages=1)
    page=n.landing()+f'<meta name="citation_article_type" content="{article_type}">'
    calls=n.transport(monkeypatch,lambda request:httpx.Response(200,text=page,headers={'content-type':'text/html'}))
    assert fetch_nature(cfg,n.TARGET,discovery_seeds=[n.seed(cfg)])==[]
    entry=MetadataCache(cfg,'nature').get(n.URL)
    assert entry['status']=='nonresearch_item' and entry['payload']['publisher_article_type']==article_type
    assert fetch_nature(cfg,n.TARGET)==[]
    coverage={}
    assert fetch_nature(cfg,n.TARGET,discovery_seeds=[n.seed(cfg)],coverage=coverage)==[]
    assert len(calls)==1 and coverage['nonresearch_cached']==1 and coverage['requests']==0


def test_unverified_registry_date_is_only_handoff_hint(tmp_path,monkeypatch):
    cfg=n.config(tmp_path,metadata_cache_enabled=True)
    n.transport(monkeypatch,lambda request:httpx.Response(403))
    with pytest.raises(httpx.HTTPStatusError) as exc:fetch_nature(cfg,n.TARGET,discovery_seeds=[n.seed(cfg)])
    [lead]=exc.value.partial_items
    monkeypatch.setattr(formal_discovery,'collect_official_candidates',lambda *args:([lead],{}))
    [row],_=formal_discovery.export_candidate_handoff(cfg,n.TARGET,tmp_path/'output')
    assert row['recent_topic_eligible'] is False
    assert row['publication_date_status']=='date_unresolved'
    assert row['discovery_date_hint']=='2026-09-03'
    assert row['handoff_status']=='discovery_lead_requires_official_verification'
    assert 'publication' not in row


def test_verified_official_date_remains_eligible(tmp_path,monkeypatch):
    cfg=n.config(tmp_path)
    n.transport(monkeypatch,lambda request:httpx.Response(200,text=n.landing(),headers={'content-type':'text/html'}))
    [record]=fetch_nature(cfg,n.TARGET,discovery_seeds=[n.seed(cfg)])
    monkeypatch.setattr(formal_discovery,'collect_official_candidates',lambda *args:([record],{}))
    [row],_=formal_discovery.export_candidate_handoff(cfg,n.TARGET,tmp_path/'output')
    assert row['recent_topic_eligible'] is True and row['publication_date_status']=='official_verified'
