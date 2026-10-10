# Restart-safe storage and recovery

The public source and report-artifact branches are described in [GitHub persistence](github-persistence.md). They complement, and do not replace, the private exact-source and complete-state recovery pair below.

## What this repair changes

The executor filesystem is a cache, not the durable store. The authoritative
recovery inputs are two separately versioned private Library artifacts:

1. The verified source bundle, including this bootstrap and the native-reading,
   original-image, independent-review and mandatory-science fixes
2. The latest verified **complete production-state snapshot**, containing config,
   caches, original images/PDFs, jobs, claims, answers, generation/batch ledgers,
   deadlines, attempt counters, delivery identities and publication history

The Site remains a third, independently owned remote archive. A state ZIP is
not a backup of the Site source/history. Refresh its authoritative source and
ownership before any future Site update. Never replace its existing history
with the restored production files.

This repair does not activate paused schedules, authorize a fresh issue, reset
an expired issue, send a report, publish a Site, or deploy old repairs. A prior
local workspace disappearing proves file loss, not the infrastructure cause.

## Host boundary: use the current Library workflow

Python here has no Library credentials or undocumented storage client. The
host uses the current Library skill and its current download/upload helpers.
Resolve the **current metadata**, preserve the exact library_file_id, file_id,
version, and returned path, and materialize that version locally. A local ZIP,
a guessed ID, a copied expected receipt, or a successful upload without its
per-item result is never remote backup evidence.

Keep a small recovery registry durably beside the artifacts, also outside this
executor. Each source/state receipt has exactly:

    library_file_id, file_id, version, sha256, size

Version 0 is valid. The hash/size bind the materialized bytes. Update an existing
state identity with expected_current_version from freshly resolved metadata;
never remove CAS to evade a conflict. Persist the successful returned version,
request correlation and independently materialized matching bytes. Keep the
previous known-good source/state receipts until the new pair is verified.

If the remote head is newer than the registry, reconcile the interrupted write
against its saved manifest and actual write result before changing the registry.
If any required receipt was lost, stop and inspect authoritative records. Do
not silently select an older state or upload an old local snapshot over it.

## Empty-workspace bootstrap

The host first retrieves the current source/state using Library. After checking
the whole source archive SHA256 against the durable source receipt, it can
extract just src/daily_agent/durable_recovery.py from that verified source
bundle. That file runs directly with Python 3.11+ and only the standard library;
there is no dependency on a former checkout, virtualenv or local journal.

    python durable_recovery.py bootstrap \
      --source-archive VERIFIED_SOURCE.zip --state-archive VERIFIED_STATE.zip \
      --registry trusted-registry.json --current fresh-library-observations.json \
      --destination NEW_EMPTY_DIRECTORY

The two JSON files must come from distinct roles: registry is the durable
expected head; current is the host's fresh Library observation. Copying one to
the other is only suitable for unit fixtures, never production verification.

The command validates every member, rejects duplicate/unsafe paths, symlinks,
missing assets, corrupt bytes, stale heads and source lacking the recovery
fence. It restores source and state together via a sibling staging directory.
It refuses an existing target. An interrupted staging directory is never an
active runtime. The recovered state always starts with a mutation fence.

For a future authorized deployment at the former runtime root, use
`--destination /workspace/shared/Daily_Agent --state-directory data/cloud` only
when that entire destination is absent. Source lives under `source/`; state is
back at `/workspace/shared/Daily_Agent/data/cloud`, preserving absolute evidence
and configuration paths. Use `PYTHONPATH=/workspace/shared/Daily_Agent/source/src`.
For an isolated drill use the default `state/` layout and keep the fence.

Do not rewrite hashes, sealed source paths or cache identities just to make
relocated evidence pass. Restore at the original state path where possible.
Otherwise retain all bytes, explicitly revalidate moved paths and let the normal
source-evidence checks reject any genuinely stale cache. Never create fresh
budgets or issue IDs as a shortcut around a frozen old contract.

## Recovery fence and owner reconciliation

`python durable_recovery.py inspect --root RESTORED_STATE` is read-only. It lists
retained active/nested attempts, claims, uncertain deliveries, and path changes.
Actual mutation entrypoints and atomic writes reject a fenced root, including
queue claim/import/retry, generation, revision recovery, schedule repair,
retention cleanup and cache asset repair. A local PID missing in a new namespace
does not prove the previous executor or worker stopped.

Releasing the fence requires a review bound to the exact source/state receipts,
a named reviewer, and structured verified outcomes with evidence references:

- Previous executor: stopped
- Claim ownership: resolved, with each job/worker/generation matched to
  worker_completed or owner_stopped; unknown, live or merely expired is blocked
- Uncertain delivery: preserved_blocked, or verified_no_uncertain_sends only
  when none exist; existing accepted/sending/uncertain ledgers stay unchanged
- Site archive ownership: verified (diagnostic_no_site only for a fixture)
- Absolute paths: original_root_verified, or relocated_revalidated

Use `release --root ... --review host-review.json` only after these checks.
Retained active/corrupt attempts, nested cleanup, outstanding reservations or
an interrupted durable batch remain blocked and require their existing explicit
reconciliation/migration path. Release neither grants user authorization nor
changes budgets, attempts, claims, answers, deadlines or delivery state. New
work still requires authorization and a valid new issue window.

## Durable checkpoints and interrupted uploads

`daily_agent.durable_checkpoint` supplies offline host-adapter contracts:

1. prepare_checkpoint snapshots the whole settled state into an external
   control directory and records its parent Library head, owner and boundary
2. begin_upload validates fresh current metadata and persists uncertainty before
   returning the exact replacement request with expected_current_version
3. The host uses the current Library upload workflow, refreshes metadata and
   materializes the actual resulting version into a separate file
4. commit_checkpoint validates successful correlated write evidence, the current
   head and the independent materialized archive before recording commitment

After interruption, reconcile_checkpoint requires freshly materialized current
bytes. An unchanged parent permits a bounded retry with CAS. A changed head,
corrupt readback, newer competing state, or target bytes with no recovered write
result fails closed. Metadata alone cannot fabricate a missing success receipt.

The control directory itself is ephemeral. The host must preserve its manifest,
request correlation and committed receipt in the durable recovery registry.
If both those records and the executor disappear, leave the write ambiguous;
never infer that the earlier remote operation failed and blindly repeat it.

## Preventing rollback of uncheckpointed work

`daily_agent.durable_boundary` adds a write-ahead marker inside the state itself.
Before any bounded generation/response/delivery batch, arm an intent (owner,
operation kind, unique token), snapshot the full state and verify its committed
remote receipt. Then begin the boundary once and execute only that authorized
bounded batch. Settle it, snapshot again, and verify the settlement receipt
before arming another batch.

Intent/ready/settled states block ordinary mutation. Only executing permits
state changes. Restore of intent/ready/executing is unresolved even if the old
process is absent: work might have consumed attempts or sent a message after the
last durable checkpoint. It cannot auto-resume or grant fresh attempts. This
conservatively sacrifices automatic progress when outcome evidence is missing
rather than refunding budgets or duplicating a delivery.

For sending, use two bounded phases. In delivery preparation, record begin,
settle the boundary, and commit its sending-intent snapshot remotely. Then arm
and remotely verify a new message-send boundary (which already contains that
sending ledger), begin it once, and only then invoke the actual messaging tool.
After acceptance, record the real message identity, verify readback, settle,
and commit a fresh checkpoint. Do not make an unacknowledged intermediate
checkpoint inside an executing boundary and then skip its ancestry. If the sending-intent backup is the last surviving state,
reconcile the existing message instead of issuing a new send. Never treat a
prepared snapshot from before begin as permission to retry a possibly sent item.

## Cost and stopping rules

Use complete snapshots at safe **bounded batch/phase boundaries**, after groups
of imported responses, and before/after external delivery. Do not upload the
roughly 192 MB production state for each individual model call. The full snapshot
is deliberately simpler than a delta chain and retains all source assets.
Failure to establish a durable boundary stops further work and leaves evidence;
it does not silently switch to local-only persistence. A small control receipt
is not a substitute for the assets and mutable state it references.

No exactly-once guarantee is claimed for messaging APIs. The supported safe
outcome after lost confirmation is a blocked ambiguous delivery awaiting remote
reconciliation. No new API credentials, service account, remote Git push or
always-on process is required by this repair.

## Quarantining an expired job whose prior owner is unknown

A restart does not prove that a historical worker stopped. For a separately
and freshly authorized issue, recovery may preserve one or more exact expired,
unanswered writer jobs in immutable quarantine while retaining the prior owner
as **unknown**. This is not retirement, supersession, a budget refund, or
permission to resume the historical issue.

`claim_quarantine.create_quarantine` requires the host-reviewed job and claim
SHA-256 values, expired job and claim, no answer, no active pointer or retry
lineage, the verified recovery fence, and a future issue scope bound to the
current executor/owner and authorization evidence. The scoped release validates
all completed answers or exact quarantine records before removing the fence.
It preserves the job, claim, reservations, history and original proof bytes.
Claim, response import, retry and identical request reuse remain blocked.

Scoped operation requires the durable batch marker even when one is absent.
Only the authorized issue date may mutate; new admissions stop at its deadline,
which no new budget may extend. Historical retention is disabled. Completion
receipts may still be recorded after admission closes. Reviewed fences remain
in complete snapshots, with hash-addressed history so another restart can
verify the original quarantine without rewriting immutable evidence.

The host must still establish the fresh current executor identity, verify
Library heads, reconcile external delivery/archive ownership, and checkpoint
intent and settlement using the supported Library workflow. An unknown owner,
unexpired job or claim, changed hash, live pointer, answer, retry lineage, or
missing proof cannot be silently classified as a stopped worker.
