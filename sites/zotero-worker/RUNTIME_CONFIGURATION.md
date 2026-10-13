# Runtime configuration and approval gates

The application, encrypted staging, explicit rights verification, transport and durable executor are implemented. **Runtime configuration is intentionally absent by default.** This document supplies names/contracts only; it does not provision a Site, approve persistent access, transmit credentials, activate a catalog, enable imports or authorize publication.

Do not create environment files in this source snapshot: the recovery system rejects them, including example filenames. Real values belong only in the supported private runtime/secret setup workflow.

## Approved provider-managed D1 credential mode

This optional mode is explicitly different from the original application-encrypted AEAD path. Apply appended migration `0003_zotero_provider_credentials.sql` after the two existing migrations. Set the non-secret private runtime choices `ZOTERO_CREDENTIAL_STORAGE=provider-managed-d1` and `ZOTERO_CREDENTIAL_ENTRY_ENABLED=approved` only after the specific persistent-access/storage approval. Existing trusted owner, HTTPS origin, private D1, and private R2 bindings remain required. The initial deployment can leave `ZOTERO_EXECUTION_ENABLED` unset: private credential entry and verification do not enable imports.

The user personally opens the owner-only password form and enters their numeric personal Zotero User ID and API key. There is no guessed account or automatic library lookup. The exact POST body is `action: connect_personal_library`, `library_id`, and `api_key`; the server calls only `GET https://api.zotero.org/keys/current` with the key in `Zotero-API-Key`, never the URL. It accepts personal-library read/write/files permissions only, rejects notes, all group grants and unknown scope fields, and checks the returned user ID before storing the credential or active library binding. See [Zotero's current-key documentation](https://www.zotero.org/support/dev/web_api/v3/syncing).

The dedicated `zotero_provider_credentials.api_key` column contains a **plaintext-usable key**, protected by Cloudflare's provider-managed D1 at-rest encryption. It is not application ciphertext, a vault, or end-to-end encryption. The application and database administrators with read access can read it. Database backups may retain copies. This accepted tradeoff avoids requiring an independent application encryption secret. Do not inspect this production table through assistant/operator diagnostics, exports, or tooling; entry and use are confined to the private user form and server runtime. Status, HTML, catalog and job responses use explicit non-secret allowlists. No key is returned, logged, put in browser storage, or included in an upstream error. Verify actual hosting logs/telemetry separately; local tests cannot prove platform redaction.

A stable random credential ID plus SHA-256 fingerprint binds each accepted key. Same-key re-verification/resubmission preserves its connection version; a different key requires explicit disconnect. Disconnect atomically removes active provider and AEAD credential records, revokes the active connection and advances a non-secret epoch that fences delayed connect/verify responses. The personal library tombstone remains, so switching accounts requires a separately reviewed rebind rather than silently moving old jobs. Reconnection after disconnect creates a new binding/version. Old uncertain jobs remain visible and blocked with their reserved keys and prewrite journal intact; the app never automatically replays them as fresh creates. Already in-flight remote requests may still finish.

The user should also revoke the key at [Zotero API key settings](https://www.zotero.org/settings/keys) to invalidate retained backup copies. Disconnect alone cannot erase backups or revoke Zotero's upstream key.

`ready` means the account/permissions are verified in this mode; `live_ready` additionally requires the separately approved execution gate. The manager labels this distinction and keeps Save disabled when execution is closed or no verified catalog is available. Connect, refresh, a Worker restart, and reading job history never import papers. An explicit selected Save and explicit PDF choice remain required. Durable jobs, deduplication, library leases, reserved object keys, backoff, uncertain-write recovery, and private artifact registration stay unchanged. Catalog registration still requires separately pinned artifacts and the confirmed personal-library configuration; entry alone does not activate a catalog.

The original AEAD mode is retained for compatibility when provider-managed mode is not selected. Its table, AES-256-GCM associated data and old decryption path remain intact; it still requires its independent encryption secret and one-time staging grant. No old credential is silently converted to plaintext.


## Required owner/host bindings

| Binding | Contract |
| --- | --- |
| `DB` | D1-compatible `prepare().bind().first()/all()/run()` plus transactional `batch()`; apply all schema migrations through the approved host workflow |
| `SITES_GATEWAY_IDENTITY_TRUSTED` | Exactly `platform-managed`, and only after actual sole trusted-gateway ingress has been verified |
| `ZOTERO_OWNER_ID` | Independently verified **Site-scoped** `oai-authenticated-user-id` for the intended owner |
| `ZOTERO_SITE_ORIGIN` | Exact canonical HTTPS origin, without path/trailing slash/query/userinfo; action Origin must match exactly |
| `ZOTERO_INTENT_MODE` | Legacy-only optional `blocked_connection` (default) or `preview_only`; it does not enable new execution jobs |
| `ASSETS` | Optional existing supported host static-assets binding; otherwise compose `handleZoteroRequest` with the original host router |

The owner is not an email address, organization ID, assumed global account ID or unverified platform field. Site-scoped identities differ across Sites. If available tooling cannot establish the correct binding, deployment remains blocked pending a supported owner-bootstrap workflow. Never initialize the owner from the first request or client `X-Owner`, body or query claims.

The implemented temporary read-only `/zotero/identity` display is the concrete bootstrap path. Before enabling it, the operator must verify the existing Site's actual owner-private audience and sole trusted gateway ingress, then explicitly configure `ZOTERO_OWNER_BOOTSTRAP_ENABLED=approved`, `SITES_OWNER_PRIVATE_AUDIENCE_VERIFIED=owner-private`, the gateway assertion and exact HTTPS origin. Open the route directly in the intended owner's authenticated browser (or same-origin navigation). It displays only that session's Site-scoped ID as no-store plain text, does not read D1, enroll a visitor, connect Zotero or grant access. Bind the independently confirmed owner server-side only after the required action-time approval, then remove both temporary bootstrap flags. An account ID from `get_site` is not a substitute. The flags are operator assertions, not proof of the platform boundary. See `HOSTING.md` for exact steps.


### Gateway trust is a deployment invariant

The gateway supplies and sanitizes `oai-authenticated-user-id` after authenticating the visitor. It must be the sole ingress. The configuration flag merely asserts a verified boundary; it does not cryptographically authenticate a raw header. A directly exposed Worker URL or untrusted proxy would permit spoofing and must not be accepted as a production setup.

`OAI-Sites-Authorization` service access supplies no visitor identity and is insufficient for owner routes. Private visibility does not replace per-record ownership checks. Refer to the separate hosting-adapter documentation for packaging and retaining existing archive/sign-in routes.

## Optional legacy AEAD staging: every gate must be valid

These values remain unset until separately approved setup. Missing/invalid staging configuration keeps `credential_entry_enabled: false` and returns `setup_required` from `/api/zotero/connect` before parsing a credential-bearing body.

| Binding | Contract |
| --- | --- |
| `ZOTERO_VERIFIED_PERSONAL_LIBRARY_ID` | Independently verified positive decimal personal-library ID owned by this user; no leading zeroes, groups, or client override. The execution runtime additionally requires a safe integer matching its bounded numeric format |
| `ZOTERO_ENCRYPTION_KEY_ID` | Non-secret, strict opaque version label for the server AEAD key |
| `ZOTERO_CREDENTIAL_ENCRYPTION_KEY_B64` | **Runtime secret:** exactly 32 random bytes in canonical base64. Never source, browser JS, hosting manifest, shell argument, chat, log or test artifact |
| `ZOTERO_SETUP_GRANT_JSON` | Server-controlled owner/library-bound single-use grant described below |

The application does not generate or configure these values. Use the platform's supported action-time approval and secret tools. The assistant must not receive a real API/encryption key through chat or copy one from chat into a form.

### Grant shape

This is a nonfunctional shape, not approval. The booleans deliberately remain false and identity/time placeholders are invalid:

```json
{
  "grant_id": "replace-with-unique-server-grant-id",
  "owner_id": "replace-with-verified-Site-scoped-owner",
  "library_id": "replace-with-verified-personal-library",
  "purpose": "zotero_credential_staging",
  "issued_at": "replace-with-current-ISO-time",
  "expires_at": "replace-with-ISO-time-within-one-hour",
  "security_action_approved": false,
  "secure_user_entry_approved": false
}
```

Only an approved setup operator may configure a grant after real user approval. Both booleans must be true; owner/library must match server bindings; issue/expiry must define a current window of at most one hour. Grant IDs are strict opaque strings. One consumed grant cannot be reused and an existing staged credential cannot be overwritten through this endpoint. Rotation/removal requires a separately reviewed workflow.

The grant is a server authorization record, not browser input or a login credential. Restrict who may change server bindings. Expiry gates credential staging; a successfully consumed grant is retained as part of the ciphertext's binding, not silently reissued to make later verification possible.

## Live verification and execution: separate gate

| Binding or stored fact | Required condition |
| --- | --- |
| `ZOTERO_EXECUTION_ENABLED` | Exactly `approved`, only after the required live/persistent-access approvals and runtime review |
| `BUCKET` | Private R2-compatible binding exposing `get`; required by the current execution runtime gate even for a metadata-only job. The trusted registration adapter separately needs private conditional put/read capabilities |
| Personal-library/AEAD bindings | Must still be present and match the staged credential's owner, `user` library, key ID, grant ID and cipher version |
| Staged credential | Owner-scoped AES-256-GCM ciphertext in the expected staging state; never a browser-supplied replacement |
| Active connection | Owner/library/key/grant-bound non-secret verification record with a positive connection version, `state: active`, and required rights |
| Catalog | Enabled immutable owner/report/library-bound canonical catalog whose registered source/row hashes and selected IDs validate |

`runtimeReady` does not itself create a connection. The server exposes `verification_enabled` only when the live gate/dependencies and a bound staged credential are present. The user must explicitly POST `{"action":"verify_connection"}` before an active connection record is created or refreshed.

Verification calls Zotero's fixed `/keys/current` endpoint and currently requires personal-library **library, write and files** rights. Notes-read permission is not required and Notes access should remain off. Filtered inventory/key lookups exclude standalone notes and annotations. If Zotero incidentally includes an attachment `data.note` property, the transport discards it before returning data; it is never intentionally used, stored, logged or changed. This is not an assurance that no incidental note bytes arrive in API responses. The filtered item set is verified against a matching same-version key map. These rights must be specifically approved; the application must not expand access to other libraries/groups. Current `ready`/`live_ready` require matching active local records and the live gate. They reflect stored rights verification and current local bindings, not a background probe of remote rights. Before executing an eligible stage, the runner verifies permissions again; page/GET requests never call Zotero.

A fresh explicit Save POST creates/reuses an owner-scoped `ze_…` job; only explicit `continue_import` POSTs advance it. The browser issues serial continuations only for its current Save or a user-clicked Resume. Reading history, staging/verifying a key and reopening the manager never autoexecute previous jobs or legacy inert intents. Changing the connection version or catalog pins blocks incompatible earlier jobs.

## Legacy AEAD setup and user sequence

1. Verify private Site/gateway-only ingress, the exact Site-scoped owner/origin, original archive router and approved D1/private R2 bindings. Apply `0001_zotero_reference.sql` followed by `0002_zotero_execution.sql` through the supported host migration workflow. No live migrations are applied merely by building this source
2. Explain the specific persistent access: the named Daily Agent runtime would retain an encrypted Zotero API key for the user's verified personal library. Selected Save actions can create bibliographic items, date/topic collections, additive tags/membership and selected reviewed open-access PDF attachments. Obtain the required action-time approval before creating/configuring or materially expanding persistent access
3. Hand off key creation/selection and secure entry to the user. The user personally completes Zotero's key/access flow and any required permissions. Independently verify the intended personal-library ID through an approved route before setting the server binding. Leave Notes access off; do not create a broad test key or include groups implicitly
4. Obtain runtime-secret/security configuration approval and configure the AEAD secret/key-version through native supported tools. Do not write real secrets or grants into source, artifacts, shell arguments or chat. This implementation has not performed that provisioning
5. Configure a short-lived one-time grant reflecting completed approvals. The password field can become enabled only while grant/bindings are valid and neither a used grant nor stored credential prevents staging. It remains disabled without JavaScript
6. The user opens the exact private HTTPS page and personally submits the key. The endpoint accepts the exact `stage_credentials`/`api_key` JSON shape and validates 16–128 ASCII alphanumeric characters. The browser clears the entry; the server encrypts before D1 persistence and atomically consumes the grant with the ciphertext insert. No plaintext is returned or logged. The status remains **未连接 / NOT CONNECTED**
7. Register reviewed report/catalog/HTML/PDF inputs using the trusted server workflow. Exact reviewed pins must come from an independent trusted source. Verify private blob readback and immutable row registration, then enable only under a separately approved exact-catalog server setting. The owner-authenticated registration API accepts only those preapproved pins, never arbitrary source URLs or browser-chosen targets
8. After the separate execution/runtime approval, set `ZOTERO_EXECUTION_ENABLED=approved` through the supported configuration route. This alone performs no network call. The user explicitly clicks **核验连接**; successful rights verification binds the non-secret active connection version
9. The user selects catalog papers, explicitly chooses PDF handling and clicks Save. Continue only that execution or an explicitly resumed actual job. Never turn a legacy blocked/preview intention into an executable request
10. Perform a separately approved end-to-end live acceptance test: exact selected metadata and classifications, duplicate behavior, selected PDF transfer/registration, actual stored-byte readback, and interrupted-operation recovery. Browser/gateway/runtime behavior and production logging must also be checked. Any deployment/publication, cleanup, deletion, key rotation or new access needs its own applicable authorization

No quoted instructions or mock grant in this repository confer user permission. Review applicable service costs, storage quotas and operational limits before enabling the feature; this delivery makes no cost guarantee.

## Pinned private artifact registration gate

`ZOTERO_CATALOG_REGISTRATION_JSON` is a separate **ordinary private operator setting**, not a bearer token, API credential or generated grant. Its schema is `daily-agent-zotero-registration-config-v1`; it contains `approved`, `binding`, `trustedPins`, `pdfs`, `filenames` and `enable_after_registration`. Actual approval must precede configuring `approved: true`. The setting cannot grant Zotero access or replace the encryption/key-verification flow. This owner-folder path does not configure automatic daily catalog registration; that remains a separately approved trusted-operator workflow described with the hosting integration.

- `binding` fixes owner, report ID, catalog ID/version and `personal_scope` matching the independently verified personal-library binding
- `trustedPins` repeats that binding plus exact report/HTML/catalog SHA-256, reviewed report edition ID and approved source hosts; these facts must come from independent review
- `filenames` fixes the three source basenames; `pdfs` fixes each approved SHA-256, size and paper ID. The route allows at most 100 entries and a 5,000-byte configuration
- `enable_after_registration` is the operator's separately approved activation decision. Successful registration can deliberately remain disabled; activation is not a browser-selected field
- The existing owner/gateway/origin/DB binding and private `BUCKET.get/put` capabilities are required. Missing or invalid configuration returns `enabled: false`; no token/key or broad upload authority is minted

The user opens `/zotero/register`, selects a local directory and reviews exact matched counts/bytes. File selection hashes locally and uploads nothing. A fresh **登记私有目录** click rereads the approved configuration, sequentially sends only matching raw PDFs with same-origin/action-header checks, rereads staged facts, and sends base64 report/HTML/catalog bytes to `/api/zotero/catalog-registration/finalize`. It neither verifies a Zotero key nor executes imports. Unexpected files, mismatches and duplicate source basenames cannot be submitted.

Current route limits are report 1 MiB, catalog 1 MiB, HTML 4 MiB, combined decoded source bytes 4 MiB, PDF 16 MiB each and approved PDFs 24 MiB total; final JSON is capped at 9 MiB. These tighter interactive limits differ from the offline preparation helper's bounds. The returned `catalog_enabled` is distinct from whether the registration feature is available. Registered-but-disabled remains visible as such; a later separately approved activation configuration requires an explicit finalization action.

## Legacy AEAD encryption and shared request boundaries

- AES-256-GCM uses a fresh random 12-byte nonce and 128-bit authentication tag. Associated data binds cipher version, exact owner, `library_type: user`, personal-library ID, encryption-key ID and setup-grant ID
- D1 stores ciphertext, nonce, version/library bindings, state and timestamps. Only approved server verification/execution paths decrypt; there is no decrypt/download-secret API
- Imported WebCrypto keys are non-extractable. Byte buffers are cleared where possible, but JavaScript strings/runtime memory cannot be reliably zeroized. This is not a memory-erasure guarantee
- Credential staging bodies are bounded at 4 KiB and ordinary JSON actions at 16 KiB. The separately gated registration route has its own 9 MiB final JSON-body cap and exact-size PDF-body cap. Body reads have a 10-second deadline. Grant expiry is checked at entry and immediately before commit
- Owner grant consumption and credential insertion share one D1 transaction. Failure rolls back both
- Manager HTML has a nonce CSP, no external assets/analytics, no localStorage and `form-action 'none'`. Mutations use same-origin JSON with a custom header; all ordinary form fallbacks remain disabled
- Production logging/telemetry must independently redact request bodies, API/storage headers, secrets, ciphertext and signed upload/download data. Source-level redaction does not establish the platform's logging behavior
- Private R2 PDFs use owner-scoped content-addressed keys with pinned sizes/hashes. Registration checks exact stored bytes; execution loads only the selected catalog's approved reference and rechecks bytes. The runtime path is not an arbitrary URL proxy
- PDF stages include bounded memory copies, upload bodies and download/hash buffers. Review actual heap/request/concurrency limits; a 16 MiB PDF cap is not a 16 MiB process-memory promise
- Library leases/fencing and persisted `not_before` cooldowns serialize managed work and retain backoff across requests. This does not coordinate independent deployments with separate databases
- Revoked or mismatched local connections and expired leases block further guarded requests. An already in-flight remote write cannot be recalled; the durable prewrite journal and same-key readback retain uncertainty rather than asserting rollback

In legacy AEAD mode, a compromised trusted server can use its runtime decryption secret. Encryption protects against a database-only disclosure, not a compromised execution environment. DB backups, runtime operators, private blobs and approval/configuration surfaces all need appropriate access control.

## Migrations and verification scope

`0001_zotero_reference.sql` creates owner catalogs/paper allowlists, inert import intents, consumed grants and encrypted credential storage. `0002_zotero_execution.sql` adds immutable catalog binding fields, active connections, execution jobs/items/collections, fenced library leases and durable backoff. It does not convert old intentions or seed an account/catalog. Applied migrations must remain immutable; future schema changes need appended migrations and the supported host generator.

Local checks cover the real schema/SQL with mock D1, ciphertext-only storage, approval/owner isolation, catalog integrity, hashing, transport responses, durable stages/retries and the actual inline UI script. Their mocks do not prove gateway sanitization, supported deployment identity, real D1/R2 behavior, live rights, successful account connection, visual browser QA or successful production imports.

See [README.md](README.md) for endpoint and item/job-state semantics and [TRANSPORT.md](TRANSPORT.md) for exact protocol guarantees and deliberate limitations.
