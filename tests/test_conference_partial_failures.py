from pathlib import Path
from contextlib import contextmanager
from datetime import datetime, timezone

import httpx
import pytest

from daily_agent.config import load_config
from daily_agent.connectors.source_failures import PartialSourceError
from daily_agent.connectors.pmlr import fetch_pmlr
from daily_agent.connectors.neurips import fetch_neurips

ROOT = Path(__file__).resolve().parents[1]


def fake_client(monkeypatch, module, responder):
    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, url, **kwargs):
            status, body = responder(url)
            return httpx.Response(status, text=body, request=httpx.Request('GET', url))
        @contextmanager
        def stream(self, method, url, **kwargs):
            response = self.get(url)
            response.headers["content-type"] = "text/html"
            yield response
    monkeypatch.setattr(f'daily_agent.connectors.{module}.httpx.Client', Client)


def test_pmlr_mixed_volume_results_preserve_success_and_denial(monkeypatch):
    cfg = load_config(ROOT)
    cfg.sources['pmlr'] = {'enabled': True, 'max_detail_pages': 0, 'volumes': [
        {'url': 'https://proceedings.mlr.press/v267/', 'venue': 'ICML 2025'},
        {'url': 'https://proceedings.mlr.press/v999/', 'venue': 'Other'}]}
    body = '<title>Published as Volume 267 by PMLR on 29 September 2026</title><div class="paper"><p class="title">Quantum circuit learning</p><a href="x25.html">paper</a></div>'
    fake_client(monkeypatch, 'pmlr', lambda url: (200, body) if '/v267/' in url else (403, ''))
    with pytest.raises(PartialSourceError) as caught:
        fetch_pmlr(cfg, datetime(2026, 10, 10, tzinfo=timezone.utc))
    assert len(caught.value.partial_items) == 1
    assert caught.value.failures[0]['status_code'] == 403
    assert '/v999/' in str(caught.value)


def test_pmlr_all_volumes_failed_is_not_empty_success(monkeypatch):
    cfg = load_config(ROOT)
    cfg.sources['pmlr'] = {'enabled': True, 'max_detail_pages': 0, 'volumes': [{'url': 'https://proceedings.mlr.press/v1/'}]}
    fake_client(monkeypatch, 'pmlr', lambda url: (403, ''))
    with pytest.raises(httpx.HTTPStatusError) as caught:
        fetch_pmlr(cfg)
    assert caught.value.partial_items == []
    assert caught.value.failures[0]['status_code'] == 403


def test_neurips_failed_detail_preserves_proceedings_stub_and_denial(monkeypatch):
    cfg = load_config(ROOT)
    cfg.sources['neurips'] = {'enabled': True, 'years': [2025], 'max_results_per_year': 1}
    page = '/paper_files/paper/2025/hash/abc-Abstract-Conference.html'
    body = f'<a href="{page}">Quantum circuit learning</a>'
    fake_client(monkeypatch, 'neurips', lambda url: (403, '') if 'Abstract' in url else (200, body))
    with pytest.raises(PartialSourceError) as caught:
        fetch_neurips(cfg)
    assert len(caught.value.partial_items) == 1
    assert caught.value.partial_items[0].abstract is None
    assert caught.value.failures[0]['target'].endswith(page)
    assert caught.value.failures[0]['status_code'] == 403


def test_neurips_mixed_years_preserve_all_failures(monkeypatch):
    cfg = load_config(ROOT)
    cfg.sources['neurips'] = {'enabled': True, 'years': [2023, 2024, 2025]}
    fake_client(monkeypatch, 'neurips', lambda url: (200, '') if url.endswith('2024') else (503, ''))
    with pytest.raises(PartialSourceError) as caught:
        fetch_neurips(cfg)
    assert caught.value.partial_items == []
    assert len(caught.value.failures) == 2
    assert '2023' in str(caught.value) and '2025' in str(caught.value)


def test_neurips_all_failed_retains_http_identity(monkeypatch):
    cfg = load_config(ROOT)
    cfg.sources['neurips'] = {'enabled': True, 'years': [2025]}
    fake_client(monkeypatch, 'neurips', lambda url: (401, ''))
    with pytest.raises(httpx.HTTPStatusError) as caught:
        fetch_neurips(cfg)
    assert caught.value.response.status_code == 401
    assert len(caught.value.failures) == 1


@pytest.mark.parametrize('status_code,expected_calls', [(403, 1), (503, 2)])
def test_total_failure_retries_only_transient_http_status(monkeypatch, status_code, expected_calls):
    from daily_agent.pipeline import _fetch_source
    from daily_agent.models import RunStatus
    from daily_agent.connectors.source_failures import finish_collection, failure_record
    calls=[]
    def fetch():
        calls.append(1)
        response=httpx.Response(status_code,request=httpx.Request('GET','https://example.org'))
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            return finish_collection('source',[],[failure_record('target',exc)],[exc],0)
    status=RunStatus()
    assert _fetch_source('source', fetch, status)==[]
    assert len(calls)==expected_calls
    assert status.sources[0].ok is False
    assert status.sources[0].retries==expected_calls-1
