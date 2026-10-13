# Daily Agent Zotero: local hosting build and release gates

This source contains a runnable, dependency-free local application plus an optional **real Vinext/Sites adapter**. No new Site, hosting resources, secret, Zotero credential, access grant, paid plan, or production deployment is created by these scripts. Use the existing Daily Agent Site identity and keep its audience unchanged.

## Approved provider-managed D1 credential mode

This optional mode is explicitly different from the original application-encrypted AEAD path. Apply appended migration `0003_zotero_provider_credentials.sql` after the two existing migrations. Set the non-secret private runtime choices `ZOTERO_CREDENTIAL_STORAGE=provider-managed-d1` and `ZOTERO_CREDENTIAL_ENTRY_ENABLED=approved` only after the specific persistent-access/storage approval. Existing trusted owner, HTTPS origin, private D1, and private R2 bindings remain required. The initial deployment can leave `ZOTERO_EXECUTION_ENABLED` unset: private credential entry and verification do not enable imports.

The user personally opens the owner-only password form and enters their numeric personal Zotero User ID and API key. There is no guessed account or automatic library lookup. The exact POST body is `action: connect_personal_library`, `library_id`, and `api_key`; the server calls only `GET https://api.zotero.org/keys/current` with the key in `Zotero-API-Key`, never the URL. It accepts personal-library read/write/files permissions only, rejects notes, all group grants and unknown scope fields, and checks the returned user ID before storing the credential or active library binding. See [Zotero's current-key documentation](https://www.zotero.org/support/dev/web_api/v3/syncing).

The dedicated `zotero_provider_credentials.api_key` column contains a **plaintext-usable key**, protected by Cloudflare's provider-managed D1 at-rest encryption. It is not application ciphertext, a vault, or end-to-end encryption. The application and database administrators with read access can read it. Database backups may retain copies. This accepted tradeoff avoids requiring an independent application encryption secret. Do not inspect this production table through assistant/operator diagnostics, exports, or tooling; entry and use are confined to the private user form and server runtime. Status, HTML, catalog and job responses use explicit non-secret allowlists. No key is returned, logged, put in browser storage, or included in an upstream error. Verify actual hosting logs/telemetry separately; local tests cannot prove platform redaction.

A stable random credential ID plus SHA-256 fingerprint binds each accepted key. Same-key re-verification/resubmission preserves its connection version; a different key requires explicit disconnect. Disconnect atomically removes active provider and AEAD credential records, revokes the active connection and advances a non-secret epoch that fences delayed connect/verify responses. The personal library tombstone remains, so switching accounts requires a separately reviewed rebind rather than silently moving old jobs. Reconnection after disconnect creates a new binding/version. Old uncertain jobs remain visible and blocked with their reserved keys and prewrite journal intact; the app never automatically replays them as fresh creates. Already in-flight remote requests may still finish.

The user should also revoke the key at [Zotero API key settings](https://www.zotero.org/settings/keys) to invalidate retained backup copies. Disconnect alone cannot erase backups or revoke Zotero's upstream key.

`ready` means the account/permissions are verified in this mode; `live_ready` additionally requires the separately approved execution gate. The manager labels this distinction and keeps Save disabled when execution is closed or no verified catalog is available. Connect, refresh, a Worker restart, and reading job history never import papers. An explicit selected Save and explicit PDF choice remain required. Durable jobs, deduplication, library leases, reserved object keys, backoff, uncertain-write recovery, and private artifact registration stay unchanged. Catalog registration still requires separately pinned artifacts and the confirmed personal-library configuration; entry alone does not activate a catalog.

The original AEAD mode is retained for compatibility when provider-managed mode is not selected. Its table, AES-256-GCM associated data and old decryption path remain intact; it still requires its independent encryption secret and one-time staging grant. No old credential is silently converted to plaintext.


## Supported packaging route

The basic Worker ESM starter only copies its single `worker/index.js` and hosting manifest. It does not provide standalone assets. Copying an import-bearing entrypoint alone loses the application modules; retaining `static` while adding D1/R2 is also invalid. An invented `ASSETS` field in `.openai/hosting.json` does not create an asset binding.

`hosting-adapter/template/` retains the official Vinext starter, pinned lockfile, `sites()` integration, and Vinext server handler. The adapter:

1. Compiles `zotero/worker/index.js` and every imported application module with the framework's Cloudflare Vite integration
2. Routes Zotero paths to the owner-authorized application first
3. Serves existing report paths through the real `ASSETS` binding; the original HTML/images remain separate static files
4. Preserves Vinext's default handler and connector-context integration for remaining framework routes
5. Emits `dist/server/index.js`, ESModule chunks, `dist/server/wrangler.json`, `dist/client/`, and `dist/.openai/` containing the manifest and generated Drizzle migration metadata

The input Vite configuration sets `assets.binding = "ASSETS"`, `run_worker_first = true`, and `not_found_handling = "none"`. The Cloudflare Vite plugin emits the client-directory path; the tested output is `assets.directory = "../client"`. This is the documented Worker-plus-assets route, not an R2-based archive replacement. R2 remains private application blob storage. [Cloudflare Vite assets](https://developers.cloudflare.com/workers/vite-plugin/reference/static-assets/), [Worker asset routing](https://developers.cloudflare.com/workers/static-assets/binding/)

## Two distinct build results

- `node scripts/build.mjs` stages the **entire local ESM graph** at `dist/server/` and SQL at `dist/migrations/`. It emits `dist/BUILD-STATUS.json` with `sites_deployable: false`. This output is for offline checks; it deliberately has no deployable hosting manifest
- The materialized private Vinext checkout builds the **actual Sites artifact** at its own `dist/`. Successful compilation and archive preservation do not prove production identity, account access, pricing, gateway behavior, or a live Zotero import

Never replace the existing deployed archive with the first output. Never copy its `dist/migrations/` and assume Sites will apply it; Sites consumes `dist/.openai/drizzle/` produced by the adapter.

## Prepare a separate private checkout

Set these local shell variables to verified paths:

- `ZOTERO_SOURCE`: this `sites/zotero-worker` directory
- `PRIVATE_CHECKOUT`: an empty private directory **outside the public source snapshot**
- `PRIVATE_ARCHIVE`: the retained original Daily Agent static `dist` directory
- `SITES_PLUGIN_ROOT`: the installed Sites plugin directory containing its documented `scripts/` helpers

No project ID is supplied to materialization. The existing Site owner adds the verified identity later in its owning workflow.

```sh
cd "$ZOTERO_SOURCE"
node scripts/build.mjs
node scripts/preflight.mjs --archive-dir "$PRIVATE_ARCHIVE"
node hosting-adapter/materialize.mjs \
  --destination "$PRIVATE_CHECKOUT" \
  --archive-dir "$PRIVATE_ARCHIVE" \
  --plugin-root "$SITES_PLUGIN_ROOT"

cd "$PRIVATE_CHECKOUT"
npm_config_cache="$PRIVATE_CHECKOUT/.sites-runtime/npm-cache" \
  node "$SITES_PLUGIN_ROOT/scripts/install-dependencies.mjs"
node scripts/prepare-migrations.mjs
node "$SITES_PLUGIN_ROOT/scripts/build-site.mjs"
node scripts/verify-archive.mjs public dist/client

cd "$ZOTERO_SOURCE"
node scripts/preflight.mjs \
  --archive-dir "$PRIVATE_ARCHIVE" \
  --adapter-dir "$PRIVATE_CHECKOUT"
```

The materializer calls the supported `project-setup.mjs --template-source` helper. It refuses a destination inside the public source snapshot, follows no archive symlinks, rejects reserved/private paths, and SHA-256 checks every file before/copy/after. It copies archive bytes only into the explicitly selected private checkout's `public/`; no private report or image is added to this template or its tests. `node_modules`, local state, source manifest identity, and secrets must also stay outside the public source snapshot.

For application-only changes, refresh the existing private checkout without replacing its identity, dependencies, archive, or configuration:

```sh
cd "$ZOTERO_SOURCE"
node hosting-adapter/materialize.mjs --destination "$PRIVATE_CHECKOUT" --refresh-runtime
cd "$PRIVATE_CHECKOUT"
node scripts/prepare-migrations.mjs
node "$SITES_PLUGIN_ROOT/scripts/build-site.mjs"
```

The build wrapper uses the standard framework runner and records all input/output hashes in ignored `.sites-runtime/zotero-build-receipt.json`. Preflight rejects stale staged modules, changed build inputs, changed output, incomplete generated migrations, and altered retained archive bytes. `--require-hostable` additionally exits 2 until the **existing verified Site identity** is in both source and rebuilt output manifests. It does not grant permission to deploy or enable execution.

### Migration contract

The source `migrations/0001_*.sql`, `0002_*.sql`, and later files remain authoritative and immutable once applied. The adapter ships generated SQLite Drizzle schema, journal, and snapshots. `prepare-migrations.mjs` applies the original schema-only SQL to **in-memory SQLite**, derives the corresponding schema, and uses the pinned official Drizzle CLI to generate journal/snapshot changes. Deployment SQL has the same statements and comments, with `--> statement-breakpoint` delimiters added so D1 receives one statement at a time. Source/output hashes and statement counts are recorded in `drizzle/zotero-source-migrations.json`.

The script refuses an altered existing migration, unsupported data mutations, external SQLite access, triggers, unsupported column types/indexes, and unexpected Drizzle output. It never executes against remote D1. Before first production application, review all SQL and preserve generated metadata in the **private Site source repository**. Append new migrations after any deployment; do not regenerate applied history. Deployment may have applied migrations even if Worker upload later fails.

### Local Worker smoke checks

The built adapter can run through the starter's local Wrangler command:

```sh
cd "$PRIVATE_CHECKOUT"
node scripts/smoke-zotero-site.mjs
# For an interactive local preview only:
XDG_CONFIG_HOME="$PRIVATE_CHECKOUT/.sites-runtime/xdg-config" npm start -- --port 8789
```

The smoke script starts and stops its own loopback server, checks every retained file through HTTP, and rejects missing configuration. The interactive preview serves only the printed loopback address, uses local bindings, and does not deploy or simulate real Sites identity. Verify archive response hashes and missing-configuration rejection; stop the owned server afterward. Local HTTP checks cannot establish production gateway sanitization or owner identity. Use supported browser QA separately when available.

## Exact manifest and bindings

The identity-free `hosting-manifest.template.json` contains only:

```json
{"d1":"DB","r2":"BUCKET"}
```

In the **existing private Site checkout**, preserve its verified `project_id` and all applicable existing fields. Remove `static` only after the mixed Worker/assets artifact is validated. Declare logical D1 binding `DB` and R2 binding `BUCKET`; Sites provisions/connects them in its supported hosting workflow. Existing differing binding names must be reconciled deliberately, never silently replaced. The local generated placeholder database and bucket identifiers are development metadata, not production resources. There is no separate native provisioning call used by this implementation.

`ASSETS` is emitted by Cloudflare's Vite integration and is not a `d1`, `r2`, or arbitrary top-level hosting manifest declaration. There is one static asset collection. Keep all existing archive paths in it.

## Runtime configuration names

Configure values only through the supported native Sites runtime-environment tool. Do not put values in source, `.openai/hosting.json`, CLI arguments, logs, or an environment file in this recovery snapshot.

- `SITES_GATEWAY_IDENTITY_TRUSTED`: exactly `platform-managed`, and only after verifying sole trusted Sites gateway ingress and sanitization of client identity headers
- `ZOTERO_OWNER_ID`: independently verified Site-scoped authenticated user ID for the intended owner
- `ZOTERO_OWNER_BOOTSTRAP_ENABLED`: temporary `approved` switch for the read-only `/zotero/identity` page; remove after owner binding
- `SITES_OWNER_PRIVATE_AUDIENCE_VERIFIED`: temporary `owner-private` assertion used by that page, set only after checking the actual existing Site audience
- `ZOTERO_CATALOG_REGISTRATION_JSON`: bounded server-side pins and file manifest for one explicitly approved owner/report/catalog registration; see the operator flow below
- `ZOTERO_SITE_ORIGIN`: exact canonical HTTPS origin, without trailing slash/path/query/credentials
- `ZOTERO_INTENT_MODE`: optional default `blocked_connection`; `preview_only` is the other supported intent-only mode
- `ZOTERO_VERIFIED_PERSONAL_LIBRARY_ID`: verified numeric personal-library ID, within the application's safe integer bound; no group library
- `ZOTERO_ENCRYPTION_KEY_ID`: non-secret AEAD key-version label
- `ZOTERO_CREDENTIAL_ENCRYPTION_KEY_B64`: sensitive runtime secret containing canonical base64 for exactly 32 random bytes
- `ZOTERO_SETUP_GRANT_JSON`: short-lived owner/library-bound single-use server approval record; configure only after the corresponding real approvals
- `ZOTERO_EXECUTION_ENABLED`: leave unset until all access, identity, encrypted credential, permission-validation, catalog, and execution gates are satisfied; the enabling value is exactly `approved`
- Bindings `DB`, `BUCKET`, and generated `ASSETS` are objects supplied by the host, not string environment variables

The API key is personally entered/submitted by the user through the approved secure Site flow; it is never passed through these scripts. The encryption secret and persistent access setup need the required user authorization. Enabling the flag alone does not establish a valid active connection. See `RUNTIME_CONFIGURATION.md` and the application checks for the complete grant and approval contract.

### Owner bootstrap and gateway requirements

Sites gateway authentication supplies `oai-authenticated-user-id`. It is stable within one Site and different across Sites. An account user ID, owner email, global account identifier, project ID, or arbitrary `get_site` owner field must not be substituted. Service-bypass authorization does not supply visitor identity. Do not make the first arbitrary request the owner; do not trust caller-supplied body/query/header identity.

The application provides the concrete read-only `GET /zotero/identity` route for bootstrap before D1 or `ZOTERO_OWNER_ID` is available. It displays only the current gateway-authenticated Site-scoped ID, sets private/no-store and restrictive content headers, writes no data, enrolls nobody, and grants no Zotero access. It is unavailable by default. This is an application display route using the documented gateway identity, not a separate platform identity API.

Use this exact operator sequence:

1. Call the supported `get_site` operation for the **existing** Site and verify the intended owner is its sole allowed viewer, with no additional users/groups/public or workspace-wide audience. Verify canonical HTTPS origin, sole Sites-gateway ingress and client identity-header sanitization. An uncertain or wider audience blocks bootstrap; do not set a flag merely to get past this check
2. After the required operator/user approval, configure `ZOTERO_SITE_ORIGIN`, `SITES_GATEWAY_IDENTITY_TRUSTED=platform-managed`, `SITES_OWNER_PRIVATE_AUDIENCE_VERIFIED=owner-private`, and temporary `ZOTERO_OWNER_BOOTSTRAP_ENABLED=approved` through the native runtime-environment operation. Deploy the saved version needed to apply that environment revision, preserving the existing audience
3. The owner opens the exact Site origin's `/zotero/identity` in their normally authenticated browser session. Use normal top-level Sites sign-in if needed. Do not use the service-bypass token, caller-supplied identity headers, another account's session, a query parameter, or a first-visitor enrollment flow. Missing visitor identity is rejected
4. Read the displayed identifier from that verified owner session and configure it as `ZOTERO_OWNER_ID` through the native runtime tool. This route does **not** perform that write itself. If an owner binding already exists, the route rejects a different authenticated visitor
5. Remove `ZOTERO_OWNER_BOOTSTRAP_ENABLED` and `SITES_OWNER_PRIVATE_AUDIENCE_VERIFIED` after binding, apply the new environment revision, and verify `/zotero/identity` returns its disabled response. Only then proceed to credential entry or owner data writes. Recheck the actual audience before any later re-enablement

No DB binding is required by the display route. Exact origin/no query, GET-only, same-origin or direct navigation, and valid gateway identity checks remain mandatory. Its temporary assertions are operator statements about the deployment; they are not cryptographic proof and cannot make a directly reachable Worker safe.

The gateway must be the only production ingress. A directly reachable Worker or untrusted proxy that accepts spoofed identity headers invalidates the trust assertion. Keep the verified owner-only Site audience and restrict all application records to that owner. Local mocked headers cannot prove this boundary.

## Owner-browser catalog registration: actual operator routes

The application now supplies authenticated staging/finalization routes when native D1/R2 write tools are unavailable. Registration is separate from a Zotero import: it pins approved Daily Agent source artifacts, privately stages verified PDF bytes, and creates the immutable owner-scoped catalog. It neither stores a Zotero API key nor calls Zotero.

### Prepare the bounded server manifest

Use the concrete offline operator tool to validate and package reviewed files before applying any runtime setting:

```sh
cd "$ZOTERO_SOURCE"
node scripts/prepare-registration-bundle.mjs \
  --pins "$PRIVATE_REVIEWED_PINS" \
  --report "$PRIVATE_REPORT_JSON" \
  --html "$PRIVATE_REVIEWED_HTML" \
  --catalog "$PRIVATE_CATALOG_JSON" \
  --pdf-dir "$PRIVATE_PDF_DIRECTORY" \
  --output-dir "$NEW_PRIVATE_REGISTRATION_BUNDLE"
```

The pins input is the independently reviewed `trustedPins` object described below. The PDF input directory contains files whose basenames are their lowercase SHA-256 digests, without an added extension. Omitting `--output-dir` performs validation only. The output directory must be new and outside the public source/`public`/`dist` trees; directories use mode 0700 and files 0600. The generator verifies all original bytes and budgets before emitting `corrected-manifest.json`, `report-reviewed.html`, `verified-catalog.json`, `pdfs/<sha256>`, `registration-config.json`, and a completion/hash `operator-manifest.json`. It does not fetch data, change Site settings, write D1/R2, or contact Zotero.

By default the emitted configuration leaves `enable_after_registration` false. Add `--enable-after-registration` only when enabling that specific catalog after registration is approved. The generated `approved: true` field records the proposed reviewed-artifact configuration; creating that local file does **not** grant operator or user approval. Review the complete private bundle, then apply its exact compact `registration-config.json` through the authorized native runtime setting. Choose that same bundle folder on `/zotero/register`; its configuration/operator files are extra local files and the UI does not upload them.

After confirming the owner, personal library, report edition and approved source files, configure one compact `ZOTERO_CATALOG_REGISTRATION_JSON` runtime value. The **serialized UTF-8 value must be at most 5,000 bytes**; never truncate hashes or omit PDF entries to fit. The schema is exactly:

```json
{
  "schema": "daily-agent-zotero-registration-config-v1",
  "approved": false,
  "binding": {
    "owner_id": "REPLACE_VERIFIED_SITE_OWNER",
    "report_id": "REPLACE_REPORT_ID",
    "catalog_id": "REPLACE_CATALOG_ID",
    "catalog_version": 1,
    "personal_scope": "personal:REPLACE_VERIFIED_LIBRARY_ID"
  },
  "trustedPins": {
    "owner_id": "REPLACE_VERIFIED_SITE_OWNER",
    "report_id": "REPLACE_REPORT_ID",
    "catalog_id": "REPLACE_CATALOG_ID",
    "catalog_version": 1,
    "personal_scope": "personal:REPLACE_VERIFIED_LIBRARY_ID",
    "report_sha256": "REPLACE_REPORT_BYTES_SHA256",
    "catalog_sha256": "REPLACE_CATALOG_BYTES_SHA256",
    "html_sha256": "REPLACE_HTML_BYTES_SHA256",
    "report_edition_id": "REPLACE_APPROVED_EDITION_ID",
    "allowed_source_hosts": ["REPLACE_APPROVED_PUBLIC_SOURCE_HOST"]
  },
  "pdfs": [{"sha256": "REPLACE_PDF_SHA256", "size": 0, "paper_id": "REPLACE_PAPER_ID"}],
  "filenames": {"report": "corrected-manifest.json", "html": "report-reviewed.html", "catalog": "verified-catalog.json"},
  "enable_after_registration": false
}
```

This intentionally nonfunctional example is **not approval**. Use actual reviewed values; set `approved: true` only for the authorized owner/report/catalog artifacts. Set `enable_after_registration: true` only if enabling that exact catalog was approved. Binding fields must exactly match `trustedPins` and the configured owner/personal-library scope. Compute hashes over the exact original file bytes, not reserialized JSON or normalized HTML. Each PDF entry must match a real approved catalog paper, original byte length and SHA-256; all prepared PDF entries must be represented exactly. Filenames are safe basenames for the owner to choose, not filesystem paths or fetch URLs.

The pinned setting is private operator configuration, not a bearer token, setup grant or standalone authorization mechanism. Configure it only through the native Site runtime-environment tool and apply its revision through the authorized deployment workflow. Do not paste the live manifest into the public source, an environment file or a deployment archive. The registration routes still require the gateway-authenticated configured owner, exact origin, D1 `DB`, private R2 `BUCKET`, and the verified personal-library ID.

### Browser/operator write sequence

1. Open `/zotero/register` on the existing private Site in the owner's authenticated browser, or follow the manager's `登记已核验的私有目录` link. Registration controls load `GET /api/zotero/catalog-registration`, which returns safe filenames/source pins and each approved PDF's staged state. With missing/invalid operator configuration it reports `setup_required`; it does not accept a replacement manifest from the browser
2. Use `选择包含来源文件与 PDF 的文件夹` to choose the reviewed folder. The browser matches and hashes the exact approved report JSON, reviewed HTML, verified catalog JSON and PDFs locally; choosing the folder alone uploads nothing. After reviewing the matched counts, explicitly click `登记私有目录`. The UI stages each missing PDF with `POST /api/zotero/catalog-registration/pdfs/<approved-sha256>`, sending the **raw original bytes** as `Content-Type: application/pdf`. No base64, ZIP, form-data envelope, URL fetch or arbitrary R2 key is accepted. The service checks exact pinned length/hash and `%PDF-` signature, writes only the deterministic private owner-bound object, and reads it back before reporting staged
3. After all PDFs are staged, finalize with `POST /api/zotero/catalog-registration/finalize`. The exact JSON shape is `{ "action": "register_catalog", "report_b64": "...", "html_b64": "...", "catalog_b64": "..." }`; each base64 string encodes the original corresponding file bytes. Both POST routes require the exact same-origin `Origin`, same-origin fetch context and `X-Zotero-Action: 1`. The authenticated UI supplies these; service-bypass access alone is insufficient
4. Finalization verifies every pinned source byte, catalog/report binding, approved source host, paper/PDF correspondence, private PDF readback and current configuration before immutable D1 registration. It conditionally enables only the exact registered row when the operator manifest authorized that step. Conflicting existing catalogs or blobs fail closed; retries reuse matching content
5. Verify the returned report/catalog/version and paper/PDF counts, then refresh registration status and the manager catalog listing. Remove or replace the temporary `ZOTERO_CATALOG_REGISTRATION_JSON` configuration when the approved registration is finished and apply the new revision. This closes the registration write window without deleting the immutable catalog

Current request budgets: each PDF at most 16 MiB, all pinned PDFs at most 24 MiB; report JSON and catalog JSON at most 1 MiB each, reviewed HTML at most 4 MiB, and those three source files combined at most 4 MiB. The final base64 JSON envelope is capped at 9 MiB. Body reads are time-bounded. These application limits are intentionally below R2's platform maximum. HTTP success is not proof of successful Zotero import; credential verification and live execution have their own gates.

## Remaining owner/platform actions

1. Confirm the existing Site identity, current owner-private audience, repository and canonical origin; open/synchronize its source through the supported Sites workflow. Do not create another Site
2. Verify actual Sites resource availability, quota/billing terms, and approval for any paid commitment before provisioning. This local build does not establish those terms
3. Review the mixed Worker/assets output and all SQL; preserve every archive file and path. Merge the adapter into that existing private repository, retain its identity and applicable declarations, remove the obsolete static-only declaration, and rebuild
4. Establish the sole-gateway and Site-scoped owner binding; verify R2 is private and D1 is owner-scoped. Configure only authorized runtime values and apply their returned revision by deploying a saved version
5. Use the supported source helper for the exact pushed commit and local artifact. Save/deploy the existing owner-private Site through the corresponding native operation; preserve any returned saved-version/deployment ID when continuing. The helper can build/package locally, but source synchronization and publication require the owning workflow and its credential
6. Check successful deployment status, real archive routes, missing-identity/non-owner rejection, D1 schema and actual runtime binding availability. Use the gated read-only identity bootstrap, then the pinned owner-browser catalog registration routes. Complete user-entered Zotero setup and approved live acceptance independently. Never describe offline tests as a successful account connection/import

No new project, credentials, Site runtime variables, remote D1/R2 contents, sharing settings, or deployment was changed by this hosting preparation.

## Limits and costs: verified 2026-10-10

The **Sites** contract specifies Cloudflare Workers with 128 MB per isolate and HTTP-based external connections, without raw TCP. The currently published upstream Cloudflare limits list a 64 MiB uncompressed Worker bundle, one-second startup, 25 MiB per static asset, and 20,000 free-plan / 100,000 paid-plan asset files. Environment variables are up to 5 KB each. These upstream limits do **not** establish the managed Sites account's plan, allowances, or pricing. The build conservatively checks the lower asset count. [Workers limits](https://developers.cloudflare.com/workers/platform/limits/)

CPU is a separate activation gate. Dependency-free MD5, SHA-256 checks, repeated PDF readback/hashing and catalog seals execute within requests. Local Node tests and a local workerd server do **not** establish the managed Site's CPU allowance or paid/free plan. A byte-size cap is not a CPU guarantee. Before enabling broader use, perform the explicitly approved, conservative live acceptance test with the current two-PDF sample, inspect real request outcomes/runtime limits, and keep larger registrations/execution disabled if the budget is uncertain or exhausted. Do not silently upgrade a plan, raise paid limits, or promise free operation.

D1 documents 100 bound parameters, 100 KB SQL statements, 2 MB values/rows, and 30-second queries; database capacity differs by plan (500 MB free / 10 GB paid). Keep PDFs in R2, not D1. R2 documents 1,024-byte object keys, 8,192-byte object metadata, and a one-write-per-second limit for a single object key. The application imposes much smaller bounded PDF sizes (16 MiB normal application limit; 32 MiB hard hashing-library cap) and per-library execution serialization. [D1 limits](https://developers.cloudflare.com/d1/platform/limits/), [R2 limits](https://developers.cloudflare.com/r2/platform/limits/)

Upstream Workers Paid has a minimum US$5/month subscription and metered dynamic execution. Standard R2 lists US$0.015/GB-month, US$4.50/million Class A operations and US$0.36/million Class B operations, with limited included usage. D1 also meters rows read/written and stored data. These are **upstream Cloudflare prices, not a quote for Sites**. The available Sites contracts did not establish this account's billing, inclusion, or whether provisioning incurs a charge. Do not assume hosting, DB, R2, or uploads are free; obtain the actual terms/spending approval before any paid commitment. No paid provisioning was performed. [Workers pricing](https://developers.cloudflare.com/workers/platform/pricing/), [R2 pricing](https://developers.cloudflare.com/r2/pricing/)

## Verified local result

The retained Vinext adapter was installed and rebuilt successfully on 2026-10-10 using the documented helpers in a separate private checkout, including the gated identity bootstrap and pinned registration routes/UI. The final local preflight found 48 original archive files (15,840,124 bytes total; largest 3,554,058 bytes), all unchanged in `public/` and `dist/client/`; the server output totals 1,033,904 bytes. Both schema migrations apply to in-memory SQLite, producing 10 application tables, and official Drizzle regeneration reports no schema drift. The materializer, append-only refresh and native-artifact proof guard suites pass; these use synthetic release evidence and do not represent a live deployment.

The actual local Wrangler/workerd server also served all 48 archive URLs with identical content hashes, including 20 normal canonical redirects. Missing Zotero runtime/owner configuration returns HTTP 503. The server used no real credentials and was stopped after the check. Production gateway behavior, authenticated owner bootstrap, provisioning, live D1/R2 and a real Zotero account remain unverified. The strict production-artifact gate intentionally exits 2 while the existing Site ID is absent; no placeholder ID was substituted.

## Scheduled publication after Worker cutover

Read `AUTOMATION_MIGRATION.md` before any production cutover. All four existing Daily Agent tasks and the private registry must adopt the Worker-aware publication guard before the next scheduled export; a successful one-time build/deploy alone is insufficient. The offline append/proof helpers preserve current private Site source and prevent static fallback.
