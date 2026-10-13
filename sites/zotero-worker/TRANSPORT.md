# Zotero transport: integrated behind explicit runtime gates

`src/zotero-transport.mjs` is a dependency-free, server-side Web API v3 transport, now imported by `src/application.mjs` and used by the gated verification/execution routes. The transport module itself does not read environment credentials or fall back to global `fetch`; constructing it has no side effects. The application supplies a guarded server fetch and a short-lived decrypted credential only after owner authorization and runtime checks. **Default configuration remains disconnected.** No real Zotero account, API key, library, upload, deployment, publication, or secure grant was used to develop or test this source.

## Verification performed

Run the isolated suite with `node --test test/zotero-transport.test.mjs`; use Node 24+ for the complete application suite. All HTTP responses come from injected local mocks; identifiers and credentials are explicitly fake. Node's built-in crypto is used only inside tests to compute real fixture hashes. The module itself has no Node/package dependencies.

The suite covers permission ownership/scopes/expiry, complete indexes and version drift, pagination, version-zero keys, HTTP-200 object-level failures, duplicate/conflict outcomes, conditional patches, parent collections, stored-PDF attachment creation, every upload stage, actual readback bytes, metadata/ETag/hash/size mismatch, redirects/host tricks, separated API/storage headers, redacted failures, size caps, registration failure, mutation uncertainty, backoff and request cancellation. It provides protocol/security regression evidence, **not live compatibility evidence**.

## Constructor contract

Export: `createZoteroTransport(config)`; typed failures: `ZoteroTransportError`.

Required configuration:

- `fetchImpl`: a trusted server fetch implementation supporting manual redirects, readable response streams, AbortSignal, credential omission and Blob request bodies. It must actually honor those options. Browser fetch can hide a cross-origin redirect and is unsuitable for this adapter. No fallback to global fetch exists
- `userID`: the positive numeric ID of one explicitly authorized personal library; leading zeroes, path/query injection and unsafe integers are rejected
- `apiKey`: obtained through a separately approved secure server-side grant. It is captured in a closure and appears only in API request headers. Do not place it in URLs, client JavaScript, application records, logs, sample files or chat
- `hashBytes` for any PDF operation: trusted local async function accepting `Uint8Array` and returning lowercase `{ md5, sha256 }` computed over those exact bytes. It must not transmit bytes elsewhere. Standard WebCrypto has **no MD5**. The integrated application supplies `src/hash-bytes.mjs`, with a bounded local MD5 implementation and WebCrypto SHA-256 over an owned byte snapshot. RFC vectors, block-boundary cases and independent fixture checks run locally; production runtime/memory acceptance remains separate. A caller-supplied unverified checksum is not a hashing implementation

Optional limits/defaults:

- `maxPdfBytes`: 16 MiB; configurable only within 8 bytes–32 MiB
- `maxJsonBytes`: 4 MiB per API response; hard maximum 16 MiB
- `maxInventoryObjects`: 50,000 per object type; maximum 100,000
- `maxInventoryBytes`: 32 MiB combined item/collection serialized metadata; maximum 64 MiB
- `pageSize`: 100; maximum 100
- `timeoutMs`: 30 seconds per request including streamed body; maximum 120 seconds
- `permissionTtlMs`: 5 minutes; cannot be increased beyond that
- `maxReadRetries`: 2; maximum 5
- `now`, `sleep`: injectable clock functions for deterministic tests. `sleep` must really wait or advance that clock; an early return does not bypass backoff

The returned object contains methods only. Error messages are stable codes, never response text, URLs, API key details, raw upstream error messages or error causes. `toJSON()` exposes only code, numeric HTTP status/retry delay, and the `uncertain` flag. The module has no logging. The application's fetch instrumentation must separately redact headers, upload forms, signed URLs and bodies.

## Permission and inventory contracts

`verifyCurrentKey({ requireWrite = true, requireFiles = true, requireNotes = false })` uses `/keys/current`, verifies exact owner identity, and requires personal-library access plus the requested write/files grants; it does not require notes permission. It returns only owner ID and booleans. All other methods require a fresh successful check. A later API 401/403 clears the remembered check. Read-only verification is possible, but writes and file operations remain gated by their grants. The integrated importer does not request a broader notes grant; Notes access should remain off. Standalone note and annotation items are explicitly excluded from inventory and item lookups. A Zotero attachment JSON response can incidentally contain `data.note`; the transport removes that property at its boundary, never returns, stores, logs or uses it. This is not a byte-level guarantee that no incidental note field arrives from Zotero. See [Zotero attachment serialization](https://raw.githubusercontent.com/zotero/dataserver/master/model/Item.inc.php).

`getItem(key)` / `getCollection(key)` return `{key, version, data}`, or `null` when absent. Item lookup uses a filtered multi-item `itemKey` query rather than a direct item endpoint, so an unexpected standalone note/annotation key cannot return its content. Key, personal-library identity, nested data identity and version header are checked. Links from response objects are neither returned nor followed.

`inventory()` returns `{libraryVersion, complete: true, includesTrashed: true, excludesNotes: true, items, collections}` only for the explicitly filtered bibliographic-item/attachment scope, after all checks succeed. Here `complete` is scoped completeness, never a claim to have read notes or annotations:

1. Page through the explicitly note/annotation-excluding item set (including matching trash items and child attachments) and all collections, building each next URL locally
2. Require exact page lengths, stable total counts, unique valid keys, one stable library version and bounded metadata
3. Fetch independent complete item and collection version maps using the same item-type exclusions; require every key/version to match the downloaded filtered objects and the same library version

Both item pages and item version proof use `includeTrashed=1&itemType=-note%20%7C%7C%20annotation`; `getItem` uses the same filter with the exact item key. Collection reads remain separate. The result is complete for the admitted bibliographic/attachment scope, not for excluded note/annotation items. [Item search syntax](https://www.zotero.org/support/dev/web_api/v3/basics#search_syntax), [Matching-object version proof](https://www.zotero.org/support/dev/web_api/v3/syncing#format_versions)

Missing totals/versions, a short page, a repeated key, a mismatching final map, or a changing library never produces `complete: true`. Large libraries fail with a stable size error instead of returning a partial index. Untrusted pagination Link URLs are ignored. This is a version-consistent filtered-item/collection snapshot, not a long-lived lock, an index of saved searches, or a complete incremental synchronization engine. Any library can change immediately after a successful response. The integrated runner requests fresh complete filtered inventories at the relevant stages instead of treating an old snapshot as authoritative. Any future persistent inventory cache must be committed atomically and invalidated/rebuilt after relevant writes or drift.

## Metadata writes

- `createItem({key, data})`: create a bibliographic item using a **caller-persisted** valid key and forced JSON `version: 0`; attachment/note/annotation types are excluded from this method. Tags, collection keys and relations are carried as metadata only
- `createCollection({key, name, parentCollection = false})`: create a caller-keyed collection and retain the explicit parent. Self-parenting is refused
- `patchItemMembership({itemKey, expectedVersion, collections?, tags?})`: send only these fields with `If-Unmodified-Since-Version`, then read back and verify the result. Arrays are complete desired lists; callers performing additive changes must merge existing memberships/tags from a fresh item before calling
- `createStoredAttachment({key, parentItem, title, filename, tags = []})`: create a version-zero child `attachment` with `linkMode: imported_file`, PDF content type, and null MD5/mtime. It does not claim a file has been uploaded

Create responses require the v3 `successful`/`unchanged`/`failed` maps, exactly one disposition for index 0, the expected saved key/version, and matching submitted fields. HTTP 200 alone never means success. An unchanged create or conflict is returned as an error for explicit reconciliation; it is never converted into a fresh random key. Older `success` response shapes are intentionally refused pending verified compatibility. No write-token header is needed with version-zero writes.

The transport does not do DOI/title deduplication, identify an existing destination collection, generate persistent keys, resolve conflicts, merge user metadata, or decide that an existing attachment belongs to an import. Those are application responsibilities. It exposes no delete, group-library, PUT/full-item overwrite, API-base override or arbitrary-URL fetch method.

## PDF stage contracts

`uploadStoredPdf({itemKey, parentItem, bytes, filename, mtime, previousMd5 = null})` composes the stages below. `mtime` is milliseconds. Files are bounded `Uint8Array` values with a PDF header; this is a format sniff, not malware analysis or a full PDF parser. The adapter copies caller bytes before hashing/authorization to prevent later caller mutation.

1. `authorizePdfUpload(input)` checks the existing attachment's parent/type/link mode and current stored hash, then submits the new hash, filename, size and mtime. New files use `If-None-Match: *`; replacement files use the supplied and verified previous MD5. Returns an opaque in-memory ticket plus safe hashes/size. A server `exists: 1` response moves directly to the registered stage, but still requires readback
2. `transferPdfUpload(ticket)` sends the prescribed multipart prefix + bytes + suffix to the validated storage host. Only storage Content-Type is sent; no Zotero API key/version, Authorization header, cookie or referrer is forwarded. Only HTTP 201 counts as transferred. The result explicitly says not yet registered/verified
3. `registerPdfUpload(ticket)` submits the upload token to the original fixed Zotero item-file endpoint with the same precondition. Only HTTP 204 counts as registered. Registration alone is not stored-byte verification
4. `verifyPdfUpload(ticket)` reads attachment metadata, obtains a validated file download, checks ETag, exact byte count, computed MD5 **and SHA-256**, and rereads metadata for version stability. Only this returns `storedBytesVerified: true`

`verifyStoredPdf({itemKey, parentItem, filename, mtime, size, md5, sha256})` performs the same verification from persisted expected facts after a restart. It downloads only through the authorized item's fixed Zotero file endpoint, never a caller URL. No download token or signed URL is exposed by the methods.

Tickets cannot be forged, transferred between transport instances, serialized for resume, or reused for a second transfer/registration. Failed transfers/registrations require explicit reconciliation. They deliberately do not auto-retry. Persist the attachment key and expected hashes before starting; after a crash, inspect that same item and use `verifyStoredPdf` where appropriate. Do not create a second attachment to hide an ambiguous first attempt. A genuinely needed reauthorization is a fresh bounded operation against the same key and the verified current hash.

Memory is bounded, not streaming end-to-end: owned upload bytes, Blob internals, download chunks plus a final contiguous copy, hash-adapter working memory and JSON object overhead can coexist. Runtime concurrency/heap budgets must allow for several times the PDF limit. Production hashing must also be bounded. The module serializes HTTP requests per instance, but the application must limit concurrent jobs/instances.

## Network boundary and retry policy

API requests are built only under `https://api.zotero.org`, scoped to the configured numeric personal-library ID (plus `/keys/current`). Every request uses API version 3, header authentication, manual redirects, no browser credentials and no referrer. General API redirects are rejected. The one supported manual API-file download redirect must lead to exactly:

`https://zoterofilestorage.s3.us-east-1.amazonaws.com`

Credentials, non-default ports, fragments, backslashes, other schemes, suffix tricks, IP hosts, generic S3 buckets and any other storage/CDN host are rejected. Storage redirects are rejected even if they lead to another path on the allowed host. The allowlist is deliberately narrow: a future official host change requires evidence, review, a source change and regression tests, not a permissive runtime override.

Successful `Backoff` responses delay later requests. GET 429/503 retries are bounded, respect server delays, and otherwise use exponential waits. A delay over 30 seconds returns `BACKOFF_ACTIVE` with its remaining duration for durable scheduling; it is never shortened. Writes are never automatically replayed. HTTP 409/412, object-level conflicts, transient errors and quota errors retain stable codes.

`uncertain: true` means a mutation may have happened or happened without a verified result. This includes network/timeout failures on writes, 5xx writes, malformed successful write responses, and post-write readback failures. Reconcile the persisted object before retrying. `uncertain: false` is not proof that an entire multi-stage import made no changes: earlier stages may have succeeded. The durable application ledger must record each stage.

## Integrated application responsibilities

`src/application.mjs` now supplies the owner-authorized application boundary:

- Staging remains the approved AES-256-GCM flow in `worker/index.js`. The user personally enters a key; only ciphertext is stored
- `POST /api/zotero/verify-connection` requires the approved execution gate, bound staged ciphertext, exact personal-library ID and explicit `verify_connection` action. It decrypts only server-side, calls the fixed `/keys/current` endpoint and persists non-secret rights/connection-version state
- Page and GET requests read local state only. The presence of transport code or staged ciphertext does not enable outbound work
- `POST /api/zotero/executions` requires a fresh `save_selected` action and creates/reuses the canonical selected-paper execution job; it does not itself make import requests
- `POST /api/zotero/executions/:id/advance` requires `continue_import` and invokes the durable runner with `maxSteps: 1`. Each invocation checks current connection/catalog bindings and guards actual transport requests with the library lease and fencing checks
- `src/job-runner.mjs` stores reserved keys, item/collection checkpoints, uncertainty and retry facts. It handles conservative identity matching, complete fresh inventories, additive memberships, version conflicts, and same-key readback after interruption. It never consumes legacy blocked/preview intents
- The application fetch wrapper uses the server fetch implementation, checks runtime/credential identity at request boundaries and records response backoff through the runner. The application disables transport-local read retries and long sleeps (`maxReadRetries: 0`), so the durable job state schedules later work
- Library-wide D1 leases/cooldowns coordinate distinct selection jobs in the configured database. This is not a global throttle across unrelated deployments or credentials
- PDF input is loaded from verified owner-scoped private R2 blob references, with a 16 MiB application limit, then hashed again. The transport never fetches a catalog source URL to obtain input bytes

The browser continues only its current explicit Save/Resume operation, reads before advancing, honors retry times, and stops at terminal/connection-blocked states or nonretryable unresolved PDF work. An interrupted page can be reopened and an actual pending execution explicitly resumed. Background connection refresh and legacy intent listing cannot execute imports.

A transport `uncertain` outcome stays distinguishable from failure and success in the runner. Metadata, classification, attachment registration and PDF-byte verification have independent evidence. An attachment failure never erases successful metadata, and successful registration alone never sets `pdf_verified`.

## Remaining gates before any live use

1. **Explicit authorization and secure entry.** The user must approve the named personal-library access, intended changes and selected PDF transfers. Creating or configuring persistent credential access requires the supported action-time approval/secure-entry flow. No source file, mocked approval or stored boolean substitutes for actual authorization
2. **Production identity and credential custody.** Configure the verified owner-private gateway/origin, D1 and private R2 bindings, both migrations, approved runtime AEAD secret and exact library/key-version binding through supported tools. Review platform logs so secrets, signed URLs, request bodies and storage forms are not captured. Encryption is implemented; production configuration and key rotation/revocation workflows are not automatically provisioned
3. **Reviewed catalog registration.** Supply independently reviewed report/catalog/HTML pins and approved PDF bytes, perform the trusted private registration/readback workflow, and separately activate the immutable catalog. The owner-authenticated registration UI/routes accept only that exact configured artifact set and enable it only if separately approved in server configuration; there is no arbitrary/public upsert or file/URL importer. Automated daily registration still requires its separate trusted-operator workflow
4. **Deliberate live enablement.** `ZOTERO_EXECUTION_ENABLED` must be exactly `approved`, runtime dependencies must exist, and the user must explicitly verify the staged key. Current application connection verification requires personal-library library/write/files permissions, with no notes-read requirement. Standalone note/annotation items are excluded and left unchanged; incidental attachment `data.note` is stripped before transport output and never logged, stored or used. Do not claim that no incidental note bytes arrive on the API response wire. It does not silently authorize other libraries/groups
5. **Runtime and live acceptance.** Verify fetch redirect/stream/AbortSignal behavior, memory limits, concurrency, actual D1/R2 behavior, real API response shapes and storage-host compatibility. With separately approved selected papers, test metadata/collection readback, duplicate handling, selected PDF registration plus actual stored-byte MD5/SHA-256 verification, and interrupted-write recovery. Any cleanup/deletion requires its own authorization
6. **Separate deployment decision.** Host packaging, owner identity, retained archives, deployment/publication and actual live enablement require their own completed checks and approvals. Review applicable service quotas and charges rather than assuming the implementation is cost-free

Unsupported application capabilities include automatic key acquisition, OAuth setup, user-facing key rotation/removal, group libraries, saved-search synchronization, WebDAV, imported-URL/snapshot/ZIP attachment uploads, binary-diff uploads, resumable multipart storage, arbitrary download sources, deletions, broad automatic conflict resolution and full PDF parsing. The transport itself remains a protocol adapter; it does not independently supply account setup, authorization or durable job state. No stub claims success for unsupported operations.

## Official evidence checked 2026-10-10

- [API basics](https://www.zotero.org/support/dev/web_api/v3/basics): HTTPS, authentication/version headers, personal-library routes, pagination and throttling
- [Write requests](https://www.zotero.org/support/dev/web_api/v3/write_requests): object keys, per-object results, conditional writes and array replacement behavior
- [Syncing](https://www.zotero.org/support/dev/web_api/v3/syncing): current-key grants, version-zero creates, library/object versions and consistent snapshots
- [File uploads](https://www.zotero.org/support/dev/web_api/v3/file_upload): authorization, full transfer, registration, existing-file preconditions and download ETags
- [Zotero Team, July 22, 2026](https://forums.zotero.org/discussion/132874/sync-error) and [Zotero Team, April 19, 2025](https://forums.zotero.org/discussion/123669/ipad-zotero-error-the-attached-file-could-not-be-found): primary confirmation of the exact regional Zotero storage host used by the allowlist

Documentation review was public and read-only. No account/API/storage probe was performed.
