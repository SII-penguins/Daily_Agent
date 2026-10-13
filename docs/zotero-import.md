# Zotero selected-paper import: integrated, default-disconnected application

Status: **integrated guarded Worker application, plus a retained Python offline
reference and synthetic UI prototype**. The Worker now wires credential staging,
explicit rights verification, trusted catalog registration, the selected-paper
manager, durable D1 execution and the server-side Zotero transport. Implementation
is present; live access is not provisioned by the source.

This delivery has not obtained a real key, connected a real account, written to a
real Zotero library, uploaded/read back a live Zotero PDF, changed the hosted Site,
or published the source. Default runtime gates keep the manager at **未连接 / NOT
CONNECTED**, with Save and credential entry disabled. An approved, configured,
explicitly verified runtime can become connected; this is no longer an
always-disconnected UI or an unwired future executor.

The Python package remains mock-only. Its `execution_mode: offline_mock` receipts
are local test evidence, never evidence of a Zotero save. A synthetic static
preview likewise remains disconnected regardless of the separate Worker's
capabilities.

## Three implementation layers

| Layer | Implemented purpose | Evidence boundary |
| --- | --- | --- |
| `daily_agent.zotero` | Python domain validation, selected-only mock importer, local SQLite receipts and process file lock | Accepts only the bound offline mock transport; no live API/key support |
| Static selection prototype | Synthetic local paper cards and classification preview | No real catalogs, keys, PDFs, library requests or enabled Save |
| `sites/zotero-worker/` | Owner-only Chinese manager, trusted catalog/private PDF registration, encrypted staging, explicit verification, guarded transport, durable D1 jobs and resume | Runnable with approved runtime dependencies; offline tests are not deployment or live acceptance |

The portable report remains read-only. `report_archive.inspect_html` rejects
active controls and scripts by design; no exception was added. The separate
`/zotero` manager uses the restrained warm-paper/dark-green reading style without
rewriting sealed report bytes. Existing Site/archive/sign-in routes must be
retained by the host integration.

The Python reference modules are unchanged:

- `domain.py`: digest-pinned report/catalog, selected-paper request, normalized
  identities, evidence-based publication classification and PDF checks
- `store.py`: durable local SQLite jobs/receipts/events and a process-releasing
  file lock; this is not the Worker's distributed lease implementation
- `importer.py`: mock-only state machine, additive classification and recovery
- `mock.py`: in-memory transport and content-addressed blob test double

The Worker implementation is separate JavaScript, not a Cloudflare Python runtime
dependency. Its central modules are `application.mjs`, `catalog-registration.mjs`,
`import-domain.mjs`, `job-runner.mjs`, `hash-bytes.mjs`, `manager-ui.mjs` and
`zotero-transport.mjs`. See its [README](../sites/zotero-worker/README.md),
[runtime gates](../sites/zotero-worker/RUNTIME_CONFIGURATION.md) and
[transport contract](../sites/zotero-worker/TRANSPORT.md) for exact interfaces.

## Selection and server authority

The manager gets catalogs and safe display metadata from authenticated server
GETs. Every paper starts unchecked. Date/topic/publication filters and the
selected-title preview change only browser memory. There is no automatic Save,
no persistent browser selection and no execution triggered by connection refresh.

The Python plan accepts its digest-bound reference request. The Worker Save
contract is deliberately explicit and separate: `action: save_selected`,
`report_id`, `catalog_id`, `catalog_version`, a nonempty unique set of at most 100
`paper_ids`, and `pdf_intent` equal to `metadata_only` or
`open_access_if_available`. It rejects extra fields. The browser cannot choose
metadata, classifications, source URLs, library IDs, credentials or collection
destinations through this endpoint.

The implemented Worker boundary authorizes every feature route using the trusted
Sites gateway's visitor identity and a separately verified Site-scoped owner.
Actions require exact same-origin HTTPS, JSON, a custom action header and bounded
bodies. A client-supplied owner claim or service bypass token is not visitor
identity. The gateway must remain the sole trusted ingress; an exposed raw Worker
cannot authenticate a spoofable header by setting a configuration flag.

## Trusted report and metadata sidecar

Both implementations support the existing `paper-first-corrected-edition-v1`
report and the separately reviewed `daily-agent-zotero-catalog-v1` sidecar. The
allowlist must cover exactly the report's papers, with unique IDs and matching
counts; repositories are excluded. Exact title/source/evidence lineage and
report HTML digest/length are checked against the pinned artifacts.

The sidecar binds identifiers, creators, venue/year, publication evidence,
reviewed topics, qualification/review/source-PDF digests and any approved PDF
reference. It deliberately does not manufacture verification from unreviewed
bibliographic data. Checksums establish artifact bindings; they do not
independently establish scientific truth.

Expected owner/report/library bindings and digests must come from an independent
private trusted registry. Browser-provided digests, a browser-provided sidecar,
or a checksum merely placed beside untrusted input do not establish trust.
Freeze the sidecar at the review/publishing boundary and retain its private
recovery evidence with the report.

The implemented Worker registration path verifies exact report/HTML/catalog
bytes, approved source hosts, canonical catalog and row bindings, and bounded
PDF bytes before issuing an in-memory preparation capability. It conditionally
stores owner-scoped private content-addressed blobs, checks readback and commits
immutable catalog rows in a D1 batch. New catalogs start with `enabled = 0`;
activation remains a separate trusted operator decision. The implemented
owner-only `/zotero/register` path accepts only an independently configured exact
artifact set, not arbitrary browser pins or metadata. A serialized review
descriptor is not an executable registration capability. R2 and D1 are not one transaction: a failed batch may
leave private blobs, never an intentionally published incomplete catalog.

## Deduplication and classification

The Worker transport fetches a fully paginated, bounded, version-consistent
inventory of the explicitly filtered bibliographic-item/attachment scope,
including matching Trash items. Standalone notes and annotations are excluded from item
responses and from the independent key/version proof using the same filter.
Completeness applies to this filtered scope, not to unread note content. The runner takes fresh inventories for relevant stages. A partial or
changing inventory cannot justify creating an apparent missing item.

Normalized DOI/arXiv identifiers provide strong identity matches; arXiv version
suffixes reduce to the root ID. Conflicting strong identifiers, multiple matches
and matches in Trash fail closed for resolution. No item is merged, deleted or
silently restored.

When both records lack DOI/arXiv, fallback requires exact normalized title,
complete ordered author names, year and venue. Unicode/case/whitespace are
normalized; punctuation, fuzzy title similarity and initials are not. Missing
supporting identity evidence blocks guessing. The Python mock suite retains
regression coverage for the same conservative design.

An item is filed in `DailyAgent / YYYY / YYYY-MM-DD` plus
`DailyAgent / Topics / <reviewed-topic>` collections. Tags record `DailyAgent`,
report date, topics and publication status. The Chinese manager translates known
display labels while the underlying classifications remain server-defined.
Verified formal publication requires separately pinned evidence;
preprint/unverified records are not promoted to journal/conference articles just
because the report included them. Existing descriptive metadata is not
overwritten; notes and annotations are left unchanged. No notes grant is needed or requested
by the integrated importer, and Notes access should remain off. Standalone note
and annotation items are excluded from inventory and exact-key item lookups.
Zotero can incidentally include an attachment `data.note` field; the transport
strips it before returning data and never uses, stores or logs it. This narrow
limitation means the implementation does not promise that no incidental note
bytes cross the API response wire. [Zotero attachment serialization](https://raw.githubusercontent.com/zotero/dataserver/master/model/Item.inc.php)

Zotero PATCH arrays replace full values. The implemented transport reads current
collection/tag objects, merges additions and uses the current version
precondition. A conflict returns to fresh read/merge through the durable runner;
it never falls back to a lossy full-item PUT. The Python mock also tests
concurrent-user-edit preservation. [Write semantics](https://www.zotero.org/support/dev/web_api/v3/write_requests)

## Durable execution and honest receipts

The Python SQLite ledger reserves object keys and survives reopening. Its local
file lock serializes processes sharing the ledger and releases on exit. These
facts remain local-reference properties, not claims about cloud coordination.

The implemented Worker separately has owner-scoped D1 execution jobs, item and
collection journals, preallocated object keys, library-wide leases, fencing
generations and persisted backoff. Execution identity includes owner, verified
personal library, connection version, report/catalog identity and hash, canonical
selected IDs and PDF intent. Equivalent reordered selections reuse a job;
unselected papers never enter its write loop. Existing collections are reconciled
by name and parent using their own durable keys.

The normal per-item progression is `queued → metadata_saved → pdf_pending →
complete` when PDF work is requested. Metadata-only jobs skip PDF work.
`already_exists` means an existing bibliographic item was reused and the requested
classification/PDF requirements were satisfied. `existing_item` remains an
independent fact. A `failure` retains successful metadata/classification flags;
`pdf_pending` does not mean a registered, verified PDF exists. Metadata-only
completion legitimately has `pdf_verified: false` and its recorded PDF intent.

Worker responses also expose `classification_saved`, `retryable`, ISO `retry_at`,
`uncertain` and safe error codes. Aggregate states distinguish `queued`,
`running`, `retry_wait`, `pdf_pending`, `complete`, `partial_failure`, `failure`
and current-connection blockers. A single failed attachment cannot erase earlier
success or be presented as complete. Registration is not byte verification.

Before a remote mutation, its intended operation and stable object key are
journaled. After interruption, the runner reconciles that key and identity;
unknown outcomes are not hidden by allocating a different item/attachment. Safe
retryable work honors server delays; unresolved uncertainty remains visible.
The lease/connection/catalog are checked before guarded requests and receipt
commits. An already in-flight remote write cannot be recalled, and there is no
transaction spanning Zotero and D1.

The live transport uses valid eight-character caller-persisted object keys and
version-zero creation. HTTP 200 must still pass per-object result checks and
readback. Zotero's temporary write-token deduplication is not a substitute for a
durable application journal. [Versioning and object keys](https://www.zotero.org/support/dev/web_api/v3/syncing),
[Write responses](https://www.zotero.org/support/dev/web_api/v3/write_requests)

## PDF integrity and network boundaries

The Python mock importer obtains already supplied content-addressed blob bytes
and never contacts Zotero or downloads a source URL. Its reference PDF limit is
64 MiB. That limit and its in-memory tests are not a Worker resource-budget claim.
The local SQLite store does use filesystem paths for its own ledger; this is
separate from accepting arbitrary paths/URLs as PDF input.

The integrated Worker uses a smaller **16 MiB application PDF limit**. It reads
only the selected owner's registered private blob reference, checks exact length,
SHA-256 and `%PDF-` signature, and computes MD5/SHA-256 with the included bounded
local hash adapter. No browser-provided URL, path or checksum can substitute for
these bytes. The PDF signature is a format sanity check, not malware analysis or
proof that the whole PDF parses. Memory is bounded but not fully streamed end to
end; upload bodies, copies and readback buffers can coexist.

The guarded Worker transport does make network requests after approved explicit
verification/advance actions. It constructs API routes only under the fixed
Zotero HTTPS service and validates the narrowly allowlisted official storage
host. API credentials are never forwarded to storage, general API redirects and
storage redirects are rejected, and returned arbitrary URLs are not a fetch API.
GET page/catalog/job routes do not make these outbound requests.

The implemented PDF flow uses stored child attachments (`imported_file`), not
linked URLs: create attachment metadata; authorize using actual MD5, size,
filename and millisecond mtime; transfer the authorized bytes; register; then
read back metadata and actual stored bytes to verify size, MD5, SHA-256, parent
and stability. `exists:1` skips transfer/registration but still requires
verification. MD5 is a protocol requirement; SHA-256 remains the stronger local
content identity. [File-upload protocol](https://www.zotero.org/support/dev/web_api/v3/file_upload)

There is intentionally no live source-download service in this application. If a
future authorized acquisition workflow is added, prefer already reviewed private
blobs. Resolve server paper IDs rather than accepting client URLs; enforce HTTPS,
source-host and safe-address policies, DNS/redirect revalidation, time/size limits
and content checks. That would be a separate reviewed capability, not permission
to loosen the Zotero storage allowlist or bypass publisher restrictions.

## Explicit private file registration

The owner-only `/zotero/register` page reads approved filenames, hashes, sizes and
limits from `/api/zotero/catalog-registration`. The operator setting
`ZOTERO_CATALOG_REGISTRATION_JSON` fixes exact owner/report/library bindings,
reviewed pins and `enable_after_registration`; it is ordinary private configuration,
not a newly created credential or token.

Directory selection matches the three configured source basenames and PDF
basenames equal to their SHA-256. It verifies local sizes/hashes before any data
transmission, displays exact matched file counts/bytes, and excludes unrelated
files. Only a fresh **登记私有目录** click rechecks configuration, stages the pinned
PDFs sequentially as raw `application/pdf`, reads staged/pin facts again, then
sends exact report/HTML/catalog bytes in the bounded final JSON request.

These owner-authenticated routes cannot choose new pins, upload arbitrary content,
change library scope or enable a catalog contrary to the server's approved policy.
Finalization reruns trusted source/PDF validation and immutable registration;
registered-but-disabled and enabled are separate results. Selecting files,
registering a catalog, or enabling it does not connect a Zotero key or Save papers.
The interactive route caps report/catalog at 1 MiB each, HTML at 4 MiB, combined
source bytes at 4 MiB and approved PDFs at 24 MiB total, with each PDF at most
16 MiB. These route bounds differ from the offline Python/helper limits. The
initial explicit owner-folder path is implemented; automatic daily catalog
production/registration still needs its separately approved trusted-operator
workflow and is not configured or promised by this page.

## Credential setup and explicit execution flow

The Python package and synthetic preview have no API key input. The Worker has
an owner-only password form, disabled until its short-lived one-time approval
and runtime-encryption gates pass. Staging uses server-side AES-256-GCM and stores
only ciphertext plus bound identifiers; active verification/job records are
non-secret. There is no endpoint to retrieve the key.

Creating/configuring ongoing access needs explicit action-time user permission
through the supported secure flow. The user personally creates/selects and
submits their credential where required; never ask them to paste a real key into
chat. Runtime secrets belong in the supported secret facility, never source,
HTML/JS, localStorage, report manifests, public Git, logs, screenshots, job
payloads or automation instructions. Source redaction does not prove that host
observability is safely configured.

The supported order is:

1. Verify owner-private hosting, gateway/origin/owner identity, retained archives,
   D1/private R2 bindings and both migrations; configure independent pins for
   reviewed owner/library-bound catalogs and PDF blobs; use the explicit owner-only
   registration UI and server-approved activation policy
2. Obtain persistent-access/security/secure-entry approvals, configure the AEAD
   runtime secret and scoped one-time grant, and have the user personally stage
   the key. **Staged remains NOT CONNECTED**
3. After the separate live-execution setup approval, enable the explicit runtime
   gate and have the user click **核验连接**. The fixed current-key check validates
   the exact personal-library identity and current application rights, including
   library/write/files, with no notes-read requirement; it cannot authorize
   unapproved other libraries
4. Select papers, explicitly choose PDF handling and click Save. This creates or
   reuses the deterministic execution; the manager then issues serial bounded
   continuation POSTs for that request. The current route processes at most one
   selected paper and one durable stage per invocation
5. Poll read-only progress, honor retry time and stop on terminal/connection
   blockers or nonretryable unresolved PDF work. On return after interruption,
   inspect execution history and explicitly click **继续此任务** if appropriate

Opening a page, connecting a key or refreshing history never autoexecutes old
jobs. Legacy `blocked_connection` and `preview_only` intentions remain separate
and are never upgraded into execution jobs. A new Save uses the current catalog
and verified personal-library connection version; a changed binding cannot
silently retarget an old job. Revocation and local connection/lease loss block
further guarded writes, without pretending already in-flight requests rolled
back. [API authentication and access](https://www.zotero.org/support/dev/web_api/v3/basics),
[Library access verification](https://www.zotero.org/support/dev/web_api/v3/syncing)

## Offline verification

From the source root with the project's test environment:

```bash
PYTHONPATH=src python -m pytest -q tests/test_zotero_import.py tests/test_zotero_preview.py
PYTHONPATH=src python scripts/preview_zotero.py --output ../prototype
cd sites/zotero-worker
npm run check
npm run build
node scripts/preflight.mjs
```

The preview command writes synthetic local assets only. Its labels demonstrate
presentation, not real catalog evidence. Existing report/archive regressions
remain required. The Python tests cover domain tampering, identity/classification,
mock-only scope, local lock/restart behavior and mock PDF integrity/failures.

The Worker tests separately exercise real schema/SQL through local D1 adapters,
owner/action isolation, encrypted staging, trusted registration and hashes,
guarded application routing, durable stages/leases/backoff/uncertainty, mock
transport responses and the actual manager inline JavaScript in a DOM/fetch/timer
harness. They include unchecked defaults, explicit PDF choice, selection without
writes, double-click/reset/navigation safety, gated verification, explicit Resume
and honest partial statuses. Registration tests also verify local pins before
transmission, exact-file-only uploads, configuration drift, staged readback,
registered-disabled state and interrupted/repeated actions. A DOM harness is not visual browser QA.

The core build stages the complete ESM graph and migrations; it does not itself
produce an approved deployment or enable credentials. The separate hosting
adapter owns host packaging/archive retention checks. Local validation and an
artifact build do not prove the production gateway, real D1/R2 behavior, actual
permissions, successful live uploads or runtime resource limits.

## Activation and acceptance boundary

The catalog registrar, hash adapter, durable executor, selected-paper manager and
transport wiring are implemented. Remaining blockers concern verified hosting,
real runtime resources and secrets, actual user authorization, private artifact
registration/activation, production review, and live acceptance; they should not
be described as an unfinished executor or a connection established by mocks.

After the applicable setup approvals, the user can personally stage the narrowly
scoped key and explicitly verify it. Before any real acceptance import, disclose
and approve the particular personal library, selected papers/PDFs and intended
classification changes. The acceptance check must establish: only selected
items affected; identity-level deduplication; original notes and collections preserved, no standalone note/annotation queries,
and incidental attachment note fields discarded without use/storage/logging;
correct DailyAgent/date/topic membership; each requested PDF registered and
byte-verified; repeated equivalent Save produces no additional items; interruption
recovers through the same recorded keys; and failed attachments remain visibly
partial. Never delete a test item automatically.

Source publication, Site deployment, actual library changes, key rotation/removal
and broader permissions remain separate authorized actions. Review current
service quotas, request/memory limits, storage and applicable charges; no test or
implementation claim is a cost guarantee. None of these documents grants the
permission required to activate those actions.
