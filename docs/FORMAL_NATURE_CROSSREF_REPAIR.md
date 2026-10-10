# Bounded Nature journal discovery repair

This connector repair has offline fixture coverage only. It has not run live mass acquisition and does not establish archive completeness or production readiness.

## Integration

For a future issue, prefer the explicit `daily_agent.formal_discovery` handoff in a new output directory. It independently calls `fetch_preferred_journals` before any broad Crossref search, passes the returned leads to `fetch_nature(discovery_seeds=...)`, and shares a 48-request, 150-second metadata budget. This does not read full papers, enroll candidates into a frozen issue, or publish a report.

A caller retaining broad Crossref collection should:

1. Call `fetch_preferred_journals(config, target_date, coverage=journal_coverage, budget=budget)` once
2. On failure, retain `exc.partial_items` and the failure/coverage object; never interpret a failed query as an empty bibliography
3. Pass those leads to `fetch_nature(config, target_date, discovery_seeds=leads, coverage=nature_coverage, budget=budget)`
4. If broad Crossref collection is still needed, pass `preferred_items=leads` and `preferred_coverage=journal_coverage` to `fetch_crossref`; a supplied empty list also prevents a duplicate journal pass
5. Honor `journal_coverage.host_blocked` rather than issuing a different Crossref endpoint after access denial/rate limiting

Ordinary `fetch_crossref` now runs its own bounded preferred pass before broad queries. A later broad 429 cannot erase the completed journal pass: the exception contains those partial items.

## Bounds and provenance

- Preferred journal discovery is targeted partial recall, capped at 16 requests and 100 returned records per query. Queries are interleaved across journals. There is no cursor-pagination completeness claim
- Calendar discovery configuration sets the inclusive date bounds. A fixed `preferred_recent_days` setting cannot replace a configured three-calendar-month window. Deposited/created registry dates are not publication dates
- Nature uses at most eight RSS feeds and 24 new landing lookups, with a hard default cap of 48 total requests and 150 seconds. The supplied shared budget can be stricter
- Only HTTPS canonical Nature landing URLs are fetched. Constructed `10.1038/s...` article URLs remain unverified leads until official title, DOI, venue and exact publication date match. Redirects, authentication challenges and fulltext/PDF endpoints are not followed
- A rolling feed plus targeted leads is explicitly marked `archive_not_validated`. Persisted discovery stubs preserve already-seen in-window leads after feed roll-off, capped at 10,000 stubs
- Strict topic acceptance happens after available public abstract enrichment. The old per-feed result limit is an output cap, not a pre-enrichment title cutoff
- Public abstracts come from citation metadata or an explicit visible Abstract region. Social snippets, generic paragraphs, hidden regions and a container spanning later major sections are not treated as abstracts. RSS summaries and registry abstracts retain their separate provenance
- Explicit accepted manuscripts, advance-online versions and versions of record retain separate stage values. An unspecified stage is never silently relabeled final VoR

## Cache and evidence handoff

Per-source `metadata_cache_enabled` opts into a separate cross-day metadata cache. Cached evidence is reapplied to the current date/topic window; a changed title/DOI identity requires a fresh official check. The cache contains compact citation/abstract excerpts and title stubs, never the article body or PDF. Blocked entries do not automatically expire. Rate limits honor Retry-After with a six-hour minimum, and warm negative results preserve their original status and checked time. Pending budget work is not negative-cached.

Nature `raw.metadata_evidence` contains:

- `kind: publisher_metadata_excerpt`
- `source_url` and `response_sha256`
- `citation_html`: exact original citation tags
- `citation_excerpt`: those tags joined with newlines, labeled as an extract by the host wrapper
- `abstract_html`: the exact public Abstract region when available

The citation/abstract packet is capped at 100 KB. `primary_landing_verified`, `abstract_verified`, date basis/precision and `publication_stage` are separate fields; metadata verification is not a scientific reading or approval.

## Deterministic checks

`tests/test_formal_nature_crossref_repair.py` covers journal-first 429 survival, host cooldown, request limits/calendar dates, duplicate-pass suppression, official seed identity, public-abstract boundaries, accepted-vs-final stages, cross-day cache, feed roll-off, fresh identity changes, pending work and exact evidence excerpts. Existing Nature, Crossref, publication-priority, query-budget, multisource and author-watchlist tests remain covered. All transport is mocked; no live mass acquisition is required.
