"""Synthetic offline fixtures only. No real papers, PDFs, library IDs or secrets."""
from copy import deepcopy
import json

import pytest

from daily_agent.zotero.domain import (
    ImportRejected, PdfRef, canonical_json, check_pdf, classification, load_verified_manifest,
    match_paper, normalize_arxiv, normalize_doi, plan_import, sha256,
)
from daily_agent.zotero.importer import run_import
from daily_agent.zotero.mock import MemoryBlobs, MockZotero
from daily_agent.zotero.store import BusyImport, SqliteReceiptStore


# In-memory signature/hash test bytes, not a distributable research PDF.
SYNTHETIC_BYTES = b"%PDF-1.4\nsynthetic-unit-test-only\n%%EOF\n"


def fixtures():
    html = b"<!doctype html><title>Synthetic report</title>"
    rows, records = [], []
    for n in range(2):
        record = {"key": f"synthetic:paper-{n}", "title": f"Synthetic research example {n}",
                  "source_url": f"https://example.org/synthetic/{n}",
                  "review_sha256": sha256(f"review-{n}".encode()),
                  "qualification_receipt_sha256": sha256(f"qualification-{n}".encode()),
                  "source_pdf_sha256": sha256(SYNTHETIC_BYTES)}
        records.append(record)
        rows.append({**record, "paper_id": record["key"], "doi": f"10.55555/synthetic-{n}",
                     "arxiv": "", "authors": ["Synthetic Example"], "year": "2026", "venue": "Synthetic Venue",
                     "item_type": "journalArticle", "publication": "published_verified",
                     "publication_evidence_sha256": sha256(b"synthetic-publication-evidence"),
                     "topics": ["ai-for-science"],
                     "pdf": {"blob_id": sha256(SYNTHETIC_BYTES), "sha256": sha256(SYNTHETIC_BYTES),
                             "size": len(SYNTHETIC_BYTES)}})
    report = {"schema": "paper-first-corrected-edition-v1", "date": "2026-10-10",
              "html_sha256": sha256(html), "html_bytes": len(html), "qualified_papers": 2,
              "paper_records": records}
    report_bytes = canonical_json(report).encode()
    catalog = {"schema": "daily-agent-zotero-catalog-v1", "report_sha256": sha256(report_bytes), "papers": rows}
    return report, html, catalog


def load(report, html, catalog):
    report_bytes = canonical_json(report).encode()
    catalog = {**catalog, "report_sha256": sha256(report_bytes)}
    catalog_bytes = canonical_json(catalog).encode()
    return load_verified_manifest(report_bytes, html, catalog_bytes, trusted_report_sha256=sha256(report_bytes),
                                  trusted_catalog_sha256=sha256(catalog_bytes))


def setup(tmp_path, *, include_pdf=True, selected=None, modify=None):
    report, html, catalog = fixtures()
    if modify:
        modify(report, catalog)
    manifest = load(report, html, catalog)
    request = {"action": "save_selected", "report_sha256": manifest.report_sha256,
               "selected_paper_ids": selected or ["synthetic:paper-0"], "include_pdf": include_pdf}
    plan = plan_import(manifest, request, personal_library_scope="personal:offline-fixture")
    store = SqliteReceiptStore(tmp_path / "receipts.sqlite")
    transport = MockZotero()
    blobs = MemoryBlobs({sha256(SYNTHETIC_BYTES): SYNTHETIC_BYTES})
    return plan, store, transport, blobs


def seed_item(transport, paper, key="ABCDEFGH"):
    from daily_agent.zotero.domain import metadata, paper_identity
    transport.create_item(key, metadata(paper), paper_identity(paper), version=0)
    return transport.items[key]


@pytest.mark.parametrize("value", ["10.55555/EXAMPLE", "doi: 10.55555/example", "https://doi.org/10.55555/example", "https://dx.doi.org/10.55555%2Fexample"])
def test_doi_normalization(value):
    assert normalize_doi(value) == "10.55555/example"


def test_doi_significant_punctuation_not_dropped():
    assert normalize_doi("10.55555/foo.") != normalize_doi("10.55555/foo")


@pytest.mark.parametrize("value", ["2601.12345v3", "arXiv:2601.12345v1", "https://arxiv.org/pdf/2601.12345v2.pdf", "https://arxiv.org/abs/2601.12345"])
def test_arxiv_root(value):
    assert normalize_arxiv(value) == "2601.12345"


def test_old_arxiv_id():
    assert normalize_arxiv("hep-th/9901001v2") == "hep-th/9901001"


@pytest.mark.parametrize("value", ["http://evil.test/10.5555/a", "javascript:alert(1)", "10.555/a", "10.5555/a b", "https://doi.org/10.5555/a?extra=1"])
def test_bad_doi(value):
    with pytest.raises(ImportRejected):
        normalize_doi(value)


def test_manifest_requires_pinned_digests():
    report, html, catalog = fixtures()
    rb, cb = canonical_json(report).encode(), canonical_json(catalog).encode()
    with pytest.raises(ImportRejected):
        load_verified_manifest(rb, html, cb, trusted_report_sha256="0" * 64, trusted_catalog_sha256=sha256(cb))
    with pytest.raises(ImportRejected):
        load_verified_manifest(rb, html + b"changed", cb, trusted_report_sha256=sha256(rb), trusted_catalog_sha256=sha256(cb))


@pytest.mark.parametrize("mutation", [
    lambda r, c: c["papers"][0].update(title="unreviewed title"),
    lambda r, c: c["papers"][0].update(review_sha256="0" * 64),
    lambda r, c: c["papers"][0].update(publication_evidence_sha256=""),
    lambda r, c: c["papers"][0].update(publication="unverified"),
    lambda r, c: c["papers"][0].update(topics=["client-invented-topic"]),
    lambda r, c: c["papers"][0]["pdf"].update(url="http://127.0.0.1/private"),
    lambda r, c: c["papers"][0]["pdf"].update(blob_id="../../private"),
    lambda r, c: c["papers"][0]["pdf"].update(size=100_000_000),
    lambda r, c: c["papers"].pop(),
    lambda r, c: r["paper_records"].append(r["paper_records"][0]),
])
def test_rejects_tampered_or_unqualified_catalog(mutation):
    report, html, catalog = fixtures()
    mutation(report, catalog)
    with pytest.raises(ImportRejected):
        load(report, html, catalog)


@pytest.mark.parametrize("change", [
    {"action": "selection_changed"}, {"selected_paper_ids": []},
    {"selected_paper_ids": ["synthetic:paper-0", "synthetic:paper-0"]},
    {"selected_paper_ids": ["unreviewed:paper"]}, {"selected_paper_ids": ["synthetic:repo"]},
    {"include_pdf": "true"}, {"report_sha256": "0" * 64}, {"url": "http://169.254.169.254/"},
    {"library_id": "other-library"}, {"topics": ["override"]},
])
def test_request_requires_explicit_selected_allowlist(change):
    manifest = load(*fixtures())
    request = {"action": "save_selected", "report_sha256": manifest.report_sha256,
               "selected_paper_ids": ["synthetic:paper-0"], "include_pdf": True, **change}
    with pytest.raises(ImportRejected):
        plan_import(manifest, request, personal_library_scope="personal:offline-fixture")


def test_group_scope_is_rejected():
    manifest = load(*fixtures())
    with pytest.raises(ImportRejected):
        plan_import(manifest, {"action": "save_selected", "report_sha256": manifest.report_sha256,
                              "selected_paper_ids": ["synthetic:paper-0"], "include_pdf": True},
                    personal_library_scope="group:123")


def test_selected_only_and_actual_byte_verification(tmp_path):
    plan, store, transport, blobs = setup(tmp_path)
    result = run_import(plan, store, transport, blobs)
    assert len(transport.items) == 1 and len(transport.attachments) == 1
    assert result[0]["status"] == "complete" and result[0]["pdf_verified"]
    assert result[0]["execution_mode"] == "offline_mock"
    assert next(iter(transport.items.values()))["title"] == plan.papers[0].title
    assert ("DailyAgent", "2026", "2026-10-10") in transport.collections
    assert ("DailyAgent", "Topics", "ai-for-science") in transport.collections
    assert "metadata_saved" in store.events(plan.job_id, plan.papers[0].paper_id)
    assert "pdf_pending" in store.events(plan.job_id, plan.papers[0].paper_id)


def test_same_selection_reversed_is_same_job(tmp_path):
    plan, *_ = setup(tmp_path, selected=["synthetic:paper-1", "synthetic:paper-0"])
    request = {"action": "save_selected", "report_sha256": plan.manifest.report_sha256,
               "selected_paper_ids": ["synthetic:paper-0", "synthetic:paper-1"], "include_pdf": True}
    assert plan_import(plan.manifest, request, personal_library_scope=plan.library_scope).job_id == plan.job_id


def test_double_click_and_reopened_ledger_do_not_write_twice(tmp_path):
    plan, store, transport, blobs = setup(tmp_path)
    first = run_import(plan, store, transport, blobs)
    calls = transport.calls[:]
    reopened = SqliteReceiptStore(tmp_path / "receipts.sqlite")
    assert run_import(plan, reopened, transport, blobs) == first
    assert transport.calls == calls


def test_metadata_only_never_requests_blob(tmp_path):
    plan, store, transport, blobs = setup(tmp_path, include_pdf=False)
    receipt, = run_import(plan, store, transport, blobs)
    assert receipt["status"] == "complete" and not receipt["pdf_verified"]
    assert blobs.reads == [] and transport.attachments == {}


def test_existing_item_preserves_metadata_notes_tags_and_collections(tmp_path):
    plan, store, transport, blobs = setup(tmp_path)
    item = seed_item(transport, plan.papers[0])
    item.update(title="User edited title", collections=["USERCOLL"], tags=[{"tag": "favorite", "type": 0}],
                notes=[{"note": "User annotation"}], abstractNote="User text")
    receipt, = run_import(plan, store, transport, blobs)
    result = transport.items["ABCDEFGH"]
    assert receipt["status"] == "already_exists" and receipt["pdf_verified"]
    assert result["title"] == "User edited title" and result["notes"] == [{"note": "User annotation"}]
    assert result["abstractNote"] == "User text" and result["collections"][0] == "USERCOLL"
    assert result["tags"][0] == {"tag": "favorite", "type": 0}
    assert transport.calls.count("create_item") == 1


def test_concurrent_edit_remerges_versioned_arrays(tmp_path):
    plan, store, transport, blobs = setup(tmp_path, include_pdf=False)
    seed_item(transport, plan.papers[0])
    def edit(item):
        item["collections"].append("LATEUSER")
        item["tags"].append({"tag": "Late edit", "type": 0})
        item["notes"].append({"note": "Unrelated edit"})
    transport.after_patch_conflict = edit
    receipt, = run_import(plan, store, transport, blobs)
    assert receipt["status"] == "already_exists"
    assert "LATEUSER" in transport.items["ABCDEFGH"]["collections"]
    assert {"tag": "Late edit", "type": 0} in transport.items["ABCDEFGH"]["tags"]
    assert transport.items["ABCDEFGH"]["notes"] == [{"note": "Unrelated edit"}]
    assert transport.calls.count("patch_classification") == 2


@pytest.mark.parametrize("operation", ["create_item", "ensure_collection", "patch_classification", "create_attachment", "upload_and_register"])
def test_uncertain_write_reconciles_after_restart(tmp_path, operation):
    plan, store, transport, blobs = setup(tmp_path)
    transport.fail_next(operation, after=True)
    first, = run_import(plan, store, transport, blobs)
    assert first["status"] in {"failure", "pdf_pending"} and first["retryable"]
    reopened = SqliteReceiptStore(tmp_path / "receipts.sqlite")
    second, = run_import(plan, reopened, transport, blobs)
    assert second["status"] == "complete" and second["pdf_verified"]
    assert len(transport.items) == len(transport.attachments) == 1
    assert transport.calls.count("create_item") == 1
    assert transport.calls.count("create_attachment") == 1
    assert transport.calls.count("upload_and_register") == 1


def test_missing_pdf_keeps_metadata_and_waits(tmp_path):
    plan, store, transport, blobs = setup(tmp_path)
    blobs.blobs.clear()
    receipt, = run_import(plan, store, transport, blobs)
    assert receipt["status"] == "pdf_pending" and receipt["metadata_saved"] and not receipt["pdf_verified"]
    blobs.blobs[sha256(SYNTHETIC_BYTES)] = SYNTHETIC_BYTES
    resumed, = run_import(plan, store, transport, blobs)
    assert resumed["status"] == "complete" and len(transport.items) == 1


def test_no_verified_pdf_does_not_claim_complete(tmp_path):
    plan, store, transport, blobs = setup(tmp_path, modify=lambda r, c: c["papers"][0].update(pdf=None))
    receipt, = run_import(plan, store, transport, blobs)
    assert receipt["status"] == "pdf_pending" and receipt["error_code"] == "verified_pdf_unavailable"
    assert not receipt["retryable"] and not receipt["pdf_verified"]


@pytest.mark.parametrize("bad", [b"%PDF-1.4\n", b"X" * len(SYNTHETIC_BYTES), SYNTHETIC_BYTES.replace(b"synthetic", b"different")])
def test_bad_blob_never_uploads_and_preserves_metadata(tmp_path, bad):
    plan, store, transport, blobs = setup(tmp_path)
    blobs.blobs[sha256(SYNTHETIC_BYTES)] = bad
    receipt, = run_import(plan, store, transport, blobs)
    assert receipt["status"] == "failure" and receipt["metadata_saved"]
    assert "upload_and_register" not in transport.calls


def test_registered_file_bytes_must_match(tmp_path):
    plan, store, transport, blobs = setup(tmp_path)
    original = transport.download_registered
    transport.download_registered = lambda key: original(key) + b"tampered"
    receipt, = run_import(plan, store, transport, blobs)
    assert receipt["status"] == "failure" and not receipt["pdf_verified"] and receipt["metadata_saved"]


def test_quota_retains_pdf_pending(tmp_path):
    plan, store, transport, blobs = setup(tmp_path)
    transport.fail_next("upload_and_register", code="storage_quota", retryable=False)
    receipt, = run_import(plan, store, transport, blobs)
    assert receipt["status"] == "pdf_pending" and receipt["error_code"] == "storage_quota"
    assert receipt["metadata_saved"] and not receipt["retryable"]


def test_existing_registered_pdf_not_duplicated(tmp_path):
    plan, store, transport, blobs = setup(tmp_path)
    item = seed_item(transport, plan.papers[0])
    transport.create_attachment("ATTACHAB", item["key"], "original-name.pdf", version=0)
    import hashlib
    transport.upload_and_register("ATTACHAB", SYNTHETIC_BYTES, hashlib.md5(SYNTHETIC_BYTES).hexdigest(), if_none_match=True)
    receipt, = run_import(plan, store, transport, blobs)
    assert receipt["status"] == "already_exists" and receipt["attachment_key"] == "ATTACHAB"
    assert len(transport.attachments) == 1


@pytest.mark.parametrize("ambiguous", ["trash", "duplicate", "conflicting_identifier", "incomplete_index"])
def test_ambiguous_identity_fails_before_any_write(tmp_path, ambiguous):
    plan, store, transport, blobs = setup(tmp_path)
    item = seed_item(transport, plan.papers[0])
    if ambiguous == "trash":
        item["deleted"] = True
    elif ambiguous == "duplicate":
        transport.items["IJKLMNPQ"] = {**deepcopy(item), "key": "IJKLMNPQ"}
    elif ambiguous == "conflicting_identifier":
        # To exercise strong-ID conflict, both rows share arXiv but disagree on DOI.
        object.__setattr__(plan.papers[0], "arxiv", "2601.12345")
        item["identity"].update(arxiv="2601.12345", doi="10.55555/other")
    else:
        transport.complete_inventory = False
    before = deepcopy(transport.items)
    receipt, = run_import(plan, store, transport, blobs)
    assert receipt["status"] == "failure" and transport.items == before
    assert transport.collections == {} and transport.attachments == {}


def test_title_fallback_requires_exact_full_author_year_venue(tmp_path):
    def no_ids(r, c):
        c["papers"][0].update(doi="", arxiv="")
    plan, store, transport, blobs = setup(tmp_path, include_pdf=False, modify=no_ids)
    item = seed_item(transport, plan.papers[0])
    item["title"] = "  SYNTHETIC research  example 0 "
    assert match_paper(plan.papers[0], [item])
    item["identity"]["authors"] = ["S. Example"]
    assert match_paper(plan.papers[0], [item]) is None
    item["identity"]["authors"] = ["Synthetic Example"]
    item["identity"]["year"] = "2025"
    assert match_paper(plan.papers[0], [item]) is None
    item["identity"].update(year="2026", doi="10.55555/different")
    assert match_paper(plan.papers[0], [item]) is None


def test_title_alone_is_not_sufficient():
    report, html, catalog = fixtures()
    catalog["papers"][0].update(doi="", arxiv="", authors=[])
    with pytest.raises(ImportRejected):
        load(report, html, catalog)


def test_second_report_updates_membership_without_duplicate_item(tmp_path):
    plan, store, transport, blobs = setup(tmp_path, include_pdf=False)
    run_import(plan, store, transport, blobs)
    def next_day(r, c):
        r["date"] = "2026-10-11"
    newer, _, _, _ = setup(tmp_path, include_pdf=False, modify=next_day)
    receipt, = run_import(newer, store, transport, blobs)
    assert receipt["status"] == "already_exists" and len(transport.items) == 1
    assert ("DailyAgent", "2026", "2026-10-10") in transport.collections
    assert ("DailyAgent", "2026", "2026-10-11") in transport.collections


def test_live_transport_and_wrong_library_refused(tmp_path):
    plan, store, transport, blobs = setup(tmp_path)
    transport.mode = "live"
    with pytest.raises(ImportRejected):
        run_import(plan, store, transport, blobs)
    assert transport.calls == []
    transport.mode = "offline_mock"
    transport.library_scope = "personal:another-user"
    with pytest.raises(ImportRejected):
        run_import(plan, store, transport, blobs)


def test_store_serializes_workers(tmp_path):
    _, store, _, _ = setup(tmp_path)
    other = SqliteReceiptStore(tmp_path / "receipts.sqlite")
    with store.lock(), pytest.raises(BusyImport):
        with other.lock():
            pass


def test_publication_classification_is_evidence_based(tmp_path):
    def preprint(r, c):
        c["papers"][0].update(item_type="preprint", publication="preprint", publication_evidence_sha256="")
    plan, *_ = setup(tmp_path, modify=preprint)
    _, tags = classification(plan.papers[0], plan.manifest.day)
    assert "publication:preprint" in tags and "publication:published_verified" not in tags


def test_identity_change_on_version_conflict_blocks_classification_and_pdf(tmp_path):
    plan, store, transport, blobs = setup(tmp_path)
    seed_item(transport, plan.papers[0])
    transport.after_patch_conflict = lambda item: item["identity"].update(doi="10.55555/different-paper")
    receipt, = run_import(plan, store, transport, blobs)
    assert receipt["status"] == "failure" and not receipt["classification_saved"]
    assert transport.items["ABCDEFGH"]["tags"] == [] and transport.attachments == {}
    assert "upload_and_register" not in transport.calls


def test_registered_remote_pdf_recovery_does_not_need_source_blob(tmp_path):
    plan, store, transport, blobs = setup(tmp_path)
    transport.fail_next("upload_and_register", after=True)
    receipt, = run_import(plan, store, transport, blobs)
    assert receipt["status"] == "pdf_pending"
    blobs.blobs.clear()
    receipt, = run_import(plan, store, transport, blobs)
    assert receipt["status"] == "complete" and receipt["pdf_verified"]
    assert transport.calls.count("upload_and_register") == 1


@pytest.mark.parametrize("registered", [True, False])
def test_trashed_attachments_never_count_as_saved(tmp_path, registered):
    plan, store, transport, blobs = setup(tmp_path)
    transport.fail_next("upload_and_register", after=registered)
    run_import(plan, store, transport, blobs)
    attachment = next(iter(transport.attachments.values()))
    attachment["deleted"] = True
    receipt, = run_import(plan, store, transport, blobs)
    assert receipt["status"] == "failure" and not receipt["pdf_verified"]
    assert len(transport.attachments) == 1


def test_malformed_unrelated_identifier_does_not_block_whole_library(tmp_path):
    plan, store, transport, blobs = setup(tmp_path, include_pdf=False)
    transport.items["ABCDEFGH"] = {"key": "ABCDEFGH", "version": 1, "title": "Unrelated manual entry",
                                   "identity": {"doi": "not actually a DOI", "arxiv": ""}}
    receipt, = run_import(plan, store, transport, blobs)
    assert receipt["status"] == "complete" and len(transport.items) == 2


def test_author_string_cannot_be_mistaken_for_creator_list():
    report, html, catalog = fixtures()
    catalog["papers"][0]["authors"] = "Synthetic Example"
    with pytest.raises(ImportRejected):
        load(report, html, catalog)


def test_transport_error_bodies_never_enter_durable_receipts(tmp_path):
    plan, store, transport, blobs = setup(tmp_path)
    private_marker = "DO_NOT_PERSIST_SECRET_OR_SIGNED_URL"
    transport.fail_next("create_item", code=private_marker)
    receipt, = run_import(plan, store, transport, blobs)
    assert receipt["error_code"] == "transport_error"
    assert private_marker not in json.dumps(receipt)
    assert private_marker.encode() not in store.path.read_bytes()
