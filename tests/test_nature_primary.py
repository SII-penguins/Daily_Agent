from contextlib import contextmanager
from datetime import datetime, timezone
from html import escape
from pathlib import Path

import httpx
import pytest

from daily_agent.config import load_config
from daily_agent.connectors.nature import DEFAULT_FEEDS, fetch_nature
from daily_agent.connectors.source_failures import PartialSourceError


ROOT = Path(__file__).resolve().parents[1]
FEED = "https://www.nature.com/npjqi.rss"
URL = "https://www.nature.com/articles/s41534-026-01234-5"
TITLE = "Quantum circuit learning with tensor networks"
VENUE = "npj Quantum Information"
DOI = "10.1038/s41534-026-01234-5"
TARGET = datetime(2026, 10, 8, tzinfo=timezone.utc)


def config(**settings):
    cfg = load_config(ROOT)
    cfg.sources["nature"] = {
        "enabled": True, "feeds": [{"name": VENUE, "url": FEED}],
        "recent_days": 30, "max_articles_per_feed": 20, "max_detail_pages": 24,
        **settings,
    }
    cfg.sources["selection"] = {"topic_relevance_gate_enabled": False}
    return cfg


def rss(entries=None, venue=VENUE):
    entries = entries if entries is not None else [{"url": URL, "title": TITLE}]
    body = ""
    for entry in entries:
        body += f"""<item rdf:about="{escape(entry['url'])}">
          <title>{escape(entry['title'])}</title><link>{escape(entry['url'])}</link>
          <dc:creator>A. Researcher</dc:creator>
          <dc:date>{entry.get('date', '2026-10-07')}</dc:date>
          <prism:publicationName>{escape(venue)}</prism:publicationName>
          <prism:doi>{entry.get('doi', DOI)}</prism:doi>
          <content:encoded><![CDATA[<p>A quantum circuit learning summary.</p>]]></content:encoded>
        </item>"""
    return f"""<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
        xmlns:dc="http://purl.org/dc/elements/1.1/"
        xmlns:prism="http://prismstandard.org/namespaces/basic/2.0/"
        xmlns:content="http://purl.org/rss/1.0/modules/content/"
        xmlns="http://purl.org/rss/1.0/">
        <channel><title>{escape(venue)}</title></channel>{body}</rdf:RDF>"""


def landing(title=TITLE, venue=VENUE, doi=DOI, date="2026/10/07", canonical=URL,
            article_type="OriginalPaper"):
    tags = {"citation_title": title, "citation_journal_title": venue,
            "citation_doi": doi, "citation_publication_date": date,
            "citation_author": "Verified Researcher", "dc.type": article_type}
    return "<html><head>" + (f'<link rel="canonical" href="{escape(canonical)}">' if canonical else "") + "".join(
        f'<meta content="{escape(value)}" name="{key}">' for key, value in tags.items() if value
    ) + "</head><body>Public metadata only</body></html>"


def fake_client(monkeypatch, responses):
    calls = []

    class Client:
        def __init__(self, **kwargs):
            assert kwargs["follow_redirects"] is False
            assert 0 < kwargs["timeout"] <= 60

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def get(self, url):
            calls.append(url)
            result = responses[url]
            if isinstance(result, Exception):
                raise result
            if isinstance(result, httpx.Response):
                return result
            status, body = result
            return httpx.Response(status, text=body, headers={"content-type": "application/rss+xml" if url.endswith(".rss") else "text/html"}, request=httpx.Request("GET", url))

        @contextmanager
        def stream(self, method, url, **kwargs):
            assert method == "GET" and kwargs["follow_redirects"] is False
            yield self.get(url)

    monkeypatch.setattr("daily_agent.connectors.nature.httpx.Client", Client)
    return calls


def test_official_defaults_cover_all_requested_journals():
    assert len(DEFAULT_FEEDS) == 8
    assert {url for _, url in DEFAULT_FEEDS} == {
        f"https://www.nature.com/{slug}.rss" for slug in
        ("nature", "natmachintell", "nphys", "ncomms", "natcomputsci", "natelectron", "natrevphys", "npjqi")
    }


def test_matching_primary_landing_is_verified_without_fulltext_claim(monkeypatch):
    calls = fake_client(monkeypatch, {FEED: (200, rss()), URL: (200, landing())})
    [item] = fetch_nature(config(), TARGET)
    assert calls == [FEED, URL]
    assert (item.source, item.title, item.url, item.doi, item.published_at) == (
        "nature", TITLE, URL, DOI, "2026-10-07")
    assert item.authors == ["Verified Researcher"]
    assert item.raw["publication_status"] == "published"
    assert item.raw["primary_landing_verified"] is True
    assert item.raw["primary_verification"] == {
        "verified": True, "method": "publisher_landing_citation_metadata",
        "url": URL, "title": TITLE, "venue": VENUE, "doi": DOI, "published_at": "2026-10-07",
    }
    assert item.raw["abstract_source"] == "publisher_rss_summary"
    assert "paper_text_status" not in item.raw
    assert "paper_document" not in item.raw
    assert item.pdf_url is None


@pytest.mark.parametrize("page,reason", [
    (landing(title="An entirely different paper"), "title"),
    (landing(venue="Nature Physics"), "journal"),
    (landing(doi="10.1038/s41534-026-09999-9"), "DOI"),
    (landing(canonical="https://www.nature.com/articles/different-paper"), "canonical"),
    (landing(canonical="https://www.nature.com.evil.test/articles/s41534-026-01234-5"), "www.nature.com"),
    (landing(canonical="http://www.nature.com/articles/s41534-026-01234-5"), "HTTPS"),
    (landing(canonical=URL + ".pdf"), "landing"),
    (landing(title=None), "citation_title"),
    (landing(venue=None), "citation_journal_title"),
    (landing(doi="not-a-doi"), "DOI"),
    (landing(date="2026/99/50"), "date"),
    ("<html><h1>Access denied</h1></html>", "citation_title"),
])
def test_failed_or_mismatched_landing_remains_metadata_only(monkeypatch, page, reason):
    fake_client(monkeypatch, {FEED: (200, rss()), URL: (200, page)})
    with pytest.raises(PartialSourceError) as caught:
        fetch_nature(config(), TARGET)
    [item] = caught.value.partial_items
    assert item.title == TITLE
    assert item.raw["publication_status"] == "metadata_only"
    assert item.raw["primary_landing_verified"] is False
    assert item.raw["primary_verification"]["verified"] is False
    assert reason in caught.value.failures[0]["message"]
    assert caught.value.failures[0]["target"] == URL


def test_forbidden_landing_retains_candidate_and_visible_http_failure(monkeypatch):
    calls = fake_client(monkeypatch, {FEED: (200, rss()), URL: (403, landing())})
    with pytest.raises(PartialSourceError) as caught:
        fetch_nature(config(), TARGET)
    assert calls == [FEED, URL]
    [item] = caught.value.partial_items
    assert item.raw["publication_status"] == "metadata_only"
    assert item.raw["primary_landing_verified"] is False
    assert caught.value.failures[0]["status_code"] == 403


def test_feed_total_failure_keeps_http_error_identity(monkeypatch):
    fake_client(monkeypatch, {FEED: (403, "Forbidden")})
    with pytest.raises(httpx.HTTPStatusError) as caught:
        fetch_nature(config(), TARGET)
    assert caught.value.partial_items == []
    assert caught.value.failures[0]["status_code"] == 403


@pytest.mark.parametrize("url", [
    "https://www.nature.com.evil.test/articles/x",
    "https://www.nature.com@evil.test/articles/x",
    "https://evil.test/articles/x", "http://www.nature.com/articles/x",
    "https://www.nature.com/nature", "https://www.nature.com/articles/x.pdf",
    "https://www.nature.com:444/articles/x",
])
def test_rss_external_or_noncanonical_candidates_are_never_requested(monkeypatch, url):
    calls = fake_client(monkeypatch, {FEED: (200, rss([{"title": TITLE, "url": url}]))})
    with pytest.raises(ValueError) as caught:
        fetch_nature(config(), TARGET)
    assert calls == [FEED]
    assert caught.value.partial_items == []


def test_offsite_login_redirect_is_not_followed(monkeypatch):
    redirect = httpx.Response(303, headers={"location": "https://idp.nature.com/authorize"},
                              request=httpx.Request("GET", URL))
    calls = fake_client(monkeypatch, {FEED: (200, rss()), URL: redirect})
    with pytest.raises(PartialSourceError) as caught:
        fetch_nature(config(), TARGET)
    assert calls == [FEED, URL]
    assert caught.value.partial_items[0].raw["primary_landing_verified"] is False


def test_final_response_host_is_checked_even_with_matching_metadata(monkeypatch):
    response = httpx.Response(200, text=landing(), request=httpx.Request("GET", "https://evil.test/articles/x"))
    fake_client(monkeypatch, {FEED: (200, rss()), URL: response})
    with pytest.raises(PartialSourceError) as caught:
        fetch_nature(config(), TARGET)
    assert caught.value.partial_items[0].raw["publication_status"] == "metadata_only"


def test_date_and_topic_filters_apply_before_landing_budget(monkeypatch):
    other = "https://www.nature.com/articles/s41534-026-05678-9"
    entries = [{"title": "Unrelated biology", "url": other},
               {"title": TITLE, "url": URL, "date": "2025-01-01"},
               {"title": TITLE, "url": URL}]
    calls = fake_client(monkeypatch, {FEED: (200, rss(entries)), URL: (200, landing())})
    monkeypatch.setattr("daily_agent.connectors.nature.discovery_relevant", lambda item, cfg: item.title == TITLE)
    [item] = fetch_nature(config(max_detail_pages=1), TARGET)
    assert item.url == URL
    assert calls == [FEED, URL]


def test_zero_detail_budget_never_promotes_rss_metadata(monkeypatch):
    calls = fake_client(monkeypatch, {FEED: (200, rss())})
    [item] = fetch_nature(config(max_detail_pages=0), TARGET)
    assert calls == [FEED]
    assert item.raw["publication_status"] == "metadata_only"
    assert item.raw["primary_verification"]["reason"] == "detail_page_budget_exhausted"


def test_unverified_fallback_can_be_disabled_without_hiding_failure(monkeypatch):
    fake_client(monkeypatch, {FEED: (200, rss()), URL: (403, "Forbidden")})
    with pytest.raises(PartialSourceError) as caught:
        fetch_nature(config(include_unverified=False), TARGET)
    assert caught.value.partial_items == []
    assert caught.value.failures[0]["status_code"] == 403


def test_missing_landing_doi_and_date_do_not_inherit_rss_verification(monkeypatch):
    fake_client(monkeypatch, {FEED: (200, rss()), URL: (200, landing(doi=None, date=None))})
    with pytest.raises(PartialSourceError) as caught:
        fetch_nature(config(), TARGET)
    [item] = caught.value.partial_items
    assert item.raw["primary_landing_verified"] is False
    assert item.raw["primary_verification"]["verified"] is False
    assert item.raw["rss_metadata"]["doi"] == DOI


@pytest.mark.parametrize("article_type", ["Editorial", "News & Views", "Author Correction", "Comment"])
def test_nonresearch_publisher_items_are_not_returned_as_papers(monkeypatch, article_type):
    fake_client(monkeypatch, {FEED: (200, rss()), URL: (200, landing(article_type=article_type))})
    assert fetch_nature(config(), TARGET) == []


def test_mixed_feed_success_and_failure_preserve_verified_items(monkeypatch):
    second = "https://www.nature.com/nature.rss"
    fake_client(monkeypatch, {FEED: (200, rss()), second: (503, "Unavailable"), URL: (200, landing())})
    cfg = config(feeds=[{"name": VENUE, "url": FEED}, {"name": "Nature", "url": second}])
    with pytest.raises(PartialSourceError) as caught:
        fetch_nature(cfg, TARGET)
    assert caught.value.partial_items[0].raw["primary_landing_verified"] is True
    assert caught.value.failures[0]["target"] == second
    assert caught.value.failures[0]["status_code"] == 503


def test_invalid_xml_is_visible_source_failure(monkeypatch):
    fake_client(monkeypatch, {FEED: (200, "<html>not an RSS feed</html>")})
    with pytest.raises(ValueError, match="not a feed"):
        fetch_nature(config(), TARGET)


def test_disabled_collector_does_not_request(monkeypatch):
    calls = fake_client(monkeypatch, {})
    assert fetch_nature(config(enabled=False), TARGET) == []
    assert calls == []


def test_verified_original_publication_date_still_must_be_in_window(monkeypatch):
    fake_client(monkeypatch, {FEED: (200, rss()), URL: (200, landing(date="2025/10/07"))})
    assert fetch_nature(config(), TARGET) == []


def test_real_nature_online_date_and_article_type_metadata_take_precedence(monkeypatch):
    page = landing(date="2026/11/01", article_type="Article") + (
        '<meta name="citation_online_date" content="2026/10/07">'
        '<meta name="citation_article_type" content="Review Article">'
    )
    fake_client(monkeypatch, {FEED: (200, rss()), URL: (200, page)})
    [item] = fetch_nature(config(), TARGET)
    assert item.published_at == "2026-10-07"
    assert item.raw["publication_type"] == "review"


def test_conflicting_citation_titles_cannot_verify_publication(monkeypatch):
    page = landing() + '<meta name="citation_title" content="A different paper">'
    fake_client(monkeypatch, {FEED: (200, rss()), URL: (200, page)})
    with pytest.raises(PartialSourceError) as caught:
        fetch_nature(config(), TARGET)
    assert caught.value.partial_items[0].raw["primary_landing_verified"] is False
