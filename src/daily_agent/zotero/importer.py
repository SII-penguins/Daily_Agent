"""Selected-only offline import orchestration with durable write reconciliation.

Only a mock transport is accepted. This is a domain reference, not a Zotero
connection. The live API, auth, Site Worker, storage fetcher and D1 adapter are
explicit future gates, not working-looking stubs.
"""
from __future__ import annotations

from typing import Protocol

from .domain import ImportRejected, check_pdf, classification, match_paper, metadata, paper_identity


class TransportError(RuntimeError):
    def __init__(self, code, *, retryable=False):
        # Codes only: never leak response bodies, URLs, credentials or signed URLs.
        allowed = {"uncertain_write", "version_conflict", "permission_denied", "authentication_required",
                   "library_locked", "rate_limited", "storage_quota", "file_not_registered",
                   "temporary_unavailable", "invalid_response", "item_create_failed", "file_upload_failed"}
        self.code = code if isinstance(code, str) and code in allowed else "transport_error"
        self.retryable = retryable
        super().__init__(self.code)


class VersionConflict(TransportError):
    def __init__(self):
        super().__init__("version_conflict", retryable=True)


class BlobUnavailable(LookupError):
    pass


class OfflineTransport(Protocol):
    mode: str
    library_scope: str

    def inventory(self) -> tuple[list[dict], bool]: ...
    def get_item(self, key: str) -> dict | None: ...
    def create_item(self, key: str, fields: dict, identity: dict, *, version: int) -> dict: ...
    def ensure_collection(self, path: tuple[str, ...]) -> str: ...
    def patch_classification(self, key: str, collections: list[str], tags: list[dict], *, version: int) -> dict: ...
    def find_attachment(self, parent: str, sha: str, size: int) -> dict | None: ...
    def get_attachment(self, key: str) -> dict | None: ...
    def create_attachment(self, key: str, parent: str, filename: str, *, version: int) -> dict: ...
    def upload_and_register(self, key: str, content: bytes, md5: str, *, if_none_match: bool): ...
    def download_registered(self, key: str) -> bytes: ...


class VerifiedBlobProvider(Protocol):
    def read_blob(self, blob_id: str) -> bytes: ...


def _matches(paper, item):
    if not item or not match_paper(paper, [item]):
        raise ImportRejected("Reserved item key has a different identity")
    return item


def append_classification(transport, paper, item_key, collection_keys, tags):
    for _ in range(3):
        latest = transport.get_item(item_key)
        if not latest or latest.get("deleted"):
            raise ImportRejected("Item missing or moved to Trash")
        _matches(paper, latest)
        collections = list(latest.get("collections", []))
        collections.extend(c for c in collection_keys if c not in collections)
        merged = list(latest.get("tags", []))
        names = {x["tag"] for x in merged}
        merged.extend({"tag": t, "type": 1} for t in tags if t not in names)
        if collections == latest.get("collections", []) and merged == latest.get("tags", []):
            return latest
        try:
            return transport.patch_classification(item_key, collections, merged, version=latest["version"])
        except VersionConflict:
            continue
    raise VersionConflict()


def _attachment_matches(attachment, key, parent, filename):
    if (not attachment or attachment.get("deleted") or attachment.get("key") != key or attachment.get("parentItem") != parent
            or attachment.get("linkMode") != "imported_file" or attachment.get("contentType") != "application/pdf"
            or attachment.get("filename") != filename):
        raise ImportRejected("Attachment identity mismatch")
    return attachment


def run_import(plan, store, transport: OfflineTransport, blobs: VerifiedBlobProvider):
    if transport.mode != "offline_mock" or transport.library_scope != plan.library_scope:
        raise ImportRejected("Only the bound offline mock personal library is permitted")
    results = []
    with store.lock():
        store.ensure(plan)
        for paper in plan.papers:
            receipt = store.get(plan.job_id, paper.paper_id)
            if receipt["status"] in {"complete", "already_exists"}:
                results.append(receipt)
                continue
            def update(**changes):
                nonlocal receipt
                receipt = store.update(plan.job_id, paper.paper_id, **changes)
            try:
                inventory, complete = transport.inventory()
                if not complete:
                    raise ImportRejected("Incomplete library identity index; import blocked")
                # Recovery probes a preallocated key before considering another create.
                owned = transport.get_item(receipt["created_key"])
                existing = match_paper(paper, inventory)
                if receipt["item_key"]:
                    item = _matches(paper, transport.get_item(receipt["item_key"]))
                    if existing and existing["key"] != item["key"]:
                        raise ImportRejected("Library identity changed; manual review required")
                elif owned:
                    item = _matches(paper, owned)
                    if existing and existing["key"] != owned["key"]:
                        raise ImportRejected("Library identity changed; manual review required")
                    update(item_key=item["key"])
                elif existing:
                    item = existing
                    update(item_key=item["key"], existing_item=True)
                else:
                    # The key was durably persisted before this operation. A timeout
                    # is reconciled on a later explicit retry, never blindly replayed.
                    item = transport.create_item(receipt["created_key"], metadata(paper),
                                                 paper_identity(paper), version=0)
                    _matches(paper, item)
                    update(item_key=item["key"])
                update(status="metadata_saved", metadata_saved=True, error_code=None, retryable=False)
                paths, tags = classification(paper, plan.manifest.day)
                collection_keys = [transport.ensure_collection(path) for path in paths]
                append_classification(transport, paper, item["key"], collection_keys, tags)
                update(classification_saved=True)
                if not plan.include_pdf:
                    update(status="already_exists" if receipt["existing_item"] else "complete")
                    results.append(receipt)
                    continue
                update(status="pdf_pending")
                if not paper.pdf:
                    update(error_code="verified_pdf_unavailable", retryable=False)
                    results.append(receipt)
                    continue
                _matches(paper, transport.get_item(item["key"]))
                # Reconcile remote evidence first. A lost upload response should not
                # require a still-available local blob if registered bytes verify.
                attachment = transport.find_attachment(item["key"], paper.pdf.sha256, paper.pdf.size)
                attachment_key = attachment["key"] if attachment else receipt["attachment_key"]
                filename = paper.pdf.sha256[:16] + ".pdf"
                if attachment:
                    if (attachment.get("deleted") or attachment.get("parentItem") != item["key"]
                            or attachment.get("linkMode") != "imported_file"):
                        raise ImportRejected("Matched attachment is deleted or not a stored child")
                else:
                    attachment = transport.get_attachment(attachment_key)
                    if attachment:
                        _attachment_matches(attachment, attachment_key, item["key"], filename)
                if not attachment or not attachment.get("registered"):
                    try:
                        content = blobs.read_blob(paper.pdf.blob_id)
                    except BlobUnavailable:
                        update(error_code="verified_blob_unavailable", retryable=True)
                        results.append(receipt)
                        continue
                    md5 = check_pdf(paper.pdf, content)
                    _matches(paper, transport.get_item(item["key"]))
                    if not attachment:
                        attachment = transport.create_attachment(attachment_key, item["key"], filename, version=0)
                    _attachment_matches(attachment, attachment_key, item["key"], filename)
                    _matches(paper, transport.get_item(item["key"]))
                    transport.upload_and_register(attachment_key, content, md5, if_none_match=True)
                attachment = transport.get_attachment(attachment_key)
                if (not attachment or attachment.get("deleted") or not attachment.get("registered")
                        or attachment.get("linkMode") != "imported_file"
                        or attachment.get("contentType") != "application/pdf"
                        or attachment.get("parentItem") != item["key"]):
                    raise ImportRejected("Attachment registration verification failed")
                # Verify actual registered bytes, not a URL or metadata shell.
                md5 = check_pdf(paper.pdf, transport.download_registered(attachment_key))
                if attachment.get("md5") != md5:
                    raise ImportRejected("Registered attachment MD5 mismatch")
                _matches(paper, transport.get_item(item["key"]))
                update(status="already_exists" if receipt["existing_item"] else "complete",
                       attachment_key=attachment_key, pdf_verified=True, pdf_sha256=paper.pdf.sha256,
                       pdf_size=paper.pdf.size, error_code=None, retryable=False)
            except TransportError as exc:
                update(status="pdf_pending" if receipt["classification_saved"] and plan.include_pdf else "failure",
                       error_code=exc.code, retryable=exc.retryable)
            except ImportRejected:
                update(status="failure", error_code="validation_or_identity_conflict", retryable=False)
            results.append(receipt)
    return results
