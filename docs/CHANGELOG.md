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

Private deployment and trial details are retained in private operational records. Offline validation does not establish production delivery.

### Still pending at this checkpoint

Private deployment and trial details are retained in private operational records. Offline validation does not establish production delivery.

Private deployment and trial details are retained in private operational records. Offline validation does not establish production delivery.

## 2026-10-02 — Reading recovery

- Full-text retrieval followed explicit PDF metadata on landing pages and retried abstract/partial caches. Parser upgrades first reused hash-verified PDFs.
- Page transcription and independent review gained checkpoints. Bounded corrections could use original pages and detail crops; the recorded fidelity budget was 7,200 seconds with a 420-second call timeout. Budget changes preserved verified caches.
- Acceptance distinguished extracted-chunk completion from full-document reading. Complete source pages, required visual fidelity and supported claims were necessary for full acceptance.
- Fully reviewed page images could resolve native OCR failure while retaining native evidence. Access denial, exhausted budgets and unreadable symbols remained explicit gaps. Inclusion or successful delivery could not remove them.

## 2026-09-30 — Limited evidence-repair validation

Private live-trial results are retained outside this repository. Offline fixtures validate the repair protocol without claiming whole-paper acceptance.

Private deployment and trial details are retained in private operational records. Offline validation does not establish production delivery.

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
