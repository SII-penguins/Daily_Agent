"""Conference event dates must not masquerade as proceedings release dates."""
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
import pytest

from daily_agent.config import load_config
from daily_agent.connectors.neurips import _publication_info, fetch_neurips
from daily_agent.connectors.pmlr import _parse_volume
from daily_agent.connectors.source_failures import PartialSourceError


ROOT = Path(__file__).resolve().parents[1]
PMLR_VOLUME = "https://proceedings.mlr.press/v306/"
PMLR_PAPER = """
<div class="paper">
  <p class="title">Accelerating Regression Tasks with Quantum Algorithms</p>
  <p class="details"><span class="authors">Chenghua Liu,&nbsp;Zhengfeng Ji</span></p>
  <a href="https://raw.githubusercontent.com/mlresearch/v306/main/assets/liu26bi/liu26bi.pdf">Download PDF</a>
  <a href="https://proceedings.mlr.press/v306/liu26bi.html">abs</a>
</div>
"""


def test_pmlr_uses_official_release_not_event_or_site_build_date():
    html = """
    <meta name="description" content="Proceedings of the 43rd International Conference on Machine Learning
    Held in Seoul, South Korea on 06-11 July 2026 Published as Volume 306 by the
    Proceedings of Machine Learning Research on 29 September 2026. Volume Edited by: Editors" />
    <h2>Volume 306: International Conference on Machine Learning, 6-11 July 2026</h2>
    """ + PMLR_PAPER + "<footer>This site last compiled Wed, 07 Oct 2026</footer>"
    item, = _parse_volume(html, PMLR_VOLUME, "ICML 2026")
    assert item.published_at == "2026-09-29"
    assert item.updated_at is None
    assert item.raw["publication_date_precision"] == "day"
    assert item.raw["publication_date_basis"] == "official_volume_publication"
    assert item.raw["publication_date_source_url"] == PMLR_VOLUME
    assert item.raw["conference_dates"] == "06-11 July 2026"
    assert item.raw["conference_year"] == "2026"
    assert item.url == PMLR_VOLUME + "liu26bi.html"
    assert item.pdf_url == "https://raw.githubusercontent.com/mlresearch/v306/main/assets/liu26bi/liu26bi.pdf"
    assert item.authors == ["Chenghua Liu", "Zhengfeng Ji"]


@pytest.mark.parametrize("header", [
    "<h2>ICML 2026, 6-11 July 2026</h2>",
    '<meta name="citation_publication_date" content="2026/07/06" />',
    "<title>Published as Volume 306 by PMLR on 31 September 2026</title>",
    "<footer>This site last compiled Wed, 07 Oct 2026</footer>",
])
def test_pmlr_missing_or_invalid_release_preserves_year_precision(header):
    item, = _parse_volume(header + PMLR_PAPER, PMLR_VOLUME, "ICML 2026")
    assert item.published_at == "2026"
    assert item.raw["publication_date_precision"] == "year"
    assert item.raw["publication_date_basis"] == "venue_year"


def test_pmlr_unknown_venue_does_not_invent_a_date():
    item, = _parse_volume(PMLR_PAPER, PMLR_VOLUME, "PMLR v306")
    assert item.published_at is None
    assert item.raw["publication_date_precision"] == "unknown"


def test_pmlr_recent_release_passes_calendar_window_but_year_only_does_not():
    from daily_agent.discovery_window import within_discovery_window

    config = load_config(ROOT)
    config.sources["discovery"] = {"lookback_calendar_months": 3}
    exact, = _parse_volume(
        "<title>Published as Volume 306 by PMLR on 29 September 2026</title>" + PMLR_PAPER,
        PMLR_VOLUME, "ICML 2026",
    )
    coarse, = _parse_volume(PMLR_PAPER, PMLR_VOLUME, "ICML 2026")
    assert within_discovery_window(exact, config, date(2026, 10, 8)) is True
    assert within_discovery_window(coarse, config, date(2026, 10, 8)) is False


def _mock_neurips(monkeypatch, pages):
    calls = []

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get(self, url):
            calls.append(url)
            status, text = pages[url]
            return httpx.Response(status, text=text, request=httpx.Request("GET", url))

    monkeypatch.setattr("daily_agent.connectors.neurips.httpx.Client", Client)
    config = load_config(ROOT)
    config.sources["neurips"] = {
        "enabled": True, "years": [2025], "max_results_per_year": 5,
        "allowed_tracks": ["Conference"], "max_index_pages_per_year": 4,
    }
    return config, calls


YEAR_URL = "https://papers.nips.cc/paper_files/paper/2025"
MAIN_URL = YEAR_URL + "/vol38-main-conference"
PAPER_URL = YEAR_URL + "/hash/abc-Abstract-Conference.html"
CREATIVE_INDEX = """
<a href="/paper_files/paper/2025/hash/art-Abstract-Creative_AI_Track.html">Quantum art</a>
<p>See also: <a href="/paper_files/paper/2025/vol38-main-conference">Main Conference</a></p>
"""
MAIN_INDEX = """
<a href="/paper_files/paper/2025">Creative AI</a>
<a href="/paper_files/paper/2025/hash/abc-Abstract-Conference.html">Quantum circuit learning</a>
<a href="/paper_files/paper/2025/hash/abc-Abstract-Conference.html">Quantum circuit learning</a>
"""


def test_neurips_follows_main_volume_even_when_year_page_has_papers(monkeypatch):
    config, calls = _mock_neurips(monkeypatch, {
        YEAR_URL: (200, CREATIVE_INDEX), MAIN_URL: (200, MAIN_INDEX),
        PAPER_URL: (200, '<h4>Abstract</h4><p>Quantum learning</p><a href="paper.pdf">Paper</a>'),
    })
    item, = fetch_neurips(config, datetime(2026, 10, 8, tzinfo=timezone.utc))
    assert calls == [YEAR_URL, MAIN_URL, PAPER_URL]
    assert item.title == "Quantum circuit learning"
    assert item.published_at == "2025"
    assert item.updated_at is None
    assert item.raw["publication_date_precision"] == "year"
    assert item.raw["publication_date_basis"] == "proceedings_year"


def test_neurips_traversal_limits_and_rejects_unrelated_links(monkeypatch):
    extra = """
    <a href="https://unrelated.example/paper_files/paper/2025/vol38-extra">Other host</a>
    <a href="/paper_files/paper/2026/vol39-main-conference">Other year</a>
    <a href="/paper_files/paper/2025/vol38-extra?next=1">Query</a>
    <a href="/paper_files/paper/2025/vol38-extra">Third volume</a>
    """
    config, calls = _mock_neurips(monkeypatch, {
        YEAR_URL: (200, CREATIVE_INDEX + extra), MAIN_URL: (200, MAIN_INDEX),
        PAPER_URL: (200, ""),
    })
    config.sources["neurips"]["max_index_pages_per_year"] = 2
    items = fetch_neurips(config)
    assert len(items) == 1
    assert calls == [YEAR_URL, MAIN_URL, PAPER_URL]


def test_neurips_failed_linked_index_is_reported_with_partial_success(monkeypatch):
    config, calls = _mock_neurips(monkeypatch, {
        YEAR_URL: (200, CREATIVE_INDEX), MAIN_URL: (403, ""),
    })
    with pytest.raises(PartialSourceError) as caught:
        fetch_neurips(config)
    assert calls == [YEAR_URL, MAIN_URL]
    assert caught.value.partial_items == []
    assert caught.value.failures[0]["target"] == MAIN_URL
    assert caught.value.failures[0]["status_code"] == 403


@pytest.mark.parametrize("date,expected,precision", [
    ("2025/12/01", "2025-12-01", "day"),
    ("2025-12-01T16:00:00Z", "2025-12-01", "day"),
    ("2025/12", "2025-12", "month"),
    ("2025", "2025", "year"),
    ("2025-02-30", "2025", "year"),
])
def test_neurips_preserves_citation_date_precision(date, expected, precision):
    info = _publication_info(f'<meta content="{date}" name="citation_publication_date">', 2025, PAPER_URL)
    assert info["publication_date"] == expected
    assert info["publication_date_precision"] == precision


def test_neurips_detail_can_supply_official_day_precision(monkeypatch):
    config, _ = _mock_neurips(monkeypatch, {
        YEAR_URL: (200, f'<a href="{PAPER_URL}">Quantum circuits</a>'),
        PAPER_URL: (200, '<meta name="citation_publication_date" content="2025/12/01">'),
    })
    item, = fetch_neurips(config)
    assert item.published_at == "2025-12-01"
    assert item.raw["publication_date_precision"] == "day"
    assert item.raw["conference_year"] == "2025"
