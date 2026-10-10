# Native full-text reading with claim-scoped original pixels

## Explicit future policy

`reading.source_evidence_policy: native_claim_evidence_v1` is an opt-in policy
for newly created PDF workflows. The absent/default policy remains
`strict_fidelity_v1`; non-PDF sources retain their existing text contract.
Unknown policy values fail closed. This proposal does not enable the option in
any configuration, edit an existing issue, reset a budget, replay a cancelled
trial, publish a report, or change a schedule.

The new policy separates three statements:

1. Every native source chunk was read, with exact page/offset/source/chunk
   identities and the existing note validator. This is extracted-text coverage,
   not a claim that every glyph or table cell was extracted correctly.
2. Selected scientific figures, plots, tables and equations are actual crops
   from the exact source PDF. Selection inspects at most eight nominated pages
   and creates at most five crops. Native captions/notes only nominate pages.
3. Every retained report claim is independently checked using its complete
   cited source chunks, the original cited pages (at most eight per request), and
   selected original crops (at most five). A complete draft may use at most two
   native CORE review packs; a field is never split across packs. Every pack
   binds the complete draft and claim plan and must pass before acceptance. The review separately checks displayed crop
   captions and experimental conditions. New scientific-analysis sentences
   receive their own independently bound pixel review in the existing
   `scientific_review` operation.

No full-document OCR, transcription, page-by-page visual reconstruction, or
whole-PDF fallback is admitted under the new policy. It never sets the old
`strict_fidelity` or whole-page inventory flags to true. Unselected decoration
and an unusable example identifier do not automatically block unrelated,
independently supported scientific claims. Unknown text is never guessed.

## Reading and source integrity

`source_evidence_policy.py` owns explicit source qualification. It verifies
native document/version identity, exact original PDF bytes and page count,
position-preserving chunks, all original note validators and complete chunk
coverage. A missing page, partial document or title/identity failure cannot
be called full reading. Local replacement/control glyphs are retained as
page-specific extraction gaps. They do not constitute a blanket paper failure,
and their absence does not prove correct table association or column order.
The independent claim reviewer sees original pixels for all retained claims.

The original `reading.py` and `reading_batches.py` are unchanged byte for byte.
`native_reading_config` removes only the new qualification selector from their
config view, keeping every actual reader setting, source/chunk identity and
original protocol hash unchanged. Existing exact raw notes may be reused;
this supplies no pixel review, publication right, quota entitlement or new
execution budget. Aggregated review caches bind the new policy modules,
selection and actual source/claim/reviewer evidence separately.

This version fails closed on unsupported local textual claims and preserves
their source gaps. It does not create a new regional transcription derivative
or silently modify source text, offsets or old note identities. A future local
repair derivative requires its own explicit provenance and affected reading.

## Finite execution and workflow order

The issue's existing source/config contract freezes the policy. The native PDF
slot set contains native chunk slots and existing primary writer/semantic/
scientific/author operations, plus one `native_visual_selection` slot charged
to the unchanged `primary_writer` pool. It does not allocate visual, fidelity,
or repaired work for a native-policy PDF. Other pools and configured limits
remain present and unchanged; unused fidelity time is not borrowed.

Order is native reading, bounded selection, CORE writing, independent CORE
pixel review, then additive scientific writing and independent scientific pixel
review. Existing bounded rewrite/semantic ordinals remain unchanged. A native
CORE review can additionally consume `semantic_overflow` at that same ordinal
(0 or 1), at most one extra pack per initial draft or rewrite. Native scientific
writing can consume ordinal 1 once for a local missing-number locator repair.
All these calls share the original `primary_writer` budget and deadline; no
whole-page or additional reading work is created. Selection
moves before claim review and completion. Rendering cannot request another
selection or alter the already independently reviewed caption/conditions.

Pending and expired queue responses retain their existing suspend/recovery
semantics. Source/queue corruption and cancellation propagate. Queue roles are
honest: independent reviewers use literal `review`, and import enforces a
worker distinct from readers, selectors and writers. Saved receipts retain
exact job, answer, worker, original deadline and file identities.

## One qualification contract at every boundary

Policy-aware mechanical verification and `native_review_supported` are shared
by aggregate cache, finite completion, cloud qualification/quota, ready-report
seal/revalidation, and rendering. `native_quality` validates CORE only as an intermediate prerequisite. New
native source-binding schema 2 freezes `require_scientific_analysis=true`;
complete daily selection always requires the exact independent scientific
review, even when the field is missing. CORE-only work can be reused through
the explicitly intermediate draft cache but cannot commit or enter a daily
selection. The same gate covers cloud and noncloud publication.
Current `final_fields` must equal the reviewed material fields; modified report
prose cannot inherit an old claim approval. A policy label or a bare PASS is
insufficient. Strict historical paths remain compatible.

Native cache reuse restores only missing derived PNGs from exact stored blobs,
then revalidates source/pixel/queue support. It never overwrites changed source
PDFs, changed existing evidence, active queue generations or admission history.
Evidence and source receipts remain protected dependencies. Reuse does not
restore publication/delivery eligibility; normal deduplication and reservation
checks still apply.

Reader-facing labels say native text coverage and selected/claim-specific
pixel checks. Local source gaps and the scope of unreviewed visual content are
visible. Selected images remain original PDF crops, never generated diagrams,
guessed formulas or fabricated experimental plots.

## Verification scope

All new tests are offline synthetic PDFs and deterministic JSON replies imported
through the real local parent queue. They establish operation accounting,
source/claim/pixel binding, role separation, finite retries and replay semantics.
They are not scientific model evaluations or measurements of real throughput.
The actual cancelled two-paper trial and its original answers remain untouched.

The full-pipeline fixture currently records four native chunks in one grouped
reading job, one selection job, one CORE writer, one CORE pixel reviewer, one
scientific writer and one scientific pixel reviewer: six jobs, nine finite
operations, zero whole-page fidelity/legacy-visual/repaired operations. Replaying
the same frozen input and ledger adds zero jobs and spends no additional stage
budget. The intentionally shallow synthetic draft still fails the independent
editorial depth gate, which is a required negative control: successful stages
are not by themselves a committed or publishable paper.

A separate complete synthetic source now passes the full automatic pipeline and
all real completion gates without mocking or changing a validator. It has seven
native chunks in two reading jobs, one selection, one CORE writer/reviewer pair
and one scientific writer/reviewer pair: seven jobs and twelve finite operations.
The actual editorial result is PASS with no issues; prepare, commit, completed
and reload succeed. Replay creates zero jobs and leaves operation/pool accounting
unchanged; changing the source PDF makes committed replay fail. This is exactly
one synthetic completion in an isolated test directory, with no publication or
formal archive entry. It is not acceptance of a real paper or a speed benchmark.

## Completed-trial follow-through (isolated, 9 October 2026)

A completed two-paper trial exposed a nine-page CORE rewrite whose evidence
was mechanically located but could not enter the original eight-page reviewer,
and one scientific sentence whose `3s` condition lacked the numeric token `3`
in its quotes. These are contract handoff failures, not evidence that a whole
source document needs to be read again. See `native_post_trial_fixes.md` for the
bounded repair, exact provenance, honest partial states, and validation limits.
