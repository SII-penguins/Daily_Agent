from __future__ import annotations

import subprocess
from pathlib import Path


def send_via_cc_connect(
    path: Path | None,
    message: str | None = None,
    *,
    project: str | None = None,
    session: str | None = None,
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
    subprocess.run(command, check=True)
