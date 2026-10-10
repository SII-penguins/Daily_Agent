"""Offline regressions for bounded journal leads and strict publisher enrichment."""
from dataclasses import replace
from datetime import datetime, timezone
from html import escape
from pathlib import Path

import httpx
import pytest

from daily_agent.config import load_config
from daily_agent.connectors.crossref import fetch_crossref, fetch_preferred_journals, _work_to_item
from daily_agent.connectors.nature import (fetch_nature, _nature_seed_from_crossref,
    _parse_public_abstract, _parse_feed, _verify_landing)
from daily_agent.connectors.official_metadata import MetadataBudget, MetadataCache
from daily_agent.connectors.source_failures import PartialSourceError

ROOT = Path(__file__).resolve().parents[1]
TARGET = datetime(2026, 10, 10, tzinfo=timezone.utc)
DOI = '10.1038/s41534-026-01234-5'
URL = 'https://www.nature.com/articles/s41534-026-01234-5'
TITLE = 'Quantum circuit learning with tensor networks'
VENUE = 'npj Quantum Information'
FEED = 'https://www.nature.com/npjqi.rss'


def config(tmp_path=None, **nature):
    cfg = load_config(ROOT)
    cfg.sources['nature'] = {'enabled': True, 'feeds': [], 'max_detail_pages': 24,
                             'max_articles_per_feed': 20, **nature}
    cfg.sources['crossref'] = {'enabled': True, 'max_queries_per_domain': 1,
        'preferred_max_requests': 16, 'preferred_queries_per_domain': 1,
        'preferred_journals': [{'name': VENUE, 'issn': '2056-6387'}]}
    cfg.sources['selection'] = {'topic_relevance_gate_enabled': False}
    return replace(cfg, root=tmp_path) if tmp_path else cfg


def work(**changes):
    return {'DOI': DOI, 'title': [TITLE], 'URL': 'https://doi.org/' + DOI,
            'type': 'journal-article', 'container-title': [VENUE],
            'published-online': {'date-parts': [[2026, 9, 3]]}, **changes}


def seed(cfg, **changes):
    return _work_to_item(work(**changes), cfg.domains[0])


def landing(title=TITLE, venue=VENUE, doi=DOI, date='2026-09-03', version=None,
            abstract='<section aria-labelledby="Abs1"><h2 id="Abs1">Abstract</h2><div id="Abs1-content"><p>We learn <b>quantum circuits</b> with tensor networks.</p></div></section>'):
    values = {'citation_title': title, 'citation_journal_title': venue,
              'citation_doi': doi, 'citation_online_date': date, 'citation_version': version}
    return '<html><head><link rel="canonical" href="' + URL + '">' + ''.join(
        f'<meta name="{key}" content="{escape(value)}">' for key, value in values.items() if value
    ) + '</head><body>' + abstract + '<section><h2>Introduction</h2><p>Full body is not an abstract.</p></section></body></html>'


def transport(monkeypatch, handler):
    calls = []
    real = httpx.Client
    def wrapped(request):
        calls.append(request)
        return handler(request)
    monkeypatch.setattr(httpx, 'Client', lambda **kwargs: real(transport=httpx.MockTransport(wrapped), **kwargs))
    return calls


def test_preferred_pass_survives_later_broad_429(monkeypatch):
    cfg = config()
    cfg.domains[:] = cfg.domains[:1]
    calls = transport(monkeypatch, lambda request: httpx.Response(200, json={'message': {'items': [work()]}})
                      if request.url.path.startswith('/journals/') else httpx.Response(429))
    with pytest.raises(PartialSourceError) as caught:
        fetch_crossref(cfg, TARGET)
    assert [request.url.path for request in calls] == ['/journals/2056-6387/works', '/works']
    [item] = caught.value.partial_items
    assert item.doi == DOI and item.raw['discovery_source'] == 'crossref_preferred_journal'
    assert not item.raw.get('primary_landing_verified')
    assert caught.value.failures[0]['status_code'] == 429


@pytest.mark.parametrize('status', [401, 403, 429])
def test_journal_host_denial_stops_all_broad_and_other_journal_queries(monkeypatch, status):
    cfg = config()
    calls = transport(monkeypatch, lambda request: httpx.Response(status))
    coverage = {}
    with pytest.raises(httpx.HTTPStatusError):
        fetch_crossref(cfg, TARGET, preferred_coverage=coverage)
    assert len(calls) == 1 and calls[0].url.path.startswith('/journals/')
    assert coverage['host_blocked'] is True


def test_preferred_queries_are_capped_interleaved_and_calendar_exact(monkeypatch):
    cfg = config()
    cfg.sources['crossref'].update(preferred_max_requests=3, preferred_recent_days=500,
        preferred_journals=[{'issn': '2056-6387'}, {'issn': '1476-4687'}])
    calls = transport(monkeypatch, lambda request: httpx.Response(200, json={'message': {'items': []}}))
    coverage = {}
    assert fetch_preferred_journals(cfg, TARGET, window_days=500, coverage=coverage) == []
    assert len(calls) == coverage['requests'] == 3
    assert [request.url.path for request in calls[:2]] == ['/journals/2056-6387/works', '/journals/1476-4687/works']
    assert all(request.url.params['filter'] == 'from-pub-date:2026-07-10,until-pub-date:2026-10-10,type:journal-article' for request in calls)
    assert coverage['complete'] is False and coverage['partial_recall'] is True
    assert coverage['reason'] == 'request_limit'


def test_supplied_preferred_leads_are_not_fetched_twice(monkeypatch):
    cfg = config()
    cfg.domains[:] = cfg.domains[:1]
    calls = transport(monkeypatch, lambda request: httpx.Response(200, json={'message': {'items': []}}))
    [item] = fetch_crossref(cfg, TARGET, preferred_items=[seed(cfg)], preferred_coverage={})
    assert item.doi == DOI
    assert [request.url.path for request in calls] == ['/works']


def test_supplied_preferred_host_cooldown_prevents_network(monkeypatch):
    cfg = config()
    calls = transport(monkeypatch, lambda request: pytest.fail('No request after rate limit'))
    assert fetch_crossref(cfg, TARGET, preferred_items=[], preferred_coverage={'host_blocked': True}) == []
    assert calls == []


def test_crossref_deposit_created_date_cannot_be_a_publication_date():
    cfg = config()
    item = seed(cfg, **{'published-online': None, 'created': {'date-parts': [[2026, 9, 3]]}})
    assert item.published_at is None and item.raw['publication_date_basis'] is None


def test_journal_seed_outside_rolling_rss_is_verified_using_public_abstract(monkeypatch):
    cfg = config()
    calls = transport(monkeypatch, lambda request: httpx.Response(200, text=landing(), headers={'content-type': 'text/html'}))
    coverage = {}
    [item] = fetch_nature(cfg, TARGET, discovery_seeds=[seed(cfg)], coverage=coverage)
    assert [str(request.url) for request in calls] == [URL]
    assert item.raw['primary_landing_verified'] is True and item.published_at == '2026-09-03'
    assert item.abstract == 'We learn quantum circuits with tensor networks.'
    assert item.raw['abstract_source'] == 'publisher_public_abstract_section'
    assert '<div id="Abs1-content">' in item.raw['abstract_evidence_region']
    assert 'Introduction' not in item.raw['abstract_evidence_region']
    assert item.raw['primary_metadata_response_sha256']
    assert item.pdf_url is None and 'paper_document' not in item.raw
    assert coverage['archive_status'] == 'archive_not_validated' and coverage['complete'] is False


@pytest.mark.parametrize('change', [
    {'DOI': '10.1000/not-nature'}, {'DOI': '10.1038/../../evil'},
    {'type': 'posted-content'}, {'container-title': ['Unknown Journal']},
    {'URL': 'https://www.nature.com/articles/s41534-026-99999-9'},
])
def test_invalid_seed_is_not_requested(monkeypatch, change):
    cfg = config()
    calls = transport(monkeypatch, lambda request: pytest.fail('Invalid seed was requested'))
    assert fetch_nature(cfg, TARGET, discovery_seeds=[seed(cfg, **change)]) == []
    assert calls == []


@pytest.mark.parametrize('page', [landing(title='Wrong title'), landing(doi='10.1038/s41534-026-99999-9'),
    landing(venue='Nature Physics'), landing(date=None), landing(date='2026-09'), landing(doi=None)])
def test_registry_seed_never_promotes_mismatched_or_missing_primary_fields(monkeypatch, page):
    cfg = config()
    transport(monkeypatch, lambda request: httpx.Response(200, text=page, headers={'content-type': 'text/html'}))
    with pytest.raises(ValueError) as caught:
        fetch_nature(cfg, TARGET, discovery_seeds=[seed(cfg)])
    [item] = caught.value.partial_items
    assert item.raw['primary_landing_verified'] is False


@pytest.mark.parametrize('version,expected', [('Accepted manuscript', 'accepted_manuscript'),
    ('Version of Record', 'version_of_record'), ('Advance online publication', 'advance_online_publication'),
    (None, 'unspecified')])
def test_explicit_publication_stage_is_preserved_without_inference(version, expected):
    cfg = config()
    candidate = _nature_seed_from_crossref(seed(cfg))
    item = _verify_landing(candidate, landing(version=version), URL)
    assert item.raw['publication_stage'] == expected
    assert item.raw['publication_stage_evidence'] == version


@pytest.mark.parametrize('body', [
    '<meta property="og:description" content="A social snippet"><p>A general body paragraph</p>',
    '<section hidden><h2>Abstract</h2><p>Hidden text</p></section>',
    '<div style="display:none"><section><h2>Abstract</h2><p>Hidden text</p></section></div>',
    '<script>const t="<div id=abstract>Not public</div>"</script>',
])
def test_nonpublic_regions_and_social_snippets_do_not_supply_abstract(body):
    assert _parse_public_abstract(body) == (None, None, None)


def test_explicit_abstract_section_handles_nested_markup_only():
    text, region, source = _parse_public_abstract('<section><h2>Abstract</h2><p>A <i>nested</i> public abstract.</p></section><p>Unrelated body.</p>')
    assert text == 'A nested public abstract.'
    assert region.endswith('</section>') and 'Unrelated' not in region
    assert source == 'publisher_public_abstract_section'


def test_citation_abstract_is_public_metadata_without_body_substitution():
    text, region, source = _parse_public_abstract('<meta name="citation_abstract" content="A citation abstract."><meta name="description" content="A different snippet">')
    assert text == 'A citation abstract.' and 'citation_abstract' in region
    assert source == 'publisher_landing_citation_metadata'


def test_nature_cache_reused_across_days_and_current_window_reapplied(tmp_path, monkeypatch):
    cfg = config(tmp_path, metadata_cache_enabled=True)
    calls = transport(monkeypatch, lambda request: httpx.Response(200, text=landing(), headers={'content-type': 'text/html'}))
    [first] = fetch_nature(cfg, TARGET, discovery_seeds=[seed(cfg)])
    [second] = fetch_nature(cfg, datetime(2026, 10, 11, tzinfo=timezone.utc), discovery_seeds=[seed(cfg)])
    assert len(calls) == 1 and second.raw['metadata_cache_hit'] is True
    assert second.raw['metadata_cache_checked_at']
    assert first.raw['primary_metadata_response_sha256'] == second.raw['primary_metadata_response_sha256']
    assert fetch_nature(cfg, datetime(2027, 1, 11, tzinfo=timezone.utc), discovery_seeds=[seed(cfg)]) == []
    assert len(calls) == 1
    payload = next((tmp_path/'data/cache/official-metadata/v1/nature').glob('*.json')).read_text()
    assert 'Full body is not an abstract' not in payload


def test_nature_access_denial_is_negative_cached_without_automatic_retry(tmp_path, monkeypatch):
    cfg = config(tmp_path, metadata_cache_enabled=True)
    calls = transport(monkeypatch, lambda request: httpx.Response(403))
    with pytest.raises(httpx.HTTPStatusError):
        fetch_nature(cfg, TARGET, discovery_seeds=[seed(cfg)])
    with pytest.raises(ValueError) as caught:
        fetch_nature(cfg, TARGET, discovery_seeds=[seed(cfg)])
    assert len(calls) == 1
    assert caught.value.failures[0]['status_code'] == 403
    assert caught.value.partial_items[0].raw['metadata_cache_checked_at']
    assert MetadataCache(cfg, 'nature').get(URL)['expires_at'] is None


def test_nature_zero_detail_budget_remains_pending_and_uncached(tmp_path, monkeypatch):
    cfg = config(tmp_path, metadata_cache_enabled=True, max_detail_pages=0)
    calls = transport(monkeypatch, lambda request: pytest.fail('No detail budget'))
    coverage = {}
    [item] = fetch_nature(cfg, TARGET, discovery_seeds=[seed(cfg)], coverage=coverage)
    assert calls == [] and coverage['pending'] == 1
    assert item.raw['primary_verification']['verified'] is False
    assert MetadataCache(cfg, 'nature').get(URL) is None


def test_coarse_registry_date_can_be_resolved_only_by_exact_publisher_date(monkeypatch):
    cfg = config()
    transport(monkeypatch, lambda request: httpx.Response(200, text=landing(), headers={'content-type': 'text/html'}))
    [item] = fetch_nature(cfg, TARGET, discovery_seeds=[seed(cfg, **{'published-online': {'date-parts': [[2026, 9]]}})])
    assert item.published_at == '2026-09-03' and item.raw['publication_date_precision'] == 'day'


def test_persisted_seed_survives_feed_rolloff_without_new_crossref_discovery(tmp_path, monkeypatch):
    cfg = config(tmp_path, metadata_cache_enabled=True)
    calls = transport(monkeypatch, lambda request: httpx.Response(200, text=landing(), headers={'content-type': 'text/html'}))
    fetch_nature(cfg, TARGET, discovery_seeds=[seed(cfg)])
    coverage = {}
    [item] = fetch_nature(cfg, datetime(2026, 10, 11, tzinfo=timezone.utc), discovery_seeds=[], coverage=coverage)
    assert item.doi == DOI and item.raw['metadata_cache_hit']
    assert len(calls) == 1 and coverage['discovery_pool_size'] == 1


def test_fresh_discovery_identity_cannot_be_shadowed_by_cached_title(tmp_path, monkeypatch):
    cfg = config(tmp_path, metadata_cache_enabled=True)
    calls = transport(monkeypatch, lambda request: httpx.Response(200, text=landing(), headers={'content-type': 'text/html'}))
    fetch_nature(cfg, TARGET, discovery_seeds=[seed(cfg)])
    with pytest.raises(ValueError) as caught:
        fetch_nature(cfg, TARGET, discovery_seeds=[seed(cfg, title=['A changed quantum title'])])
    [item] = caught.value.partial_items
    assert item.title == 'A changed quantum title'
    assert not item.raw['primary_landing_verified']


def test_output_cap_does_not_prevent_later_landing_abstract_enrichment(monkeypatch):
    cfg = config(max_articles_per_feed=1)
    first = seed(cfg)
    second_doi = '10.1038/s41534-026-01235-6'
    second_url = 'https://www.nature.com/articles/s41534-026-01235-6'
    second = seed(cfg, DOI=second_doi, title=['Quantum learning with a second approach'], URL='https://doi.org/'+second_doi)
    def handler(request):
        page = landing() if str(request.url) == URL else landing(title=second.title, doi=second_doi).replace(URL, second_url)
        return httpx.Response(200, text=page, headers={'content-type': 'text/html'})
    calls = transport(monkeypatch, handler)
    results = fetch_nature(cfg, TARGET, discovery_seeds=[first, second])
    assert len(calls) == 2 and len(results) == 1
    assert results[0].raw['primary_landing_verified']


def test_pending_seed_pool_retries_with_later_detail_budget(tmp_path, monkeypatch):
    cfg = config(tmp_path, metadata_cache_enabled=True, max_detail_pages=0)
    calls = transport(monkeypatch, lambda request: httpx.Response(200, text=landing(), headers={'content-type': 'text/html'}))
    [pending] = fetch_nature(cfg, TARGET, discovery_seeds=[seed(cfg)])
    assert not pending.raw['primary_landing_verified'] and calls == []
    cfg.sources['nature']['max_detail_pages'] = 1
    [verified] = fetch_nature(cfg, TARGET, discovery_seeds=[])
    assert verified.raw['primary_landing_verified'] and len(calls) == 1


def test_shared_request_budget_counts_actual_requests_in_both_phases(monkeypatch):
    cfg = config()
    cfg.domains[:] = cfg.domains[:1]
    budget = MetadataBudget(max_requests=1)
    calls = transport(monkeypatch, lambda request: httpx.Response(200, json={'message': {'items': [work()]}}))
    journal_coverage = {}
    leads = fetch_preferred_journals(cfg, TARGET, budget=budget, coverage=journal_coverage)
    nature_coverage = {}
    with pytest.raises(ValueError):
        fetch_nature(cfg, TARGET, discovery_seeds=leads, budget=budget, coverage=nature_coverage)
    assert len(calls) == budget.requests == journal_coverage['requests'] == 1
    assert nature_coverage['requests'] == 0 and nature_coverage['pending'] == 1


def test_warm_rss_denial_preserves_original_status_and_checked_time(tmp_path, monkeypatch):
    cfg = config(tmp_path, metadata_cache_enabled=True, feeds=[{'name': VENUE, 'url': FEED}])
    calls = transport(monkeypatch, lambda request: httpx.Response(403))
    with pytest.raises(httpx.HTTPStatusError):
        fetch_nature(cfg, TARGET)
    with pytest.raises(ValueError) as caught:
        fetch_nature(cfg, TARGET)
    assert len(calls) == 1
    assert caught.value.failures[0]['status_code'] == 403
    assert caught.value.failures[0]['checked_at'] and caught.value.failures[0]['cached']


def test_rate_limit_retry_after_longer_than_six_hours_is_preserved(tmp_path, monkeypatch):
    import time
    cfg = config(tmp_path, metadata_cache_enabled=True)
    transport(monkeypatch, lambda request: httpx.Response(429, headers={'retry-after': '86400'}))
    before = time.time()
    with pytest.raises(httpx.HTTPStatusError):
        fetch_nature(cfg, TARGET, discovery_seeds=[seed(cfg)])
    entry = MetadataCache(cfg, 'nature').get(URL)
    assert entry['status'] == 'rate_limited' and entry['expires_at'] >= before + 86400


def test_metadata_packet_contains_exact_excerpts_and_response_hash(monkeypatch):
    cfg = config()
    page = landing()
    transport(monkeypatch, lambda request: httpx.Response(200, text=page, headers={'content-type': 'text/html'}))
    [item] = fetch_nature(cfg, TARGET, discovery_seeds=[seed(cfg)])
    evidence = item.raw['metadata_evidence']
    assert evidence['kind'] == 'publisher_metadata_excerpt' and evidence['source_url'] == URL
    assert evidence['response_sha256'] == item.raw['primary_metadata_response_sha256']
    assert all(region in page for region in evidence['citation_html'])
    assert evidence['citation_excerpt'] == '\n'.join(evidence['citation_html'])
    assert evidence['abstract_html'] in page and 'Full body' not in evidence['abstract_html']


def test_public_accepted_notice_does_not_become_final_version():
    cfg = config()
    candidate = _nature_seed_from_crossref(seed(cfg))
    notice = '<div class="c-article-banner">This is an unedited manuscript that has been accepted for publication.</div>'
    item = _verify_landing(candidate, landing() + notice, URL)
    assert item.raw['publication_stage'] == 'accepted_manuscript'


def test_abstract_container_cannot_swallow_a_later_major_section():
    body = '<section><h2>Abstract</h2><p>Abstract text.</p><h2>Results</h2><p>Full results text.</p></section>'
    assert _parse_public_abstract(body) == (None, None, None)


def test_changed_cached_identity_can_be_reverified_without_stale_promotion(tmp_path, monkeypatch):
    cfg = config(tmp_path, metadata_cache_enabled=True)
    current = {'title': TITLE}
    calls = transport(monkeypatch, lambda request: httpx.Response(200, text=landing(title=current['title']), headers={'content-type': 'text/html'}))
    fetch_nature(cfg, TARGET, discovery_seeds=[seed(cfg)])
    current['title'] = 'A changed quantum title'
    coverage = {}
    [item] = fetch_nature(cfg, TARGET, discovery_seeds=[seed(cfg, title=[current['title']])], coverage=coverage)
    assert item.raw['primary_landing_verified'] and item.title == current['title']
    assert len(calls) == 2 and coverage['cache_identity_invalidated'] == 1
