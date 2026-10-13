# Reviewed report selection (local candidate)

This is an independent server-reviewed mode. The existing full scientific bootstrap, current task prompts, issue seals,
checkpoints, schedules and destinations remain unchanged. The Zotero source package
is an overlay for the private Site runtime, never a replacement scientific generator.
The original `public/reports/.../index.html` and its HTML hash remain the scientific
publication proof. The authenticated enhancement is a separate response route.

## One-time reviewed runtime integration

Review the generic source diff, then reopen the current official private Site source.
Apply the runtime/template changes as an explicitly reviewed initial runtime migration.
Keep all archived bytes, previous catalogs, DB migrations, credential configuration,
owner identity and personal library binding. Do not use daily migration-verification
as a publication bypass. This candidate does not deploy, update a registry or modify
any task. The private `build_archive_navigation.py` is outside this source and must
be minimally integrated by its owner; do not reconstruct it from this overlay.

Runtime integration adds the four `src/reviewed-*.mjs` modules and the reviewed-mode
branches in application/catalog-registration/job-runner/zotero-transport. Materialize
also copies `hosting-adapter/reviewed-catalogs.mjs` to the private
`zotero/hosting-adapter/` directory. Vite loads all `zotero/reviewed-issues/*.json` and
compiles them into the server Worker only. The build and publication proof reject
client projection leaks, missing server pins and source maps. Existing `.dazb`
registration and archive-refresh guards retain their strict behavior.

## Existing daily publication contract: narrow post-seal hook

1. Finish scientific qualification/review and seal the issue using the unchanged scientific workflow. Reopen current Site source and preserve its exact current commit. Prepare the new canonical projection against the candidate sealed entry using independently reviewed full-issue report, HTML, catalog batches and trusted source hashes. Never derive the full qualified-paper list from catalog subsets. Normal future entries require their real `approval_sha256`; the typed legacy migration path is not available to ordinary publication.
2. For navigation, run the retained archive renderer only in a disposable copy. Pass the authenticated baseline, candidate render, approved new sealed entries and mappings derived from validated projections through `composeNavigationAppend`. It discards regenerated historical files, restores old entry/file metadata bytes, validates new titles/anchors, and preserves the JavaScript behavior. Copy no unchecked renderer output into the Site.
3. Run archive refresh dry-run/apply with `navigationProjections: [{path, projectionSha256}]`, or `--navigation-projections <JSON pin-list file>`. Those files are canonical prepared projections, not arbitrary navigation receipts. Refresh validates them against the candidate archive and retains its clean-source, staged-byte and interruption fences. Existing compiled projections are validated too. The projection paths are outside the public archive.
4. Dry-run/apply `append-reviewed-issue.mjs` with the same exact projection pin and original Site HEAD. It writes one canonical `zotero/reviewed-issues/<issue_id>.json`, preserves every prior catalog, verifies archive identity/seal/source hashes and fences interrupted writes. It accepts only the public archive changes already staged by refresh; no runtime/schema/migration changes are allowed.
5. Perform one stable build and ordinary publication proof. The proof independently derives routes from committed validated catalogs, rechecks the complete navigation composition and all previous immutable files, then verifies the native artifact. Do not use migration-verification for daily updates. Keep the frozen scientific source and all schedules/task prompts unchanged.
6. Deliver the enhanced `/zotero/reports/<issue_id>` reading URL through the existing reservation/dispatch protocol, while retaining the separately verified sealed-original URL and HTML hash. Failed Git backup is not a failed Site delivery. A navigation or projection validation error blocks that unverified change; never bypass a guard or regenerate sealed originals.

The only mutable global navigation files are `index.html`, `archive/index.html`, `assets/archive-browser.js` and `manifest.json`, and only after the strict transform proof succeeds. All historical report, reading and image bytes stay immutable. New catalogs remain server-only and must not enter static/client output. The initial reviewed runtime migration may map an existing edition through the exact reversible URL-only helper; ordinary new-issue publication uses the append composition instead.

Programmatic builder interface:

```js
const issue = await buildReviewedIssue({
  issue_id, entry, htmlBytes, issueReportBytes, issueReportSha256,
  paper_ids, anchors, batches
});
```

`entry` is the reviewed archive manifest entry (`page`, `key`, `date`, `identity`,
`source_sha256`, `approval_sha256`). `issueReportBytes` is the full sealed qualification
report in the existing `paper-first-corrected-edition-v1` source schema, with its
independently trusted SHA256. `paper_ids` must equal its entire paper-record set.
`anchors` contains `{paper_id,anchor}` for unique original `article` or `section`
IDs; the parser verifies the bound title and source URL inside that exact element.
An absent/ambiguous anchor or unsupported active HTML fails closed. A batch contains
`binding`, `trustedPins`, exact UTF8 `report` and `catalog` source strings, plus
`pdf_sources: [{paper_id,url}]` (or an empty array). Batch source pins and existing
registration schemas are unchanged. Multiple batches share one issue selection.
A missing supported full report/anchor mapping is an integration blocker, never a
reason to guess the old report's metadata or edit its scientific seal.

CLI equivalent (all paths and hashes come from the current reviewed run):

```sh
node hosting-adapter/prepare-reviewed-issue.mjs \
  --input /private/current/reviewed-input.json \
  --input-sha256 <approved-input-sha256> --output /private/current/projection.json
node hosting-adapter/append-reviewed-issue.mjs \
  --site-dir /private/current/site --registry /private/current/registry.json \
  --expected-commit <current-site-head> --projection /private/current/projection.json \
  --projection-sha256 <projection-sha256>
# Re-run exactly the reviewed append arguments with --apply.
```

The CLI input mirrors the builder arguments, replacing bytes/strings with relative
`html_file`, `issue_report_file`, and per-batch `report_file`/`catalog_file` paths.
Inputs cannot escape their directory; private projection output is forbidden inside
the public source checkout. Projection files have a 2 MiB per-issue / 8 MiB total
compiled budget. Crossing either bound stops publication and retains all old issues.

## Owner experience and execution guarantees

Link the accepted edition to `/zotero/reports/<issue_id>` from the private navigation
integration. The old sealed URL stays available. The new route verifies original
bytes and inserts unchecked paper controls, one report Save, and the separate PDF
intent. It removes original CSP meta elements only in this response, adds an
HTTP CSP allowing same-origin script/API, and reports original/projection hashes
in headers. The original file and every historical response route stay unchanged.
No selection persists across reloads; reading, selection and refresh cause no writes.

Save contains only projection identity, current trusted connection version, selected
paper IDs and PDF intent. It admits reviewed metadata for selected batches and creates
the existing idempotent jobs. Bounded explicit continuation uses the same dedup,
reserved keys, leases, backoff, uncertain-write handling and per-paper result ledger.
The issue pins owner and personal library, not a historical connection version.
Refresh therefore supports a legitimate same-owner/library reconnect; stale intents
and old in-flight jobs are fenced. Another owner/library is never accepted.

Metadata and classification complete before optional PDF handling. Missing PDF bytes
or file permission leave those successful stages intact and only the PDF pending.
The first choice is existing owner/hash-bound private R2 content. A reviewed missing
PDF may be fetched only during the user's PDF-enabled execution. The current network
policy intentionally supports exact HTTPS arXiv/export.arxiv PDF paths only, maximum
three redirects, no credentials/cookies, 15 seconds, two concurrent requests and
16 MiB per PDF. Every hop and exact hash/length/PDF signature is checked. Unsupported
publishers remain PDF-unavailable; there is no arbitrary URL proxy, login/paywall
bypass, Notes request, new secret, service or automation.

## Acceptance still required on the actual private Site

Synthetic tests exercise two dates/two batches, owner/CSRF gates, no-write reads,
partial success, retry/dedup, source/anchor tampering, strict fallback preservation,
current-connection fencing and append-only publication. Before production acceptance,
validate the actual issue's full source/anchor mapping, compile with the official Site
runner, independently verify private native artifact/audience/owner route headers,
and perform an owner-authorized real Save. Local tests do not claim these live checks.

## Explicit admission of an existing legacy seal

Normal new daily projections still require the real `entry.approval_sha256`.
An already-published legacy entry can be admitted only during the reviewed initial
runtime migration, never by ordinary `append-reviewed-issue.mjs`. The original
manifest entry must remain byte-for-byte the same object. There is no fallback
from missing approval and no fabricated approval value.

For that migration only, pass an additional builder argument:

```js
legacyMigration: {
  approved: true,
  previousManifest,                 // independently verified prior Site manifest
  compositionBytes,                 // exact original composition evidence bytes
  independentStaticReviewBytes      // exact original static review evidence bytes
}
```

The projected discriminator replaces, rather than invents, `approval_sha256`:

```json
{
  "legacy_seal": {
    "kind": "legacy-sealed-migration-v1",
    "entry_path": "<exact original entry path>",
    "html_sha256": "<original sealed HTML SHA256>",
    "composition_schema": "daily-agent-oct13-host-composition-v1",
    "composition_manifest_sha256": "<exact composition bytes SHA256>",
    "independent_static_review_sha256": "<exact review bytes SHA256>",
    "full_adapter_sha256": "<full issueReportBytes SHA256>"
  }
}
```

The builder rehashes both supplied evidence byte arrays; the hashes must match the
unchanged prior/current entry. It requires composition schema
`daily-agent-oct13-host-composition-v1`, review schema
`daily-agent-independent-static-composition-review-v1`, and the exact decision and
verdict `PASS_STATIC_ENVELOPE`. No verdict aliases are accepted. Composition/review
HTML hashes bind the original HTML hash; byte length binds actual source bytes;
review and composition paper-component hashes must match. Composition paper count,
component qualified count and review paper-array length must equal the complete
adapter's qualified count. Every supplied check must pass and required fixes must
be empty. Static-only / NOT_PERFORMED browser evidence never becomes browser QA.

CLI input may provide `legacy_migration` with `approved: true` and relative
`previous_manifest_file`, `composition_file`, `independent_static_review_file` paths.
The private integrator provides actual evidence; the generic code contains only
synthetic fixtures. The complete adapter is still provided via the existing
`issue_report_file` and independent `issue_report_sha256` pin. Initial migration
proof uses the actual previous Git manifest to reject new-issue substitution.
After migration, immutable admitted legacy catalogs remain readable; daily proof
permits their unchanged presence but refuses adding any new legacy catalog.

## Browser restoration, attachment capability and ASSETS canonicalization

A persisted `pageshow` restores liveness and performs only read-only issue/job
refresh. Every request and operation-finally handler is fenced by the page lifecycle
generation. Late pre-hide responses cannot replay mutations, overwrite a newer
operation or unlock its controls. Only another explicit Save/retry advances work.
Pending, partial and complete outcomes are labelled separately.

Read APIs expose `pdf_fetch_supported`, `pdf_cache_status` and truthful per-paper
`pdf_status`: `cache_or_reviewed_fetch_on_save`, `cache_only_on_save`, or `unavailable`.
Cache bytes are not claimed verified merely because a catalog has PDF pins. If Save
finds neither cached bytes nor a supported reviewed fetch source, that paper retains
its saved citation/classification with `REVIEWED_PDF_SOURCE_UNAVAILABLE`, no false
PDF success, and no endless retry. Unsupported publisher PDFs may still succeed
from already-verified private cached bytes; only pinned arXiv sources are fetched.

The original-asset reader manually follows at most one standard redirect from the
pinned `index.html` URL to its exact same-origin directory slash URL. Only the exact
root-relative path or absolute canonical URL is accepted, with no added query,
fragment, credentials, port or alternate path. Both requests omit credentials and
caller headers. A second redirect is rejected; final byte length and SHA256 are
still checked against the original immutable seal.
