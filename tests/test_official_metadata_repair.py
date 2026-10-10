from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
import json

import httpx
import pytest

from daily_agent.config import load_config
from daily_agent.connectors.official_metadata import (
    MetadataCache, MetadataBudget, MetadataLimit, discovery_bounds,
    exact_date_in_window, fetch_text,
)
from daily_agent.connectors.pmlr import fetch_pmlr
from daily_agent.connectors.source_failures import PartialSourceError
from daily_agent.workflow_state import StateCorrupt

ROOT = Path(__file__).resolve().parents[1]
VOLUME = 'https://proceedings.mlr.press/v306/'
TARGET = datetime(2026, 10, 10, tzinfo=timezone.utc)


def config(tmp_path, **values):
    cfg = replace(load_config(ROOT), root=tmp_path)
    cfg.sources['pmlr'] = dict(enabled=True, metadata_cache_enabled=True,
        volumes=[{'url': VOLUME, 'venue': 'ICML 2026'}], max_results_per_volume=2,
        max_detail_pages=4, **values)
    return cfg


def index(titles, published='29 September 2026'):
    return f'<title>Published as Volume 306 by PMLR on {published}</title>' + ''.join(
        f'<div class="paper"><p class="title">{title}</p><a href="p{i}.html">abs</a></div>'
        for i, title in enumerate(titles))


def landing(title, abstract='Quantum machine learning with quantum neural networks.'):
    return (f'<meta name="citation_title" content="{title}">'
            '<meta name="citation_conference_title" content="International Conference on Machine Learning">'
            '<meta name="citation_publication_date" content="2026/09/29">'
            f'<div id="abstract"><p>{abstract}</p><p>Nested <b>public</b> abstract.</p></div>')


def transport(monkeypatch, pages):
    calls = []
    real = httpx.Client
    def handler(request):
        calls.append(str(request.url))
        status, body = pages[str(request.url)]
        return httpx.Response(status, text=body, headers={'content-type':'text/html'}, request=request)
    monkeypatch.setattr('daily_agent.connectors.pmlr.httpx.Client',
                        lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs))
    return calls


def test_complete_index_before_cap_and_fair_quantum_lookup(tmp_path, monkeypatch):
    titles = ['World model agent'] * 245 + ['Quantum circuit learning', 'Neural quantum states']
    pages = {VOLUME:(200,index(titles))}
    pages.update({VOLUME+f'p{i}.html':(200,landing(title)) for i,title in enumerate(titles)})
    calls = transport(monkeypatch,pages)
    cfg = config(tmp_path)
    coverage = {}
    items = fetch_pmlr(cfg,TARGET,coverage=coverage)
    assert coverage['discovered'] == 247
    assert VOLUME+'p245.html' in calls
    assert VOLUME+'p246.html' in calls
    assert len(calls) == 5
    assert len(items) == 2
    assert all(item.raw['primary_landing_verified'] for item in items)
    assert any('Nested public abstract' in (item.abstract or '') for item in items)
    cached = MetadataCache(cfg,'pmlr').get(VOLUME,kind='index')
    assert len(cached['payload']['items']) == 247
    assert coverage['pending'] > 0 and coverage['complete'] is False


def test_cache_reuses_success_negative_and_advances_tail(tmp_path, monkeypatch):
    titles = ['Quantum circuit learning one','Quantum circuit learning two','Quantum circuit learning three']
    pages = {VOLUME:(200,index(titles)),VOLUME+'p0.html':(200,landing(titles[0])),
             VOLUME+'p1.html':(404,'Not found'), VOLUME+'p2.html':(200,landing(titles[2]))}
    calls = transport(monkeypatch,pages)
    cfg = config(tmp_path); cfg.sources['pmlr']['max_detail_pages'] = 2
    with pytest.raises(PartialSourceError):
        fetch_pmlr(cfg,TARGET)
    assert calls == [VOLUME,VOLUME+'p0.html',VOLUME+'p1.html']
    with pytest.raises(PartialSourceError) as caught:
        fetch_pmlr(cfg,datetime(2026,10,11,tzinfo=timezone.utc))
    items = caught.value.partial_items
    assert caught.value.failures[0]['cached'] is True
    assert calls == [VOLUME,VOLUME+'p0.html',VOLUME+'p1.html',VOLUME+'p2.html']
    assert MetadataCache(cfg,'pmlr').get(VOLUME+'p1.html')['status'] == 'not_found'
    assert any(item.raw.get('metadata_cache_hit') for item in items)
    assert all('paper_document' not in item.raw and 'paper_text_status' not in item.raw for item in items)


def test_old_and_unknown_dates_do_not_become_recent(tmp_path,monkeypatch):
    calls=transport(monkeypatch,{VOLUME:(200,index(['Quantum circuit learning'],'9 July 2026'))})
    assert fetch_pmlr(config(tmp_path),TARGET)==[]
    assert calls==[VOLUME]
    assert discovery_bounds(config(tmp_path),TARGET,365)==(date(2026,7,10),date(2026,10,10))
    assert not exact_date_in_window('2026',(date(2026,7,10),date(2026,10,10)))
    assert not exact_date_in_window('2026-07-09',(date(2026,7,10),date(2026,10,10)))
    assert exact_date_in_window('2026-07-10',(date(2026,7,10),date(2026,10,10)))
    assert discovery_bounds(config(tmp_path),date(2026,5,31))[0]==date(2026,2,28)


def test_corrupt_cache_fails_closed_and_negative_is_not_verified(tmp_path):
    cfg=config(tmp_path);cache=MetadataCache(cfg,'pmlr')
    cache.put(VOLUME+'p.html',{'reason':'denied'},status='blocked')
    entry=cache.get(VOLUME+'p.html');assert entry['expires_at'] is None
    path=next(cache.root.glob('*.json'));value=json.loads(path.read_text());value['status']='verified'
    path.write_text(json.dumps(value))
    with pytest.raises(StateCorrupt):cache.get(VOLUME+'p.html')
    cache.put(VOLUME+'pending.html',{},status='budget_exhausted')
    assert cache.get(VOLUME+'pending.html') is None


def test_streaming_budgets_and_external_redirect_are_not_followed():
    calls=[]
    def handler(request):
        calls.append(str(request.url))
        if request.url.path=='/redirect':return httpx.Response(302,headers={'location':'https://evil.test/'})
        return httpx.Response(200,content=b'x'*100)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(MetadataLimit):
            fetch_text(client,'https://example.org/large',MetadataBudget(),50,lambda url:None)
        budget=MetadataBudget(max_requests=0)
        with pytest.raises(MetadataLimit):
            fetch_text(client,'https://example.org/never',budget,1000,lambda url:None)
        with pytest.raises(httpx.HTTPStatusError):
            fetch_text(client,'https://example.org/redirect',MetadataBudget(),1000,lambda url:None)
    assert calls==['https://example.org/large','https://example.org/redirect']


def test_identity_mismatch_does_not_supply_abstract(tmp_path,monkeypatch):
    title='Quantum circuit learning'
    transport(monkeypatch,{VOLUME:(200,index([title])),VOLUME+'p0.html':(200,landing('Different paper'))})
    with pytest.raises(PartialSourceError) as exc:
        fetch_pmlr(config(tmp_path),TARGET)
    item=exc.value.partial_items[0]
    assert item.abstract is None
    assert item.raw['abstract_status']=='identity_mismatch'
    assert item.raw['official_metadata_status']=='official_index_verified'


def test_pmlr_access_denial_stops_other_landing_requests(tmp_path,monkeypatch):
    titles=['Quantum circuit learning one','Quantum circuit learning two']
    calls=transport(monkeypatch,{VOLUME:(200,index(titles)),VOLUME+'p0.html':(403,'Forbidden')})
    cfg=config(tmp_path)
    for _ in range(2):
        with pytest.raises(PartialSourceError):fetch_pmlr(cfg,TARGET)
    assert calls==[VOLUME,VOLUME+'p0.html']


def test_unknown_volume_day_can_be_resolved_by_official_landing(tmp_path,monkeypatch):
    title='Quantum circuit learning'
    html=index([title]).replace('29 September 2026','date unavailable')
    transport(monkeypatch,{VOLUME:(200,html),VOLUME+'p0.html':(200,landing(title))})
    [paper]=fetch_pmlr(config(tmp_path),TARGET)
    assert paper.published_at=='2026-09-29'
    assert paper.raw['publication_date_basis']=='official_landing_citation'


def test_metadata_deadline_stops_before_a_request(monkeypatch):
    monkeypatch.setattr('daily_agent.connectors.official_metadata.time.monotonic',lambda:100)
    budget=MetadataBudget(seconds=1)
    monkeypatch.setattr('daily_agent.connectors.official_metadata.time.monotonic',lambda:102)
    with pytest.raises(MetadataLimit,match='time_budget'):budget.reserve()
    assert budget.requests==0


def test_rate_limit_retains_longer_retry_after():
    from daily_agent.connectors.official_metadata import failure_ttl
    response=httpx.Response(429,headers={'retry-after':'86400'},request=httpx.Request('GET',VOLUME))
    try:response.raise_for_status()
    except httpx.HTTPStatusError as exc:assert failure_ttl(exc)==86400
