# Throughput release: implementation and adoption

Public-fixture note: the measurements below describe their dated validation checkpoint. The public tree uses synthetic text with the same page/chunk geometry and planner assertions; it does not redistribute the original full-paper fixtures. See [fixture provenance](../tests/fixtures/README.md).

This release is prepared in an isolated checkout based on commit `4d9447524e72bfae21f38b70431a8ca320503585`. Production files, the stopped revision, report publication and paused automations have not been changed or restarted.

## Implemented changes

1. Fidelity-first reading for explicit parent-queue execution. Full-page visual observation, transcription and independent fidelity review precede the single complete reading of the accepted reconstructed text. The discarded preliminary native read is omitted only when reconstruction succeeds. Failed/disabled reconstruction keeps native fallback and the existing insufficient-evidence gate.
2. Bounded cross-paper dispatch. One supervisor and one shared Execution lease visit the original frozen candidates once in order. Queue suspension yields to the next sibling, while qualified results commit independently. Any unresolved admitted job still gates the next batch, including when a later sibling exhausts a budget or opens the shared writer circuit. Cancellation and integrity failures do not dispatch further siblings.
3. Bounded reading transport. Up to four same-page adjacent missing chunks, at most 10,000 source characters, share one queued request. Every source chunk retains its ID, page, offset, independent note and quote validator. Each member consumes its original finite operation slot/ordinal. Atomic group admission cannot add members to an existing job, change order, transfer a slot or rebind an older single-chunk job. Failed members receive only the original bounded repair ordinal.
4. Revision presentation finalization. New explicitly authorized revision contracts can freeze validated committed science into a presentation checkpoint. Selection has a separate finite operation and a 600-second per-job window bounded by the original issue/attempt deadline; this is not a measurement or guarantee of a remote model's execution time. Successful selection/crops revalidate before the unique ready seal. Expiry, exhausted budget and invalid immutable selection become durable blocked states.
5. Complete resumable observations. Bounded queue sets retain roles, operation bindings, current/expired claims and pending/answer-available/expired state. Queue-observation contention suspends rather than spends an issue failure. Expired science jobs explicitly require authorized recovery; the next supervisor launch is refused before new attempt accounting. Expired stop intent is persisted before budget settlement so a settlement-write interruption cannot erase it. Role separation, exact queue generation and answer integrity remain mandatory, including reading cache reprojection paths.

No candidate/quota/deadline/stage-budget expansion, new paid backend, concurrent writer to the same issue, review removal, source truncation, page sampling or publication authority is introduced. The section-classifier proposal is not included.

## Evidence of efficiency

All implementation validation uses temporary roots and fake/offline model answers. These are measured task counts and orchestration rounds, not measured production model throughput.

- Fidelity-first alone: 2-page evidence fixture 20 to 13 requests; 8-page fixture 80 to 52. Full-page and independent review counts are unchanged.
- Cross-paper dispatch: two candidates across 14 stages need 14 answer/resume rounds instead of 28, with the same 28 unique scientific jobs. An eight-candidate first pass admits exactly one first-stage job each, in original order.
- Real-document transport planning: repaired source chunks for ZX, DriveWorld and WebWorld are unchanged at 20 + 39 + 62 = 121. The deterministic bounded planner creates 16 + 24 + 30 = 70 requests. It still requires 121 separately validated notes. The 42.1% reduction is a request-count calculation, not a wall-clock benchmark.
- Combined queue integration: a 2-page fixture completes all 7 final chunk notes through 8 actual temporary queue jobs (2 observations, 2 transcriptions, 2 independent reviews, 2 grouped reading jobs); restart creates no extra jobs. Writer and scientific review remain required afterwards.

The production audit's 274 new jobs included 34 native reading jobs, 45 visual jobs, 102 fidelity jobs, 75 repaired-reading jobs and 18 author/writing/review jobs. Creation-to-answer intervals include queuing and scheduling, so they are not pure model runtimes. Real 8-paper + 2-repository deadline performance remains unverified.

## Cache compatibility

`reading.py`, `visual_reading.py`, `visual_fidelity.py` and `paper_document.py` retain their exact bytes. Original source/chunk/model/settings fingerprints and valid lower-stage notes can be reused normally. Already-bound single-chunk operations use the original prompt and job identity. A new persisted group manifest applies only to unadmitted missing work. Neither hashes nor stored PASS labels are migrated.

Changes to `editorial.py` intentionally invalidate aggregate deferred draft/review reuse. The new reading transport manifest can also affect downstream aggregate evidence identities. Do not assume all final reviews are free to reuse merely because lower-stage notes match.

## Future-new-issue enablement

After this code release is separately approved and installed, newly enrolled cloud issues using `parent_queue` and an explicit Execution use bounded dispatch/reading automatically. Fidelity-first applies when fidelity is enabled. Legacy workflows without an Execution and non-parent backends keep their original routes. Limits remain from the normal frozen configuration; no performance flag grants new quota.

For a future explicit revision, authorize its fresh contract with `presentation_policy=production_revision.PRESENTATION_POLICY` to enable the presentation checkpoint path. Historical contracts without that policy cannot use a fabricated presentation checkpoint to bypass a scientific expiry stop. A validated authorized presentation transition can finish frozen committed science without resuming expired scientific jobs.

Code installation is separate from resuming generation, publishing a report or re-enabling schedules. None of those actions is performed by this release preparation.

## Current stopped revision and remaining limits

- The historical stopped production revision has a different frozen source contract. It cannot be resumed under this release by editing its source hash, reinitializing a budget or moving state to a fresh root. Existing migration handling for an uncertain expired active attempt is not a general authorization to upgrade an already settled stopped issue. A separately reviewed recovery/new-contract decision must preserve all used/exposed budget, candidate lineage, original job bindings and committed evidence.
- Revision-aware replacement of expired scientific operations is still unsupported. The state is blocked with `expired_requires_authorized_recovery`, not automatically retried. No nonce, expiry edit or full new budget is created.
- Uncommitted failed/ineligible materials may repeat local validation while siblings wait. Exact model caches and finite job identities remain bounded, but local CPU/runtime is not guaranteed free. Failure is not cached permanently because later answers or eligibility evidence could change its meaning.
- Presentation selection is currently serial. Its failure can block the entire issue seal, and the new presentation blocked state has no automatic partial-report exit. It avoids repeated admission, not all possible waiting.
- The independent section-classification defect remains a separate proposal. Nonstandard real method headings can still be conservatively rejected; that proposed schema change would invalidate older document caches and is not silently included here.

Safety/correctness acceptance, production deployment approval and real 8+2 throughput acceptance are separate decisions. Final test results and fixed-tree hashes are recorded with the release evidence.
