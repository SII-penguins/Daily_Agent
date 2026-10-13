# Daily Agent: preserve the Worker during scheduled archive publication

## Release blocker and scope

A successful one-time Zotero deployment is insufficient: the four existing Daily Agent tasks still describe static archive publication. Reusing that path after cutover can replace the Worker/assets artifact with a static-only artifact. **The migration is not operationally complete until the private registry and all four actual task instructions have been updated and read back.** This document and its helpers make no task, registry, Site, credential, runtime-setting, or deployment changes.

The reviewed local task snapshots schedule generation at 21:00, recovery at 00:30/05:30, delivery at 09:00, and verification at 09:15, Asia/Shanghai. Preserve their existing IDs, destinations, calendar/date semantics, enablement, and schedules unless separately authorized. Local snapshots are not evidence of their current live state: the read-only task listing returned an unexpected service error during this preparation. The operator must obtain a fresh successful listing/read before editing.

The active workflow is host-mediated, paper-first publication. It explicitly excludes the legacy delivery CLI. `daily_agent.cloud_site_delivery` nevertheless retains a static-only source-proof contract; its `bind-site`/static proof is **not** a Worker release path. Preserve old immutable receipts for audit, but fence this legacy path from new Worker publications and recovery. No old receipt is rewritten to claim a Worker deployment.

## Private registry changes, performed only by the authorized owner workflow

Keep the existing registry schema, Site URL, owner-private audience, dispatch/checkpoint requirements, and all unrelated fields. Add this field under `global_delivery` using real values obtained from native Site results, never the placeholders below:

```json
{
  "site_runtime_profile": {
    "schema": "daily-agent-site-runtime-profile-v1",
    "mode": "worker-assets",
    "state": "migration_pending",
    "project_id": "EXISTING_SITE_ID",
    "site_source_commit_sha": "PREVIOUS_VERIFIED_SITE_SOURCE_COMMIT",
    "version_id": "PREVIOUS_VERIFIED_SAVED_VERSION",
    "deployment_id": "PREVIOUS_VERIFIED_SUCCESSFUL_DEPLOYMENT"
  }
}
```

`site_source_commit_sha` identifies the **private Site repository**, not the separate public Daily Agent recovery-source commit. Keep both identities distinct. While pending, the tuple is the last verified pre-migration baseline. After a successful cutover, set `state` to `active` and replace the tuple with the verified Worker source/version/deployment. Thereafter advance the tuple only after proving each exact successful Worker publication. Preserve prior tuples, evidence, and proofs in append-only private checkpoints. Unknown/missing values are a blocker, never invented IDs.

- No profile: only the historical exact static manifest is recognized by the general guard; the Worker append helper refuses this mode
- `migration_pending`: scheduled append/publication must stop. The currently successful live version remains in place
- `active`: require the same project, owner-private append-only contract, Worker manifest with `d1: "DB"`, `r2: "BUCKET"`, and no `static` key. Never silently fall back to static

Record the revised registry/operating-contract hashes and updated automation instruction hashes through the existing private durable-checkpoint workflow. Do not place private registry details, task IDs, report bytes, PDFs, or runtime values into the public source repository.

## Ordered cutover

1. Reopen the real current Site, its source head and audience. Reconcile against the private registry and last successful saved version/deployment; retain the original static archive bytes and receipts. No new Site identity
2. After authorization, install the pending profile and the Worker-aware fail-closed instructions in **all four** tasks, then read back each. Do this before publishing the Worker so no concurrent static export can overwrite it. A task already running with an old instruction snapshot must be reconciled/finished before cutover; a registry edit alone does not change an in-flight run
3. In the existing private Site repository, merge the supported adapter described in `HOSTING.md`. Preserve `.openai/hosting.json.project_id`; use `DB`/`BUCKET` and the real Vite `ASSETS` integration; retain every old archive byte under `public/`. Keep generated Drizzle journal/snapshots and original migration mapping. Never replace that repository with a recovered public-source ZIP or the old static `dist/`
4. Perform supported installation/build, complete source-input/output hashing, archive verification, and local smoke checks. Review provisioning/billing, runtime configuration and migration effects under their separate approval gates. A local build does not prove managed CPU limits, gateway identity, private R2, or a live account
5. Use the supported Site source workflow to save/push the **entire private framework source**, package its actual `dist`, and capture the returned exact commit. Save/deploy that exact artifact to the existing Site. Preserve actual raw native results and the saved TAR. The first migration proof uses `--migration-verification` only while the registry is pending; this flag is read-only evidence checking, not permission to deploy or activate
6. Verify successful native deployment/version/artifact identity, old/new HTTP archive bytes, owner-private audience, runtime binding/configuration retention, missing-identity/non-owner rejection and applicable live acceptance gates. Then authorize and persist the active profile tuple, read it back, and confirm all four task instructions still enforce it. Only then is normal scheduled publication enabled by the contract

If any step fails, leave the current successful live version and pending fence in place; report the exact blocked step. A failed upload may already have applied D1 migrations, so inspect actual state before retrying. Do not create another Site or regenerate applied migration history.

## Every future report: concrete private-source append and proof

Variables refer to fresh verified private paths/values. `ZOTERO_SOURCE` is this checked-out tools directory; `PRIVATE_CHECKOUT` is the opened existing Site Git checkout; `REGISTRY` is the verified private current registry; `CURRENT_SITE_HEAD` is freshly returned by the Site source-opening workflow. `CANDIDATE_ARCHIVE` is a separate private staging directory, not the live source tree.

1. Recover the current approved scientific/report state through the established source/state process. Verify the current final scientific seal, source/PDF hashes, reviewed HTML hash, edition identity, and publication eligibility. This helper validates bytes and structure; a self-authored manifest is not scientific approval
2. Seed `CANDIDATE_ARCHIVE` from the current private Site `public/`, then export **only the new approved edition** into it. Do not re-export historical states and change their old audit/created/format records. Existing report/example pages, assets, full manifest entries and index links remain intact. New reports use `reports/DATE-EDITION-HTMLSHA12/index.html`, with identity and source hash equal to the exact reviewed HTML SHA-256. Never overwrite a date-only path
3. Run the established safe-HTML/link archive verifier against the candidate first (`PYTHONPATH="$RECOVERED_SOURCE/src" python3 -m daily_agent.report_archive --output "$CANDIDATE_ARCHIVE" --verify`), as well as the scientific seal check. Then run the offline guard and default read-only append preflight, then explicitly apply the same reviewed local change:

```sh
node "$ZOTERO_SOURCE/hosting-adapter/publication-proof.mjs" guard \
  --registry "$REGISTRY" --hosting "$PRIVATE_CHECKOUT/.openai/hosting.json" \
  --project-id "$EXISTING_SITE_ID"

node "$ZOTERO_SOURCE/hosting-adapter/refresh-archive.mjs" \
  --site-dir "$PRIVATE_CHECKOUT" --archive-dir "$CANDIDATE_ARCHIVE" \
  --registry "$REGISTRY" --project-id "$EXISTING_SITE_ID" \
  --expected-commit "$CURRENT_SITE_HEAD"
# Apply only after that candidate and its scientific seal have been reviewed:
node "$ZOTERO_SOURCE/hosting-adapter/refresh-archive.mjs" \
  --site-dir "$PRIVATE_CHECKOUT" --archive-dir "$CANDIDATE_ARCHIVE" \
  --registry "$REGISTRY" --project-id "$EXISTING_SITE_ID" \
  --expected-commit "$CURRENT_SITE_HEAD" --apply

cd "$PRIVATE_CHECKOUT"
node "$SITES_PLUGIN_ROOT/scripts/build-site.mjs"
node scripts/verify-archive.mjs public dist/client
node scripts/smoke-zotero-site.mjs
```

The append helper requires an active Worker profile, a clean private Git checkout at the fresh exact head, disjoint input/output trees and no symlinks. It rejects reserved routes, encoded/dot paths, unexpected assets, altered old entries, lost index links, noncanonical new editions, files over 25 MiB and over 20,000 assets. It replaces only root `index.html` and `manifest.json`; existing edition/asset bytes cannot change. It hashes framework/runtime/schema/migration/binding files before and after. It performs no network or remote data writes.

Local refresh uses a transaction directory under ignored `.sites-runtime/`, preserves `previous-public/`, and records the parent commit and before/after archive hashes. An interrupted operation leaves `archive-refresh-pending.json`; builds, runtime-refresh materialization, and preflight refuse that state. Reconcile actual trees against the recorded hashes and preserved prior tree before clearing the marker. Do not clear it merely to get a successful build. The marker and local build receipt are evidence aids, not authoritative remote publication proof.

4. Reopen/recheck native Site source state for concurrency before publication. If its head moved, do not force-push or deploy stale output; reopen the new source, re-verify its complete archive and repeat the append/build. The current `site-workflow.mjs --project-id` helper accepts the actual source-opening result and preparation argument arrays via its documented stdin contract, saves the full source, builds/packages, and returns the exact pushed commit and TAR. Use that supported workflow and returned commit; do not manufacture a Git credential or put one in arguments/files/logs
5. Save and deploy the exact TAR and commit through native Sites operations. Verify their actual completion; capture full successful raw tool results privately. Do not construct a hand-written summary that merely says succeeded
6. Bind local inputs/output to the saved native artifact and successful deployment with the offline proof:

```sh
node "$ZOTERO_SOURCE/hosting-adapter/publication-proof.mjs" prove \
  --registry "$REGISTRY" --repo "$PRIVATE_CHECKOUT" \
  --dist "$PRIVATE_CHECKOUT/dist" \
  --build-receipt "$PRIVATE_CHECKOUT/.sites-runtime/zotero-build-receipt.json" \
  --deployment "$RAW_DEPLOYMENT_RESULT" --site-version "$RAW_VERSION_RESULT" \
  --artifact "$EXACT_SAVED_TAR" --expected-commit "$PUSHED_SITE_COMMIT" \
  --entry-path "$NEW_ENTRY_PATH" --html-sha256 "$REVIEWED_HTML_SHA256" \
  > "$NEW_PRIVATE_PROOF_FILE"
```

The official TAR has a `dist/` root. Proof requires its native `archive_storage` hash/size/file-count, safe TAR members exactly equal to actual built output, native saved-version/source/deployment match, same Site URL, actual committed framework inputs, ASSETS/DB/BUCKET output, retained migration history, and all prior archive bytes/entries. Normal archive-only publication rejects changes to non-public framework/runtime source; an intentional runtime release needs its own reviewed migration procedure, not this daily bypass. A proof sets `remote_http_verified`, `owner_audience_verified`, and `gateway_identity_verified` to false: the host must separately verify those real properties and store their native evidence.

7. Check new/old HTTP archive hashes and live Worker availability using the approved private access route. A service-bypass token can verify assets but does not establish gateway-authenticated owner identity. After all evidence passes, advance the private profile tuple and persist/remote-verify the checkpoint and existing **dispatch reservation before any chat send**. Then send the approved report link under the existing exact-message/readback rules. Unknown acceptance remains a reconciliation state; never blindly resend

## Exact changes to the four existing task instructions

Append the common Worker-profile rule and replace static publication language in each task. Do not change the task schedule or destination as part of this migration.

- **21:00 generation:** read the current private profile at start. Preserve scientific workflow/next-day selection and sealed state. Prepare new private archive/catalog candidates only; never independently recreate, publish or replace Site `dist`. A pending/unknown profile permits scientific preparation but blocks any publication
- **00:30 / 05:30 recovery:** retain all existing no-duplicate/reconciliation logic. Restore public recovery source and private report state independently from the actual private Site repository. Open the Site's current authoritative source head; preserve active Worker profile, runtime source, logical resources, runtime settings and applied migration history. Never restore a static Site from the recovery ZIP or replay an old static deployment
- **09:00 delivery:** require active profile; fresh Site/source/audience evidence; append-only private `public/` update; full supported Worker build; exact source/TAR/native deployment proof; HTTP archive verification; updated profile/checkpoint and durable dispatch reservation. Reject static artifacts or missing Worker bindings before any save/deploy. Only then use the existing approved chat delivery/readback flow
- **09:15 verification:** read back the actual Site/profile/version/deployment/commit and report route. Confirm Worker routes remain present, resource/runtime configuration still matches its approved revision, previous archives are retained, and dispatch acceptance matches the checkpoint. Missing evidence or static downgrade is a blocker requiring reconciliation, not a reason to publish an old static fallback or blindly resend

Common rule to insert verbatim: “Once global_delivery.site_runtime_profile exists, no static-only artifact or legacy static source proof may be published to this Site. migration_pending, unknown state, wrong project, missing DB/BUCKET or stale source/artifact evidence blocks publication. Use the current private Vinext Site source, Worker-first ASSETS, exact committed build and successful native saved-version/deployment proof. Preserve immutable archives, runtime configuration and applied migration history.”

## Future verified catalog registration

The scientific pipeline may prepare/review the exact private report/HTML/catalog/PDF bundle and proposed pinned registration configuration using `scripts/prepare-registration-bundle.mjs`; see `HASHING_CATALOG.md` and `HOSTING.md`. Persist it privately and mark **registration pending** until the approved write path actually succeeds. Do not put PDFs, private catalog manifests, runtime configuration or credentials in public assets/Git.

The implemented writer is the existing owner-authenticated `/zotero/register` page and its pinned same-origin PDF/finalize API. Current supported operation requires the configured authenticated owner browser and an explicit approved action against the exact server-pinned files. A native D1/R2 write tool, unattended authenticated-owner bridge, or service credential with visitor identity has not been established. Site cutover does not automatically register the next day's catalog.

Do not promise the user a daily folder-selection chore, or promise unattended completion. Prepare and queue the trusted bundle without asking for repeated work by default, expose the pending state honestly, and use the existing endpoint only when an authorized owner context and explicit action are available. Full unattended registration remains a separate supported operator-authentication and scope-approval gate. Invented headers/tokens, bypass-token impersonation, embedding PDFs in archives, or direct unverified database seeding are not substitutes.

## Failure, rollback and retention

After active Worker cutover, old static artifacts/receipts are historical evidence, **not rollback targets**. Static rollback can discard application routing/bindings; whether managed Sites retains/removes resources in that case is unverified. Do not attempt it without a separately reviewed migration and authorization.

On failure leave the currently successful version live. If authorized, turn off the execution flag using the supported runtime revision/deploy workflow, or deploy a previously verified compatible Worker build with the same DB/R2 and retained migration history. Never silently downgrade schema, delete blobs, recreate resources, reapply mutated migrations or upgrade a paid plan. Remote retry and restoration must preserve existing private receipts and ambiguous dispatch state.

## Offline checks and remaining operator actions

```sh
cd "$ZOTERO_SOURCE"
node --test hosting-adapter/materialize.test.mjs \
  hosting-adapter/refresh-archive.test.mjs hosting-adapter/publication-proof.test.mjs
```

The current release has a locally built mixed artifact, source/archive guards and synthetic regression tests. The operator still must obtain fresh live automation state, authorize/update/read back registry plus all four task instructions, integrate the existing private Site repository and real identity, approve applicable resource/configuration costs/access, publish/verify the actual Worker migration, and activate the profile. No helper here claims those actions occurred.
