# Incremental cloud execution

## Enrollment and authority

The normal cloud supervisor enrolls only a new, non-pilot cloud + parent_queue
issue, at first authoritative issue-budget creation. It records
`execution_protocol=incremental-cloud-v1` in that existing budget and creates a
stable `state/incremental-issues/<date>/` planner. Neither source hashes nor
configuration values select a new directory. Configuration/source changes reject
an existing planner in place, preserving its budgets and evidence.

Old complete budget records without that marker remain on the legacy path.
Visible old activity without its authoritative budget fails closed. An enrolled
issue missing either its budget or planner cannot reset itself or fall back to
legacy. Enrollment interrupted before all records are committed fails closed.
This is not a migration tool, and it never reopens a sealed issue.

Ordinary cloud generation must be the supervisor's recorded active worker:
its budget attempt, running state, date, PID and process group must match, and
its issue wall/runtime ceiling must remain available. A missing, idle, expired
or different process attempt cannot use either the manual pipeline or direct
`generate` entry to bypass runtime accounting. The supervisor persists launch
intent before Popen; the child permits a one-second bounded wait for on_start's
initial PID/PGID publication (before any slower process inspection).

Manual cloud runs and supervised generation acquire locks in the same order:
cloud manifest, ready report, pipeline, batch execution, then queue. Generation
holds publication locks through its final seal, passing an explicit already-held
ready-lock flag to sealing. The read-only gate rejects any existing seal,
outbox, delivered receipt, or cloud manifest, including prepared, accepted,
uncertain and confirmed states. Legacy manual runs also take ready before
pipeline, matching existing delivery reconciliation.

## Frozen planning and per-paper transaction

The initial discovery result is saved once. Each original batch persists its
selected order before enrichment, and its complete enriched records before model
work. The original policy remains at most six batches of eight candidates.
Enrichment still runs once for each newly frozen original batch; the adapter
does not rerun it per paper. A resumable source checkpoint is not an execution
budget.

A shared `Execution` object handles each original batch. Within it each material
completes author work, native reading, visuals/fidelity, any repaired reading,
primary/semantic/presentation stages and scientific analysis before the next
material starts. Existing paper/repository qualification and final cloud quota
logic remain authoritative. Splitting writer groups of two into singletons can
increase native parent job groups but introduces no paid backend or unbounded
candidate/operation capacity.

For a qualified material, the immutable full completion envelope is written
first, then its hash reference committed to the journal. Only afterward are the
library and editorial projections written. If either projection write is
interrupted, recovery reloads the referenced envelope, rechecks mechanical,
independent-review and asset evidence without model calls, and rebuilds both
views. An orphan envelope is never promoted. Completion is not publication and
reserves no permanent quota.

Unqualified or partial records remain visibly incomplete. A PASS label by itself
cannot create a completed-paper record. A completed A is not reread when B
suspends and resumes at a later stage.

## Budgets, operations and reuse

The separate native/repaired/visual/fidelity/primary-writer pools are shared over
the entire original batch, across all singleton calls and process restarts.
Author eligibility and its configured original-batch cap are frozen once.
Actual repaired document bytes and chunk IDs bind once, with the same bounded
chunk capacity. Queue admissions bind the actual locked prompt/image/role and
active job ID; callers cannot gain extra calls by changing a nonce.

Local monotonic stage deadlines reach the transport timeout, and PendingResponse
(BaseException) settles reservations in finally before process exit 75. Parent
waiting time is excluded. These are local stage-process windows, not measured
parent-worker model runtime. Unknown hard-kill reservations forfeit their entire
reserved remainder. Primary backend/JSON/schema failure opens one durable batch
circuit. Unsupported changed queue generations fail closed, rather than claiming
that a retry has free capacity.

Stage exhaustion degrades missing work locally while allowing independent pools
to finish. Already validated caches may be reused, but cache hits grant no fresh
budget. Existing reading/visual/fidelity whole-file protocol hashes invalidate
prior low-level caches strictly when these source files change; no cross-source
PASS migration bridge is provided. Existing same-protocol settings migrations
retain their existing evidence checks.

Author/scientific audit envelopes retain full responses, including incomplete or
rejected additive work. They do not invent new mandatory success gates; existing
consumers must validate any positive claims before adopting them.

## Verification scope

All implementation tests use temporary directories and offline responses. Tests
cover actual queue creation/import, A completion followed by B suspension at all
14 operation stages, stable job identities, zero A transport calls on later
resumes, projection-write failures, finite 6 x 8 planning, immutable enrichment,
source/config conflicts, corrupt/missing ledgers, concurrent entry locks,
reservations, hard-kill forfeiture, stage deadlines, author limits, bounded
repairs and durable circuits. No test result establishes a production 8+2
throughput guarantee or permission to publish.
