"""Bounded process supervision with cooperative cancellation and verified orphan cleanup."""
from __future__ import annotations

import os
import math
import re
import signal
import subprocess
import threading
import time
from pathlib import Path


class WorkflowCancelled(RuntimeError):
    pass


class CleanupPending(RuntimeError):
    """The previous process group cannot yet be confirmed stopped."""


def process_namespace() -> str:
    """Stable Linux execution fence shared by a supervisor and its children.

    Numeric process IDs (including a missing PID) only have meaning inside this
    boot and PID namespace. Never manufacture a fallback for an old journal or
    an environment whose namespace cannot be verified.
    """
    import hashlib
    import uuid
    from daily_agent.workflow_state import StateCorrupt
    try:
        boot = uuid.UUID(Path('/proc/sys/kernel/random/boot_id').read_text().strip())
        namespace = os.readlink('/proc/self/ns/pid')
        if boot.int == 0 or not re.fullmatch(r'pid:\[[1-9][0-9]*\]', namespace):
            raise ValueError('Invalid boot or PID namespace identity')
    except (OSError, ValueError) as exc:
        raise StateCorrupt('Cannot verify execution namespace; operator review required') from exc
    return hashlib.sha256(f'{boot}\n{namespace}'.encode('ascii')).hexdigest()


def validate_process_identity(identity: dict) -> None:
    """A null description retains launch evidence but never authorizes a signal."""
    if (not isinstance(identity, dict) or type(identity.get("pid")) is not int
            or identity["pid"] <= 1 or type(identity.get("pgid")) is not int
            or identity["pgid"] != identity["pid"] or "description" not in identity
            or identity["description"] is not None and
            (not isinstance(identity["description"], str) or not identity["description"].strip())):
        raise ValueError("Invalid process group identity")


def process_identity(pid: int) -> dict | None:
    """Coarse ps birth/argv evidence; inspection failure is distinct from absence."""
    try:
        result = subprocess.run(["ps", "-ww", "-p", str(pid), "-o", "lstart=", "-o", "command="],
                                capture_output=True, text=True, timeout=3, check=False,
                                env={**os.environ, "LC_ALL": "C"})
        text = result.stdout.strip()
        if not text and not result.stderr.strip() and result.returncode in {0, 1}:
            return None
        if (result.returncode or result.stderr.strip() or len(text.splitlines()) != 1
                or not re.fullmatch(r"\w{3}\s+\w{3}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\s+\d{4}\s+.+", text)):
            raise CleanupPending("Cannot inspect process identity; invalid ps result")
        return {"pid": pid, "description": text, "pgid": os.getpgid(pid)}
    except ProcessLookupError:
        return None
    except (OSError, subprocess.SubprocessError) as exc:
        raise CleanupPending("Cannot inspect process identity; operator review required") from exc


def _group_has_live_members(pgid: int) -> bool:
    try:
        result = subprocess.run(["ps", "-axo", "pgid=,stat="], capture_output=True, text=True, timeout=3,
                                env={**os.environ, "LC_ALL": "C"})
    except (OSError, subprocess.SubprocessError) as exc:
        raise CleanupPending("Cannot inspect process group; operator review required") from exc
    if result.returncode or result.stderr.strip() or not result.stdout.strip():
        raise CleanupPending("Cannot inspect process group; invalid ps result")
    live = False
    for line in result.stdout.splitlines():
        parts = line.split()
        if (len(parts) != 2 or not parts[0].isdecimal()
                or not re.fullmatch(r"[A-Za-z][A-Za-z0-9<+\-]*", parts[1])):
            raise CleanupPending("Cannot inspect process group; malformed ps row")
        if int(parts[0]) == pgid and not parts[1].startswith("Z"):
            live = True
    return live


def stop_group(pid: int, *, grace_seconds: float = 3) -> None:
    if type(pid) is not int or pid <= 1 or not math.isfinite(grace_seconds) or grace_seconds < 0:
        raise ValueError("Invalid process group stop policy")
    def send(signum):
        try:
            os.killpg(pid, signum)
        except ProcessLookupError:
            pass  # The following inspection still has to confirm the group is empty.
        except OSError as exc:
            # macOS may return EPERM for zombie-only groups.
            if _group_has_live_members(pid):
                raise CleanupPending("Cannot signal live process group; operator review required") from exc
    send(signal.SIGTERM)
    deadline = time.monotonic() + grace_seconds
    while _group_has_live_members(pid):
        if time.monotonic() >= deadline:
            break
        time.sleep(.05)
    else:
        return
    send(signal.SIGKILL)
    deadline = time.monotonic() + max(.2, grace_seconds)
    while _group_has_live_members(pid):
        if time.monotonic() >= deadline:
            raise CleanupPending("Process group still has live members after SIGKILL")
        time.sleep(.05)


def inspect_orphan(identity: dict, expected_root: Path | None = None) -> str:
    """Read-only: empty, verified live, or a CleanupPending with the blocking reason."""
    try:
        validate_process_identity(identity)
    except ValueError as exc:
        raise CleanupPending("Invalid cleanup identity; operator review required") from exc
    pid = identity["pid"]
    current = process_identity(pid)
    if not _group_has_live_members(pid):
        return "empty"
    if current is None:
        raise CleanupPending("Process group leader is gone but live members remain; operator review required")
    if identity["description"] is None or any(current.get(key) != identity[key] for key in ("pid", "pgid", "description")):
        raise CleanupPending("Unverified process group still has live members; operator review required")
    if expected_root is not None:
        root = re.escape(str(expected_root))
        command = current["description"]
        if (not re.search(r"(?:^|\s)-m\s+daily_agent\.(?:cli|cloud_workflow|cloud_pilot|production_revision)(?=$|\s)", command)
                or not re.search(rf"(?:^|\s)--root(?:=|\s+)(?:{root}|\"{root}\"|'{root}')(?=$|\s)", command)):
            raise CleanupPending("Orphan does not belong to this workflow; operator review required")
    return "verified"


def stop_verified_orphan(identity: dict | None, expected_root: Path | None = None) -> bool:
    if identity is None or inspect_orphan(identity, expected_root) == "empty":
        return False
    pid = identity["pid"]
    stop_group(pid)
    return True


def run_process(command: list[str], root: Path, timeout: float, *, on_tick=None,
                on_start=None, log_prefix: Path | None = None, heartbeat_seconds: float = 30) -> int:
    if timeout <= 0:
        return 124
    handles = []
    previous_handlers = {}
    process = None
    timed_out = False
    deadline = time.monotonic() + timeout
    def cancelled(signum, frame):
        raise WorkflowCancelled(f"Cancelled by signal {signum}")
    try:
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGTERM, signal.SIGINT):
                previous_handlers[signum] = signal.signal(signum, cancelled)
        options = {}
        if log_prefix is not None:
            log_prefix.parent.mkdir(parents=True, exist_ok=True)
            for name, suffix in (("stdout", ".out.log"), ("stderr", ".err.log")):
                handle = log_prefix.with_suffix(suffix).open("ab", buffering=0)
                handles.append(handle)
                options[name] = handle
        process = subprocess.Popen(command, cwd=root, start_new_session=True, close_fds=True, **options)
        if on_start:
            # Persist the known launch before any fallible external inspection.
            on_start({"pid": process.pid, "pgid": process.pid, "description": None})
        identity = process_identity(process.pid)
        if on_start and identity is not None:
            on_start(identity)
        next_heartbeat = time.monotonic()
        while True:
            # Callbacks (including checkpoint fsync) and identity inspection can
            # consume the remaining budget. Never turn a late exit into success.
            now = time.monotonic()
            if now >= deadline:
                timed_out = True
                return 124
            result = process.poll()
            if result is not None:
                return result
            if on_tick and now >= next_heartbeat:
                on_tick()
                next_heartbeat = now + heartbeat_seconds
            time.sleep(min(.2, max(.001, deadline - time.monotonic())))
    finally:
        # Also clean up on SIGTERM, Ctrl-C, callback/write failure and unexpected exceptions.
        cleanup_error = None
        resource_error = None
        if process is not None:
            try:
                stop_group(process.pid, grace_seconds=3 if timed_out else .2)
            except (Exception, KeyboardInterrupt) as exc:
                cleanup_error = exc if isinstance(exc, CleanupPending) else CleanupPending(f"Cannot confirm process group cleanup: {exc}")
            # Even failed group cleanup must attempt to reap the direct child.
            try:
                process.wait(timeout=5)
            except (Exception, KeyboardInterrupt) as exc:
                if cleanup_error is None:
                    cleanup_error = CleanupPending(f"Cannot reap workflow child: {exc}")
        for signum, handler in previous_handlers.items():
            try:
                signal.signal(signum, handler)
            except Exception as exc:
                resource_error = resource_error or exc
        for handle in handles:
            try:
                handle.close()
            except Exception as exc:
                resource_error = resource_error or exc
        if cleanup_error is not None:
            raise cleanup_error
        if resource_error is not None:
            raise resource_error
