from datetime import date
from pathlib import Path

import pytest

from daily_agent.config import load_config
from daily_agent.connectors.crossref import _date_parts, _work_to_item
from daily_agent.discovery_window import within_discovery_window
from daily_agent.models import MaterialRecord


ROOT = Path(__file__).resolve().parents[1]


def item(parts, field="published-online", **extra):
    config = load_config(ROOT)
    work = {"DOI": "10.1234/date-test", "title": ["Quantum kernel learning"],
            "type": "journal-article", field: {"date-parts": [parts]}, **extra}
    return _work_to_item(work, config.domains[0])


@pytest.mark.parametrize("parts,expected,precision", [
    ([2026], "2026", "year"),
    ([2026, 7], "2026-07", "month"),
    ([2026, 7, 8], "2026-07-08", "day"),
    ([2024, 2, 29], "2024-02-29", "day"),
])
def test_crossref_preserves_date_precision_through_material_roundtrip(parts, expected, precision):
    paper = item(parts)
    assert paper.published_at == paper.updated_at == expected
    assert paper.raw["publication_date_precision"] == precision
    restored = MaterialRecord.from_item(paper).to_digest_item()
    assert restored.raw["published_at"] == expected
    assert restored.updated_at == expected
    assert restored.raw["publication_date_precision"] == precision


@pytest.mark.parametrize("parts,issue", [
    ([2026], date(2026, 2, 1)),
    ([2026, 7], date(2026, 10, 1)),
    ([2026, 7], date(2026, 10, 8)),
    ([2026, 8], date(2026, 10, 8)),
])
def test_coarse_crossref_dates_do_not_prove_exact_window_membership(parts, issue):
    assert not within_discovery_window(item(parts), load_config(ROOT), issue)


@pytest.mark.parametrize("parts,issue,eligible", [
    ([2026, 7, 7], date(2026, 10, 8), False),
    ([2026, 7, 8], date(2026, 10, 8), True),
    ([2026, 10, 8], date(2026, 10, 8), True),
    ([2026, 10, 9], date(2026, 10, 8), False),
    ([2026, 2, 27], date(2026, 5, 31), False),
    ([2026, 2, 28], date(2026, 5, 31), True),
    ([2024, 2, 28], date(2024, 5, 31), False),
    ([2024, 2, 29], date(2024, 5, 31), True),
])
def test_exact_crossref_dates_obey_calendar_month_boundaries(parts, issue, eligible):
    assert within_discovery_window(item(parts), load_config(ROOT), issue) is eligible


def test_existing_online_and_print_publication_dates_keep_precedence():
    online = item([2026, 7, 8], **{"published-print": {"date-parts": [[2026, 8, 9]]}})
    printed = item([2026, 8, 9], field="published-print")
    assert online.published_at == "2026-07-08"
    assert printed.published_at == "2026-08-09"
    assert printed.raw["publication_date_precision"] == "day"


@pytest.mark.parametrize("value", [None, {}, {"date-parts": []},
    {"date-parts": [[2026, 2, 29]]}, {"date-parts": [[2026, 13]]},
    {"date-parts": [[0]]}, {"date-parts": [[True]]},
    {"date-parts": [[2026, 7, 8, 9]]}, {"date-parts": [["2026"]]}])
def test_malformed_crossref_dates_never_manufacture_dates(value):
    assert _date_parts(value) is None
