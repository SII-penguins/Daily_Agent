# Private owner-upload bundles

## Storage boundary

Private report/catalog/PDF bytes never enter public or client/static output.
Native hosted dispatch can serve existing static files before Worker authorization;
`run_worker_first` and local Worker tests alone do not prove a hosted boundary.
The build retains validated private inputs outside static output, clears the old
reserved prefix, and rejects any remaining private static payload or redirect rule.
All original archive files must remain byte-identical. The old reserved paths are
still denied defensively, with bare/query GET/HEAD and asset-first regressions.

## Explicit owner workflow

The owner receives one credential-free `.dazb` file through a private delivery
channel, selects that one file, and explicitly clicks Prepare. File selection,
page loads and refreshes do not mutate. No folder selection or key re-entry is
required. The browser reads and hashes one bounded member at a time, then sends
same-origin requests:

1. Upload one pinned PDF per request into immutable private R2
2. Upload that batch's three pinned sources and register its catalog disabled
3. Verify every staged PDF, the full pinned paper-ID union and current personal
   library/connection version, then atomically enable the exact catalog set

Partial runs require refresh, reselection and another explicit click to resume.
Retries validate the submitted bytes even when an immutable object already exists.
Corrupt prior objects stop preparation rather than being overwritten. No browser
storage, automatic retry, arbitrary URL fetch or automatic Zotero import is used.
Every Zotero write still requires a separate explicit selected Save action.

## Fixed file format and server authority

The file is eight ASCII bytes `DAZB0001`, a four-byte big-endian unsigned header
length, a canonical UTF-8 JSON header (maximum 65,536 bytes), then raw concatenated
members. The header schema is `daily-agent-owner-bundle-v1`. Each member has a
fixed ID, batch ID, kind, payload-relative contiguous offset, size and SHA-256.
Order is manifest batch order; report, HTML, catalog, then that batch's PDFs.
There is no compression, archive extraction, external filename or URL resolution.

The owner-only API independently derives the exact header and total size from
compiled server pins. The browser compares the header bytes and total file size
before reading members; it never trusts a selected file's index as authority.
This rejects duplicate, missing, extra, reordered, overlapping and trailing data.
The server independently enforces every request's owner, same-origin/action
headers, media type, streamed byte bound, source schema, actual size and SHA.
PDFs require a PDF signature. Source envelopes reject duplicate/extra fields and
noncanonical base64 and enforce both encoded and decoded limits.

Limits remain 16 MiB per PDF, 24 MiB PDF bytes per batch, 1 MiB each for report and
catalog, 4 MiB HTML and 4 MiB combined source bytes per batch. Runtime registration
may use native SHA-256 without protocol MD5; the default preparer and actual Zotero
attachment transfers still compute their original required hashes.

## Private input, generation and deployment

Only a separate private deployment checkout may contain `zotero/bundles` with
manifest.json, each batch's three fixed source filenames and PDFs named by SHA.
Never put personal manifests, reports, owner/library IDs or PDFs in reusable/public
source. The manifest schema is `daily-agent-zotero-private-bundles-v1`; it pins the
owner, personal scope, connection version, exact paper union and disabled batch
registration configs. Unknown files, symlinks, changed bytes and invalid unions
are rejected. Vite compiles the validated manifest into server output only.

Run the generic local generator with explicit absolute paths:

    node scripts/create-owner-bundle.mjs --private-root PRIVATE_CHECKOUT --output PRIVATE_FILE.dazb

The output must be outside the input checkout, reusable source, public and dist
paths. The generator validates original bytes, writes mode 0600, and reports the
whole-file SHA, size and member count. It performs no network or service writes.
Deliver this artifact privately; never add it to the Site's static files.

The compiled `owner-upload-v1` mode is required whenever a private manifest exists;
missing mode remains contained. Set `ZOTERO_PRIVATE_BUNDLES_REQUIRED=approved` to
fail closed if the compiled manifest is absent. Keep execution disabled until
separate deployment approval. Even with execution approved, incomplete catalogs,
legacy rows and a wrong/revoked/version-changed connection cannot admit imports.
Existing journal, reserved keys, leases, backoff, uncertain-write behavior, AEAD
and provider modes are unchanged.

## Verification

Tests use synthetic files, dummy credentials and in-memory SQLite/R2 only. They
cover exact layout, malformed files, per-member browser reads, explicit clicks,
interruption/retry/concurrency, strict raw/envelope validation, authentication,
connection changes, exact union, immutable collisions and selected-Save admission.
As-built and forced asset-first local probes verify every old private path and
all archive bytes. Actual hosted denial and owner-gateway checks remain separate
required release evidence; local passing tests do not establish them.
