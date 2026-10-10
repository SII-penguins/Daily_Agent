"""Local, bounded base + latest-delta checkpoints for opt-in paper-first-v3.

``export_checkpoint(root, base.zip)`` creates a standalone immutable base.
Later exports with ``base_bundle=base.zip`` contain current small files and only
objects absent from that base. Restore needs the base and ONE latest delta, not
an ever-growing chain. Newly added objects are intentionally repeated in deltas;
choose the base after the large PDFs/source documents are available.

This module neither uploads nor establishes remote durability. The host must
upload the actual bundles, retain their identities, and materialize those exact
bytes for recovery. An empty-directory restore proves only local recovery.
Callers must serialize workflow changes while exporting. Only this workflow's
root belongs here, never legacy state. All non-object files except *.lock are
snapshotted; PDFs and other large data belong in objects/<sha256>. JSON state/queue/accepted string references use that exact path form and are
checked for missing objects. Immutable object payloads are hash-verified but
opaque; the workflow must validate their receipt semantics.
"""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
import zipfile


class CheckpointError(ValueError):
    """Unsafe, corrupt, incomplete, or over-limit checkpoint input."""


@dataclass(frozen=True)
class Limits:
    max_files: int = 20_000
    max_metadata_bytes: int = 16 * 1024 * 1024
    max_manifest_bytes: int = 4 * 1024 * 1024
    max_object_bytes: int = 1024 * 1024 * 1024
    max_total_bytes: int = 4 * 1024 * 1024 * 1024
    max_archive_bytes: int = 4 * 1024 * 1024 * 1024


DEFAULT_LIMITS = Limits()
_MANIFEST = "checkpoint-manifest.json"
_FORMAT = "paper-first-v3-checkpoint-v1"
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_CHUNK = 1024 * 1024


def _json(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")


def _decode(data, label):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise CheckpointError(f"Duplicate JSON key in {label}")
            value[key] = item
        return value
    try:
        return json.loads(data, object_pairs_hook=unique,
                          parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise CheckpointError(f"Invalid JSON: {label}") from exc


def _path(path):
    """Do not resolve away symlinks before checking them."""
    path = Path(os.path.abspath(path))
    for part in (path, *path.parents):
        if part.is_symlink():
            raise CheckpointError(f"Symlinks are not allowed: {part}")
    return path


def _name(name):
    if (not isinstance(name, str) or not name or len(name) > 1024
            or "\\" in name or ":" in name or any(ord(c) < 32 for c in name)
            or name.startswith("/") or name.endswith("/")):
        raise CheckpointError(f"Unsafe archive path: {name!r}")
    parts = name.split("/")
    if len(parts) > 32 or any(p in {"", ".", ".."} for p in parts):
        raise CheckpointError(f"Unsafe archive path: {name!r}")
    return name


def _size(value):
    if type(value) is not int or value < 0:
        raise CheckpointError("Invalid file size")
    return value


def _digest(value):
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise CheckpointError("Invalid object SHA-256")
    return value


def _copy(source, destination=None, *, limit):
    digest = hashlib.sha256()
    count = 0
    while chunk := source.read(_CHUNK):
        count += len(chunk)
        if count > limit:
            raise CheckpointError("File exceeds checkpoint size limit")
        digest.update(chunk)
        if destination is not None:
            destination.write(chunk)
    return digest.hexdigest(), count


def _check_refs(value, objects, label):
    # Only exact reference strings count; prose containing a path is not a ref.
    pending = [value]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
        elif isinstance(value, str) and value.startswith("objects/"):
            digest = value[8:]
            if not _SHA.fullmatch(digest) or digest not in objects:
                raise CheckpointError(f"Missing/invalid object reference in {label}: {value}")


def _validate_metadata(metadata, objects):
    if "state.json" not in metadata:
        raise CheckpointError("Missing state.json")
    for name, data in metadata.items():
        if name.endswith(".json"):
            value = _decode(data, name)
            if name == "state.json" and (not isinstance(value, dict) or value.get("protocol") != "paper-first-v3"):
                raise CheckpointError("Only paper-first-v3 workflow state is supported")
            _check_refs(value, objects, name)


def _validate_manifest(manifest, limits):
    if (not isinstance(manifest, dict)
            or set(manifest) != {"format", "base_sha256", "files", "objects", "included_objects"}
            or manifest["format"] != _FORMAT):
        raise CheckpointError("Invalid checkpoint manifest")
    if manifest["base_sha256"] is not None:
        _digest(manifest["base_sha256"])
    files, objects, included = (manifest[k] for k in ("files", "objects", "included_objects"))
    if not isinstance(files, dict) or not isinstance(objects, dict) or not isinstance(included, list):
        raise CheckpointError("Invalid checkpoint inventory")
    if len(files) + len(objects) > limits.max_files or "state.json" not in files:
        raise CheckpointError("Missing state.json or too many files")
    for name, info in files.items():
        _name(name)
        if name == _MANIFEST or name == "objects" or name.startswith("objects/") or name.endswith(".lock"):
            raise CheckpointError(f"Reserved snapshot path: {name}")
        if not isinstance(info, dict) or set(info) != {"sha256", "size"}:
            raise CheckpointError("Invalid snapshot file identity")
        _digest(info["sha256"])
        _size(info["size"])
        if any(str(p) in files for p in PurePosixPath(name).parents if str(p) != "."):
            raise CheckpointError("Conflicting file/directory paths")
    for digest, size in objects.items():
        _digest(digest)
        if _size(size) > limits.max_object_bytes:
            raise CheckpointError("Object exceeds checkpoint size limit")
    if any(not isinstance(d, str) or d not in objects for d in included) or len(set(included)) != len(included):
        raise CheckpointError("Invalid included object inventory")
    if included != sorted(included):
        raise CheckpointError("Object inventory must be sorted")
    if manifest["base_sha256"] is None and set(included) != set(objects):
        raise CheckpointError("Standalone base is missing objects")
    metadata_size = sum(info["size"] for info in files.values())
    if metadata_size > limits.max_metadata_bytes or metadata_size + sum(objects.values()) > limits.max_total_bytes:
        raise CheckpointError("Checkpoint exceeds aggregate size limit")
    return manifest


@contextmanager
def _read_bundle(path, limits):
    """Validate before use and keep the same open archive for extraction."""
    path = _path(path)
    if not path.is_file() or path.stat().st_size > limits.max_archive_bytes:
        raise CheckpointError("Missing or oversized checkpoint bundle")
    try:
        with path.open("rb") as raw:
            archive_hash, _ = _copy(raw, limit=limits.max_archive_bytes)
            raw.seek(0)
            with zipfile.ZipFile(raw) as archive:
                entries = archive.infolist()
                if len(entries) > limits.max_files + 1:
                    raise CheckpointError("Too many archive entries")
                names = set()
                for entry in entries:
                    name = _name(entry.filename)
                    mode = entry.external_attr >> 16
                    if (name in names or entry.is_dir() or stat.S_IFMT(mode) not in (0, stat.S_IFREG)
                            or entry.flag_bits & 1 or entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
                        raise CheckpointError("Duplicate, nonregular, encrypted, or unsupported archive entry")
                    names.add(name)
                if _MANIFEST not in names or archive.getinfo(_MANIFEST).file_size > limits.max_manifest_bytes:
                    raise CheckpointError("Missing or oversized checkpoint manifest")
                manifest = _validate_manifest(_decode(archive.read(_MANIFEST), _MANIFEST), limits)
                expected = set(manifest["files"]) | {f"objects/{d}" for d in manifest["included_objects"]} | {_MANIFEST}
                if names != expected:
                    raise CheckpointError("Missing or unexpected archive files")
                metadata = {}
                for name in sorted(names - {_MANIFEST}):
                    if name.startswith("objects/"):
                        digest = name[8:]
                        size = manifest["objects"][digest]
                    else:
                        digest, size = (manifest["files"][name][k] for k in ("sha256", "size"))
                    if archive.getinfo(name).file_size != size:
                        raise CheckpointError(f"Archive file size mismatch: {name}")
                    if name.startswith("objects/"):
                        with archive.open(name) as source:
                            actual, count = _copy(source, limit=size)
                    else:
                        metadata[name] = archive.read(name)
                        actual, count = hashlib.sha256(metadata[name]).hexdigest(), len(metadata[name])
                    if actual != digest or count != size:
                        raise CheckpointError(f"Corrupt checkpoint file: {name}")
                _validate_metadata(metadata, manifest["objects"])
                yield archive, manifest, metadata, archive_hash
    except (OSError, zipfile.BadZipFile, RuntimeError, EOFError, NotImplementedError) as exc:
        raise CheckpointError(f"Cannot read checkpoint: {path.name}") from exc


def _base(stack, base_bundle, limits):
    if base_bundle is None:
        return None
    base = stack.enter_context(_read_bundle(base_bundle, limits))
    if base[1]["base_sha256"] is not None:
        raise CheckpointError("The base must be standalone, not a delta")
    return base


def put_asset(workflow_dir, content, *, limits=DEFAULT_LIMITS):
    """Copy bytes or a local regular file into immutable objects/<sha256>."""
    root = _path(workflow_dir)
    root.mkdir(parents=True, exist_ok=True)
    objects = _path(root / "objects")
    objects.mkdir(exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=objects, prefix=".asset-", delete=False) as target:
            temporary = Path(target.name)
            if isinstance(content, bytes):
                if len(content) > limits.max_object_bytes:
                    raise CheckpointError("Object exceeds checkpoint size limit")
                target.write(content)
                digest = hashlib.sha256(content).hexdigest()
            else:
                source = _path(content)
                if not source.is_file():
                    raise CheckpointError("Asset source must be a regular file")
                with source.open("rb") as stream:
                    digest, _ = _copy(stream, target, limit=limits.max_object_bytes)
            target.flush()
            os.fsync(target.fileno())
        destination = _path(objects / digest)
        try:
            os.link(temporary, destination)
        except FileExistsError:
            if not destination.is_file():
                raise CheckpointError("Object conflicts with a non-file")
            with destination.open("rb") as stream:
                existing, _ = _copy(stream, limit=limits.max_object_bytes)
            if existing != digest:
                raise CheckpointError("Existing immutable object is corrupt")
        return digest
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _snapshot(root, limits):
    files, objects, metadata, paths = {}, {}, {}, {}
    total = 0
    metadata_total = 0
    for directory, dirs, names in os.walk(root, followlinks=False):
        for name in sorted(dirs + names):
            path = _path(Path(directory) / name)
            relative = path.relative_to(root).as_posix()
            _name(relative)
            if path.is_dir():
                if relative.startswith("objects/"):
                    raise CheckpointError("Objects directory cannot contain subdirectories")
                continue
            if not path.is_file():
                raise CheckpointError("Only regular workflow files are allowed")
            if relative.endswith(".lock"):
                continue
            if len(files) + len(objects) >= limits.max_files:
                raise CheckpointError("Too many workflow files")
            if relative.startswith("objects/"):
                digest = _digest(relative[8:])
                with path.open("rb") as stream:
                    actual, count = _copy(stream, limit=limits.max_object_bytes)
                if actual != digest:
                    raise CheckpointError(f"Corrupt workflow object: {digest}")
                objects[digest], paths[digest] = count, path
                total += count
            else:
                remaining = limits.max_metadata_bytes - metadata_total
                with path.open("rb") as stream:
                    data = stream.read(remaining + 1)
                if len(data) > remaining:
                    raise CheckpointError("Mutable files exceed metadata size limit; use objects/")
                metadata[relative] = data
                metadata_total += len(data)
                files[relative] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
                total += len(data)
            if total > limits.max_total_bytes:
                raise CheckpointError("Workflow exceeds aggregate size limit")
    _validate_metadata(metadata, objects)
    return files, objects, metadata, paths


def _entry(name):
    entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    entry.create_system = 3
    entry.external_attr = (stat.S_IFREG | 0o600) << 16
    # Stored bytes make large already-compressed PDFs/PNGs cheap and deterministic.
    entry.compress_type = zipfile.ZIP_STORED
    return entry


def export_checkpoint(workflow_dir, bundle_path, *, base_bundle=None, limits=DEFAULT_LIMITS):
    """Create a new immutable local bundle; return its manifest (not a receipt).

    With a base, only objects absent from it are written. The full current
    inventory means deletions are respected when restoring the latest delta.
    Referenced objects must remain present locally even if they are in the base.
    Existing output paths are never replaced.
    """
    root, destination = _path(workflow_dir), _path(bundle_path)
    if not root.is_dir() or destination.is_relative_to(root):
        raise CheckpointError("Use an existing workflow root and an output outside it")
    if destination.exists():
        raise CheckpointError("Checkpoint output already exists")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with ExitStack() as stack:
            base = _base(stack, base_bundle, limits)
            files, objects, metadata, paths = _snapshot(root, limits)
            old_objects = base[1]["objects"] if base else {}
            for digest in objects.keys() & old_objects.keys():
                if objects[digest] != old_objects[digest]:
                    raise CheckpointError("Base object identity conflict")
            included = sorted(objects.keys() - old_objects.keys())
            manifest = _validate_manifest({"format": _FORMAT, "base_sha256": base[3] if base else None,
                "files": files, "objects": objects, "included_objects": included}, limits)
            manifest_bytes = _json(manifest)
            if len(manifest_bytes) > limits.max_manifest_bytes:
                raise CheckpointError("Checkpoint manifest exceeds size limit")
            with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".checkpoint-", delete=False) as raw:
                temporary = Path(raw.name)
                with zipfile.ZipFile(raw, "w", allowZip64=True) as archive:
                    archive.writestr(_entry(_MANIFEST), manifest_bytes)
                    for name in sorted(metadata):
                        archive.writestr(_entry(name), metadata[name])
                    for digest in included:
                        with _path(paths[digest]).open("rb") as source, archive.open(_entry(f"objects/{digest}"), "w", force_zip64=True) as target:
                            actual, count = _copy(source, target, limit=limits.max_object_bytes)
                        if actual != digest or count != objects[digest]:
                            raise CheckpointError("Object changed during export")
                if raw.tell() > limits.max_archive_bytes:
                    raise CheckpointError("Bundle exceeds archive size limit")
                raw.flush()
                os.fsync(raw.fileno())
            os.link(temporary, destination)
            return manifest
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def restore_checkpoint(bundle_path, destination, *, base_bundle=None, limits=DEFAULT_LIMITS):
    """Verify base + latest delta, then atomically populate an empty directory.

    No partial destination is left on a validation/copy failure. Existing data,
    symlinks, wrong bases, missing/corrupt objects and unsafe ZIP entries fail.
    """
    destination = _path(destination)
    if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
        raise CheckpointError("Restore destination must be absent or empty")
    staging = None
    try:
        with ExitStack() as stack:
            base = _base(stack, base_bundle, limits)
            current = stack.enter_context(_read_bundle(bundle_path, limits))
            archive, manifest, metadata, _ = current
            if manifest["base_sha256"] != (base[3] if base else None):
                raise CheckpointError("Missing or wrong base bundle")
            base_objects = base[1]["objects"] if base else {}
            required = set(manifest["objects"]) - set(manifest["included_objects"])
            if not required <= base_objects.keys():
                raise CheckpointError("Base is missing required objects")
            if set(manifest["included_objects"]) & base_objects.keys():
                raise CheckpointError("Delta redundantly includes base objects")
            for digest in required:
                if manifest["objects"][digest] != base_objects[digest]:
                    raise CheckpointError("Base object identity conflict")
            destination.parent.mkdir(parents=True, exist_ok=True)
            staging = Path(tempfile.mkdtemp(dir=destination.parent, prefix=".restore-"))
            for name, data in metadata.items():
                target = staging / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            (staging / "objects").mkdir(exist_ok=True)
            for digest, size in manifest["objects"].items():
                source_archive = base[0] if digest in required else archive
                with source_archive.open(f"objects/{digest}") as source, (staging / "objects" / digest).open("xb") as target:
                    actual, count = _copy(source, target, limit=size)
                if actual != digest or count != size:
                    raise CheckpointError("Object changed during restore")
            _path(destination)
            if destination.exists() and (not destination.is_dir() or any(destination.iterdir())):
                raise CheckpointError("Restore destination changed during recovery")
            os.replace(staging, destination)
            staging = None
            return manifest
    finally:
        if staging is not None:
            shutil.rmtree(staging)
