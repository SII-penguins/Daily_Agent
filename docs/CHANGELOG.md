# Change and validation history

[Project guide](../README.md) · [中文项目指南](../README.zh-CN.md) · [Documentation](README.md)

This file separates development and acceptance milestones from the project guide. Dates are checkpoints, not continuing guarantees. Undated follow-ups below retain the original record's uncertainty. Passing tests, source readiness, scientific acceptance, external delivery and scheduled production acceptance are separate claims.

## 2026-10-08

### Research and report standard

- Balanced AI-for-quantum and quantum-for-AI coverage; QAS/QNAS remains an organizational grouping with precise subtags rather than a claim that all included research terms are equivalent.
- New discovery uses three calendar months. Verified formal publication ranks above lower-weight eligible arXiv preprints. This supersedes the earlier cloud formal-papers-only gate; full reading, required visual fidelity and claim support remain mandatory.
- Daily capacity remains a target of eight papers and two repositories. Fully reviewed quota overflow and screening-only overflow are retained as different states. Evidence-bound cross-day caches preserve valid work while live delivery history/reservations still control selection.
- Added independently reviewed scientific explanation, original source-bound figures/tables/equations and primary-source author/group context. Presentation changes cannot rewrite scientific approval or manufacture missing evidence.
- Portable HTML and dated private-Site archives preserve original evidence, historical page bytes and separate calibration examples.

See [source policy](research-source-policy.md), [reuse](deferred-review-cache.md), [scientific analysis](scientific-analysis.md), [visual assets](ORIGINAL_SCIENTIFIC_ASSETS.md), [author context](author-research-context.md) and [HTML/archive](html-report-ui.md).

### Cloud migration and pilot validation

- Cloud operation uses a separate public-source root and explicit `parent_queue`. The tested local Codex path failed before inference during app-server initialization; native assistant workers supplied the explicit alternative. This was not evidence of autonomous cloud Codex execution.
- Public collection remained subject to network authorization and source access restrictions. Keyed Scholar, CORE, IEEE, Unpaywall and legacy Feishu integrations were not silently imported into the cloud profile.
- The original isolated VLANeXt text pilot passed its reading/evidence checks and exact remote message readback. Calibration items did not enter production history.
- An older native HTML attachment send remained accepted with incomplete attachment-identity readback; it was neither resent nor retroactively confirmed.
- The corrected r4 HTML pilot showed original Figures 7, 1 and 2, Tables 3 and 4, and verified author/group context. Missing numbered objective-equation evidence was stated honestly. Figure 2 distinguished LIBERO from LIBERO-plus and disclosed the comparison discrepancy with Table 1.
- Real cloud-browser checks covered desktop and a 393px mobile-equivalent layout, all five decoded images, captions, provenance, archive navigation and back/forward navigation.
- The private Site-link pilot was sent once and its exact linked caption read back, confirming that isolated transport. This did not establish the older native attachment's identity or prove a production HTTP byte round trip.
- The finalized code checkpoint reported **1,010 passing tests**. This is offline regression evidence, not proof of eight-paper daily capacity, future network access or scheduler availability.

### Still pending at this checkpoint

The first scheduled production cycle for **9 October 2026 (Asia/Shanghai)** had not run. At the initial isolated r4 Site-link confirmation, the archive contained four calibration examples and no formal production days; that count describes the initial transport checkpoint, not subsequent UI snapshots. Enabled schedules and a confirmed pilot did not establish unattended overnight generation or morning production delivery acceptance.

The migration and pilot did not introduce paid API calls, new persistent credentials, user-computer scheduler changes or Feishu changes. Source-code synchronization is a separate release action; a historical migration checkpoint must not be read as a statement about the current Git remote.

## 2026-10-02 — Reading recovery

- Full-text retrieval followed explicit PDF metadata on landing pages and retried abstract/partial caches. Parser upgrades first reused hash-verified PDFs.
- Page transcription and independent review gained checkpoints. Bounded corrections could use original pages and detail crops; the recorded fidelity budget was 7,200 seconds with a 420-second call timeout. Budget changes preserved verified caches.
- Acceptance distinguished extracted-chunk completion from full-document reading. Complete source pages, required visual fidelity and supported claims were necessary for full acceptance.
- Fully reviewed page images could resolve native OCR failure while retaining native evidence. Access denial, exhausted budgets and unreadable symbols remained explicit gaps. Inclusion or successful delivery could not remove them.

## 2026-09-30 — Limited evidence-repair validation

A real single-page image/transcription/independent-review cycle passed after one correction and five API calls. That result did **not** certify a whole paper.

The then-current eight-paper snapshot still contained four complete-body readings, three abstract-only documents and one structurally incomplete PDF. Full corpus quality acceptance had not passed; limited-evidence labels remained. Later pilot success does not retroactively certify that snapshot.

The repair protocol used at most one feedback-guided transcription correction and one bounded claim rewrite from already-read evidence. Repaired claims still had to pass the ordinary checks. Screenshots were presentation artifacts, not a substitute for visual verification. OpenReview 401/403 remained explicit source failure; another source could not establish access to its reviews.

## Earlier and undated recovery follow-ups

### Delivery and process recovery

- Scheduled and manual external delivery converged on immutable report snapshots, an outbox and confirmed receipts. Publication history advanced after confirmation, with bounded cross-trigger retries and circuits for permanent or ambiguous failures.
- Recovery used the remaining review window; broad candidate discovery was separated from small expensive-reading batches. The legacy full workflow required both paper and repository targets before early completion. The later cloud profile permits partial counts without relaxing evidence gates.
- `schedule status` stayed read-only; `schedule repair` reconciled under a lease; `schedule resolve-delivery` recorded an operator-confirmed outcome without sending. Local fault injection neither validated live delivery nor installed a monitoring daemon.
- Retry timing survived controller restarts. Local delivery preflight occurred before outbox creation, so missing tools/configuration did not masquerade as an ambiguous remote send. Failures after adapter invocation still needed remote reconciliation. Preview files remained excluded from publication history.
- Missing process leaders or unconfirmed termination preserved process evidence and blocked same-day stages as `cleanup_pending`. Version changes and budget resets could not bypass cleanup. Confirmed cleanup preserved attempt counts and checkpoints. Process identity remained a coarse snapshot; descendants escaping the recorded group were not guaranteed to be reclaimed.

See the current [workflow recovery guide](workflow-recovery.md) rather than treating these historical bullets as an operational runbook.

### Evidence-based reading and writing previews

Reading retained page/section locations, chunk notes, quote/number/condition checks and independent semantic review. Detailed local notes were not automatically uploaded. Papers without located problem/method evidence could not be published; incomplete reading stayed incomplete, and unchanged published papers were suppressed by default.

The layered writing preview separated problem/method, results/conditions, limits and editorial insight. Its default two featured papers required completed reading and sufficient evidence; shorter items retained their boundaries. Source approval fields, quotes and page records remained in separate notes.

An existing approval snapshot could produce an isolated composition preview without new retrieval, model calls or publication registration:

```bash
python scripts/preview_writing.py --input path/to/approval.json \
  --output tmp/writing-preview --date 2026-09-28
```

The output directory must not already exist. Markdown, HTML, notes, input snapshot and `composition-audit.json` record layout/composition, not fresh full-reading acceptance; original evidence gaps persist.

An evidence audit similarly inspects a snapshot without a model or publication:

```bash
python scripts/audit_evidence.py --input path/to/approval.json \
  --output tmp/evidence-audit.json
```

Live [reading](../scripts/VALIDATE_READING.md) and [writing](../scripts/VALIDATE_WRITING.md) validation use the configured model and may incur API charges. Isolated validation outputs are not automatically published or promoted into production history.
