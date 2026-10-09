# Explicit production revisions

The ordinary cloud date handoff and budget are unchanged. `production_revision`
is an additive namespace under the same canonical root:
`data/state/production-revisions/<date>/<revision_id>/`. It is not a pilot or a
fresh production root and does not relax ordinary new-issue eligibility.

## Authorization and identity

`authorize_revision(root, day, parent_identity=..., authorization=..., policy=...)`
is caller-authorized, not automatic. `authorization` requires a specific source,
verbatim/request text, and timezone-aware authorization timestamp. The caller is
responsible for genuine user provenance. The parent must be the exact confirmed,
reconciled, Site-bound production handoff; the destination room and Site project
are inherited and immutable. An optional `carry_keys` limits original rows.

Policy requires an absolute `deadline`, `max_runtime_seconds`, `max_failures`,
`max_resumes`, and `per_launch_seconds`. The issue identity hashes this request;
a repeat returns the same issue without restoring runtime or operation budgets.
Changing an existing revision number's request fails closed. Missing initialized
budget or source/config mismatch never creates a replacement namespace.

The contract freezes source/config identities and a finite pool snapshot of at
most the configured original batch size times batch count (maximum 8 x 6).
Generation today is pool-only; it does not claim refreshed all-source discovery.
Validated PDFs, reading, figures, reviews and library evidence use the original
root. Reports, editorial files, logs, ready seals, handoffs and batch ledgers
use the explicit revision namespace. Canonical pipeline/dispatch locks still
serialize interactions with ordinary work.

## Generation and freezing

`run_generation(root, day, revision_id, timeout=900)` reserves from this revision's
frozen budget, supervises a worker, persists child identity and charges measured
runtime. Exit 75 is a parent-queue suspension, not a failure or a new issue.
Every resume preserves candidate and enriched batch identity. The worker invokes
the same per-material completion contract as normal incremental work; it never
calls the legacy date pipeline. An uncertain orphan is retained until explicit
`recover_generation` verifies its process identity and stops it. No raw PID or
missing child identity permits a reset.

At the cutoff, stop/settle any active child before `freeze_completed`. This
function calls no model: it gathers only hash-verified, independently revalidated
committed material completions, including siblings of an unfinished batch.
Unfinished papers are excluded. Parent rows may be carried only byte-exactly with
their original version and approval hash. This is a narrowly authorized resend
exception, not cleared publication dates or a fabricated update label.

`seal_ready` is also available to a trusted worker supplying final approvals.
It verifies the frozen contract, source evidence, assets, publication history,
and reservations. `prepare_handoff` independently verifies the immutable seal,
renders HTML, and hashes revision provenance along with content/destination.

## Delivery and publication

Revisions use schema 3 with `edition_type=production_revision`, `revision_id`,
`revision_number`, `parent_identity`, and `contract_sha256`. Existing legacy
schema 1/2 verification and byte identities are not changed.

Archive export includes confirmed/accepted revisions, or an exact explicit
`--current-revision-id` prepared candidate. Production revision pages are
`reports/YYYY-MM-DD-rN-<sealed identity prefix>/index.html`; original date pages
and original downloads retain their exact bytes. The index labels revisions and
chooses latest by date and revision number.

`bind_site` requires genuine successful deployment/version tool results for the
inherited project. Exact committed static source, full manifest, original HTML,
revision metadata and caption are verified. This is source proof, not an HTTP
round-trip assertion. `record_transition` retains the original prepared → sending
→ accepted → confirmed flow and exact destination/body readback. An uncertain
send may be reconciled; it may not be blindly resent.

All unresolved revision sends reserve canonical aliases for every date,
including the same day. Only the exact current revision can exclude itself.
After confirmation, reconciliation appends an immutable `revision-publications`
event and updates canonical material publication state. The legacy date/rank
index is never overwritten. Interrupted reconciliation leaves the confirmed
receipt reserving its contents until idempotent recovery succeeds.

## Offline verification and operation limits

No module operation deploys a Site or sends a message. Tests use temporary roots,
offline fixture tool evidence, and local fixture Git repositories. Real revision
initialization, generation, queue answers, deployment and sending are separate
operator actions after the code/tests are reviewed.

A runtime ceiling is not a paper-count promise. Absolute deadline and cumulative
runtime both apply; config changes cannot extend an initialized issue. Ordinary
future scheduling remains independent from today's authorized revision.

## Executor-loss transaction fencing (explicit migration, never automatic)

A missing PID in a different execution namespace is not proof an old child died.
`workflow_runtime.process_namespace()` binds Linux boot ID and PID namespace.
Every new worker receives immutable `--expected-attempt` and
`--expected-namespace` arguments; persisted active budget and generation state
must agree with both and with its PID/PGID. Started, heartbeat and final callbacks
re-read and compare identities under the generation lock. A stale callback may
not replace a newer attempt, and `CleanupPending` never clears an active budget.
Same-namespace recovery holds that lock from ownership validation through
verified signal and settlement. Cross-namespace cleanup is refused.

For an explicitly approved expired legacy attempt, the separate
`retire_and_replace` transaction can fence the old namespace without asserting
process death. It requires:

1. `migration_evidence(root, day, old_revision_id)` and root approval of its exact
   `sha256`, not just a date or an assumption no progress occurred
2. A genuine cross-executor shared-lock probe whose retained device/inode records
   match `evidence.payload.locks`; the caller supplies its verified source
3. The original active launch window has expired and the original issue deadline
   has not; the exact old contract, budget, state, candidates, batches, execution
   journals and queue JSON remain identical to the approved evidence
4. Simultaneous nonblocking locks in the order dispatch → old generation → old
   handoff → canonical pipeline → canonical writer queue. Never replace locks
5. All nonempty execution namespaces validate. Missing/unknown journals, original
   batch identity changes, or lost operation-bound queue jobs fail closed

API:

```python
preflight = migration_evidence(ROOT, DAY, OLD_RID)  # read-only
# Root reviews/approves preflight['sha256'] and the real shared-lock probe.
replacement = retire_and_replace(
    ROOT, DAY, OLD_RID,
    expected_evidence_sha=APPROVED_EVIDENCE_SHA,
    authorization={'source':VERIFIED_AUTHORIZATION_SOURCE,
                   'text':EXPLICIT_MIGRATION_AUTHORIZATION,
                   'authorized_at':VERIFIED_UTC_AUTHORIZATION_TIME},
    expected_source_version=APPROVED_TARGET_SOURCE_HASH,
    shared_lock_proof={'source':VERIFIED_CROSS_EXECUTOR_PROBE_REFERENCE,
                       'shared_locking_verified':True,
                       'locks':APPROVED_PREFLIGHT_LOCK_IDENTITIES},
)
```

The snapshot is immutable and determines one replacement RID. Snapshot existence
alone permanently blocks new-code operations in the old RID, even if a crash
prevents the next retired marker. The replacement has the same report edition,
original candidate universe, carry-forward rows, deadline, caps, logical issue
and batch IDs, operation/job bindings, stage spending, outstanding reservations,
circuit state and committed outputs. It does not receive a fresh 48 candidates
or stage budgets. Outstanding stage reservations remain present and are
conservatively forfeited by the existing execution recovery rules on reopening.

The unresolved launch incurs `max(reserved launch seconds, wall exposure until
fence)` additional runtime charge, separately labeled `forfeited_seconds` with
`measured=False`, plus one failure; resume count is inherited. This is deliberately
not reported as measured model execution. Original mutable budget/state stay
unchanged: a late old supervisor may still write them, so every retry and the
replacement's authority derive only from the sealed snapshot. Queue bytes are
proof only and are never restored over newer answers or claims.

A crash at any target-copy stage resumes the same snapshot/replacement and checks
existing copied bytes. It does not recompute debt, choose a new ID, reset counters,
or re-read the old mutable budget. `migration-complete.json` is written last;
without it the target cannot run. After completion, a repeated migration does not
rewrite target budget even if that target has progressed. Replacement budget reads
check inherited lower bounds and immutable snapshot/accounting fields.

No migration is run by importing this module or running tests. The root must
approve the actual evidence and target source after offline acceptance.
