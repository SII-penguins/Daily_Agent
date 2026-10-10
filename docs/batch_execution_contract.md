# Issue-scoped batch execution contract

`daily_agent.batch_execution` is an explicit issue-scoped ledger. The opt-in
adapter in `incremental_issue` and `pipeline` now connects it for newly enrolled
cloud + parent_queue issues. Existing and unknown legacy issues are not migrated.
See `incremental_execution.md` for activation, lock order, and recovery. This
implementation changes neither configured budgets nor the 8-paper/2-repository
soft targets, and is not a throughput or delivery guarantee.

## Identity and ownership

Use the original planner-assigned batch ID under a stable issue directory:
`<issue>/batch-execution/<sha256(original_batch_id)>/journal.json`. The caller
must persist that ID once; a resume is not a new original batch. This path does
not depend on `cloud_cache._CODE_VERSION` or the whole source tree. No discovery
of alternative ledgers or budget reset is permitted after a conflict.

`candidate(record)` freezes the full enriched input, its SHA256, material key,
version, actual source aliases, chunk/page identities and required pages. The
candidate list itself preserves the original order. A protocol string explicitly
versions this contract. The effective reading/writer settings, budgets and chunk limit are read from
`config.sources` at creation and compared on resume; incompatible changes fail
closed rather than starting over. Missing or invalid configuration also fails
closed (no duplicated hard-coded fallback constants).

An initialization marker prevents a missing journal from being mistaken for a
fresh budget after an earlier successful open.

An explicit `Execution` context owns `workflow_state.exclusive_lock` for its
lifetime. Share this object among threads, join the threads before leaving the
context, and never use a `ContextVar`. `RLock` serializes mutations inside that
process. Another process cannot recover an active owner's reservations. The
existing atomic JSON/read helpers preserve old bytes on invalid reads. A failed
journal write poisons the instance until it is closed and reopened.

## Five budgets and conservative recovery

The five separate pools are `native`, `repaired`, `visual`, `fidelity`, and
`primary_writer`. Both reading pools freeze `reading.run_budget_seconds`;
visual/fidelity freeze their respective reading settings; writer freezes
`llm_writer.run_budget_seconds`. The current repository values are respectively
1800, 1800, 600, 7200, and 14400 seconds; the module does not hard-code them.

`with execution.stage(pool)` atomically reserves **all** remaining time before
entering the body. Concurrent windows sharing a pool are reference-counted and
charge their monotonic union: three overlapping 10-second windows cost 10
seconds. Disjoint windows accumulate. Other pools have independent windows.
The last exit settles in `finally`, including `PendingResponse`, which inherits
`BaseException`. The reservation token identifies one durable settlement;
re-reading/repeating that settlement cannot refund twice.

Only enclose active stage execution. Exit before parent-queue exit-75 waiting,
sleeping or polling. These are local stage-process windows, **not measured model
runtime in a parent worker**. The yielded local deadline is for the transport to
enforce; admission rejects an exhausted deadline. A ledger cannot forcibly stop
an external model. A window that runs beyond its budget records the measured
elapsed time but charges at most the reserved amount, never a negative balance.

On lease reacquisition, any unfinished reservation is charged separately as
`forfeited`, with `measured: null`. Unknown hard-kill time is never asserted to
be measured active time and never refunded. This intentionally exhausts that
pool's remaining budget; recovery does not silently restore capacity.

## Finite operation slots and circuit

`admit(material, phase, substep, ordinal, exact_input, queue_job_id=...,
queue_role=...)` requires a reserved active stage. Alternatively supply an
explicit nonnegative `retry_generation`. Slots are keyed by frozen material,
phase, substep and zero-based ordinal; the slot then permanently binds exact
input SHA256, protocol, queue job/generation and the unchanged queue role.
An identical resume returns the identical slot. Changed content or a changed
job/generation cannot create a replacement slot. A deliberate retry must consume
another already-allocated ordinal; unsupported extra retries fail closed.

Allocated capacity per frozen material:

- Native: `chunk:<id>` ordinals 0 and 1, up to the frozen chunk cap
- Repaired: a once-only `bind_repaired(record)` replaces that material's repaired
  topology with the actual reviewed derivative document and chunk IDs, up to the
  same frozen chunk cap; it cannot append slots after any repaired admission
- Visual: `page:<number>` ordinal 0 for each required page
- Fidelity: `transcribe:<number>` and `review:<number>` ordinals 0 and 1 per page
- Paper primary writer: `draft`, `rewrite`, `presentation`, `scientific_writer`,
  `scientific_review`, `author_research`, `author_review` each ordinal 0;
  `semantic` ordinals 0 and 1
- Native-policy PDF only: `native_visual_selection` ordinal 0;
  `semantic_overflow` ordinals 0 and 1 (one additional <=8-page complete-field
  pack per existing initial/rewrite review); `scientific_writer` ordinal 1
  (one local missing-number correction). Strict-policy slots are unchanged.
  These are finite exact-input slots in the original `primary_writer` pool,
  not a new time allocation or a way to retry a sealed job
- Repository primary writer: its own material-bound `draft` ordinal 0

The author's original eligible ordered subset is frozen once with
`bind_author_candidates`, under the original configured per-batch cap (maximum
12), never reset for each singleton. Full author proposal/reviewer evidence is
preserved under `raw.author_research_evidence` as additive audit data.

Operation accounting categories do not rename queue roles: e.g. the queue role
`review` remains `review`, even under the `fidelity` budget.

`writer_failure('backend'|'json'|'schema', detail)` durably opens the original
batch circuit on the first such failure. It blocks subsequent primary stage
entry/admission across resumes. Pending responses must not call it. Other pools
remain independent. The module intentionally does not guess which arbitrary
exception is a backend/schema failure; the future transport adapter owns that
explicit classification.

## Completion transaction and trust

1. `prepare_completion` writes a content-addressed immutable envelope containing
   full record, draft, editorial review, scientific result, evidence, and asset
   hash manifest, plus material/input/protocol identity
2. `commit_completion(..., validator=existing_review_validator(config))` loads
   the exact envelope, revalidates it, then atomically publishes its journal
   reference
3. `completed(..., validator=...)` only returns referenced, hash-verified,
   revalidated envelopes. Unreferenced orphan envelopes are never promoted

Crashing between steps 1 and 2 leaves an inert orphan. Retrying the same write is
idempotent. Crashing after step 2 leaves a recoverable reference. A changed or
missing referenced envelope fails closed, including on journal reopen.
Library/editorial views are future rebuildable projections, not the authority.
Completion is neither publication nor a permanent claim on delivery quota.

The supplied read-only adapter deserializes the full objects and uses the
existing `deferred_review_cache._supported`, offline `editorial.review_draft`,
and `_assets(save=False)` checks. Thus a bare `PASS` cannot establish qualified
paper completion. Completion records must also preserve the frozen source/version
and enriched input identity, allowing only explicitly declared output fields
and existing reviewed-transcription documents retaining the exact native input.
It does not call a model or repair assets. Scientific analysis
and author research remain additive as today: their result envelopes are
retained as opaque data, not trusted positive approvals. Consumers must use the
existing science/author validation before reusing those claims (as rendering
already does with `reviewed_analysis`); new native daily-selection contracts require independent scientific analysis;
strict historical contracts retain their existing gate. The callback argument is
an explicit trusted integration/test seam, not a model-supplied approval flag.

## Integration boundary

Only newly enrolled cloud + parent_queue issues enter the incremental adapter.
The constructor rejects blocked supplied states. Production callers additionally
hold authoritative publication locks and read seals/receipts before generation;
prepared/sending/accepted/uncertain/confirmed issues are never regenerated.
Stable planner and budget protocol markers prevent a missing source-hash cache
or planner from manufacturing fresh execution capacity.

Explicit adapter shape:

```python
with Execution(issue_dir, saved_batch_id, [candidate(r) for r in enriched],
               config, protocol="batch-execution-v1", issue_state=issue_state) as execution:
    with execution.stage("native") as window:
        slot = execution.admit(record.key, "native", "chunk:c0001", 0,
                               exact_prompt, queue_job_id=existing_job_id,
                               queue_role="reading")
        # Existing transport receives window["deadline"]. Pending propagates.
    sha = execution.prepare_completion(record.key, record=record.to_dict(),
        draft=draft.to_dict(), review=review.to_dict(), science=science_result,
        evidence=complete_evidence, asset_hashes=verified_assets)
    execution.commit_completion(record.key, sha,
        validator=existing_review_validator(config))
```

Existing callers that omit `execution` retain their legacy path. The explicit
transport computes exact prompt/image/queue-role identity under the queue lock,
binds the actual active queue generation, and clamps timeout to the local stage
remainder. Pending responses propagate through finally settlement. A replaced
active generation cannot reuse an occupied finite slot; unsupported expired-job
retries fail closed rather than gaining capacity. No new backend is enabled.

### Required science in new native contracts

Native contract settings include `native_qualification_contract` with exact
`schema_version: 1` and boolean `require_scientific_analysis: true`. Journal
validation rejects missing, false, numeric or string substitutes, even after
outer checksums are recomputed. Final completion uses `_supported`, which
requires native CORE plus exact independently reviewed science. The separate
CORE draft cache grants no completion rights. Source marker schema 2 and live
configuration cannot be deleted or weakened to bypass noncloud/cloud selection
or ready-report revalidation. No old sealed record or strict contract is migrated.
