# Zotero manager: integrated application, disconnected by default

This dependency-free Worker ESM application implements an owner-only Chinese Zotero manager, trusted catalog registration, provider-managed private credential storage, optional legacy encrypted staging, explicit key verification, durable selected-paper execution, and a server-side Zotero transport. **Implementation is present; a live connection is not provisioned by this source.** No real account credentials, grants, runtime secrets, production database, or live imports have been configured by this work.

`/zotero` uses the existing warm-paper/dark-green visual direction. It reads actual owner-scoped server catalogs, starts with every paper unchecked, previews date/topic/publication classifications, and requires an explicit PDF choice and Save. With default/unconfigured bindings it shows **未连接 / NOT CONNECTED** and disables Save. Credential staging is separately disabled until its one-time approval gate is valid. “Encrypted and staged” never means “connected.”

## Approved provider-managed D1 credential mode

This optional mode is explicitly different from the original application-encrypted AEAD path. Apply appended migration `0003_zotero_provider_credentials.sql` after the two existing migrations. Set the non-secret private runtime choices `ZOTERO_CREDENTIAL_STORAGE=provider-managed-d1` and `ZOTERO_CREDENTIAL_ENTRY_ENABLED=approved` only after the specific persistent-access/storage approval. Existing trusted owner, HTTPS origin, private D1, and private R2 bindings remain required. The initial deployment can leave `ZOTERO_EXECUTION_ENABLED` unset: private credential entry and verification do not enable imports.

The user personally opens the owner-only password form and enters their numeric personal Zotero User ID and API key. There is no guessed account or automatic library lookup. The exact POST body is `action: connect_personal_library`, `library_id`, and `api_key`; the server calls only `GET https://api.zotero.org/keys/current` with the key in `Zotero-API-Key`, never the URL. It accepts personal-library read/write/files permissions only, rejects notes, all group grants and unknown scope fields, and checks the returned user ID before storing the credential or active library binding. See [Zotero's current-key documentation](https://www.zotero.org/support/dev/web_api/v3/syncing).

The dedicated `zotero_provider_credentials.api_key` column contains a **plaintext-usable key**, protected by Cloudflare's provider-managed D1 at-rest encryption. It is not application ciphertext, a vault, or end-to-end encryption. The application and database administrators with read access can read it. Database backups may retain copies. This accepted tradeoff avoids requiring an independent application encryption secret. Do not inspect this production table through assistant/operator diagnostics, exports, or tooling; entry and use are confined to the private user form and server runtime. Status, HTML, catalog and job responses use explicit non-secret allowlists. No key is returned, logged, put in browser storage, or included in an upstream error. Verify actual hosting logs/telemetry separately; local tests cannot prove platform redaction.

A stable random credential ID plus SHA-256 fingerprint binds each accepted key. Same-key re-verification/resubmission preserves its connection version; a different key requires explicit disconnect. Disconnect atomically removes active provider and AEAD credential records, revokes the active connection and advances a non-secret epoch that fences delayed connect/verify responses. The personal library tombstone remains, so switching accounts requires a separately reviewed rebind rather than silently moving old jobs. Reconnection after disconnect creates a new binding/version. Old uncertain jobs remain visible and blocked with their reserved keys and prewrite journal intact; the app never automatically replays them as fresh creates. Already in-flight remote requests may still finish.

The user should also revoke the key at [Zotero API key settings](https://www.zotero.org/settings/keys) to invalidate retained backup copies. Disconnect alone cannot erase backups or revoke Zotero's upstream key.

`ready` means the account/permissions are verified in this mode; `live_ready` additionally requires the separately approved execution gate. The manager labels this distinction and keeps Save disabled when execution is closed or no verified catalog is available. Connect, refresh, a Worker restart, and reading job history never import papers. An explicit selected Save and explicit PDF choice remain required. Durable jobs, deduplication, library leases, reserved object keys, backoff, uncertain-write recovery, and private artifact registration stay unchanged. Catalog registration still requires separately pinned artifacts and the confirmed personal-library configuration; entry alone does not activate a catalog.

The original AEAD mode is retained for compatibility when provider-managed mode is not selected. Its table, AES-256-GCM associated data and old decryption path remain intact; it still requires its independent encryption secret and one-time staging grant. No old credential is silently converted to plaintext.


## Run and verify locally

Use Node 24 or newer. The core source and tests require no package installation:

```sh
cd sites/zotero-worker
npm run check
npm run build
node scripts/preflight.mjs
```

The local tests use in-memory SQLite/D1 adapters, synthetic credentials, injected transport responses, and a DOM/fetch/timer harness executing the manager's actual inline JavaScript. The hashing tests check real MD5/SHA-256 fixture bytes. These are offline regression checks, not browser visual QA, production D1 acceptance, or a real Zotero import test.

`npm run build` copies and syntax-checks the complete Worker/source module graph and all SQL migrations into disposable local build output. `dist/BUILD-STATUS.json` records this as a local ESM graph with `sites_deployable: false`. It does not register, provision, publish, enable, or connect anything. The separate hosting adapter owns host packaging and archive retention; use its documentation for that workflow.

## Explicit user flow

1. An approved operator configures the owner-private runtime and registers independently reviewed, immutable catalogs and private PDF bytes. Registration starts disabled; an independently approved exact-catalog activation setting may enable it after verified registration
2. After the required persistent-access and secure-entry approvals, the user personally enters their personal numeric library ID and key in the provider-managed owner-only form. The server verifies the exact identity/scope before saving. The browser clears the key field. The optional legacy AEAD flow instead encrypts and stages it
3. The provider-managed form verifies on its explicit connect submission; **核验连接** explicitly checks an existing connection again. The separate live execution gate and verified catalog are still required for Save. Reading a page or GET endpoint never verifies a key or calls Zotero
4. The user selects up to 100 catalog papers, chooses `metadata_only` or `open_access_if_available`, and explicitly clicks **Save to Zotero · 保存所选**. Only those opaque IDs and the pinned catalog identity are submitted
5. Save creates or reuses a deterministic execution job. The manager reads its progress and sends serial, bounded **continue_import** actions for that expressly requested job. The current application processes at most one selected paper and one durable stage per advance; a stage can contain several guarded API requests
6. Retryable work follows the server's `retry_at`. Completion, terminal failure, a lost connection, or nonretryable unresolved PDF work stops automatic continuation. Closing the page stops its continuation loop; an already submitted server step may still finish
7. On returning, the manager reads actual execution history without running it. The user may inspect results or explicitly click **继续此任务** for a resumable job. Changed connection bindings or catalog pins remain server-enforced blockers

There is no autonomous queue consumer, cron, or service worker automatically draining previous jobs. Legacy `blocked_connection` and `preview_only` intents remain separate records; neither connection verification nor the new executor converts them into executable jobs.

## Implemented safeguards

- Every feature route first compares the trusted gateway's `oai-authenticated-user-id` with the independently verified Site-scoped owner binding. Missing configuration, anonymous/foreign visitors, and wrong origins fail closed
- Client `X-Owner`, email, query/body ownership claims and the first visitor cannot establish an owner. The gateway must be the sole trusted ingress; a configuration flag is not authentication for an exposed raw Worker
- All catalog, execution, intent, grant, and credential records are owner-scoped. Library-wide fenced leases coordinate execution jobs and persist server backoff
- POST actions require the exact HTTPS Origin, same-origin fetch metadata when present, JSON content type, `X-Zotero-Action: 1`, and bounded bodies. No permissive CORS/OPTIONS path exists
- Manager responses are private/no-store, have a nonce-based CSP, no external assets or analytics, no browser persistent storage, and no ordinary form submission fallback. Untrusted display text is inserted with `textContent`
- Selection only changes the in-page preview. No import request is made by checking a paper, changing a filter, refreshing connection state, or opening history
- The Save endpoint rejects browser metadata, URLs, credentials, library identifiers and extra fields. DOI-shaped IDs can contain slashes but remain opaque allowlist values, never fetch targets or route segments
- Execution identity includes owner, personal library, connection version, immutable report/catalog version and hash, canonical selected IDs, and PDF intent. Equivalent selections reuse one durable job
- The executor revalidates connection, catalog identity, lease generation and per-stage evidence. Reserved Zotero object keys and a prewrite journal support readback after interruption; uncertain outcomes are not hidden by creating a different object
- A complete version-consistent inventory of the explicitly filtered bibliographic-item/attachment scope, with matching filtered key/version proof, supports DOI/arXiv matching and a conservative normalized title/author/year/venue fallback. Conflicts, ambiguous matches and matches in Trash fail closed
- Standalone note/annotation items are excluded from inventory and item lookups; the importer does not intentionally query, use, store or change notes. No notes grant is required and Notes access should remain off. Zotero can incidentally include an attachment `note` property in its JSON response; the transport discards it before returning data and never logs or uses it, so this is not a guarantee that no note bytes cross the response wire. Existing bibliographic metadata is not replaced. Managed date/topic collections and publication/topic tags are merged additively with fresh version preconditions
- PDF execution consumes only pinned, bounded bytes in private owner-scoped storage. Upload registration alone is not success: stored bytes, size, MD5, SHA-256, parent and metadata must be read back and verified

See [RUNTIME_CONFIGURATION.md](RUNTIME_CONFIGURATION.md) for exact runtime/approval gates and [TRANSPORT.md](TRANSPORT.md) for the transport contract and remaining live acceptance requirements.

## API contract

All routes require the configured owner. GET routes are read-only and never create/advance jobs or make outbound Zotero requests.

| Method | Path | Behavior |
| --- | --- | --- |
| GET | `/zotero` | Chinese catalog/selection/connection/execution manager |
| GET | `/zotero/register` | Owner-only local-file verification and explicit private catalog registration |
| GET | `/api/zotero/catalog-registration` | Approved pinned filenames/digests/limits and staged/registered/enabled facts; read-only |
| POST | `/api/zotero/catalog-registration/pdfs/:sha256` | Explicit exact-size/hash-approved PDF staging into private storage; raw `application/pdf` body |
| POST | `/api/zotero/catalog-registration/finalize` | Revalidate pinned source bytes/PDFs, register immutable catalog, and enable only if independently configured |
| GET | `/api/zotero/connection` | Local gate and stored verification state; default `ready: false`, `live_ready: false` |
| GET | `/api/zotero/catalogs` | Available verified, enabled catalogs owned by this user |
| GET | `/api/zotero/reports/:report_id/catalogs/:catalog_id` | Owner-scoped display metadata and server paper allowlist |
| POST | `/api/zotero/connect` | One-time approved encrypted staging; default `setup_required` before reading credentials |
| POST | `/api/zotero/verify-connection` | Explicit fixed-service personal-library permission check and bound active-connection record |
| GET | `/api/zotero/executions` | Up to 50 actual owner execution jobs with per-item results; never resumes them |
| POST | `/api/zotero/executions` | Fresh explicit selection creates/reuses a durable execution; no outbound import occurs in this Save handler |
| GET | `/api/zotero/executions/:job_id` | Current read-only job/item state |
| POST | `/api/zotero/executions/:job_id/advance` | One guarded execution step for the requested job |
| GET | `/api/zotero/import-intents` | Legacy owner-only blocked/preview intents |
| GET | `/api/zotero/import-intents/:job_id` | One legacy intent |
| POST | `/api/zotero/import-intents` | Legacy `save_to_zotero` records an inert intent only; not the manager Save endpoint |

Ordinary JSON action requests include:

```text
Origin: <exact configured HTTPS Site origin>
Content-Type: application/json
X-Zotero-Action: 1
```

Example new execution body, using synthetic identifiers:

```json
{
  "action": "save_selected",
  "report_id": "report-20261010",
  "catalog_id": "catalog-v1",
  "catalog_version": 1,
  "paper_ids": ["doi:10.55555/example", "paper-b"],
  "pdf_intent": "metadata_only"
}
```

PDF staging instead requires `Content-Type: application/pdf` with the same Origin/action-header rules and raw pinned bytes. Catalog finalization accepts exactly `action: "register_catalog"`, `report_b64`, `html_b64`, and `catalog_b64`; source pins and owner/library targets cannot be supplied by the browser.

Verification accepts exactly `{"action":"verify_connection"}`; advance accepts exactly `{"action":"continue_import"}`. Staging accepts exactly `action: "stage_credentials"` and `api_key`, but a real key must only be personally submitted through the approved secure form, never pasted into source, shell commands, chat, URLs, logs, examples, or test fixtures. No endpoint returns the stored key.

`pdf_intent` is explicitly `metadata_only` or `open_access_if_available`. The latter authorizes the selected, reviewed open-access bytes when the live feature is approved; it does not authorize publisher-access bypasses, arbitrary URL fetching, or unselected attachments.

## Status semantics

Per-item states and evidence are separate:

| State | Meaning |
| --- | --- |
| `queued` | Selected item is recorded; no completed metadata stage is claimed |
| `metadata_saved` | Bibliographic item created or matched and verified; classification/PDF work may remain |
| `pdf_pending` | Metadata/classification may be saved, but the requested PDF is not yet verified; inspect retry/error facts |
| `complete` | Required metadata/classification and, when requested, stored-PDF verification are complete |
| `already_exists` | An existing item was reused and required classification/PDF work is complete |
| `failure` | This item's last step failed; earlier successful stages remain visible; inspect `retryable`, `retry_at`, and `uncertain` |

Responses also include `metadata_saved`, `classification_saved`, `pdf_verified`, `existing_item`, safe `error_code`, `retryable`, `uncertain`, and ISO `retry_at`. A failed attachment must not be reported as an entirely saved PDF. No generic “Saved” label substitutes for these facts.

Aggregate job states include `queued`, `running`, `retry_wait`, `pdf_pending`, `complete`, `partial_failure`, and `failure`. Readback can report `connection_required` when the current local gate/connection no longer matches. Aggregate failure may coexist with completed individual items. Retrying an uncertain operation first reconciles the same persisted object key; it does not erase the ambiguity or guarantee eventual success.

## Trusted catalog and PDF boundary

`src/catalog-registration.mjs` verifies independently reviewed owner/report/library pins against exact report, HTML and catalog bytes. It checks the source allowlist, evidence lineage, canonical catalog/row hashes, and PDF sizes/signatures/SHA-256 before preparing an in-memory registration capability. `registerTrustedCatalog` verifies private blob storage/readback and writes immutable owner-bound catalog rows in a D1 batch, initially with `enabled = 0`.

The owner-only `/zotero/register` path accepts only the exact previously approved server-pinned artifact set; it is not an arbitrary/public registration API. The operator configures `ZOTERO_CATALOG_REGISTRATION_JSON` with independently reviewed bindings, pins, filenames, PDF hashes/sizes and an explicit `enable_after_registration` decision. There is no seed data in the migrations. A changed catalog requires a new immutable identity; the browser cannot choose metadata, targets, pins or activation policy. R2 and D1 do not share one transaction: a failed registration can leave private content-addressed blobs, but must not publish an incomplete selectable catalog.

The registration page requires an explicit directory choice, hashes each required file locally before transmitting anything, shows matched counts/byte totals, and sends only exact matched files after an explicit Register click. It rereads configuration before uploads and staged facts before finalization. PDF uploads are sequential raw bodies; source JSON/HTML are sent in the bounded final JSON body. Registered-but-disabled is distinct from enabled; neither operation connects to Zotero or executes a Save. Route limits are report/catalog 1 MiB each, HTML 4 MiB, combined source bytes 4 MiB, each PDF 16 MiB and total PDFs 24 MiB. This implements an explicit owner-folder registration path. Automated daily catalog production/registration still requires a separately approved trusted-operator workflow; it is not installed or promised by this UI.

`node scripts/prepare-catalog.mjs --help` describes the offline artifact preflight. Validation is the default and makes no network or database calls. Its optional private JSON descriptor is for review; serializing it does not preserve the in-memory registration capability. Re-prepare from reviewed source bytes in the trusted registration process.

Managed paths are `DailyAgent/<year>/<report-date>` and `DailyAgent/Topics/<server-topic>`. Tags include `DailyAgent`, `daily:<date>`, `publication:<status>` and `topic:<topic>`. The UI translates known topic/publication labels to Chinese for display without altering these server classifications.

## Host integration and remaining release gates

Preserve the existing Site router, access policy, sign-in handling, archive URLs and assets. `handleZoteroRequest(request, env)` returns `null` for unrelated routes; the default export can delegate those to an existing `ASSETS` binding. Failed owner authorization never falls through to static Zotero pages. Without a host handler, unrelated routes return `host_router_required` rather than pretending archives exist. Consult the separate hosting-adapter documentation before packaging or deployment.

Still required before real use:

1. Verify private deployment, trusted gateway ingress, correct Site-scoped owner/origin, retained archives, supported D1/R2 bindings and both migrations
2. Obtain the required user approvals for persistent personal-library access, runtime secret/security configuration, secure credential entry and intended writes/uploads; no such approval is conferred by these files
3. Supply independently reviewed registration configuration and private artifacts through the owner-authenticated pinned registration workflow; approve activation separately in server configuration
4. Deliberately enable the approved runtime gate, have the user explicitly verify the key, and confirm the exact personal-library library/write/files rights; note-reading permission is not required
5. Run a separately authorized live acceptance test for selected metadata, collections, deduplication, PDF upload/readback and interruption recovery; browser/runtime/gateway behavior remains distinct from mocks
6. Obtain any separate publication/deployment approval and review operational quotas, memory, storage, request limits and applicable charges. This implementation makes no cost or quota promise

Production key rotation, credential deletion/replacement, broader access changes and administrative recovery still require a separately reviewed workflow. Passing offline tests, staging a key, rendering a page or producing a host artifact is not proof of a working production connection.

### Owner identity bootstrap

`/zotero/identity` is an explicitly enabled read-only display for an authenticated owner session, before the D1 owner binding exists. It never enrolls the first visitor. Actual owner-private audience and gateway-only ingress must be verified first; use the temporary flags and close the display after binding the independently confirmed Site-scoped ID. See [runtime configuration](RUNTIME_CONFIGURATION.md) and [hosting steps](HOSTING.md).
