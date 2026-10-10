# Opt-in paper-first workflow (v3)

This is a separate, deliberately small workflow for **new issues in empty roots**. Existing issues, publication seals, delivery receipts and the legacy scientific protocols remain unchanged. It is explicitly invoked; it does not silently replace a running supervisor or resume a previously published date. It produces qualified portable HTML but never sends or publishes it. The authorized host still owns discovery, formal-source verification, model dispatch, private storage and delivery.

## Critical path

For an ordinary paper there are **two model jobs**:

1. Read the full native PDF text once; write the Chinese explanation and scientific insight; choose at most three useful original figures/tables/formulas and bounded author context
2. Independently check the complete source against the displayed claims, every number and its conditions, claim versus inference, author context, and the actual selected page/crop pixels

A correct paper is no longer split into dozens of model-read chunks, two separate CORE review packs, a science writer, a science reviewer, and a separate presentation reviewer. There is no OCR/page-transcription work log. Native text and page anchors remain inspectable. A paper that cannot supply enough native text, exceeds capacity or fails review remains unqualified; it is never silently truncated.

One repair round can fix prose, a caption, a figure/subpanel label or crop coordinates. It reuses the same source and returns a complete corrected draft, followed by an independent review. Normal maximum: **four jobs per paper**. A mechanical first-draft failure can use fewer. REJECT and expired jobs are terminal and preserved. Each paper advances independently: one pending or rejected paper cannot prevent another paper's qualified HTML. A partial seal states exact qualified, pending and rejected counts and prevents further admissions.

Acceptance performs exact source-page anchor checks, validates the selected crops, and records immutable source, draft, crop and independent-answer identities. Rendering/sealing validates these hashes and qualification bindings without repeating native extraction, image rendering, semantic review or the legacy whole-report validator chain. This is not a claim that hashes establish scientific truth; the independent review is still essential.

## Run it explicitly

The package's existing PDF and test dependencies are sufficient. The native parent queue is the only model transport in this workflow; there is no paid API or automatic credential discovery.

```bash
python -m daily_agent.paper_first init --root /path/to/new-issue \
  --date 2026-10-11 --manifest /path/to/papers.json
python -m daily_agent.paper_first advance --root /path/to/new-issue
# The host dispatches pending jobs through the existing parent_writer protocol,
# imports actual responses under distinct reader/reviewer identities, then:
python -m daily_agent.paper_first advance --root /path/to/new-issue
python -m daily_agent.paper_first render --root /path/to/new-issue
python -m daily_agent.paper_first seal --root /path/to/new-issue
```

`render` is an updateable preview. `seal` creates immutable `report.html` with only qualified papers, even when others remain pending; it neither cancels already running host workers nor authorizes external delivery. The host must stop dispatching after a seal. `--diagnostic` permits an isolated past-date experiment and labels its HTML. A diagnostic must never be installed as a formal dated edition.

A paper manifest is a JSON list containing `key`, `title`, `version`, `source_date`, `url`, `pdf_url`, `pdf_path`, and preferably `pdf_sha256`. Source dates must fall within the preceding three calendar months through the issue date. Inputs and implementation bytes are frozen at initialization. A changed implementation must use a new issue; the old source is needed to resume an old issue.

Formal publication is not inferred from bibliographic metadata. Without a verified primary snapshot the report says formal publication is unconfirmed. An optional `publication` object requires `venue`, `url`, `evidence_path` and an exact `quote`; the host is responsible for obtaining the actual primary publisher/proceedings evidence. Author and affiliation statements are limited to source support; group history and correspondence stay unknown unless explicit.

The supplied manifest is a bounded discovery result (at most 12 papers), not a replacement discovery engine. The existing three-month collectors/material pool may supply it. Repository discovery/README processing and publication delivery stay in their existing explicit host paths; this paper workflow does not claim unattended 8+2 automation.

## Small private checkpoints

`compact_checkpoint.py` implements an immutable base plus one current delta:

```python
from daily_agent.compact_checkpoint import export_checkpoint, restore_checkpoint
export_checkpoint(issue_root, base_zip)  # after PDFs/native source are prepared
export_checkpoint(issue_root, delta_zip, base_bundle=base_zip)
restore_checkpoint(delta_zip, empty_recovery_root, base_bundle=base_zip)
```

The base holds source objects once. A delta contains current small mutable state, queue jobs/claims/answers, and objects added since that base (drafts, reviews and selected images). Newly added objects are repeated in a later delta by design; recovery needs just the base and latest delta, not a chain. No legacy runtime state is placed in this new root. Content-addressed objects, path traversal, symlinks, missing references, corrupt bases and nonempty recovery targets are checked.

The helper performs local export/restore only. The host must upload the actual base/delta to private durable storage and materialize their exact identities before claiming restart durability. Public GitHub holds source and separately approved public-safe HTML assets, never these private checkpoint ZIPs. An uncertain remote upload must be reconciled rather than retried blindly.

## Verification boundary

Synthetic transport tests establish finite calls, fair progress, repair behavior, source/answer/crop binding and immutable sealing. They are not scientific or throughput evidence. A semantic pilot must start with new semantic state and new actual worker responses, record all normal and repair calls, time through qualified HTML, preserve failures, and disclose reused source PDFs separately from genuinely cold source acquisition. Remote backup time must be reported separately as well as included in the overall measurement.

The [10 October two-paper semantic pilot](paper-first-v3-pilot.md) records actual timing, the preserved repair, remote restore proof and the frozen-versus-final source distinction.
