# Cloud deliverable quota accounting (P0 partial fix)

Cloud editorial approval now applies the same final eligibility function as the
independent cloud handoff audit **before** quota allocation. A limited editorial
PASS, incomplete full-text read, missing independent claim review, or failed PDF
visual-fidelity check cannot consume a deliverable quota slot or stop refill.
The shortlist, drafts, reviews, and material records still retain those results;
legacy non-cloud limited-card policy is unchanged. Handoff continues to re-audit.

This is deliberately a partial fix, not incremental-paper recovery. The pipeline
still completes enrichment, drafting, analysis and review for the original batch
before persisting its completed approval results. Paper A can still be held up by
paper B. Nothing here changes per-stage budgets, issue deadlines/runtime/failure/
resume limits, leases, immutable jobs, independent review, or delivery receipts.
Batch count and candidate caps are unchanged; rejected repos still reserve slots
under the existing policy, while remaining paper slots continue progressing.

## Cache consequences

`editorial.py` is included in the aggregate deferred-review protocol hash, so this
change conservatively invalidates old aggregate draft/review reuse. Unchanged
native reading, visual reading, and fidelity implementation hashes retain their
lower-stage cache eligibility. Cloud issue checkpoints hash all Python source
files, so their old namespace also becomes a miss. No checkpoint or production
state is migrated, rewritten, or deleted by this patch. Existing sealed reports
and confirmed delivery receipts are not changed.

## Deferred incremental design

A safe next change should retain one original enrichment batch and its candidate
ordering, then run the scientific chain on each already-enriched material with
an explicit shared batch execution context, not singleton functions that reset
budgets. Native and repaired-document reading need separate pools, matching the
two original invocations. Visual reading, fidelity and writer need their own
cumulative active-time allowances and work counters; writer backend circuit
state must survive material boundaries. Waiting in other stages must not consume
an unrelated stage allowance. Runtime counters must not be encoded by mutating
configuration, because configuration participates in cache/request identities.

Persist the original batch identity, attempted material identities, shared
remaining stage budgets/counters/circuit state, and completed material/artifact
references under the existing issue lease before advancing. Charge reservations
conservatively before work so interruption cannot restore spent allowance. Bind
resume state to exact input/evidence/protocol identities, and retain the existing
issue budget as the outer ceiling. Only after the full chain completes and its
independent gates pass should a completed-paper checkpoint become reusable.
Changes to evidence/protocol must force revalidation, never reuse a bare PASS.

Required incremental regressions remain: A completes, B suspends, restart does
not reread A; repeated suspends never renew stage/candidate/issue allowances;
writer circuit remains shared; cache/evidence changes invalidate; persistent repo
failure cannot starve paper work; partial output remains explicitly incomplete.
These incremental guarantees are not implemented or claimed by this patch.

Regression entry point: `tests/test_cloud_approval_quota.py` (temporary roots,
synthetic reviewed candidates, no network/model/delivery calls), plus existing
reading, deferred-cache, recovery, cloud-handoff and reservation suites.
