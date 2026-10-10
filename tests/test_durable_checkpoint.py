"""Offline fault injection only; synthetic identities never prove a Library write."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil

import pytest

from daily_agent import durable_checkpoint as checkpoint
from daily_agent.cloud_workflow import init_profile
from daily_agent.state_archive import restore, snapshot, verify
from daily_agent.workflow_state import StateCorrupt


@pytest.fixture
def prepared(tmp_path):
    root = tmp_path / "state"
    init_profile(Path(__file__).resolve().parents[1], root, "codex")
    # Each category must survive as opaque state, including uncertain outcomes.
    files = {
        "data/writer-queue/jobs/job.json": b'{"status":"claimed","attempt":4}',
        "data/writer-queue/claims/claim.json": b'{"worker":"fixture","expires":"past"}',
        "data/writer-queue/responses/response.json": b'{"answer":"fixture"}',
        "data/state/budgets.json": b'{"elapsed_seconds":91,"remaining_seconds":9}',
        "data/history/items.json": b'{"seen":["fixture"]}',
        "reports/issue/assets/page.png": b"offline synthetic image bytes",
        "data/state/cloud-delivery/offline.json": b'{"state":"uncertain","message_id":"fixture"}',
    }
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    previous = snapshot(root, tmp_path / "previous.zip")
    parent = {"library_file_id": "fixture-library", "file_id": "fixture-file-0", "version": 0,
              "sha256": previous["sha256"], "size": previous["size"]}
    control = tmp_path / "control"
    pending = checkpoint.prepare_checkpoint(root, control, parent=parent,
                                            purpose="offline recovery test", boundary="writer pause",
                                            owner="fixture-owner")
    return {"root": root, "control": control, "parent": parent, "previous": previous,
            "pending": pending, "files": files, "tmp": tmp_path}


def upload_evidence(fixture):
    pending = fixture["pending"]
    checkpoint_id = pending["manifest"]["checkpoint_id"]
    upload = checkpoint.begin_upload(fixture["control"], checkpoint_id, current=fixture["parent"])
    archive = pending["manifest"]["archive"]
    current = {"library_file_id": fixture["parent"]["library_file_id"], "file_id": "fixture-file-1",
               "version": 1, "sha256": archive["sha256"], "size": archive["size"]}
    remote = fixture["tmp"] / "materialized.zip"
    shutil.copyfile(upload["request"]["file"], remote)
    write_result = {"status": "success", "operation": "replace", "checkpoint_id": checkpoint_id,
                    "attempt_id": upload["attempt_id"], "request": {
                        "library_file_id": upload["request"]["library_file_id"],
                        "expected_current_version": upload["request"]["expected_current_version"]},
                    "result": deepcopy(current)}
    return upload, current, remote, write_result


def commit(fixture, evidence):
    upload, current, remote, result = evidence
    return checkpoint.commit_checkpoint(fixture["control"], upload["checkpoint_id"],
                                        write_result=result, current=current, materialized_archive=remote)


def test_complete_snapshot_roundtrip_and_zero_cas(prepared):
    evidence = upload_evidence(prepared)
    upload, current, remote, write_result = evidence
    assert upload["request"]["expected_current_version"] == 0
    receipt = commit(prepared, evidence)
    assert receipt["library"] == current
    assert receipt["write_result"] == write_result
    assert checkpoint.checkpoint_status(prepared["control"])["pending"] is None
    # Idempotent acknowledgement cannot create a second receipt/version.
    assert commit(prepared, evidence) == receipt
    assert len(checkpoint.checkpoint_status(prepared["control"])["committed"]) == 1
    restored = prepared["tmp"] / "restored"
    restore(remote, restored, current["sha256"])
    for relative, content in prepared["files"].items():
        assert (restored / relative).read_bytes() == content
    manifest = verify(remote)
    assert not any("journal.json" in name or "snapshots/" in name for name in manifest["files"])
    assert receipt["manifest"]["owner"] == "fixture-owner"
    assert receipt["manifest"]["boundary"] == "writer pause"


@pytest.mark.parametrize("version", [-1, None, True, False, "0", 0.0, float("inf")])
def test_invalid_versions_fail_closed(prepared, version):
    parent = dict(prepared["parent"], version=version)
    with pytest.raises(StateCorrupt):
        checkpoint.begin_upload(prepared["control"], prepared["pending"]["manifest"]["checkpoint_id"], current=parent)
    assert checkpoint.checkpoint_status(prepared["control"])["pending"]["phase"] == "prepared"


@pytest.mark.parametrize("field,value", [("version", 1), ("file_id", "other"),
                                           ("library_file_id", "other"), ("sha256", "f" * 64), ("size", 2)])
def test_stale_cas_blocks_upload_before_attempt(prepared, field, value):
    current = dict(prepared["parent"])
    current[field] = value
    with pytest.raises(StateCorrupt):
        checkpoint.begin_upload(prepared["control"], prepared["pending"]["manifest"]["checkpoint_id"], current=current)
    assert checkpoint.checkpoint_status(prepared["control"])["pending"]["attempts"] == []


def test_interrupted_upload_requires_reconciliation_and_retains_cas(prepared):
    upload, _, _, _ = upload_evidence(prepared)
    with pytest.raises(StateCorrupt, match="reconcile"):
        checkpoint.begin_upload(prepared["control"], upload["checkpoint_id"], current=prepared["parent"])
    # Simulate a fresh host read proving the write did not reach Library.
    outcome = checkpoint.reconcile_checkpoint(prepared["control"], upload["checkpoint_id"],
                                               current=prepared["parent"], materialized_archive=prepared["previous"]["archive"])
    assert outcome["status"] == "retry_ready"
    retry = checkpoint.begin_upload(prepared["control"], upload["checkpoint_id"], current=prepared["parent"])
    assert retry["request"] == upload["request"]
    assert retry["attempt_id"] != upload["attempt_id"]
    assert len(checkpoint.checkpoint_status(prepared["control"])["pending"]["attempts"]) == 2


def test_uploaded_bytes_without_receipt_stay_ambiguous_until_recovered(prepared):
    upload, current, remote, write_result = upload_evidence(prepared)
    status = checkpoint.reconcile_checkpoint(prepared["control"], upload["checkpoint_id"], current=current,
                                              materialized_archive=remote)
    assert status["status"] == "awaiting_write_result"
    assert checkpoint.checkpoint_status(prepared["control"])["committed"] == []
    with pytest.raises(StateCorrupt):
        checkpoint.begin_upload(prepared["control"], upload["checkpoint_id"], current=prepared["parent"])
    status = checkpoint.reconcile_checkpoint(prepared["control"], upload["checkpoint_id"], current=current,
                                              materialized_archive=remote, write_result=write_result)
    assert status["status"] == "committed"


@pytest.mark.parametrize("fault", ["missing_cas", "wrong_cas", "bool_cas", "wrong_id", "wrong_file",
                                    "stale_version", "negative_version", "result_hash", "result_size",
                                    "ambiguous_status", "wrong_operation", "wrong_checkpoint", "wrong_attempt",
                                    "extra_response", "no_response", "current_advanced", "same_file_id"])
def test_bad_receipt_or_remote_head_never_commits(prepared, fault):
    upload, current, remote, result = upload_evidence(prepared)
    if fault == "missing_cas":
        del result["request"]["expected_current_version"]
    elif fault == "wrong_cas":
        result["request"]["expected_current_version"] = 1
    elif fault == "bool_cas":
        result["request"]["expected_current_version"] = False
    elif fault == "wrong_id":
        result["result"]["library_file_id"] = "different-library"
    elif fault == "wrong_file":
        result["result"]["file_id"] = "different-file"
    elif fault == "stale_version":
        result["result"]["version"] = current["version"] = 0
    elif fault == "negative_version":
        result["result"]["version"] = -1
    elif fault == "result_hash":
        result["result"]["sha256"] = current["sha256"] = "f" * 64
    elif fault == "result_size":
        result["result"]["size"] = current["size"] = 1
    elif fault == "ambiguous_status":
        result["status"] = "unknown"
    elif fault == "wrong_operation":
        result["operation"] = "create"
    elif fault == "wrong_checkpoint":
        result["checkpoint_id"] = "0" * 32
    elif fault == "wrong_attempt":
        result["attempt_id"] = "0" * 32
    elif fault == "extra_response":
        result["error"] = "unrelated batch failed"
    elif fault == "no_response":
        result = None
    elif fault == "current_advanced":
        current["version"] = 2
    elif fault == "same_file_id":
        result["result"]["file_id"] = current["file_id"] = prepared["parent"]["file_id"]
    with pytest.raises(StateCorrupt):
        commit(prepared, (upload, current, remote, result))
    status = checkpoint.checkpoint_status(prepared["control"])
    assert status["committed"] == []
    assert status["pending"]["phase"] == "awaiting_result"


@pytest.mark.parametrize("fault", ["truncated", "same_size_corruption", "wrong_export", "missing", "symlink"])
def test_corrupt_materialization_never_commits(prepared, fault):
    evidence = upload_evidence(prepared)
    _, _, remote, _ = evidence
    if fault == "truncated":
        remote.write_bytes(remote.read_bytes()[:-1])
    elif fault == "same_size_corruption":
        data = bytearray(remote.read_bytes())
        data[0] ^= 1
        remote.write_bytes(data)
    elif fault == "wrong_export":
        remote.write_bytes(b"<html>download failed</html>")
    elif fault == "missing":
        remote.unlink()
    elif fault == "symlink":
        remote.unlink()
        remote.symlink_to(evidence[0]["request"]["file"])
    with pytest.raises(StateCorrupt):
        commit(prepared, evidence)
    assert checkpoint.checkpoint_status(prepared["control"])["committed"] == []


def test_checkpoint_snapshot_corruption_blocks_upload(prepared):
    manifest = prepared["pending"]["manifest"]
    archive = prepared["control"] / "snapshots" / manifest["archive"]["filename"]
    archive.write_bytes(b"corrupt")
    with pytest.raises(StateCorrupt):
        checkpoint.begin_upload(prepared["control"], manifest["checkpoint_id"], current=prepared["parent"])


def test_snapshot_failure_never_publishes_pending(prepared, monkeypatch):
    def interrupted_snapshot(*args, **kwargs):
        raise OSError("simulated snapshot interruption")
    monkeypatch.setattr(checkpoint.state_archive, "snapshot", interrupted_snapshot)
    control = prepared["tmp"] / "snapshot-failed"
    with pytest.raises(OSError):
        checkpoint.prepare_checkpoint(prepared["root"], control, parent=prepared["parent"],
                                      purpose="fixture", boundary="fixture", owner="fixture")
    assert checkpoint.checkpoint_status(control)["pending"] is None


def test_failed_local_commit_write_keeps_pending_and_can_retry(prepared, monkeypatch):
    evidence = upload_evidence(prepared)
    original = checkpoint.atomic_json
    def interrupted_commit(*args, **kwargs):
        raise OSError("simulated journal interruption")
    monkeypatch.setattr(checkpoint, "atomic_json", interrupted_commit)
    with pytest.raises(OSError):
        commit(prepared, evidence)
    assert checkpoint.checkpoint_status(prepared["control"])["pending"] is not None
    assert checkpoint.checkpoint_status(prepared["control"])["committed"] == []
    monkeypatch.setattr(checkpoint, "atomic_json", original)
    assert commit(prepared, evidence)["library"]["version"] == 1


def test_new_checkpoint_ancestry_cannot_roll_back(prepared):
    evidence = upload_evidence(prepared)
    receipt = commit(prepared, evidence)
    with pytest.raises(StateCorrupt, match="Parent differs"):
        checkpoint.prepare_checkpoint(prepared["root"], prepared["control"], parent=prepared["parent"],
                                      purpose="fixture", boundary="fixture", owner="fixture")
    pending = checkpoint.prepare_checkpoint(prepared["root"], prepared["control"], parent=receipt["library"],
                                            purpose="next", boundary="next pause", owner="fixture")
    current = dict(receipt["library"], version=0)
    with pytest.raises(StateCorrupt):
        checkpoint.reconcile_checkpoint(prepared["control"], pending["manifest"]["checkpoint_id"],
                                         current=current, materialized_archive=evidence[2])
    assert checkpoint.checkpoint_status(prepared["control"])["pending"]["phase"] == "prepared"


def test_controls_cannot_enter_snapshot_or_follow_symlink(prepared):
    for control in (prepared["root"], prepared["root"] / "control"):
        with pytest.raises(ValueError, match="outside"):
            checkpoint.prepare_checkpoint(prepared["root"], control, parent=prepared["parent"],
                                          purpose="fixture", boundary="fixture", owner="fixture")
    link = prepared["tmp"] / "linked-control"
    link.symlink_to(prepared["control"], target_is_directory=True)
    with pytest.raises(StateCorrupt, match="symlink"):
        checkpoint.checkpoint_status(link)


def test_preparing_again_never_discards_pending(prepared):
    with pytest.raises(StateCorrupt, match="Pending"):
        checkpoint.prepare_checkpoint(prepared["root"], prepared["control"], parent=prepared["parent"],
                                      purpose="fixture", boundary="fixture", owner="fixture")
    assert checkpoint.checkpoint_status(prepared["control"])["pending"] == prepared["pending"]


def test_manifest_tampering_fails_closed(prepared):
    path = prepared["control"] / "journal.json"
    journal = json.loads(path.read_text())
    journal["pending"]["manifest"]["owner"] = "changed-owner"
    path.write_text(json.dumps(journal))
    with pytest.raises(StateCorrupt, match="manifest changed"):
        checkpoint.checkpoint_status(prepared["control"])


def test_remote_metadata_hash_cannot_disguise_invalid_archive(prepared, monkeypatch):
    # Even a lying/inconsistent snapshot adapter cannot prepare non-archive bytes.
    def bad_snapshot(root, output):
        output = Path(output)
        output.write_bytes(b"not a state snapshot")
        return {"archive": str(output), "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
                "size": output.stat().st_size, "file_count": 0}
    monkeypatch.setattr(checkpoint.state_archive, "snapshot", bad_snapshot)
    with pytest.raises(StateCorrupt, match="valid state archive"):
        checkpoint.prepare_checkpoint(prepared["root"], prepared["tmp"] / "bad-control", parent=prepared["parent"],
                                      purpose="fixture", boundary="fixture", owner="fixture")


@pytest.mark.parametrize("hardlink", [False, True])
def test_local_snapshot_is_not_remote_readback(prepared, hardlink):
    upload, current, remote, write_result = upload_evidence(prepared)
    local = Path(upload["request"]["file"])
    if hardlink:
        remote.unlink()
        remote.hardlink_to(local)
    else:
        remote = local
    with pytest.raises(StateCorrupt, match="separately materialized"):
        commit(prepared, (upload, current, remote, write_result))
    assert checkpoint.checkpoint_status(prepared["control"])["committed"] == []


def test_corrupted_committed_receipt_fails_closed(prepared):
    commit(prepared, upload_evidence(prepared))
    path = prepared["control"] / "journal.json"
    journal = json.loads(path.read_text())
    del journal["committed"][0]["write_result"]["request"]["expected_current_version"]
    path.write_text(json.dumps(journal))
    with pytest.raises(StateCorrupt, match="CAS"):
        checkpoint.checkpoint_status(prepared["control"])


def test_late_success_can_be_reconciled_after_retry_is_prepared(prepared):
    # An old inflight result may arrive after an unchanged-head read; retain it.
    evidence = upload_evidence(prepared)
    upload, current, remote, write_result = evidence
    checkpoint.reconcile_checkpoint(prepared["control"], upload["checkpoint_id"],
                                     current=prepared["parent"], materialized_archive=prepared["previous"]["archive"])
    checkpoint.begin_upload(prepared["control"], upload["checkpoint_id"], current=prepared["parent"])
    outcome = checkpoint.reconcile_checkpoint(prepared["control"], upload["checkpoint_id"],
                                               current=current, materialized_archive=remote, write_result=write_result)
    assert outcome["status"] == "committed"


def test_failed_begin_journal_write_returns_no_upload_and_stays_prepared(prepared, monkeypatch):
    def interrupted_begin(*args, **kwargs):
        raise OSError("simulated failure before attempt publication")
    monkeypatch.setattr(checkpoint, "atomic_json", interrupted_begin)
    with pytest.raises(OSError):
        checkpoint.begin_upload(prepared["control"], prepared["pending"]["manifest"]["checkpoint_id"],
                                current=prepared["parent"])
    assert checkpoint.checkpoint_status(prepared["control"])["pending"]["phase"] == "prepared"
    assert checkpoint.checkpoint_status(prepared["control"])["pending"]["attempts"] == []


def test_materialization_changed_during_verification_cannot_commit(prepared, monkeypatch):
    evidence = upload_evidence(prepared)
    remote = evidence[2]
    original = checkpoint.state_archive.verify
    def change_after_verify(stream):
        result = original(stream)
        if Path(stream.name) == remote:
            content = bytearray(remote.read_bytes())
            content[0] ^= 1
            remote.write_bytes(content)
        return result
    monkeypatch.setattr(checkpoint.state_archive, "verify", change_after_verify)
    with pytest.raises(StateCorrupt, match="changed during verification"):
        commit(prepared, evidence)
    assert checkpoint.checkpoint_status(prepared["control"])["committed"] == []
