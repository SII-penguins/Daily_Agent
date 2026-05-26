from __future__ import annotations

import subprocess
from pathlib import Path


def send_via_cc_connect(path: Path, message: str | None = None) -> None:
    command = ["cc-connect", "send", "--file", str(path)]
    if message:
        command.extend(["--message", message])
    subprocess.run(command, check=True)
