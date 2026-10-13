# Exact-byte hashing and trusted private catalogs

This is source-only, offline-tested infrastructure. Importing these modules does not
fetch, register, deploy, connect an account, or change a Zotero library. There is no
public or arbitrary catalog upsert route. The optional owner-authenticated, server-pinned registration flow below is disabled unless explicitly configured. Live services were not exercised.

## Hash adapter

`src/hash-bytes.mjs` is dependency-free and works with Worker `Uint8Array`,
`DataView`, and WebCrypto. It does not import Node APIs.

```js
import { createHashBytes } from './src/hash-bytes.mjs';
const hashBytes = createHashBytes({ maxBytes: 16 * 1024 * 1024 });
// Supply to the explicitly authorized transport factory:
// createZoteroTransport({ fetchImpl, userID, apiKey, hashBytes });
const { md5, sha256 } = await hashBytes(pdfBytes);
```

- MD5 implements RFC 1321 over the exact byte view, including nonzero byte offsets
- SHA-256 uses WebCrypto; it is never substituted for the MD5 required by Zotero
- MD5 is an integrity/protocol field only, never a security identity or signature
- Both adapter hashes use one owned byte snapshot, so an async caller mutation
  cannot change which bytes one hash covers
- Shared memory and non-`Uint8Array` values are rejected, rather than coerced
- Default PDF bound is 16 MiB, inclusive; configurable upper bound is 32 MiB,
  matching the transport's allowed maximum
- `md5Bytes(bytes)` and async `sha256Bytes(bytes)` are separately exported, with
  a 32 MiB hard bound; `hashBytes(bytes)` is the default 16 MiB adapter
- Input is never text-decoded, normalized, base64-encoded, or expanded to the
  entire backing `ArrayBuffer` before hashing

Tests include all seven RFC vectors, independent `node:crypto` comparisons at
padding/block boundaries and random sizes, Unicode/binary/subarray inputs,
caller mutation, shared-memory rejection, and exact maximum/maximum+1 bounds.
Node crypto appears only in tests, not Worker source.

## Trust boundary

`prepareTrustedCatalog` accepts three exact byte streams:

1. `paper-first-corrected-edition-v1` report manifest
2. Exact report HTML matching its seal
3. `daily-agent-zotero-catalog-v1` Python metadata sidecar

It also accepts reviewed pins from a separate, authenticated server/operator
registry, a server binding, and an offline PDF-byte reader. The pins must never be
accepted from a browser Save request, inferred from untrusted metadata, or treated
as authoritative merely because a checksum file sits beside the inputs. Hashes
prove integrity/binding, not publisher authenticity, permission to redistribute,
scientific correctness, or a fresh correction/retraction check.

The server binding has exactly:

```json
{
  "owner_id": "synthetic-owner",
  "report_id": "synthetic-report",
  "catalog_id": "synthetic-catalog",
  "catalog_version": 1,
  "personal_scope": "personal:12345"
}
```

The numeric example is a synthetic test placeholder, never a selected account.
The separately reviewed `trustedPins` contains those same five fields plus:

- `report_sha256`: exact raw manifest digest
- `catalog_sha256`: exact raw Python sidecar digest
- `html_sha256`: exact raw HTML digest, also required to equal the report's seal
- `report_edition_id`: expected manifest edition
- `allowed_source_hosts`: explicit exact publisher host allowlist

`personal_scope` must identify the already approved personal target library. It
cannot be a group, browser-selected library, username guess, or an empty scope.

```js
const prepared = await prepareTrustedCatalog({
  binding,
  trustedPins,
  reportBytes,
  htmlBytes,
  catalogBytes,
  readPdfBytes: sha256 => privateLocalReader(sha256),
});
```

Preparation checks all bytes before permitting catalog registration. The optional HTTP flow may separately stage independently pinned PDF objects before final source validation; it cannot publish or enable catalog rows until every source validates:

- Independent raw report/catalog/HTML pins and report-to-HTML size/seal
- Exact owner/report/catalog/version/personal-scope pin binding
- Supported schemas, expected edition and canonical calendar date
- Complete report paper allowlist, unique IDs and exact row count
- Exact title/source URL/review/qualification/PDF lineage per report row
- Strict catalog, paper, PDF, manifest and manifest-paper field allowlists
- UTF-8, duplicate JSON keys (including escaped duplicates), prototype-related
  keys, non-scalar Unicode, non-finite/fractional numbers and excessive nesting
- HTTPS metadata URLs with explicit host allowlist, no credentials, query,
  fragment, nondefault port, IP literals or private-host suffixes
- Valid DOI/arXiv forms, ordered author strings, stable identity prerequisites,
  publication status/item-type consistency and reviewed topic identifiers
- PDF content addressing, size, `%PDF-` header and independently computed SHA-256
- 4 MiB JSON, 16 MiB HTML, at most 1,000 papers and 256 MiB aggregate unique PDFs

The profile requires all known Python paper fields explicitly, including empty
identifier/evidence strings and `pdf: null` for an intentional metadata-only row.
Publication evidence digests remain provenance references; this function does
not reinterpret evidence documents or promote a preprint to a published article.
Only already reviewed, separately pinned sidecars may assert verified metadata.

Prepared descriptors are deeply frozen. Owned PDF snapshots remain in a private
in-memory `WeakMap`, are omitted from JSON, and are not shared with callers.
Serialized descriptors cannot be passed back as registration capabilities.
Re-prepare original pinned bytes before registering a saved descriptor.

## Stored representation and read API

The original sidecar's fields and array order are retained. Canonical JSON sorts
object keys recursively, preserves exact Unicode strings and array order, and
uses no whitespace. For this integer-only, fixed-key schema it matches Python's
`json.dumps(..., ensure_ascii=False, sort_keys=True, separators=(',', ':'))`.

The executor migration provides these catalog columns:

- `catalog_blob_json`: complete canonical source sidecar JSON
- `content_sha256`: SHA-256 of UTF-8 `catalog_blob_json`
- `source_catalog_sha256`: independent digest of the original raw sidecar
- `report_sha256`, `report_day`, `personal_scope`
- `registration_sha256`: SHA-256 of canonical `registrationBinding(row)`

The exact registration payload is:

```js
{
  schema: 'daily-agent-zotero-registration-v1',
  owner_id, report_id, catalog_id, catalog_version, personal_scope,
  report_sha256, report_day, source_catalog_sha256, content_sha256
}
```

Each `zotero_catalog_papers.metadata_json` is the canonical corresponding source
paper object, without added/untrusted metadata. `display_title` must equal its
exact title. Prepared descriptors also carry `row_sha256` for offline review;
execution/readback establishes row identity by equality to the canonical embedded
paper, so a separate row-digest column is unnecessary.

```js
const result = await readRegisteredCatalog({
  db, ownerId, reportId, catalogId,
  personalScope, // optional on owner-only display; required for target execution
  requireEnabled: true, // default; disabled/missing catalogs return null
});
// result is null, or frozen { row, papers }
```

This shared reader rechecks canonical equality, both stored canonical/registration
hashes, report binding, personal scope when supplied, strict metadata validation,
and exact paper-row identity. It does not authenticate an HTTP caller. Routes
must establish the owner first, and must sanitize their public result. Never
return the private row, pins, internal blob keys or account-binding metadata as a
public browser catalog. Legacy rows lacking integrity columns are not executable.

## Private blob namespace and registration

`privateBlobKey(ownerId, sha256)` is async and returns:

```text
zotero-pdfs/<SHA256(UTF8 ownerId)>/<PDF SHA256>
```

The key is owner-scoped and does not reveal the raw owner identifier. Buckets
must remain private; there is no public download/blob route. Content hashes and
object keys alone are never authorization to download another user's PDF.

```js
const blobs = createPrivateR2BlobStore(injectedPrivateBucket, { ownerId });
const context = { ownerId, reportId, catalogId, paperId, sha256, size };
// readBlob(context) -> Uint8Array | null
// putBlobIfAbsent(context, bytes) -> boolean
```

Runtime readers must validate the requested paper against its authenticated
registered catalog before obtaining bytes. The adapter itself checks the owner,
context shapes, object byte bounds and namespace. Registration supplies context
from its trusted prepared rows. The adapter uses an `If-None-Match: *` equivalent
conditional put and an explicit SHA-256 checksum; it never overwrites an object.

```js
const receipt = await registerTrustedCatalog({
  prepared,
  expectedBinding: independentlySelectedServerBinding,
  db: injectedD1Binding,
  blobs,
});
```

Only an explicit server-side/operator call performs this operation. There is no
network/service discovery and no implicit request handling in this primitive. The
separate owner-only route calls it after explicit finalization. It requires an
in-process prepared capability, an exact independent expected binding, an atomic
D1 batch interface and owner-bound conditional private blob operations.

- Existing catalog rows and every existing blob are checked before any mutation
- Identical duplicate registration is an immutable no-op
- Any changed version, metadata, binding or stored PDF causes a conflict
- All PDFs are written/read back before one atomic catalog/paper insert batch
- Rows are inserted disabled (`enabled = 0`); registration grants no execution or
  activation authority and never resets a separately enabled catalog on retry
- This primitive has no update, replacement, delete, or public upsert; the separately gated explicit owner finalization route can conditionally enable an exactly matching newly verified catalog
- Concurrent identical registration is checked and treated idempotently
- D1 and R2 have no shared transaction: a failed D1 batch may leave verified,
  owner-scoped private orphan blobs. A retry verifies/reuses them. There is no
  automatic destructive cleanup and no partial enabled catalog

## Offline CLI

The CLI has no database, deployment, Site, upload, HTTP, or account functionality.
It reads only explicitly named local inputs. It does not acquire or regenerate
trust pins. Without `--output`, it validates and prints counts only.

```sh
node scripts/prepare-catalog.mjs \
  --pins /private/reviewed-pins.json \
  --report /private/report.json \
  --html /private/report.html \
  --catalog /private/catalog.json \
  --pdf-dir /private/pdfs
```

Add `--output /private/prepared-seed.json` only to write an offline private review
artifact. Its parent directory must already exist. The file is exclusively
created with mode 0600 and is never overwritten. Output inside the public source
tree, `public`, `dist`, `node_modules` or `.git` is rejected, including parent
symlinks resolving there. It contains private metadata and local source paths,
so do not publish it. It copies no PDF bytes and contains no executable SQL.

PDF filenames are bare SHA-256 digests in the explicit private PDF directory.
Individual input symlinks are rejected and local reads are size-bounded. Output
logs contain counts and safe error codes, never titles, paper/account IDs, source
paths, private PDF contents or credentials.

## Verification performed

`node --test test/hash-catalog.test.mjs` passes synthetic-only tests for hashing,
strict preparation, immutable registration, corrupted/missing bytes, atomic DB
failure/retry, owner separation, shared readback, R2-like conditional writes and
CLI privacy/default behavior. All DBs are in-memory and all bucket adapters local.

A separate in-memory preflight of the existing private package verified all
8 catalog rows and all 8 exact PDFs (19,314,990 bytes), raw source pins and the
report HTML seal. It used placeholder owner/library bindings; it did not persist
an artifact, copy real metadata/PDFs into source/tests, issue network requests,
write D1/R2/Zotero, change a Site or select a real account. This is an offline
compatibility result, not an authorized live import or deployment certification.


## Concrete owner registration flow

`src/registration-routes.mjs` exports
`handleRegistrationRoute(request, env, auth, helpers)`. The existing Worker calls
it only after its platform-managed owner gateway authentication. It uses that
same identity, same-origin Origin and Sec-Fetch-Site checks, and the existing
`X-Zotero-Action: 1` guard. It does not invent a password, bearer token, setup
grant, persistent credential, alternate authentication header or public uploader.

The browser page is `/zotero/register`. The backend routes are:

- `GET /api/zotero/catalog-registration`: safe filenames, independently pinned
  source hashes, exact PDF hash/size manifest, staged flags and byte limits. It
  never returns owner/library bindings or the private operator configuration
- `POST /api/zotero/catalog-registration/pdfs/<sha256>`: explicit raw
  `application/pdf` upload. The digest must already be in the server-approved
  manifest. Header, byte count and SHA-256 must match before a conditional,
  owner-scoped private object put. Existing different bytes are never replaced
- `POST /api/zotero/catalog-registration/finalize`: exact JSON fields `action`
  (`register_catalog`), `report_b64`, `html_b64`, `catalog_b64`. It rejects extra
  and duplicate fields, verifies raw source pins and full report/catalog/PDF
  lineage, then registers all immutable rows. If specifically enabled by the
  server configuration, it conditionally sets `enabled=1` only for the identical
  owner, version, scope, source hashes, canonical content and registration hash

The PDF staging authority is its separately reviewed exact server manifest, not
browser content or a browser-provided digest alone. Staged PDFs remain private;
staging is not catalog registration, activation, connection or Zotero import.
The final action rechecks all approved sources and all exact staged bytes. A
failed finalization never promotes an incomplete catalog. Repeat and concurrent
identical staging/finalization are idempotent.

The sole server configuration is `ZOTERO_CATALOG_REGISTRATION_JSON`, serialized
UTF-8 size at most **5,000 bytes**:

```js
{
  schema: 'daily-agent-zotero-registration-config-v1',
  approved: true,
  binding,       // the exact five-field owner/report/catalog/version/scope binding
  trustedPins,   // the reviewed pins documented above, with that same binding
  pdfs: [{ sha256, size, paper_id }],
  filenames: {
    report: 'corrected-manifest.json',
    html: 'report-reviewed.html',
    catalog: 'verified-catalog.json'
  },
  enable_after_registration: true
}
```

This is ordinary private operator configuration, not a bearer secret. It must
match `ZOTERO_OWNER_ID` and the configured verified personal-library ID. Setting
it requires the appropriate deployment/operator authority; preparing the file
alone does not apply it. Missing, malformed, mismatched, overbudget or unapproved
configuration keeps mutations disabled. Each object write, D1 registration batch
and activation rechecks the approved configuration. A configuration change after
registration but before activation leaves the valid row disabled.

The Worker HTTP flow uses stricter memory budgets than offline preparation:

- One PDF: 16 MiB; all approved PDFs combined: 24 MiB
- Report and catalog: 1 MiB each; HTML: 4 MiB
- All three decoded source inputs combined: 4 MiB
- Final JSON envelope: 9 MiB; raw upload timeout: 10 seconds
- The approved configuration is budget-checked before any object/DB access

The known private eight-paper package fits those HTTP budgets. Tests cover the
actual outer Worker route through raw upload, exact finalization and enabled
catalog listing, in addition to direct-handler attacks, revocation races,
timeout/truncation and immutable replay.

## Private operator bundle CLI

`scripts/prepare-registration-bundle.mjs` validates the same pinned inputs,
checks the HTTP memory budgets and generates the exact 5,000-byte-bounded server
configuration from reviewed metadata. It does not recompute untrusted files and
automatically treat their new digests as approved pins.

```sh
node scripts/prepare-registration-bundle.mjs \
  --pins /private/reviewed-pins.json \
  --report /private/report/corrected-manifest.json \
  --html /private/report/report-reviewed.html \
  --catalog /private/verified-catalog.json \
  --pdf-dir /private/pdfs
```

The default only validates and prints safe counts. Add
`--output-dir /private/new-operator-bundle` to explicitly create a private upload
folder. Its parent must exist and the output folder must not already exist.
Add `--enable-after-registration` only when the intended operator configuration
should permit the later explicit finalization action to activate that catalog;
without this flag, registration remains disabled.

All bytes validate before the first output write. The new directory and PDF
subdirectory use mode 0700; files use 0600 and exclusive creation. It copies the
exact pinned report, HTML, catalog and PDFs, emits `registration-config.json`,
and writes a checksummed `operator-manifest.json` last. If disk writing fails,
there may be a private partial folder without a complete manifest; no existing
file is overwritten or automatically deleted. Public/source-tree output is
rejected, including resolved parent symlinks.

The folder is private material, not a publishable source artifact. An authorized
operator must separately review and apply the configuration. The authenticated
owner then selects the private folder on `/zotero/register` and explicitly clicks
Register. Neither the CLI nor successful catalog registration connects Zotero or
saves any paper to a Zotero library; that remains a fresh explicit Save action.
