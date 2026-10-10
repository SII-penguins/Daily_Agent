# Proposal: supervised revision presentation finalizer

This isolated proposal changes no running revision or existing production data.
New authorizations opt in using `presentation_policy=production_revision.PRESENTATION_POLICY`.
The policy enters the immutable revision identity. Historical contracts omit it
and retain their existing API and sealed handoff behavior. Do not add the field
to an old contract or change an old source hash. The historical stopped production revision is not migrated by
this proposal.

## State transitions

1. Supervised science generation retains its existing frozen candidates,
   enriched inputs, batch execution, reviews and immutable completion envelopes.
2. After the last paper/repository is committed, the worker revalidates the
   journal references and writes `presentation/checkpoint.json`. It returns
   `state=awaiting_presentation`, with no ready seal. A cutoff freeze enters the
   same checkpoint using only committed, currently eligible rows.
3. The next `run_generation` launch detects that checkpoint before entering any
   science pipeline. It verifies the exact completion hashes and approved rows,
   then derives display copies. Only `material.raw.paper_visual_selection` and
   `material.reading.paper_visual_assets` may differ. Carry and repositories stay
   byte-for-byte equal. A changing scientific base or current publication state
   fails closed, rather than silently altering the frozen selection.
4. Each selected new paper may bind exactly one existing queue operation via
   `parent_writer.request(execution=..., operation=...)`. Source pixels and page
   hashes come from the existing visual-selection implementation. At most five
   crops are extracted from the original PDF. Real claim/answer/worker and source
   evidence are captured and independently rechecked. Empty selections require
   explicit coverage/gaps; invalid/error states never count as complete.
5. Pending operations preserve the checkpoint. They never reread science or
   replace a queue generation. Once all display rows validate, the supervised
   finalizer alone seals the report; existing handoff/Site binding follows the
   original exact-byte procedure. First handoff preparation rechecks immutable
   display evidence/crops again, independently of the elapsed generation deadline;
   an already-created immutable handoff remains readable. The public bare `seal_ready` rejects new-policy
   contracts, preventing accidental bypass of the checkpoint.

## Bounded work and honest accounting

The proposed selection-validity window is explicitly frozen at 600 seconds per
paper, one operation per paper, at most five crops. It is not a report-wide
600-second window. The current minimal finalizer admits selections serially:
it suspends on the first ordinary pending response, so one pass does not enqueue
all eight papers. The total end-to-end bound has not been measured; this proposal
cannot promise eight papers in ten minutes. Each later paper starts its own
finite window, still limited by the shared original issue budget/deadline.
Normal unclaimed, unexpired pending work remains awaiting; it is not terminal.
This is an initial finite
policy, not a claim that all selections will finish in ten minutes. A read-only
production queue sample (6 page images, with exact runtime identifiers
retained only in private validation evidence) took 193.024404
seconds end to end. This includes waiting and is not measured model compute. It
is only one sample; eight-page selections and subsequent verification can take
longer. Thus 90 seconds is not a defensible universal bound and 600 remains a
bounded initial choice requiring observation. Production
adoption should compare actual end-to-end selector observations before choosing
or approving a different frozen policy.

The original transport argument remains 90 seconds; that is metadata, not a hard
native-worker runtime limit. New jobs have `expires_at` clipped to the immutable
operation deadline, which is also limited by remaining issue wall deadline,
remaining issue runtime and the active supervised window. Existing shared jobs
with incompatible later deadlines are rejected, never rewritten or replaced.
Claims cannot outlive the queue deadline; expired imports are rejected.

`presentation/operations/<key digest>.json` records real UTC admission time,
deadline and the exact queue input/job. An unclosed operation accrues elapsed
wall time continuously, including waiting, restart and failure. A valid immutable
result closes its accounting interval at final validation time. These intervals
are added to existing supervisor runtime for future admission and final checks;
overlap is deliberately double-counted conservatively. No budget file or science
Execution slot is invented to represent external work. No full allowance is
created on resume. A timestamp preceding initial admission is rejected. This implementation assumes
a trustworthy UTC clock; it does not maintain a durable high-water timestamp
for a rollback within the admitted interval, so that narrower clock-adjustment
case is not claimed to be detected.

This protocol limits authorized answer acceptance and subsequent admission. It
cannot physically terminate an independently running native selector after its
lease expires. Such a worker must obey its assignment deadline; its late output
is refused. Do not describe the queue TTL as proof of process termination or
measured model compute. An expired operation remains blocked; retry requires a
separately reviewed explicit protocol, not a nonce or new operation slot.

## Files and integration boundaries

- `production_revision.py`: identity opt-in, checkpoint, pure committed-envelope
  verification, finite presentation operation, supervised finalizer and gates
- `paper_visual_assets.py`: optional execution/operation forwarding on
  `request_visual_selection`; old API unchanged by default
- `parent_writer.py`: optional explicit execution `transport_deadline()` hook;
  all ordinary callers remain unchanged
- `tests/test_revision_presentation.py`: offline PDF/queue/state-machine fixtures

The parallel bounded-batch proposal may replace the CLI exception print with:

```python
from daily_agent.batch_dispatch import response_payload
print(json.dumps({**response_payload(exc), 'issue_date': str(day),
                  'revision_id': a.revision_id}))
```

This proposal does not copy or modify its pipeline/editorial/batch-dispatch
sources. Integrate that small print change only when the module is present, then
run a single stable-source acceptance suite.

A current live revision cannot hot-enable this protocol: its source/identity and
operations are frozen. Any later migration must explicitly preserve scientific
completion hashes, candidate universe, original batch/queue identities, spent
and reserved resources and retirement authority. It must not reread completed
science or create fresh model slots. No live migration is performed here.

## Known publication-availability limit and separately reviewable next step

This proposal deliberately implements fail-closed completion only. An expired
selection, bad immutable answer/crop, or invalid claim blocks the whole finalizer.
It does not turn into a scientifically failed paper, but it also cannot currently
produce a sealed limited report. The operation deadline is immutable and a bad
answer cannot be overwritten, so repeating `generate` cannot fix this condition;
the controller persists `presentation_blocked` with reason, exhaustion time,
original operation/job identities and an immutable notification key. Subsequent
`run_generation` returns the same payload before admission, without another
subprocess, resume increment or failure increment. The CLI returns exit 78 and
`auto_resume:false`; callers should notify once using `notification_key` as their
deduplication key. No external notification is sent by this module. Corrupt or
missing marked terminal evidence fails closed. Operators must decide what to do
with the blocked report rather than continue blind resumption. This is an unresolved publication
availability limitation, not a claim that complete science was lost.

A minimal separate follow-on design would freeze an explicit display-failure
policy in the new authorization. Each operation would close exactly once with
one of `verified_assets`, `reviewed_absence`, or `presentation_unavailable`.
For the last state, immutable evidence would preserve the original science
completion, exact failed/expired job and claim, failure code, source PDF hash,
actual elapsed window and explicit reader-facing missing-image explanation.
No new job, changed answer, renewed deadline or revised scientific verdict would
be allowed. The finalizer would independently check the failure evidence and
seal an explicitly limited report only if that frozen policy permitted it.
Scientific approval, carry rows, quotas and claim/source checks would remain
unchanged. Missing source or no candidate pages must never be labelled pixel-
reviewed absence. Tests must distinguish honest unavailable state from corrupt
provenance: unverifiable/contradictory failure evidence would still block.

This follow-on is a design proposal only; current contracts do not gain a silent
fallback. Root must review its exact report wording and gate semantics before
implementation/integration.
