# Daily Agent throughput release v2

Public-fixture note: the measurements below describe their dated validation checkpoint. The public tree uses synthetic text with the same page/chunk geometry and planner assertions; it does not redistribute the original full-paper fixtures. See [fixture provenance](../tests/fixtures/README.md).

This implementation includes the accepted v1 release and two further bounded improvements: small waves of independent reading groups, and a compact asset inventory derived from fully transcribed and independently reviewed pages. Scientific coverage, original finite operation slots, issue budgets and publication checks remain required.

## What changed

- Reading reuses `concurrent_reads` (original range 1–4; configured value 3) as a per-paper unanswered-job window. All earlier admitted single/group/repair jobs occupy the window across resumes. One pending group no longer prevents another eligible group within that window from entering the queue. The batch still advances in frozen candidate order under one supervisor and one Execution lease.
- A full global queue (original cap 200) yields an explicit capacity wait before any new operation is admitted. It does not invent a job ID, consume failure allowance or hide existing batch jobs. Expired jobs still require authorized recovery; no new retry capability was added.
- Successful complete fidelity reconstruction provides a distinct, verified page-asset inventory. It avoids the redundant standalone native-text/image pre-screen for new, unbound pages. Full page transcription, separate image review, complete reading and independent scientific review remain. Failed/partial reconstruction uses the original native visual fallback. Existing admitted visual jobs retain their exact identity and must be resolved normally.
- The new inventory stores hashes and references to original accepted fidelity assets. The writer receives compact coverage/provenance metadata rather than a duplicate of all asset text. Selection continues to inspect original pixels.
- Identical note bytes are not atomically rewritten on every resume. Each locked wait-set observation reads a shared job once and still validates every operation binding. No PASS/FAIL or claim validation is cached across resumes.

## Measured offline evidence

These tests use temporary real queues, original finite ledgers, deterministic fixture answers and actual source documents where stated. They do not measure real model latency or prove the 8-paper + 2-repository deadline.

1. Same 2-page combined fixture: v1 uses 8 jobs (2 visual, 2 transcription, 2 independent review, 2 grouped reading). V2 uses 6 (the same transcription/review/reading jobs), yielding the same 7 individually validated chunk notes. Completed replay adds zero jobs; scientific approval remains absent until its separate gates run.
2. Real repaired ZX / DriveWorld / WebWorld fixtures: 121 chunk notes still require the same 70 grouped reading jobs. Width 2 produces 8/12/15 actual answer-resume rounds; width 3 produces 6/8/10. Width 1's sequential dependency is 16/24/30. Partial answers occupy the window until answered; no-answer resumes create no new jobs.
3. Offline width-3 local resume medians were approximately 0.139/0.217/0.296 seconds for those three documents. These exclude subprocess startup, answer import, model waiting and production filesystem contention. Inclusive timing breakdowns overlap and are not additive. They were sampled during focused validation, not claimed as production speedups.
4. A 16-page visual fixture preserves all 16 transcription and 16 independent-review calls while removing 16 redundant pre-screen calls (48 to 32 calls in that portion). Writer visual metadata shrinks from 2,634 to 456 UTF-8 bytes; all accepted asset contents remain available in the original fidelity evidence.

The historical 45-page visual sample was ZX / QUFIG / DriveWorld. The 121-to-70 grouped-reading sample is ZX / DriveWorld / WebWorld. These are different samples; their savings must not be added into a supposed single-report total. More pending work does not guarantee higher steady-state throughput when the available same-role workers are already saturated.

## Compatibility and enablement

Future newly authorized cloud + parent_queue issues with explicit Execution use these paths automatically when fidelity is enabled. Legacy execution=None and disabled-fidelity behavior stay on the original paths. No queue capacity, worker count, candidate limit or budget is raised.

The four lower-stage files (`reading.py`, `visual_reading.py`, `visual_fidelity.py`, `paper_document.py`) remain byte-identical to v1 and production. Their exact source/protocol/model cache checks remain active. Aggregate editorial reuse deliberately invalidates when the editorial/visual-inventory/consumer protocol changes. This is not a promise that all previous drafts or reviews can be reused.

The historical paused production revision has a frozen old source contract. It cannot adopt this code by editing its source hash or by reinitializing a new full budget. Any future restoration needs an explicitly reviewed migration/new-contract decision with existing evidence and budgets preserved. At that validation checkpoint, generation, publication and recurring tasks remained paused; code delivery did not authorize restarting them. Current operational status must be checked privately.

## Remaining limits

- Revision-aware expired operation replacement is not implemented; it stops for authorized recovery
- Uncommitted failed materials may repeat local checks while another material waits
- Presentation selection remains serial; its failure can block the issue and there is no automatic partial-report publication
- The separate section-classifier proposal is not included
- Real 8+2 throughput and the next production deadline have not been accepted by these offline tests

## Validation evidence

The release evidence sibling directory records independent reading review (148 focused tests) and independent visual review (118 focused tests) by scope. Those overlapping suites must not be summed into a unique-test count. The combined final whole-repository run and its identical before/after source/test/fixture manifest are the final integration check. Earlier defect reproductions and intermediate logs are retained with their actual outcomes.
