"""Offline, isolated recovery tests for the opt-in compact checkpoint helper."""
import hashlib
import json
from pathlib import Path
import stat
import zipfile

import pytest

from daily_agent.compact_checkpoint import (
    CheckpointError, Limits, export_checkpoint, put_asset, restore_checkpoint,
)


MANIFEST = "checkpoint-manifest.json"


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def workflow(tmp_path):
    root = tmp_path / "workflow"
    root.mkdir()
    pdf = put_asset(root, b"%PDF-1.7\n" + bytes(range(256)) * 16_384)
    document = put_asset(root, b'{"text":"full paper source"}')
    write_json(root / "state.json", {
        "protocol": "paper-first-v3", "phase": "reading", "pdf": f"objects/{pdf}",
        "document": f"objects/{document}", "pending_job": "paper-one-write",
    })
    write_json(root / "data/writer-queue/paper-one-write.job.json", {
        "job_id": "paper-one-write", "prompt": "Write the accepted paper.",
        "source": f"objects/{document}", "attempt": 1,
    })
    write_json(root / "data/writer-queue/paper-one-write.claim.json", {"attempt": 1})
    (root / "workflow.lock").touch()
    return root, pdf, document


def snapshot(root):
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in root.rglob("*") if p.is_file() and not p.name.endswith(".lock")}


def rewrite_bundle(source, target, change):
    with zipfile.ZipFile(source) as archive:
        entries = [(item, archive.read(item)) for item in archive.infolist()]
    entries = change(entries)
    with zipfile.ZipFile(target, "w") as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return target


def edit_manifest(source, target, mutate):
    def change(entries):
        result = []
        for entry, data in entries:
            if entry.filename == MANIFEST:
                manifest = json.loads(data)
                mutate(manifest)
                data = json.dumps(manifest).encode()
            result.append((entry, data))
        return result
    return rewrite_bundle(source, target, change)


def test_base_and_latest_delta_restore_assets_and_inflight_queue_without_reupload(tmp_path):
    root, pdf, _ = workflow(tmp_path)
    base = tmp_path / "base.zip"
    base_manifest = export_checkpoint(root, base)
    restored_base = tmp_path / "base-restore"
    restore_checkpoint(base, restored_base)
    assert snapshot(restored_base) == snapshot(root)
    assert base_manifest["base_sha256"] is None
    base_bytes = base.read_bytes()

    crop = put_asset(root, b"PNG crop pixels" * 4000)
    write_json(root / "accepted/paper-one.json", {"crop": f"objects/{crop}"})
    write_json(root / "data/writer-queue/paper-one-write.answer.json", {"draft": "Complete evidence-backed draft"})
    write_json(root / "data/writer-queue/active.json", {"job_id": "paper-one-write"})
    write_json(root / "data/writer-queue/retry.json", {"next_attempt": 2})
    write_json(root / "state.json", {"protocol": "paper-first-v3", "phase": "reviewing", "pdf": f"objects/{pdf}", "crop": f"objects/{crop}"})
    first_delta = tmp_path / "delta-1.zip"
    manifest = export_checkpoint(root, first_delta, base_bundle=base)
    assert manifest["included_objects"] == [crop]
    assert manifest["base_sha256"] == hashlib.sha256(base_bytes).hexdigest()
    assert first_delta.stat().st_size < base.stat().st_size / 30
    with zipfile.ZipFile(first_delta) as archive:
        assert f"objects/{pdf}" not in archive.namelist()
        assert "workflow.lock" not in archive.namelist()

    # A later checkpoint needs only the same immutable base and latest delta.
    write_json(root / "state.json", {"protocol": "paper-first-v3", "phase": "accepted", "pdf": f"objects/{pdf}", "crop": f"objects/{crop}"})
    last_delta = tmp_path / "delta-2.zip"
    export_checkpoint(root, last_delta, base_bundle=base)
    first_delta.unlink()
    destination = tmp_path / "fresh"
    destination.mkdir()
    restore_checkpoint(last_delta, destination, base_bundle=base)
    assert snapshot(destination) == snapshot(root)
    assert (destination / "data/writer-queue/retry.json").is_file()
    assert base.read_bytes() == base_bytes
    assert last_delta.stat().st_size < base.stat().st_size / 30


def test_archives_are_deterministic_and_outputs_immutable(tmp_path):
    root, _, _ = workflow(tmp_path)
    first, second = tmp_path / "one.zip", tmp_path / "two.zip"
    assert export_checkpoint(root, first) == export_checkpoint(root, second)
    assert first.read_bytes() == second.read_bytes()
    with pytest.raises(CheckpointError, match="already exists"):
        export_checkpoint(root, first)
    with pytest.raises(CheckpointError, match="outside"):
        export_checkpoint(root, root / "bad.zip")
    delta1, delta2 = tmp_path / "d1.zip", tmp_path / "d2.zip"
    export_checkpoint(root, delta1, base_bundle=first)
    export_checkpoint(root, delta2, base_bundle=first)
    assert delta1.read_bytes() == delta2.read_bytes()


def test_latest_inventory_respects_deleted_unreferenced_assets_and_small_files(tmp_path):
    root, _, document = workflow(tmp_path)
    base = tmp_path / "base.zip"
    export_checkpoint(root, base)
    (root / "objects" / document).unlink()
    (root / "data/writer-queue/paper-one-write.job.json").unlink()
    state = json.loads((root / "state.json").read_text())
    del state["document"]
    write_json(root / "state.json", state)
    delta = tmp_path / "delta.zip"
    export_checkpoint(root, delta, base_bundle=base)
    restored = tmp_path / "restore"
    restore_checkpoint(delta, restored, base_bundle=base)
    assert snapshot(restored) == snapshot(root)
    assert not (restored / "objects" / document).exists()


def test_deleted_live_object_is_not_silently_recovered_from_base_during_export(tmp_path):
    root, pdf, _ = workflow(tmp_path)
    base = tmp_path / "base.zip"
    export_checkpoint(root, base)
    (root / "objects" / pdf).unlink()
    with pytest.raises(CheckpointError, match="Missing/invalid object reference"):
        export_checkpoint(root, tmp_path / "delta.zip", base_bundle=base)
    assert not (tmp_path / "delta.zip").exists()


def test_wrong_missing_base_and_delta_as_base_rejected(tmp_path):
    root, _, _ = workflow(tmp_path)
    base, delta = tmp_path / "base.zip", tmp_path / "delta.zip"
    export_checkpoint(root, base)
    export_checkpoint(root, delta, base_bundle=base)
    with pytest.raises(CheckpointError, match="Missing or wrong base"):
        restore_checkpoint(delta, tmp_path / "missing")
    write_json(root / "state.json", {"protocol": "paper-first-v3", "phase": "different"})
    wrong = tmp_path / "wrong.zip"
    export_checkpoint(root, wrong)
    with pytest.raises(CheckpointError, match="Missing or wrong base"):
        restore_checkpoint(delta, tmp_path / "wrong", base_bundle=wrong)
    with pytest.raises(CheckpointError, match="standalone"):
        export_checkpoint(root, tmp_path / "nested.zip", base_bundle=delta)
    with pytest.raises(CheckpointError, match="Missing or wrong base"):
        restore_checkpoint(base, tmp_path / "unnecessary-base", base_bundle=wrong)


@pytest.mark.parametrize("corrupt_base", [False, True])
def test_corrupt_objects_are_rejected_before_destination_creation(tmp_path, corrupt_base):
    root, pdf, _ = workflow(tmp_path)
    base, delta = tmp_path / "base.zip", tmp_path / "delta.zip"
    export_checkpoint(root, base)
    added = put_asset(root, b"new crop")
    export_checkpoint(root, delta, base_bundle=base)
    source = base if corrupt_base else delta
    digest = pdf if corrupt_base else added
    corrupt = rewrite_bundle(source, tmp_path / "corrupt.zip", lambda entries: [
        (entry, bytes([data[0] ^ 1]) + data[1:] if entry.filename == f"objects/{digest}" else data)
        for entry, data in entries])
    destination = tmp_path / "restore"
    with pytest.raises(CheckpointError, match="Corrupt checkpoint"):
        restore_checkpoint(delta if corrupt_base else corrupt, destination,
                           base_bundle=corrupt if corrupt_base else base)
    assert not destination.exists()


def test_missing_object_member_and_missing_base_inventory_rejected(tmp_path):
    root, pdf, _ = workflow(tmp_path)
    base = tmp_path / "base.zip"
    export_checkpoint(root, base)
    missing = rewrite_bundle(base, tmp_path / "missing.zip", lambda entries: [
        (entry, data) for entry, data in entries if entry.filename != f"objects/{pdf}"])
    with pytest.raises(CheckpointError, match="Missing or unexpected"):
        restore_checkpoint(missing, tmp_path / "restore")
    delta = tmp_path / "delta.zip"
    export_checkpoint(root, delta, base_bundle=base)
    absent = hashlib.sha256(b"absent").hexdigest()
    malformed = edit_manifest(delta, tmp_path / "missing-in-base.zip",
                              lambda value: value["objects"].update({absent: 6}))
    with pytest.raises(CheckpointError, match="Base is missing"):
        restore_checkpoint(malformed, tmp_path / "restore2", base_bundle=base)


def test_object_identity_conflict_rejected(tmp_path):
    root, pdf, _ = workflow(tmp_path)
    base, delta = tmp_path / "base.zip", tmp_path / "delta.zip"
    export_checkpoint(root, base)
    export_checkpoint(root, delta, base_bundle=base)
    conflict = edit_manifest(delta, tmp_path / "conflict.zip",
                             lambda value: value["objects"].update({pdf: 1}))
    with pytest.raises(CheckpointError, match="identity conflict"):
        restore_checkpoint(conflict, tmp_path / "restore", base_bundle=base)


@pytest.mark.parametrize("name", ["../outside", "/absolute", "a/../../outside", "a\\outside", "a//b", "C:/outside", "a/./b"])
def test_archive_path_traversal_rejected(tmp_path, name):
    root, _, _ = workflow(tmp_path)
    base = tmp_path / "base.zip"
    export_checkpoint(root, base)
    malicious = rewrite_bundle(base, tmp_path / "bad.zip", lambda entries: entries + [(name, b"bad")])
    with pytest.raises(CheckpointError, match="Unsafe archive path"):
        restore_checkpoint(malicious, tmp_path / "restore")
    assert not (tmp_path / "restore").exists()
    assert not (tmp_path / "outside").exists()


def test_archive_symlink_duplicate_and_file_directory_conflicts_rejected(tmp_path):
    root, _, _ = workflow(tmp_path)
    base = tmp_path / "base.zip"
    export_checkpoint(root, base)
    symlink = zipfile.ZipInfo("link")
    symlink.create_system = 3
    symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
    malicious = rewrite_bundle(base, tmp_path / "link.zip", lambda entries: entries + [(symlink, b"/outside")])
    with pytest.raises(CheckpointError, match="nonregular"):
        restore_checkpoint(malicious, tmp_path / "restore")
    with pytest.warns(UserWarning, match="Duplicate name"):
        duplicate = rewrite_bundle(base, tmp_path / "duplicate.zip", lambda entries: entries + [entries[0]])
    with pytest.raises(CheckpointError, match="Duplicate"):
        restore_checkpoint(duplicate, tmp_path / "restore")
    def conflict(value):
        value["files"]["state.json/child"] = {"sha256": hashlib.sha256(b"x").hexdigest(), "size": 1}
    bad = edit_manifest(base, tmp_path / "conflict.zip", conflict)
    with pytest.raises(CheckpointError, match="Conflicting file/directory"):
        restore_checkpoint(bad, tmp_path / "restore")


@pytest.mark.parametrize("kind", ["root", "object", "metadata", "directory", "output", "restore"])
def test_filesystem_symlinks_rejected(tmp_path, kind):
    root, pdf, _ = workflow(tmp_path)
    bundle = tmp_path / "base.zip"
    if kind == "restore":
        export_checkpoint(root, bundle)
        link = tmp_path / "link"
        link.symlink_to(root, target_is_directory=True)
        with pytest.raises(CheckpointError, match="Symlinks"):
            restore_checkpoint(bundle, link)
        return
    if kind == "root":
        link = tmp_path / "link"
        link.symlink_to(root, target_is_directory=True)
        root = link
    elif kind == "output":
        bundle.symlink_to(tmp_path / "uncreated")
    else:
        source = root / "objects" / pdf if kind == "object" else root / "state.json"
        if kind == "directory":
            (root / "linked").symlink_to(root / "data", target_is_directory=True)
        else:
            content = source.read_bytes()
            source.unlink()
            backing = tmp_path / "backing"
            backing.write_bytes(content)
            source.symlink_to(backing)
    with pytest.raises(CheckpointError, match="Symlinks"):
        export_checkpoint(root, bundle)


def test_corrupt_local_assets_and_content_addressed_collision_rejected(tmp_path):
    root, pdf, _ = workflow(tmp_path)
    original = (root / "objects" / pdf).read_bytes()
    (root / "objects" / pdf).write_bytes(b"modified")
    with pytest.raises(CheckpointError, match="Corrupt workflow object"):
        export_checkpoint(root, tmp_path / "base.zip")
    with pytest.raises(CheckpointError, match="immutable object is corrupt"):
        put_asset(root, original)


def test_metadata_asset_and_aggregate_limits(tmp_path):
    root, _, _ = workflow(tmp_path)
    with pytest.raises(CheckpointError, match="metadata size"):
        export_checkpoint(root, tmp_path / "tiny.zip", limits=Limits(max_metadata_bytes=5))
    with pytest.raises(CheckpointError, match="size limit"):
        export_checkpoint(root, tmp_path / "tiny2.zip", limits=Limits(max_object_bytes=5))
    with pytest.raises(CheckpointError, match="Too many"):
        export_checkpoint(root, tmp_path / "tiny3.zip", limits=Limits(max_files=1))
    with pytest.raises(CheckpointError, match="aggregate"):
        export_checkpoint(root, tmp_path / "tiny4.zip", limits=Limits(max_total_bytes=5))
    with pytest.raises(CheckpointError, match="size limit"):
        put_asset(root, b"too long", limits=Limits(max_object_bytes=2))
    base = tmp_path / "base.zip"
    export_checkpoint(root, base)
    with pytest.raises(CheckpointError, match="oversized"):
        restore_checkpoint(base, tmp_path / "restore", limits=Limits(max_archive_bytes=10))
    with pytest.raises(CheckpointError, match="manifest"):
        restore_checkpoint(base, tmp_path / "restore2", limits=Limits(max_manifest_bytes=10))


def test_nonempty_restore_destination_is_never_modified(tmp_path):
    root, _, _ = workflow(tmp_path)
    base = tmp_path / "base.zip"
    export_checkpoint(root, base)
    destination = tmp_path / "restore"
    destination.mkdir()
    (destination / "keep").write_text("precious")
    with pytest.raises(CheckpointError, match="absent or empty"):
        restore_checkpoint(base, destination)
    assert list(destination.iterdir()) == [destination / "keep"]
    assert (destination / "keep").read_text() == "precious"


def test_json_references_missing_state_and_duplicate_keys_rejected(tmp_path):
    root, _, _ = workflow(tmp_path)
    (root / "state.json").write_text('{"phase":1,"phase":2}')
    with pytest.raises(CheckpointError, match="Invalid JSON"):
        export_checkpoint(root, tmp_path / "duplicate.zip")
    write_json(root / "state.json", {"protocol": "paper-first-v3", "asset": "objects/not-a-digest"})
    with pytest.raises(CheckpointError, match="Missing/invalid object"):
        export_checkpoint(root, tmp_path / "reference.zip")
    (root / "state.json").unlink()
    with pytest.raises(CheckpointError, match="Missing state.json"):
        export_checkpoint(root, tmp_path / "missing.zip")


def test_put_asset_path_deduplicates_and_rejects_symlink_source(tmp_path):
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF test input")
    root = tmp_path / "workflow"
    digest = put_asset(root, source)
    assert put_asset(root, source.read_bytes()) == digest
    assert list((root / "objects").iterdir()) == [root / "objects" / digest]
    link = tmp_path / "source-link"
    link.symlink_to(source)
    with pytest.raises(CheckpointError, match="Symlinks"):
        put_asset(root, link)


def test_legacy_root_cannot_be_enrolled_as_compact_checkpoint(tmp_path):
    root, _, _ = workflow(tmp_path)
    write_json(root / "state.json", {"protocol": "legacy"})
    with pytest.raises(CheckpointError, match="Only paper-first-v3"):
        export_checkpoint(root, tmp_path / "legacy.zip")
    assert not (tmp_path / "legacy.zip").exists()
