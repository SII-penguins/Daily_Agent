"""Offline contracts for whole-state, Library-mediated durable checkpoints.

This module does NOT authenticate, call, upload to, or impersonate Library. An
external authorized host must use actual Library results, retain the exact CAS
guard, materialize the returned version, and supply normalized evidence here.
Caller-created JSON is not independent proof of a remote write.

Identity shape: ``library_file_id``, ``file_id``, ``version`` (a nonnegative
integer, including zero), ``sha256`` and ``size``. Hash/size describe exact bytes.
The host converts a documented decimal Library version to int before calling.

Workflow: prepare_checkpoint -> begin_upload -> host replacement/materialization
-> commit_checkpoint. The returned ``request`` is the replacement request; never
remove expected_current_version to bypass a conflict. A started upload remains
pending across exceptions/restarts. Before another write, reconcile_checkpoint
with fresh current metadata and freshly materialized bytes. A target already
present without a recovered write result stays ambiguous and cannot be retried.

Normalized write_result shape::

    {"status": "success", "operation": "replace",
     "checkpoint_id": "<prepare result>", "attempt_id": "<begin result>",
     "request": {"library_file_id": "<submitted id>",
                 "expected_current_version": 0},
     "result": {<identity from actual successful Library write>}}

The host records request correlation from its actual invocation, not a guessed
response field. ``current`` is independently refreshed authoritative metadata,
not the write result copied twice. Materialization must be pinned to its file_id
and checked by the host. Python validates consistency, not provenance/freshness.
Control files and snapshots must live outside state_root. They are committed in
one fsynced atomic local journal; a local commit is not a future freshness claim.
The host must retain the returned manifest/receipt in its durable records too;
this module cannot make an executor-local journal survive executor loss.
No jobs, claims, responses, budgets, history, or assets are selectively omitted.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from uuid import uuid4

from daily_agent import state_archive
from daily_agent.workflow_state import StateCorrupt, atomic_json, exclusive_lock, read_json


_IDENTITY_KEYS = {"library_file_id", "file_id", "version", "sha256", "size"}
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_ID = re.compile(r"[0-9a-f]{32}\Z")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _nonempty(value, label):
    if not isinstance(value, str) or not value.strip():
        raise StateCorrupt(f"Missing/invalid {label}")
    return value


def _integer(value, label):
    if type(value) is not int or value < 0:
        raise StateCorrupt(f"{label} must be a nonnegative integer")
    return value


def _identity(value):
    if not isinstance(value, dict) or set(value) != _IDENTITY_KEYS:
        raise StateCorrupt("Incomplete or ambiguous Library identity")
    result = dict(value)
    for key in ("library_file_id", "file_id"):
        _nonempty(result[key], key)
    for key in ("version", "size"):
        _integer(result[key], key)
    if not isinstance(result["sha256"], str) or not _SHA256.fullmatch(result["sha256"]):
        raise StateCorrupt("Invalid SHA256 identity")
    return result


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def _control(control_dir, root=None):
    original = Path(control_dir)
    if original.is_symlink():
        raise StateCorrupt("Checkpoint control directory cannot be a symlink")
    control = original.resolve()
    if root is not None and control.is_relative_to(Path(root).resolve()):
        raise ValueError("Checkpoint controls must be outside the state root")
    control.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in ("journal.json", "checkpoint.lock", "snapshots"):
        if (control / name).is_symlink():
            raise StateCorrupt("Checkpoint controls cannot contain symlinks")
    return control


def _manifest(value, control):
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise StateCorrupt("Invalid checkpoint manifest")
    checkpoint_id = value.get("checkpoint_id")
    if not isinstance(checkpoint_id, str) or not _ID.fullmatch(checkpoint_id):
        raise StateCorrupt("Invalid checkpoint id")
    _identity(value.get("parent"))
    for key in ("purpose", "boundary", "owner", "created_at", "state_root"):
        _nonempty(value.get(key), key)
    if control.is_relative_to(Path(value["state_root"]).resolve()):
        raise StateCorrupt("Checkpoint controls are inside archived state")
    archive = value.get("archive")
    if not isinstance(archive, dict) or archive.get("filename") != f"{checkpoint_id}.zip":
        raise StateCorrupt("Invalid checkpoint archive path")
    _integer(archive.get("size"), "archive size")
    _integer(archive.get("file_count"), "archive file count")
    if not isinstance(archive.get("sha256"), str) or not _SHA256.fullmatch(archive["sha256"]):
        raise StateCorrupt("Invalid checkpoint archive digest")
    return value


def _load(control):
    journal = read_json(control / "journal.json")
    if journal is None:
        return {"schema_version": 1, "committed": [], "pending": None}
    if not isinstance(journal, dict) or journal.get("schema_version") != 1:
        raise StateCorrupt("Invalid checkpoint journal")
    if not isinstance(journal.get("committed"), list) or "pending" not in journal:
        raise StateCorrupt("Incomplete checkpoint journal")
    previous = None
    for receipt in journal["committed"]:
        if not isinstance(receipt, dict):
            raise StateCorrupt("Invalid checkpoint receipt")
        manifest = _manifest(receipt.get("manifest"), control)
        if receipt.get("manifest_sha256") != _hash(manifest):
            raise StateCorrupt("Checkpoint manifest changed")
        identity = _identity(receipt.get("library"))
        if previous is not None and manifest["parent"] != previous:
            raise StateCorrupt("Broken checkpoint ancestry")
        if identity["library_file_id"] != manifest["parent"]["library_file_id"] or identity["version"] <= manifest["parent"]["version"]:
            raise StateCorrupt("Checkpoint receipt rollback")
        if (identity["sha256"], identity["size"]) != (manifest["archive"]["sha256"], manifest["archive"]["size"]):
            raise StateCorrupt("Checkpoint receipt bytes mismatch")
        write_result = receipt.get("write_result")
        if not isinstance(write_result, dict):
            raise StateCorrupt("Missing committed Library write evidence")
        attempt_id = write_result.get("attempt_id")
        if not isinstance(attempt_id, str) or not _ID.fullmatch(attempt_id):
            raise StateCorrupt("Invalid committed upload attempt")
        _nonempty(receipt.get("committed_at"), "commit time")
        _validate_write({"manifest": manifest, "attempts": [attempt_id]}, write_result, identity)
        previous = identity
    pending = journal["pending"]
    if pending is not None:
        if not isinstance(pending, dict) or pending.get("phase") not in {"prepared", "awaiting_result"}:
            raise StateCorrupt("Invalid pending checkpoint")
        manifest = _manifest(pending.get("manifest"), control)
        if pending.get("manifest_sha256") != _hash(manifest):
            raise StateCorrupt("Pending checkpoint manifest changed")
        if previous is not None and manifest["parent"] != previous:
            raise StateCorrupt("Pending checkpoint no longer descends from committed head")
        attempts = pending.get("attempts")
        if not isinstance(attempts, list) or any(not isinstance(a, str) or not _ID.fullmatch(a) for a in attempts) or len(attempts) != len(set(attempts)):
            raise StateCorrupt("Invalid checkpoint upload attempts")
        if pending["phase"] == "awaiting_result" and not attempts:
            raise StateCorrupt("Missing checkpoint upload attempt")
    return journal


def _pending(journal, checkpoint_id):
    pending = journal["pending"]
    if pending is None or pending["manifest"]["checkpoint_id"] != checkpoint_id:
        raise StateCorrupt("No matching pending checkpoint")
    return pending


def _bytes(path, identity, *, verify_archive=False):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise StateCorrupt("Expected a materialized regular file")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        if before.st_size != identity["size"]:
            raise StateCorrupt("Materialized size mismatch")
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
        if size != identity["size"] or digest.hexdigest() != identity["sha256"]:
            raise StateCorrupt("Materialized digest mismatch")
        manifest = None
        if verify_archive:
            try:
                stream.seek(0)
                manifest = state_archive.verify(stream)
            except Exception as exc:
                raise StateCorrupt("Materialized checkpoint is not a valid state archive") from exc
        after = os.fstat(stream.fileno())
        fingerprint = lambda stat: (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        if fingerprint(before) != fingerprint(after) or fingerprint(after) != fingerprint(path.stat()):
            raise StateCorrupt("Materialized file changed during verification")
        return manifest


def _local_archive(control, manifest):
    path = control / "snapshots" / manifest["archive"]["filename"]
    archived = _bytes(path, manifest["archive"], verify_archive=True)
    if len(archived["files"]) != manifest["archive"]["file_count"] or archived.get("source_root") != manifest["state_root"]:
        raise StateCorrupt("Snapshot manifest differs from checkpoint preparation")
    return path


def prepare_checkpoint(state_root, control_dir, *, parent, purpose, boundary, owner):
    """Snapshot all state and freeze its CAS ancestry; return its pending record.

    Existing pending work blocks a new snapshot. Reconcile or finish it first.
    ``parent`` must come from the host's authoritative current Library evidence.
    """
    root = Path(state_root).resolve()
    if not root.is_dir():
        raise ValueError("State root must exist")
    parent = _identity(parent)
    for key, value in (("purpose", purpose), ("boundary", boundary), ("owner", owner)):
        _nonempty(value, key)
    control = _control(control_dir, root)
    with exclusive_lock(control / "checkpoint.lock"):
        journal = _load(control)
        if journal["pending"] is not None:
            raise StateCorrupt("Pending checkpoint must be reconciled before another snapshot")
        if journal["committed"] and journal["committed"][-1]["library"] != parent:
            raise StateCorrupt("Parent differs from last committed checkpoint; reconcile externally")
        checkpoint_id = uuid4().hex
        directory = control / "snapshots"
        directory.mkdir(exist_ok=True, mode=0o700)
        archive = directory / f"{checkpoint_id}.zip"
        saved = state_archive.snapshot(root, archive)
        archive_identity = {"filename": archive.name, "sha256": saved["sha256"],
                            "size": saved["size"], "file_count": saved["file_count"]}
        manifest = {"schema_version": 1, "checkpoint_id": checkpoint_id,
                    "state_root": str(root), "created_at": _now(), "parent": parent,
                    "purpose": purpose, "boundary": boundary, "owner": owner,
                    "archive": archive_identity}
        _manifest(manifest, control)
        _local_archive(control, manifest)
        # Snapshot publication must be durable before its journal can reference it.
        with archive.open("rb") as stream:
            os.fsync(stream.fileno())
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        pending = {"manifest": manifest, "manifest_sha256": _hash(manifest),
                   "phase": "prepared", "attempts": []}
        journal["pending"] = pending
        atomic_json(control / "journal.json", journal)
        return pending


def begin_upload(control_dir, checkpoint_id, *, current):
    """Persist an uncertain attempt BEFORE returning the host's CAS request.

    Never call again after a timeout until reconcile_checkpoint permits retry.
    A fresh matching current read is mandatory even for the first attempt.
    """
    control = _control(control_dir)
    current = _identity(current)
    with exclusive_lock(control / "checkpoint.lock"):
        journal = _load(control)
        pending = _pending(journal, checkpoint_id)
        if pending["phase"] != "prepared":
            raise StateCorrupt("Upload may already have happened; reconcile before retry")
        manifest = pending["manifest"]
        if current != manifest["parent"]:
            raise StateCorrupt("Library CAS parent changed")
        archive = _local_archive(control, manifest)
        attempt_id = uuid4().hex
        pending["attempts"].append(attempt_id)
        pending["phase"] = "awaiting_result"
        atomic_json(control / "journal.json", journal)
        return {"checkpoint_id": checkpoint_id, "attempt_id": attempt_id,
                "request": {"file": str(archive), "library_file_id": current["library_file_id"],
                            "expected_current_version": current["version"]}}


def _validate_write(pending, write_result, current):
    manifest = pending["manifest"]
    if not isinstance(write_result, dict) or set(write_result) != {"status", "operation", "checkpoint_id", "attempt_id", "request", "result"}:
        raise StateCorrupt("Missing or ambiguous Library write result")
    if write_result["status"] != "success" or write_result["operation"] != "replace":
        raise StateCorrupt("Library replacement was not confirmed successful")
    if write_result["checkpoint_id"] != manifest["checkpoint_id"] or write_result["attempt_id"] not in pending["attempts"]:
        raise StateCorrupt("Library result belongs to another checkpoint/upload attempt")
    request = write_result["request"]
    if not isinstance(request, dict) or set(request) != {"library_file_id", "expected_current_version"}:
        raise StateCorrupt("Missing Library CAS request evidence")
    _integer(request["expected_current_version"], "expected_current_version")
    if request != {"library_file_id": manifest["parent"]["library_file_id"], "expected_current_version": manifest["parent"]["version"]}:
        raise StateCorrupt("Library CAS request did not match prepared parent")
    result = _identity(write_result["result"])
    if result != current:
        raise StateCorrupt("Write result no longer matches current Library version")
    parent = manifest["parent"]
    if current["library_file_id"] != parent["library_file_id"] or current["version"] <= parent["version"] or current["file_id"] == parent["file_id"]:
        raise StateCorrupt("Library identity/version failed to advance")
    if (current["sha256"], current["size"]) != (manifest["archive"]["sha256"], manifest["archive"]["size"]):
        raise StateCorrupt("Library write is not the prepared snapshot")


def _commit(control, journal, pending, write_result, current, materialized_archive):
    _validate_write(pending, write_result, current)
    # Readback alone is not a write receipt. Validate both, and the local snapshot.
    local_archive = _local_archive(control, pending["manifest"])
    _bytes(materialized_archive, current, verify_archive=True)
    if Path(materialized_archive).samefile(local_archive):
        raise StateCorrupt("Readback must be separately materialized, not the local snapshot")
    receipt = {"manifest": pending["manifest"], "manifest_sha256": pending["manifest_sha256"],
               "library": current, "write_result": write_result, "committed_at": _now()}
    journal["committed"].append(receipt)
    journal["pending"] = None
    atomic_json(control / "journal.json", journal)
    return receipt


def commit_checkpoint(control_dir, checkpoint_id, *, write_result, current, materialized_archive):
    """Commit only matching actual host write evidence AND exact remote readback.

    Readback must be a separate file, never the prepared snapshot or a hardlink.
    Every failed check leaves the pending record intact. Repeating a successful
    commit is safe only with matching receipt and still-current remote evidence.
    """
    control = _control(control_dir)
    current = _identity(current)
    with exclusive_lock(control / "checkpoint.lock"):
        journal = _load(control)
        if journal["pending"] is None and journal["committed"]:
            receipt = journal["committed"][-1]
            if receipt["manifest"]["checkpoint_id"] == checkpoint_id and receipt["write_result"] == write_result and receipt["library"] == current:
                _bytes(materialized_archive, current, verify_archive=True)
                local_archive = control / "snapshots" / receipt["manifest"]["archive"]["filename"]
                if local_archive.exists() and Path(materialized_archive).samefile(local_archive):
                    raise StateCorrupt("Readback must be separately materialized, not the local snapshot")
                return receipt
        pending = _pending(journal, checkpoint_id)
        if not pending["attempts"]:
            raise StateCorrupt("No recorded upload attempt")
        return _commit(control, journal, pending, write_result, current, materialized_archive)


def reconcile_checkpoint(control_dir, checkpoint_id, *, current, materialized_archive, write_result=None):
    """Read-before-retry after any interruption; no remote mutation happens here.

    Unchanged parent bytes allow another CAS attempt. Matching new snapshot bytes
    require the actual successful write result to commit; absent evidence leaves
    it ambiguous. All other heads (including rollback) fail closed.
    """
    control = _control(control_dir)
    current = _identity(current)
    with exclusive_lock(control / "checkpoint.lock"):
        journal = _load(control)
        pending = _pending(journal, checkpoint_id)
        manifest = pending["manifest"]
        _local_archive(control, manifest)
        _bytes(materialized_archive, current)
        if write_result is not None:
            receipt = _commit(control, journal, pending, write_result, current, materialized_archive)
            return {"status": "committed", "receipt": receipt}
        if current == manifest["parent"]:
            pending["phase"] = "prepared"
            atomic_json(control / "journal.json", journal)
            return {"status": "retry_ready", "checkpoint_id": checkpoint_id}
        parent = manifest["parent"]
        if (pending["attempts"] and current["library_file_id"] == parent["library_file_id"]
                and current["version"] > parent["version"] and current["file_id"] != parent["file_id"]
                and (current["sha256"], current["size"]) == (manifest["archive"]["sha256"], manifest["archive"]["size"])):
            _bytes(materialized_archive, current, verify_archive=True)
            pending["phase"] = "awaiting_result"
            atomic_json(control / "journal.json", journal)
            return {"status": "awaiting_write_result", "checkpoint_id": checkpoint_id}
        raise StateCorrupt("Library head conflicts with pending checkpoint; refuse rollback or blind overwrite")


def checkpoint_status(control_dir):
    """Read local receipts/pending evidence; this does not check remote freshness."""
    control = _control(control_dir)
    with exclusive_lock(control / "checkpoint.lock"):
        return _load(control)
