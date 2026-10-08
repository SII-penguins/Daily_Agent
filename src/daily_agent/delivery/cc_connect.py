from __future__ import annotations

import subprocess
from pathlib import Path
from daily_agent.workflow_runtime import run_process


def send_via_cc_connect(
    path: Path | None,
    message: str | None = None,
    *,
    project: str | None = None,
    session: str | None = None,
    timeout_seconds: float = 60,
) -> None:
    command = ["cc-connect", "send"]
    if path:
        command.extend(["--file", str(path)])
    if message:
        command.extend(["--message", message])
    if project:
        command.extend(["--project", project])
    if session:
        command.extend(["--session", session])
    if not path and not message:
        raise ValueError("cc-connect delivery requires a file or message")
    code = run_process(command, Path.cwd(), timeout_seconds)
    if code == 124:
        raise subprocess.TimeoutExpired(command, timeout_seconds)
    if code:
        raise subprocess.CalledProcessError(code, command)
