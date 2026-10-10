"""Durable local workflow records. Reads never repair or discard evidence."""
from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
import tempfile
from pathlib import Path


class StateCorrupt(RuntimeError):
    pass


class WorkflowBusy(RuntimeError):
    pass


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON member")
        value[key] = item
    return value


def _reject_constant(value):
    raise ValueError("Non-finite JSON number")


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Non-finite JSON number")
    return number


def read_json(path: Path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object,
                           parse_constant=_reject_constant, parse_float=_finite_float)
        if value is None:
            raise StateCorrupt(f"Null workflow record: {path}")
        return value
    except FileNotFoundError:
        return None
    except (ValueError, UnicodeError) as exc:
        raise StateCorrupt(f"Invalid workflow record: {path}") from exc


def _recovery_scopes(path):
    """An explicitly scoped recovery never grants old-issue mutation rights."""
    lexical = Path(path).absolute()
    resolved = lexical.resolve()
    found = []
    for ancestor in dict.fromkeys((lexical, *lexical.parents, resolved, *resolved.parents)):
        marker = ancestor / '.daily-agent-recovery-scope.json'
        if marker.exists() or marker.is_symlink():
            if marker.is_symlink():
                raise StateCorrupt('Recovery issue scope cannot be a symlink')
            record = read_json(marker)
            from daily_agent.claim_quarantine import validate_scope
            if (not isinstance(record, dict) or record.get('schema_version') != 1
                    or not isinstance(record.get('quarantined_jobs'), list)
                    or not record['quarantined_jobs']):
                raise StateCorrupt('Invalid scoped recovery evidence')
            scope = validate_scope(record.get('scope'), require_future=False)
            found.append((ancestor, scope))
    return found


def assert_scope_admission(root):
    from datetime import datetime, timezone
    for _, scope in _recovery_scopes(root):
        if datetime.now(timezone.utc) >= datetime.fromisoformat(scope['deadline']):
            raise StateCorrupt('Authorized fresh-issue scope deadline expired; new admission blocked')


def assert_issue_allowed(root, issue_date, *, admission=True):
    if admission: assert_scope_admission(root)
    for _, scope in _recovery_scopes(root):
        if str(issue_date) != scope['issue_date']:
            raise StateCorrupt('Recovery permits only the newly authorized issue; old issues remain fenced')


def _assert_scoped_path(path):
    import re
    lexical = Path(path).absolute()
    resolved = lexical.resolve()
    for root, scope in _recovery_scopes(path):
        for candidate in (lexical, resolved):
            if not candidate.is_relative_to(root):
                continue
            relative = candidate.relative_to(root)
            if relative.as_posix() == '.daily-agent-recovery-scope.json':
                raise StateCorrupt('Recovery issue scope is immutable')
            for part in relative.parts:
                match = re.search(r'(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)', part)
                if match and match[1] != scope['issue_date']:
                    raise StateCorrupt('Historical issue path stays immutable under scoped recovery')
        boundary = root / '.daily-agent-boundary.json'
        if not boundary.exists() and lexical != boundary and resolved != boundary:
            raise StateCorrupt('Scoped recovery requires a durable batch intent before mutation')


def assert_mutation_allowed(path: Path) -> None:
    """A restored filesystem does not prove old executor/worker ownership ended.

    This check applies before durable writes, even when the caller bypasses a CLI.
    The recovery module alone writes/removes the fence after exact host review.
    """
    lexical = Path(path).absolute()
    resolved = lexical.resolve()
    for ancestor in dict.fromkeys((lexical, *lexical.parents, resolved, *resolved.parents)):
        marker = ancestor / '.daily-agent-recovery.json'
        if marker.exists() or marker.is_symlink():
            raise StateCorrupt('Recovered state is fenced; reconcile executor, claims, delivery and archive ownership before mutation')
        boundary_path = ancestor / '.daily-agent-boundary.json'
        if boundary_path.exists() or boundary_path.is_symlink():
            boundary = read_json(boundary_path)
            if (not isinstance(boundary, dict) or boundary.get('phase') not in {'intent','ready','executing','settled'}
                    or boundary.get('state_root') != str(ancestor.resolve())):
                raise StateCorrupt('Invalid or relocated durable batch marker; operator reconciliation required')
            # Only the bounded-work controller may advance its marker. Other
            # state cannot change until the intent is remotely verified + begun.
            if lexical != boundary_path and resolved != boundary_path.resolve() and boundary['phase'] != 'executing':
                raise StateCorrupt('Durable batch is not executing; verify a remote intent checkpoint and begin first')
    _assert_scoped_path(path)


def atomic_bytes(path: Path, content: bytes) -> None:
    assert_mutation_allowed(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        # fsync the directory too, so rename survives a power loss on supported filesystems.
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def atomic_json(path: Path, value) -> None:
    if isinstance(value, dict):
        for _, scope in _recovery_scopes(path):
            from datetime import datetime
            for key in ('deadline', 'issue_deadline'):
                if key in value:
                    try:
                        deadline = datetime.fromisoformat(value[key])
                        if deadline.utcoffset() is None or deadline > datetime.fromisoformat(scope['deadline']):
                            raise ValueError('Beyond authorized scope')
                    except (TypeError, ValueError) as exc:
                        raise StateCorrupt('Issue deadline cannot exceed authorized recovery scope') from exc
            for key in ('date', 'issue_date'):
                day = value.get(key)
                if isinstance(day, str) and len(day) == 10 and day != scope['issue_date']:
                    raise StateCorrupt('Historical issue record cannot be changed under scoped recovery')
    atomic_bytes(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8"))


@contextlib.contextmanager
def exclusive_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise WorkflowBusy(f"Workflow is active; refusing concurrent mutation: {path}") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
