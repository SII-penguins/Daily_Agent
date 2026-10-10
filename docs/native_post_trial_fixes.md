# Isolated post-trial handoff fixes, 9 October 2026

## Scope and immutable evidence

This patch is an offline candidate copied from the evidence-scoped proposal.
It does not modify production, the completed two-paper trial, its code/driver,
schedules, Sites, sealed jobs, or publication state. No new real model trial
has been run. The private validation package retained the observed nine-page rewrite and
single unsupported time-horizon token. Public repository fixtures use synthetic
text and identities with the same relevant regression structure; they are not
redistributed paper text or a new real-model trial. See the
[fixture provenance note](../tests/fixtures/README.md).

## CORE review: at most two complete-field packs

The writer and the sole permitted rewrite now receive exact chunk-to-page
mapping and the review contract. A deterministic exhaustive partition of at
most nine CORE fields finds a feasible plan if one exists: at most two packs,
at most eight original page images per pack. A field's complete evidence is
never split or truncated. A field requiring more than eight pages, or a draft
that cannot fit two such packs, fails with exact page/field diagnostics before
review admission. There is no increase to per-request page count, reading
coverage work, or configured stage budget.

A two-pack result has explicit manifest schema 2 and
`native_claim_review_packs_v2` identity; it is not an upgraded single-pack
approval. Each input binds the entire draft, all claim evidence, exact field
assignment and complete cited pages, source document, selected image identities,
and the original independent reviewer receipt. Asset caption/condition review
is assigned to the first pack. All packs and all assets must be supported;
missing, stale, repeated, swapped, modified, unsupported, expired, or mixed-draft
responses cannot become PASS. Waiting for pack two leaves the complete draft
unchanged. The second request consumes `semantic_overflow` ordinal 0 or 1
matching the existing initial/rewrite review, within the original writer pool.

The immutable real MCP rewrite retains all nine fields and its exact page
union `[1,2,4,5,6,7,8,9,14]` in the offline plan test. This establishes admission
feasibility, not that an actual independent model will approve its science.

## Scientific response: exact local repair and failure isolation

The mechanical gate now reports precise schema paths, claim IDs, and missing
numeric tokens instead of only “incomplete analysis.” The observed response
has exactly one failure: `vla_component_interaction`, missing numeric token
`3` from its quoted evidence for a `3s` condition. Numbers and source-location
requirements are unchanged. Prompts state existing schema lengths, ordered
paragraph IDs, continuous-quote requirements, numerical condition coverage,
and the single eight-page scientific review bound.

Only native missing-numeric-token failures may consume one local repair in
`scientific_writer` ordinal 1. Untargeted claims and every claim ID/kind/facet
must remain byte-equivalent JSON data. The repair receives complete relevant
source chunks from cited pages; it is not a new full reading or whole-article
rewrite. Both writer receipts are retained and the resulting full analysis
still requires an independent original-pixel review. A repair cannot enlarge
the original budget or create a second repair. Other schema/semantic failures
stay material-local and no longer disable a sibling paper's writer. Actual
transport/JSON failures retain the previous circuit behavior.

Stored scientific outcomes explicitly distinguish CORE-only qualification from
CORE plus independently accepted scientific analysis. Failed or incomplete
scientific text is not published; diagnostics and attempt counts remain visible
to the controller.

## Author evidence and native figure nomination

Author prompts now match continuous-excerpt and claim bounds. Empty validated
source sets do not enter independent author review and do not create a reviewed
empty-source cache. Proposed/reviewer uncertainty prose is audit-only; it cannot
publish affirmative unverified affiliations, roles, or lab facts. Independently
verified base metadata and honest unknown statuses remain available.

Native nomination scans full native captions and distinguishes qualitative
experimental outputs, including GT/predicted trajectories, from quantitative
performance plots. The actual Reasoning-VLA Figure 4 page 16 is nominated in a
bounded eight-page plan. A declared qualitative result cannot substitute for
quantitative plot coverage, and omitted/unrecognized/uninspected figures remain
explicitly scoped unknowns. At most five crops and the original 2 MB aggregate
PNG cap are unchanged. Figure 4 can itself use roughly 1.85 MB, so honest output
may contain fewer crops; the selector must account for important omissions.

## Verification boundary

Tests use synthetic original PDFs, real local immutable queue envelopes, and
frozen observed responses. They cover exact replay, all-packet acceptance,
missing/unsupported/tampered evidence, independent worker identities, budgets,
finite operation slots, targeted repair scope, sibling isolation, and old strict
behavior. Tests do not establish model quality, production reliability, or a
new wall-clock speedup. A fresh authorized real trial would be required for those
claims. Review/analysis cache identities change with these semantic contracts;
raw native reading identities and immutable historical artifacts are preserved.

## Enforced complete-selection boundary for new native workflows

The requested daily-report standard includes deep scientific analysis. New
native source bindings use schema 2 and freeze
`qualification_contract={schema_version:1,require_scientific_analysis:true}`.
Native execution ledgers freeze the same contract. Native configuration defaults
the requirement to true and rejects false, null, strings or numeric substitutes;
marker and ledger checks require exact JSON types. Missing/deleted/invalid
markers cannot downgrade a new native record to a legacy approval path.

`native_quality` remains the CORE prerequisite needed to generate science.
`native_review_supported` and final `_supported` now require actual complete,
source-bound, independently pixel-reviewed science. This reaches completion,
`audit_reading`, cloud qualification, and ready-report validation. Noncloud
publication also compares actual and expected policies and enforces the same
final gate. A prior PASS, a writer-only response, a forged scope label, missing
review, changed pixels, or a deleted marker cannot count as complete selection.
Audit rows derive scope from evidence rather than trusting the saved scope label.

A separate `_core_cache_supported` predicate is used only by the deferred draft
cache. It preserves qualified CORE as a reusable intermediate; it is not a
publication or completion credential. Cache load revalidates source, notes,
CORE pixel receipts and assets, so changing the cache role/scope does not create
approval. The new requirement is excluded only from the raw reading-settings
view, preserving exact source/note reading identities. Scientific work still
runs after a CORE cache hit and may resume its exact immutable queued request.

Strict-policy behavior remains on its existing branch. Existing sealed issues,
original trial code/state and historical artifacts are not migrated, rewritten,
or resealed. Old native source/protocol identities cannot be silently promoted
into the new schema-2 contract; they remain bound to their original evidence and
validator. The completed real trial remains 0/2 fully completed papers. This
package has no second real-model trial and no deployment.

## Truthful failure-status handoff

A failed or pending run now says “本期未完成可交付日报” rather than asserting
that no content met evidence requirements. The status-only handoff rechecks the
existing approval snapshot and reports observed fully qualified paper/repository
counts and unsealed counts. Missing/malformed snapshots or invalid ready files
produce an explicit unknown, not a fabricated zero. CORE-only native work does
not inflate the completed count. A present ready file is not called sealed until
its existing validator succeeds. The original failure is shown as the blocker.
This is read-only progress reporting: no partial seal, new admission, model call,
publication, or automatic deadline-delivery algorithm is introduced.
