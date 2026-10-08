"""Calendar-month discovery window; deferred reviewed work has separate retention."""
from __future__ import annotations
import calendar
from datetime import date, datetime, timezone


def calendar_month_start(day: date, months: int = 3) -> date:
    ordinal = day.year * 12 + day.month - 1 - max(0, months)
    year, month0 = divmod(ordinal, 12)
    month = month0 + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def lookback_start(config, run_date):
    months = int(config.sources.get('discovery', {}).get('lookback_calendar_months', 0))
    return calendar_month_start(run_date, months) if months > 0 else None


def within_discovery_window(item, config, run_date):
    start = lookback_start(config, run_date)
    if not start or item.item_type != 'paper':
        return True
    raw = item.raw
    values = [getattr(item, 'published_at', None), raw.get('published_at'),
              getattr(item, 'updated_at', None), getattr(item, 'source_updated_at', None)]
    dates = []
    for value in values:
        try:
            dates.append(date.fromisoformat(str(value)[:10]))
        except ValueError:
            continue
    # Year-only metadata is not an exact recent-publication claim.
    if raw.get('publication_date_precision') == 'year':
        return False
    return bool(dates and any(start <= value <= run_date for value in dates))
