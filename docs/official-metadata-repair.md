# Bounded official metadata discovery

The new explicit host path is `daily_agent.formal_discovery.export_candidate_handoff(config, target_date, new_output_root)`, or:

```sh
python -m daily_agent.formal_discovery --root /path/to/new-profile --date 2026-10-11 --output /path/to/new-candidates
```

This runs metadata discovery only and writes `candidates.json`, `coverage.json` and compact exact publisher citation extracts. It does not acquire PDFs, read full papers, approve scientific claims, initialize/seal an issue, publish or deliver. The existing paper-first host must deliberately consume the candidates, compare them with other eligible work, obtain permitted full sources and perform its normal full reading/independent review. An output candidate is not an accepted report item. Old sealed issues and frozen source implementations are untouched.

## What changed

- PMLR parses every title in a bounded official volume response before final caps. The 7.16 MB/6,552-entry ICML 2026 index fits its 16 MiB/10,000-entry defaults. Broad title cues earn an abstract lookup; they do not bypass final topical relevance
- Metadata lookup rotates across the two quantum directions and exploratory interests, with subtopic rotation. Existing global high-scoring agent titles can no longer consume every discovery slot
- Official PMLR abstracts and identity/date/conference metadata are retrieved from canonical public landing pages. Conference-event/build dates are not publication dates
- Crossref's independently callable preferred-journal pass runs before broad queries. A broad-query 429 cannot erase journal work already obtained; host denials/rate limits stop further same-host calls
- Nature combines rolling RSS, supplied journal leads and a bounded persistent discovery pool, then verifies canonical publisher metadata and explicit public abstracts. Accepted manuscripts and version-of-record status remain distinct; an unspecified stage is not described as VoR
- Cross-day positive and explicit negative metadata caches reuse official evidence and retain blocked/rate-limited/missing/invalid states. Official dates are screened again on every run
- Exact official abstract/date evidence survives merging with an arXiv primary record. Metadata caching never sets full-document-read or scientific-approval flags

## Bounds and failures

PMLR: at most 42 GET attempts, 40 new landing lookups, 150 seconds, 24 MiB streamed bytes. Nature plus supplied journal backfill in the host wrapper share 48 attempts, 150 seconds, 24 MiB: at most 16 journal queries, 8 RSS feeds and 24 new landings. Cache hits spend no network calls. Responses stream with per-response limits and remaining-time checks; no automatic redirect following. No credential use, paid API, fulltext/PDF endpoint or access bypass is added.

The time cap prevents new requests after the deadline and closes on the next streamed chunk; one blocking network read is limited to the remaining timeout. Local parsing/serialization overhead is not a guaranteed hard wall-clock interrupt.

Cache namespace: profile root `data/cache/official-metadata/v1`. Per source: at most 2,000 records/128 MiB by default; cache capacity failure is explicit rather than deleting evidence implicitly. Index TTL 24 hours; verified metadata 30 days; missing metadata 48 hours; most content failures 24 hours; transient errors 1 hour; 429 at least 6 hours and respects a longer Retry-After. Access-denied/login-redirect entries do not expire automatically. Unattempted budget work is never cached as a failed paper. Cache JSON/envelopes are integrity checked and existing workflow mutation fences apply.

Calendar bounds remain three calendar months inclusive. For 2026-10-10, the window is 2026-07-10 through2026-10-10. Unknown/coarse dates cannot qualify; a bounded official landing can resolve a missing volume day. Older volumes no longer appear in the new profile's active PMLR list. Event year alone is not treated as the publication date.

## Known limits

Nature and Crossref discovery remains partial recall: targeted journal queries plus rolling/persisted RSS leads. No official three-month Nature archive traversal was validated or claimed. Endpoint denial leaves visible metadata-only leads. The existing final topic scorer can still reject a novel relevant paper whose terminology does not match its interests; broad recall cues only get it a bounded lookup. A publication metadata record is not scientific validation.

The new host wrapper bypasses old issue source checkpoints deliberately; it must be explicitly adopted for fresh future issues. It does not mutate legacy pipeline source snapshots. It exports a `publication` input only from verified publisher evidence with an exact citation extract; early accepted-manuscript candidates remain visible but are not automatically labeled final published records in the paper-first manifest.

Tests are deterministic local HTTP fixtures, including the complete already-downloaded PMLR volume replay. No live mass acquisition was run for this repair. The full legacy regression suite should be run after the parent's active source changes stabilize; focused passing tests are not a full-suite pass.
