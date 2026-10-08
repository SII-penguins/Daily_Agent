"""Offline source-health contracts; no real credentials, network, or writer calls."""
import importlib
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from daily_agent.config import DomainConfig, load_config
from daily_agent.connectors.source_failures import PartialSourceError

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ['crossref', 'dblp', 'openalex', 'semantic_scholar', 'github', 'arxiv']
TARGET = datetime(2026, 5, 20, tzinfo=timezone.utc)
FEED = '''<feed xmlns="http://www.w3.org/2005/Atom"><entry>
<id>https://arxiv.org/abs/2605.00001v1</id><title>Quantum circuit learning</title>
<updated>2026-05-19T00:00:00Z</updated><published>2026-05-19T00:00:00Z</published>
<summary>A method.</summary></entry></feed>'''
PAYLOADS = {
    'crossref': {'message': {'items': [{'DOI': '10.1/test', 'title': ['Quantum circuit learning'], 'type': 'journal-article'}]}},
    'dblp': {'result': {'hits': {'hit': [{'info': {'key': 'conf/x/2026', 'title': 'Quantum circuit learning', 'year': '2026'}}]}}},
    'openalex': {'results': [{'id': 'https://openalex.org/W123', 'title': 'Quantum circuit learning', 'publication_date': '2026-05-19', 'type': 'article'}]},
    'semantic_scholar': {'data': [{'paperId': 'p1', 'title': 'Quantum circuit learning', 'publicationDate': '2026-05-19'}]},
    'github': {'items': [{'full_name': 'owner/repo', 'description': 'Quantum tools', 'updated_at': '2026-05-19T00:00:00Z'}]},
}


def setup_source(monkeypatch, name, statuses, *, malformed=False):
    module = importlib.import_module('daily_agent.connectors.' + name)
    cfg = load_config(ROOT)
    object.__setattr__(cfg, 'domains', [DomainConfig('quantum','quantum',1,'',['one','two'],[],[],['one','two'])])
    cfg.sources[name] = {'enabled': True, 'max_queries_per_domain': 2, 'max_results_per_query': 1,
                         'search_enabled': True, 'trending_enabled': False, 'request_delay_seconds': 0,
                         'rate_limit_retries': 0, 'recent_days': 7}
    if hasattr(module, 'credential_value'):
        monkeypatch.setattr(module, 'credential_value', lambda key: None)
    if hasattr(module, '_queries'):
        monkeypatch.setattr(module, '_queries', lambda domain: ['one', 'two'])
    calls=[]
    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, url, **kwargs):
            index = len(calls)
            calls.append(url)
            status = statuses[min(index, len(statuses)-1)]
            if malformed:
                return httpx.Response(status, text='<html>gateway</html>',request=httpx.Request('GET',url))
            if name == 'arxiv':
                return httpx.Response(status,text=FEED,request=httpx.Request('GET',url))
            return httpx.Response(status,json=PAYLOADS[name],request=httpx.Request('GET',url))
    monkeypatch.setattr(module.httpx,'Client',Client)
    return lambda: getattr(module, 'fetch_' + name)(cfg, TARGET), calls


@pytest.mark.parametrize('name', SOURCES)
@pytest.mark.parametrize('code', [401, 403, 429, 503])
def test_all_failed_discovery_never_looks_like_empty_success(monkeypatch, name, code):
    fetch, calls = setup_source(monkeypatch, name, [code])
    with pytest.raises(httpx.HTTPStatusError) as caught:
        fetch()
    assert caught.value.partial_items == []
    assert caught.value.failures and all(row['status_code']==code for row in caught.value.failures)
    if code in {401, 403, 429}:
        assert len(calls)==1


@pytest.mark.parametrize('name', SOURCES)
@pytest.mark.parametrize('code', [403, 429, 503])
def test_partial_discovery_preserves_items_and_exposes_failed_query(monkeypatch, name, code):
    fetch, calls = setup_source(monkeypatch, name, [200, code])
    with pytest.raises(PartialSourceError) as caught:
        fetch()
    assert len(caught.value.partial_items)==1
    assert caught.value.partial_success is True
    assert caught.value.failures[0]['status_code']==code


@pytest.mark.parametrize('name', SOURCES[:-1])
def test_malformed_json_is_visible_as_failure(monkeypatch, name):
    fetch, calls = setup_source(monkeypatch, name, [200], malformed=True)
    with pytest.raises(ValueError) as caught:
        fetch()
    assert caught.value.partial_items==[]
    assert caught.value.failures


@pytest.mark.parametrize('code', [401, 403, 429])
def test_arxiv_does_not_switch_hosts_after_denial_or_exhausted_rate_limit(monkeypatch, code):
    fetch, calls = setup_source(monkeypatch, 'arxiv', [code])
    with pytest.raises(httpx.HTTPStatusError): fetch()
    assert len(calls)==1 and calls[0].startswith('https://export.arxiv.org/')


def test_invalid_source_payload_is_not_retried_as_transient():
    from daily_agent.pipeline import _fetch_source
    from daily_agent.models import RunStatus
    calls=[]
    def fetch():
        calls.append(1)
        raise ValueError('malformed JSON')
    assert _fetch_source('bad',fetch,RunStatus())==[]
    assert len(calls)==1
