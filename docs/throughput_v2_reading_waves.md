# Bounded reading admission waves (v2 proposal)

Public-fixture note: the measurements below describe their dated validation checkpoint. The public tree uses synthetic text with the same page/chunk geometry and planner assertions; it does not redistribute the original full-paper fixtures. See [fixture provenance](../tests/fixtures/README.md).

This change is isolated from production and the accepted v1 release. It changes scheduling and local I/O only. Existing frozen production revisions cannot adopt its new source hash in place. No real models, generation, publishing or automation resumption have been authorized for this proposal.

## Scope

Reuse `reading.concurrent_reads`, clamped to the original 1–4 range, as the per-paper unclosed reading-job window. The repository setting is 3. All original admitted unanswered jobs occupy the window on every resume, including repairs and later manifest groups. A new resume does not add another full window. Existing answered groups can project validated notes without new inference. Each material retains its manifest, source spans, prompts, finite per-chunk slots, and ordinal 0/1 limits.

Only ordinary PendingResponse yields to another frozen reading group. Expiry, cancellation and integrity errors stop further local admissions. The whole batch still has one supervisor, Execution lease and original candidate order. Two papers at width 3 produce at most three first-wave reading jobs each, leaving capacity for the second paper. No new worker, queue quota or budget pool is created.

The actual global pending limit remains 200 and is checked under the existing queue lock before admission. For explicit executions, a full queue now produces a typed capacity wait with no invented job ID. Any already-admitted batch jobs remain in the wait set. CLI, supervisor and prepare preserve the capacity diagnostic as exit 75, without increasing the issue failure counter. Waiting/resuming still consumes the original runtime and resume allowances.

Two narrow I/O reductions preserve every original validation: identical serialized note bytes skip atomic rewriting; one locked wait-set observation reads a shared job file once while still checking each operation's binding independently. No approval, failure or claim is cached across resumes.

## Real document fixtures, offline queues

The same repaired-document fixtures used by v1 retain 121 chunk notes and 70 exact jobs: ZX 20 chunks / 16 jobs, DriveWorld 39 / 24, WebWorld 62 / 30. All answers below are deterministic fixture responses, validated through the real queue and reading code.

| Width | ZX wait rounds | DriveWorld wait rounds | WebWorld wait rounds |
|---|---:|---:|---:|
| 1, v1 behavior | 16 | 24 | 30 |
| 2 | 8 | 12 | 15 |
| 3, current configured value | 6 | 8 | 10 |

A wait round ends when all offered fixture answers are imported before the next resume. Real workers may answer partially or at different rates; existing unclosed jobs then continue occupying the window. The benefit is fewer supervisor resumes and better utilization when one or two papers remain. When enough independent jobs already saturate the real same-role workers, this does not guarantee a faster steady-state throughput.

`evidence/reading-wave-profile.json` records total offline resume time plus inclusive planning, receipt validation, pending-queue scan, cache-note validation and projection timings. Function spans overlap and must not be summed as independent components. It excludes real subprocess startup, real model execution, answer latency and production filesystem contention. It is not an 8+2 deadline benchmark.

## Deferred presentation optimization

Presentation remains serial. Starting two selection jobs together also starts two immutable 600-second deadlines and accrues both exposure debts. Without proven reader/selector capacity this can worsen expiry. A future change would need a separate bounded wave and full pending-result/terminal-state review; this proposal does not implement it.

## Validation obligations

Cover no-answer resume, partial-answer refill, repaired subgroup occupancy, all real fixture notes, exact replay with zero added jobs, real full-200 queue, CLI/supervisor/prepare exit75 with zero failure increments, two-paper fairness, cancellation/expiry/integrity, queue claim/import contention, and budget conservation. Keep all four lower-stage scientific protocol files byte-identical. Final combined source must receive independent review and a stable full test run after the separate verified-visual optimization is integrated.
