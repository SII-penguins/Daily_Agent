# Cross-day deferred-paper reuse

Papers that pass all evidence gates but miss the daily count stay in the durable
material library. Screening-only overflow is also retained, without being labeled
scientifically reviewed. Every issue still recalculates topical fit, ranking and
quota allocation and applies delivery-history/reservation exclusion.

`data/deferred-reviews/reviews/` stores integrity-checked, content-addressed review
snapshots. A snapshot is reusable only when the paper/version, actual document
(including chunks), citation evidence, reading/model/extraction settings, and
review protocol match. Dates, topic weights, selection tags, quotas and scheduling
budgets do not invalidate an otherwise identical review.

A verified hit preserves full-text reading, strict visual-fidelity results,
claim evidence and independent semantic review. Mechanical claim checks run again
against the exact evidence. A label such as `PASS` or `model_checked` alone is never
enough: every valid field must have its complete successful independent check.
Incomplete, unsupported, stale-version and failed results cannot be promoted into
successful cache entries. Deterministic editorial review and selection still run.

On a miss, existing chunk/page/fidelity stage caches remain available. For example,
changed citation context requires new drafting and semantic checks, while unchanged
text chunks need no model reread. Actual text changes invalidate reading and all
dependent results; model/reading-protocol changes invalidate matching assessments.
A PDF must have its exact local source bytes and advertised SHA-256 available.

Asset manifests bind all local PDF/page/review dependencies to SHA-256. Immutable
copies live in `data/deferred-reviews/blobs/`, outside selected-report retention.
Missing or damaged assets can be repaired from matching verified copies; a
missing/damaged copy forces a cache miss, never a fabricated verification success.
Retention also protects paths referenced by screening- and quota-deferred records.
Later-added presentation crops remain independently hash-validated by the visual
asset renderer and are preserved when restoring a reading snapshot.

Snapshots confer no publication rights. Delivered/reserved identities are excluded
by the live material-pool selection rules, and no history or reservation state is
ever restored from a cached review. Source/PDF version changes require new evidence.

Offline regression coverage: `tests/test_deferred_review_cache.py`,
`tests/test_cloud_checkpoints.py`, `tests/test_trusted_reading.py`,
`tests/test_paper_pool.py` and delivery-reservation tests. All model responses in
these tests are counted local fakes; no paid model or source fetch is required.

Text reading, visual observation and fidelity reconstruction also include their
own implementation-file SHA-256 in their lower-stage cache identities. This is
conservative: any edit within that stage's file invalidates its results, including
nonsemantic edits. It does not hash the whole repository or selection policy;
changes to other stage files preserve unaffected lower-stage work. Legacy cache
migration is permitted only within the same protocol fingerprint. As a result,
upgrading prompts/validators cannot bypass new checks by falling through to an
older per-chunk or per-page cache.
