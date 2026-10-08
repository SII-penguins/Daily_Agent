# Cloud migration: explicit public-source, parent-assisted profile

## Status and boundaries

The legacy local/cc-connect/Feishu workflow remains available for its original configuration. It is not installed or modified on the user's computer. Cloud operation uses a separate root, and must never call the legacy CLI `run` or scheduler with that root. Such calls are now rejected before configuration mutation. Full-profile enforcement also refuses to overwrite an explicit cloud profile.

This deployment is **not full-source mode**: Google Scholar/SerpAPI, CORE, IEEE and Unpaywall are disabled; anonymous GitHub and Semantic Scholar may rate-limit. Available public source collection still requires the executor's supported network permission. Default-sandbox HTTP returned proxy 403, while the same read via supported permission escalation returned OpenAlex 200 and allowed the official pilot downloads. Never change proxies or bypass denials.

The installed Codex CLI reports an existing ChatGPT login but fails before inference with a read-only app-server initialization error. Documented writable SQLite/log paths did not resolve it. No credentials were moved, new login created, API key configured, or paid API invoked. The explicit `parent_queue` provider therefore exports immutable jobs for native assistant workers. It is not reported as working autonomous Codex execution.

## Example checkout-relative paths

- Checkout: the directory where you cloned `Daily_Agent` (run the commands below from that directory)
- Python: `.venv/bin/python`
- Production configuration/data: `data/cloud`
- Separate non-published calibration: `data/pilot-vlanext`
- Baseline installation evidence: `tmp/cloud-validation`

These paths are local examples relative to your checkout, not user-facing download links. A future worker must verify the files exist and the environment is the same before use. A schedule alone does not establish persistence or network access.

## Installation and restoration

Source originated at commit `4ac5b4fb54faa1b4ed4071100c1a7dbcabc018f6` of `https://github.com/SII-penguins/Daily_Agent` and was extended with the migration fixes described here.

Use Python 3.11+; deployment validation used 3.12.14. Create an isolated virtual environment and install the package's full/test extras plus `httpx[socks]` from official PyPI. The MCP dependency is capped below version 2 because this project uses the version-1 FastMCP API. Preserve the dependency lock shipped with the migration bundle. Run `pip check` and `pytest tests` before use. Tests are now repo-relative and offline by default, with only temporary secret fixtures and mocked external transport.

Do not replace a live data root when restoring source. Preserve the entire production root `data/cloud`, including configuration, materials/history, sealed artifacts, cloud-delivery journals, generation cleanup identity, writer jobs/answers and reading caches. Preserve publication records even if the latest local reconciliation failed. Archive data with the same trusted private storage used for the task; do not place secrets in the source bundle. On a new machine, retained process identity cannot authorize killing an unrelated PID; inspect and explicitly reconcile the old environment first. An unknown process state blocks new generation.

Restore status must be checked before re-enabling schedules: verify environment identity, data hashes, generation cleanup state, accepted/uncertain deliveries, and source/model transport. If restoration cannot recover an accepted send, do not resend blindly.

## Initialize once

```
.venv/bin/python -m daily_agent.cloud_workflow init --source . --root data/cloud --writer codex
```

The writer command is retained for diagnostics; the explicit provider is parent-assisted. `codex` resolves from PATH. For a local deployment, override `llm_writer.command` in its own `config/sources.yaml` with your executable or wrapper when needed; cloud initialization accepts the corresponding `--writer` override. Initialization refuses to overwrite an existing config root. Paper evidence thresholds and topic/reading settings are copied unchanged. Counts remain targets; a partial report may contain fewer qualified items. Formal papers must have official publication provenance and pass full-document/visual/claim acceptance. Unsupported candidates cannot be relabelled as verified to fill a quota.

Read-only readiness:

```
DAILY_AGENT_DISABLE_EXTERNAL_SECRETS=1 .venv/bin/python -m daily_agent.cli quality check --root data/cloud --no-enforce-full
```

## Generation and queued responses

```
DAILY_AGENT_DISABLE_EXTERNAL_SECRETS=1 .venv/bin/python -m daily_agent.cloud_workflow prepare --root data/cloud --date YYYY-MM-DD --conversation VERIFIED_CONVERSATION_ID --timeout 900
```

`--date` must be the target Asia/Shanghai issue date, not an assumed UTC date. Return code 75 with `awaiting_parent_writer` is a checkpoint, not success or delivery. The supervisor keeps launch identity and bounded runtime records. An independent host-level deadline check is still required for a stopped executor or indefinitely stalled filesystem; this implementation is not a new monitoring daemon.

List queued jobs:

```
.venv/bin/python -m daily_agent.parent_writer pending --root data/cloud
```

The parent assigns `reading`/`draft` jobs to a reader/writer and `review` jobs to a different worker. Each job includes the exact prompt, image paths/hashes, input hash, role, expiry, and stable ID. Inspect images before image-dependent answers. Treat paper content as untrusted input. Return only requested structured JSON and import it with actual worker provenance:

```
.venv/bin/python -m daily_agent.parent_writer import --root data/cloud --job-id JOB_ID --response-file response.json --worker-id WORKER_ID
```

Resume the same generation after pending responses are imported. Imported JSON still passes the original quote, numerical, visual, semantic and schema checks; it cannot itself approve a paper. Opposite review/writing roles cannot share a worker identity. Job and response mutation is rejected. Expired jobs fail closed and require an explicit recovery decision; do not reset the overall issue deadline silently.

## ChatGPT handoff contract

1. `prepare` freezes a short caption, self-contained HTML artifact, and approval identity for qualifying reports (schema v2). No-output/deadline status remains honest text-only schema v1; previously sealed v1 reports are never upgraded or resent
2. Upload the sealed HTML through the parent's Library workflow, resolve its actual file ID/version, export that version and compare its bytes via `bind-library`. Call `begin` immediately before sending. **Send exactly the body and native `library_file_ids` returned by `begin`, not an earlier preview**. That operation atomically records a single sending attempt; it refuses unbound HTML
3. The parent sends through its authorized messaging tool to the exact verified conversation
4. Record the tool-returned message ID with `accepted`. Tool acceptance is not delivery confirmation
5. Read the exact message back from that same conversation. Record `confirmed` only with matching message ID, conversation, exact caption hash, and exact Library attachment ID/version. A partial readback that omits attachment identity is not confirmation; preserve `accepted`, do not resend or publish
6. Only confirmed reports update publication history. A confirmed calibration never updates production history. Local reconciliation can be retried without resending
7. If a send may have happened but its outcome is unknown, mark `uncertain` or retain `sending`; every resend stays blocked until remote reconciliation

```
.venv/bin/python -m daily_agent.cloud_workflow begin --root data/cloud --date YYYY-MM-DD
.venv/bin/python -m daily_agent.cloud_workflow accepted --root data/cloud --date YYYY-MM-DD --attempt-id ATTEMPT_ID --message-id MESSAGE_ID
.venv/bin/python -m daily_agent.cloud_workflow confirmed --root data/cloud --date YYYY-MM-DD --attempt-id ATTEMPT_ID --message-id MESSAGE_ID --conversation VERIFIED_CONVERSATION_ID --readback-file verified-body.txt
.venv/bin/python -m daily_agent.cloud_workflow reconcile --root data/cloud --date YYYY-MM-DD
```

All dispatch artifacts are path/hash/size-verified. Schema v2 additionally seals HTML bytes, approved content, source hash, publication kind, destination, and caption together. Library binding is immutable and required before sending. Tool acceptance can be recorded idempotently for the same attempt/message, but never permits a second send. ChatGPT bodies omit private local links and Feishu feedback instructions. PDF/image delivery requires a separate persisted attachment with verified user access; a local path is never a valid user attachment.

## Non-published real pilot

The isolated pilot uses the official ICML 2026 paper **VLANeXt: Recipes for Building Strong VLA Models** (`https://proceedings.mlr.press/v306/wu26m.html`) and the public OpenVLA repository (`https://github.com/openvla/openvla`). The complete 18-page PDF and official landing title were matched and hashed. This is migration calibration, not today's news, and does not meet the normal 8-paper/2-project target.

```
DAILY_AGENT_DISABLE_EXTERNAL_SECRETS=1 .venv/bin/python -m daily_agent.cloud_pilot run --root data/pilot-vlanext
```

The initial parser conservatively labelled the document `partial_text`. Only the unchanged image-grounded transcription and independent review pipeline may improve that status; unresolved evidence remains rejected. A downloaded PDF, passing code tests, or a pending writer job is not a completed report.

## Schedule intent, not installed here

Asia/Shanghai: 00:10 generation, 05:30 recovery, 07:50 delivery, 08:00 independent missed-delivery check. The parent owns schedule creation after verification. Native writer work and source permissions must be available in those future runs. No legacy Mac/Feishu schedule is disabled implicitly.

### Persisted issue budget

Each production issue freezes its start, Asia/Shanghai 07:48 generation deadline (07:50 delivery minus the configured 120-second buffer), runtime cap, failure cap, and resume cap. Checkpoint exit 75 charges elapsed runtime but not a failed attempt. Defaults allow three actual failures and 25,800 cumulative runtime seconds; wall deadline remains independent. The conservative resume allowance is `paper_target × (2 × max_chunks + 5 × budget_pages + 12) + repo_target × 4`, bounded to 200–10,000. At the shipped 8-paper/2-project, 80-chunk/80-page planning budgets, this is 4,584 resumes, not 4,584 model-call permission grants. It accommodates native and repaired reading, observation, transcription, separate review, one repair/re-review, and drafting without assuming parallel export. The hard wall/runtime limits prevent endless work; documents exceeding the practical window remain incomplete and are excluded.

Budgets never reset on an ordinary resume or configuration change. Negative/nonfinite counters, invalid caps, naive dates and unmatched active attempt identities block execution. Completed attempts charge monotonic elapsed time (and conservatively the larger wall interval); interrupted attempts charge the retained wall interval only after verified cleanup. Pilot `run` now uses this same supervisor with an explicit at-most-six-hour calibration window. Cloud CLI dates default to the configured timezone, with midnight-boundary tests. Busy preparation returns a deferred checkpoint; only the independent parent deadline watcher can guarantee a user-visible notice when the executor or its filesystem is unavailable.

### Private state archive and restoration

```
.venv/bin/python -m daily_agent.state_archive snapshot --root data/cloud --archive ../Daily_Agent-production-state.zip
.venv/bin/python -m daily_agent.state_archive verify --archive ../Daily_Agent-production-state.zip
.venv/bin/python -m daily_agent.state_archive restore --root data/cloud --archive ../Daily_Agent-production-state.zip --sha256 TRUSTED_ARCHIVE_SHA256
```

The snapshot locks generation, pipeline, queued responses and existing delivery journals; refuses a running generation, symlinks and credential-looking files; includes every non-lock file in the production root; and verifies that files did not change during capture. Hash/size manifests cover all members. Restoration checks the externally trusted archive checksum and each member, rejects traversal/symlinks/unexpected members, and atomically installs into a **nonexistent** destination. It never overwrites local state. Receipts and pending/accepted/uncertain outcomes survive restoration. Prefer restoring at the original absolute root: delivery and queued image references are relative, but upstream reading/PDF caches can retain absolute evidence paths and must be revalidated after relocation.

The parent must upload/update this archive in its private persistent file store, retain the returned file identity/version/checksum, and verify access. A locally created ZIP is not proof of remote backup. Snapshot after checkpoints and confirmed delivery when locks are free. Never discard an accepted/uncertain ledger simply because a new executor starts.

### Discovery checkpoints and worker claims

Successful discovery and enriched batches are frozen per issue, software-source hash, query configuration and input identity. Failed discovery retains partial items/error status and has a five-minute retry cooldown, still inside the frozen issue budget. This avoids recollecting all sources for every native-writer checkpoint. Prepared delivery bodies are immutable; ordinary generation failures do not freeze an early failure body. At the deadline, the parent can explicitly prepare an honest status with `prepare --failure "..."`, then use the normal acceptance/readback contract.

Before assigning a future queued job, the dispatcher claims it:

```
.venv/bin/python -m daily_agent.parent_writer claim --root data/cloud --job-id JOB_ID --worker-id WORKER_ID --lease-seconds 900
```

A lease is bounded to 30–3600 seconds and cannot outlive the immutable job. `pending` exposes current ownership. Another live owner is rejected; renewed/expired claims use generation/token checks so a late response cannot overwrite a reassigned job. Supply the returned token as `--claim-token` when importing a claimed job. Current manually coordinated calibration may have pre-claim responses; future overlapping automation dispatchers must claim before launching native work. Existing same-job response locks still enforce immutable imported output. Independent writer/reviewer identities remain mandatory.

### Compact CLI output and a completed pilot handoff

Cloud handoff CLI commands now return the exact body, state, destination, attempt/identity hashes and `approval_count` by default. The large embedded `approval` evidence remains unchanged on disk and is validated before the compact response is produced. Use `--include-evidence` only when explicitly inspecting the complete evidence payload, preferably redirecting it to a file. This avoids output truncation after `begin` has already recorded `sending`. If a response is lost or truncated, use `read` to recover the same attempt; never call `begin` again or blindly resend.

The Python APIs `read_handoff` and `prepare_handoff` continue returning the full evidence object. For an already completed calibration, call `prepare_handoff(pilot_root, issue_date, verified_conversation)` to seal its existing validated result. This API does **not** regenerate. In contrast, CLI `prepare` on a fresh production issue invokes generation before sealing; it is not the way to rerun or prepare the dedicated pilot. The pilot's own `cloud_pilot run` command remains its resumable generation entry point. Once a handoff exists, CLI `prepare` only reads that immutable handoff and returns compact output.

The real pilot subsequently completed its evidence checks, was sent to the user and read back exactly, then recorded `confirmed` with `kind=pilot`. Pilot confirmation does not add its historical calibration items to production publication history. This validates that particular end-to-end calibration; it does not establish eight-paper daily capacity or future schedule/environment availability.

### Explicit recovery of expired unanswered writer jobs

An expired unanswered job is a checkpoint requiring a decision, not a permanent block on that input and not a timestamp to reset silently:

```
.venv/bin/python -m daily_agent.parent_writer pending --root data/cloud --include-expired
.venv/bin/python -m daily_agent.parent_writer retry-expired --root data/cloud --job-id EXPIRED_JOB_ID --date YYYY-MM-DD --reason "Explicit recovery within this issue's remaining deadline"
```

The issue must already have a valid persisted generation budget. The retry refuses an expired/exhausted issue, a still-live job or an answered job. It preserves the old immutable job and claim, creates a new hashed generation with lineage, and publishes an active pointer. The new expiry is capped by both six hours and the **existing frozen issue deadline**. Runtime, failure and resume counters are never reset. Each input has at most three explicit retries per issue (or the lower configured failure limit); aggregate retry accounting is bounded by the frozen resume cap and the pending-job cap remains 200.

Claim the returned new job ID before assigning work, then import using its fresh token. Late claims/responses for retired generations are rejected. `pending --include-expired` omits retired generations but shows currently expired unanswered jobs. Completed responses remain reusable. If the issue has no time left, prepare the honest deadline status instead of renewing jobs; a later issue can explicitly retry under its own newly established budget.

An expired job now produces explicit CLI JSON `state=expired_parent_writer`, `job_id`, and `issue_date`. The supervisor forwards that marker from the current worker attempt rather than returning an empty generic wait. A scheduled dispatcher should inspect the frozen issue budget, use the explicit retry command only while time/budget remain, claim the new generation, and otherwise send the deadline status. Exit 75 remains a checkpoint, not a failed process attempt.


### HTML Library handoff (schema v2)

The new default for qualifying cloud reports is `delivery_format: html`; `prepare --delivery-format text` is an explicit compatibility option for new handoffs only. An existing handoff always remains unchanged. Production schedules should use HTML; no-output status does not require attachment upload. The HTML renderer is offline/self-contained and preserves approved fields and the original report/source disclosures; it does not change paper acceptance gates. No public hosting, messaging credentials or new API secrets are needed.

```
# Parent uploads artifacts[1].path using Library and materializes its verified version.
.venv/bin/python -m daily_agent.cloud_workflow bind-library --root data/cloud --date YYYY-MM-DD --library-file-id VERIFIED_LIBRARY_ID --library-version 1 --verified-library-file /consumer-local/library-export.html
.venv/bin/python -m daily_agent.cloud_workflow begin --root data/cloud --date YYYY-MM-DD
# Send exact body plus returned library_file_ids; persist tool acceptance, then read back.
.venv/bin/python -m daily_agent.cloud_workflow accepted --root data/cloud --date YYYY-MM-DD --attempt-id ATTEMPT_ID --message-id MESSAGE_ID
.venv/bin/python -m daily_agent.cloud_workflow confirmed --root data/cloud --date YYYY-MM-DD --attempt-id ATTEMPT_ID --message-id MESSAGE_ID --conversation VERIFIED_CONVERSATION_ID --readback-file verified-caption.txt --readback-attachments-file verified-attachments.json
```

`verified-attachments.json` must contain the exact observed/mapped identity, e.g. `[{"file_id":"VERIFIED_LIBRARY_ID","version":1}]`. It is normalized by the authorized parent from actual remote evidence; copying the expected binding into this file is not verification. If a message readback exposes only an opaque attachment reference or reports partial metadata, a separately supported authoritative mapping is required. Without one the outcome remains **accepted, attachment readback unverified**, even if the user can see the attachment. Never infer that partial metadata means no send occurred. A Library round-trip export validates stored bytes, but does not on its own prove the file was attached to a particular message.

Library version numbers are nonnegative integers: an initial upload can legitimately be version `0`. Preserve the actual tool-returned version; never substitute `1`. Booleans, negatives and string versions are rejected.

`bind-library` is an offline validation boundary: the repository cannot authenticate external Library metadata and deliberately has no messaging tools. The parent is responsible for the truth of ID/version/export provenance, recipient access and remote readback. Once bound, do not replace that Library file with another version while delivery is pending. Schema v2 validates that a confirmed receipt retains attachment evidence, and publication reconciliation works on copies so publication writers cannot mutate sealed approval data.

For UI calibration, use a separate pilot-marked root and copy the previously approved snapshot byte-for-byte; revalidate the ready report normally before creating a new HTML handoff. Preserve source hashes and source evidence paths. Do not copy or modify a confirmed delivery ledger, regenerate expensive reading merely for layout, alter quality results, or commit calibration items to production publication history. The original confirmed text pilot remains the authoritative receipt for that earlier send.

### Partial attachment readback and unresolved-send reservations

Some native attachment messages currently read back as `partial: true`, with an opaque `CalpicoFile` attachment reference but no Library ID/version. This verifies neither the Library identity nor the attachment bytes in that message. A round-trip Library export verifies the stored file separately; it cannot fill in missing message metadata.

After exact caption/message/conversation readback, the parent can preserve the actual limited observation without confirming delivery:

```
# observed-readback.json contains actual returned references, never expected Library IDs:
# {"partial":true,"attachments":[{"attachment_id":"RETURNED_ATTACHMENT_ID","target":"RETURNED_OPAQUE_TARGET","type":"file"}]}
.venv/bin/python -m daily_agent.cloud_workflow observe-readback --root data/cloud --date YYYY-MM-DD --attempt-id ATTEMPT_ID --message-id MESSAGE_ID --conversation VERIFIED_CONVERSATION_ID --readback-file actual-caption.txt --readback-observation-file observed-readback.json
```

The receipt remains `accepted`, with `readback_observation.attachment_identity_verified=false` and `publication_reconciled=false`. Observation is bound to the accepted attempt/message, exact caption and conversation; conflicting replacement observations are rejected. It does not add Library IDs, assert successful delivery, update publication history or allow resending. A deadline watcher can distinguish “tool accepted, no readback,” “caption and file presence observed, identity unverified,” and fully identity-verified `confirmed`. Report the limitation once; do not create duplicate sends or repeated missing-delivery notices when the caption and a file are demonstrably present.

To prevent the same items appearing tomorrow while transport remains unresolved, candidate selection derives reservations from valid same-root non-pilot receipts in `sending`, `uncertain`, `accepted`, or `confirmed` before local reconciliation finishes. Canonical and existing DOI/arXiv/title identity keys are used before reading work, rechecked before sealing and again before sending. Reservations do not expire automatically and never masquerade as publication. Corrupt receipt/artifact evidence fails closed. A global `cloud-dispatch.lock` serializes begin transitions across issue dates; a previously prepared report is rechecked against both unresolved reservations and current publication history immediately before sending. No explicit release/negative-send repair operation is added by this change; ambiguous outcomes remain blocked for operator review.

Pilot receipts and calibration roots never reserve production items. Same-issue resumptions keep their existing receipt and never create a second attempt. Successfully reconciled confirmations fall back to the existing publication dedup/version-update policy.

### Private Site transport with exact-caption confirmation

Native HTML attachment readback remains strict and separate. An unbound **prepared** HTML handoff may instead receive an immutable Site transport binding. An accepted native receipt cannot be converted, resent, or promoted through this path. Its original caption artifact, approval hash and content identity are not rewritten.

1. Prepare the qualified HTML handoff normally, without sending it.
2. Export the exact current `{root,date,identity}` with `report_archive --current-*`; the archive explicitly labels it as a prepared website preview, not chat delivery. Preserve existing archive page bytes.
3. Through supported Sites tools, publish the exact Git source and wait for terminal `succeeded`. Persist the actual publish tool response and actual `get_site_version` response for that deployment/version. The parent must verify authorized private access separately.
4. Bind the deployment to the report using the command below. The binder reads **the version response's exact Git commit**, not the working tree. It compares every `dist` file and the manifest byte-for-byte/hash-for-hash, validates same-project static hosting, and verifies the dated entry's sealed HTML, approval and content identity. That content identity already seals the original ready-report source hash; the binding explicitly retains it as well.
5. Call `begin`, send its exact `body` to the verified conversation, then record `accepted`. Site mode uses `dispatch_body_sha256` for the actual linked caption; `body_sha256` remains the original content-seal caption hash. Never send the old native-attachment caption.
6. Read that exact message back. Site-mode `confirmed` requires matching message/attempt/conversation and the exact bound linked caption, then permits normal publication reconciliation. No native attachment identity is claimed. Site publication alone never confirms chat receipt or marks research items published.

```
.venv/bin/python -m daily_agent.cloud_workflow bind-site --root ROOT --date YYYY-MM-DD --site-archive-dir EXPORT_DIR --site-deployment-file PUBLISH_TOOL_RESULT.json --site-version-file VERSION_TOOL_RESULT.json --site-repo SITE_GIT_REPO
.venv/bin/python -m daily_agent.cloud_workflow begin --root ROOT --date YYYY-MM-DD
.venv/bin/python -m daily_agent.cloud_workflow accepted --root ROOT --date YYYY-MM-DD --attempt-id ATTEMPT_ID --message-id MESSAGE_ID
.venv/bin/python -m daily_agent.cloud_workflow confirmed --root ROOT --date YYYY-MM-DD --attempt-id ATTEMPT_ID --message-id MESSAGE_ID --conversation VERIFIED_CONVERSATION --readback-file ACTUAL_CAPTURED_LINK_CAPTION.txt
```

This is proof of the exact source associated with a successful Sites deployment, **not** a claimed production HTTP byte round trip. No mandatory production-page login/fetch is added. Tool response authenticity and actual access remain the parent's responsibility; the repository contains no Sites or messaging credentials. Deployment/version/source-proof/manifest/caption evidence is copied into content-addressed root-relative artifacts so receipt verification survives restoration without the original Git repository. A changed deployment/version/archive or caption cannot replace an existing binding.

After the real flow is validated, production may set `cloud.delivery_format: html` and `cloud.delivery_transport: site`. A configured Site transport refuses `begin` without a verified Site binding. Existing accepted/uncertain native attempts and no-output text-status messages retain their previous behavior. Do not activate the new configuration before upgrading scheduled instructions and preserving the state/source backup.

Fresh cloud profiles also preserve `config/research-context-evidence.json` byte-for-byte when supplied by the source and set `report_writing.paper_visual_selection_provider: parent_queue`, so cloud generation uses the intended context registry and parent-assisted visual selection.
