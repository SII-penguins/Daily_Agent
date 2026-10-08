# Cloud delivery and artifact-integrity audit

Audit baseline: `4ac5b4fb54faa1b4ed4071100c1a7dbcabc018f6`.

## Preserve the existing reliability contract

The baseline already seals Markdown and approval identity, journals attempted delivery,
blocks blind retries after ambiguous outcomes, and reconciles publication separately.
Keep those properties in the cloud bridge. A messaging tool's accepted response is
not delivery confirmation. Bind each attempt to report/body hashes, date, channel,
conversation and outbox identity; retain its returned message identifier. Confirm by
matching a readback. Never resend an accepted or uncertain message automatically.

Local generation must not mark an item externally published. Public-source mode may
relax integration availability, but must not silently relax evidence requirements.
Readiness hashes establish integrity, not factual quality. Seal an explicit quality
assessment alongside report and approval data. Retain limited-evidence labels and
never describe structural/legacy approval as verified full-text reading.

## Artifact repairs implemented in this audit

- Cached PDF links are reused only when the local file exists, is bounded, starts
  with the PDF marker, and matches the reviewed source hash when available. A
  previously recorded cache hash is checked too.
- Prefer the exact retained source PDF reviewed by the pipeline, avoiding another
  download and version drift. Remote PDF candidates must match reviewed evidence.
- Cache writes are atomic and include SHA-256, byte count and a root-relative path;
  the legacy absolute-path field is retained for compatibility.
- Retention protects paths referenced by retained ready-report and cloud-outbox
  records, including nested approval assets. Corrupt delivery JSON stops cleanup
  before deletion. Unreferenced old reports and PDF-date directories remain eligible.

Remaining integration requirements: persist attachments before ChatGPT delivery;
verify recipient-accessible links; make the cloud seal portable; protect any new
outbox storage location through the same retention collector. Referenced manifests
must be explicitly retired before their assets become cleanup candidates.

## Concrete mature-project comparisons

- [tomorrow-one transactional-outbox](https://github.com/tomorrow-one/transactional-outbox)
  explicitly documents at-least-once delivery and consumer deduplication through
  sequence/source identifiers. Adopt durable identities; do not assume a messaging
  endpoint implements receiver deduplication or copy an automatic resend policy.
- [Temporal activity guidance](https://learn.temporal.io/tutorials/python/standalone-activities/)
  likewise requires receiver/activity idempotency for effectively-once effects.
  A durable scheduler alone does not make a remote message exactly-once.
- [Paperless-ngx sanity checker](https://github.com/paperless-ngx/paperless-ngx/blob/main/src/documents/sanity_checker.py)
  validates document/archive integrity. Apply the same principle to reviewed PDF
  bytes, retained report seals and their dependent assets.
- [GROBID coordinates](https://grobid.readthedocs.io/en/latest/Coordinates-in-PDF/)
  supply locations for text and document structures. This can improve difficult
  PDF provenance; it does not replace claim checking or semantic review.
- [Zotero duplicate detection](https://www.zotero.org/support/duplicate_detection)
  uses bibliographic identity. Preserve source provenance and paper versions rather
  than treating discovery-channel IDs as necessarily distinct works.

## Verification

`tests/test_cloud_artifact_integrity.py` covers retained reviewed bytes, remote
version mismatch, corrupt/stale caches, compatible legacy caches, reference-aware
retention and corrupt-manifest fail-closed behavior. Run it with recovery and
workflow-resilience suites. Network delivery and external acceptance are not tested
by these offline unit tests.
