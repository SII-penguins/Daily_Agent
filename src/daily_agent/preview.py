from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen

from daily_agent.config import AppConfig
from daily_agent.feedback.server import DEFAULT_FEEDBACK_HOST, DEFAULT_FEEDBACK_PORT, make_feedback_handler

DEFAULT_REPORT_PORT = 8766


@dataclass(frozen=True)
class PreviewLinks:
    html_url: str
    markdown_url: str | None
    feedback_url: str

    def to_dict(self) -> dict[str, str | None]:
        return {
            "html_url": self.html_url,
            "markdown_url": self.markdown_url,
            "feedback_url": self.feedback_url,
        }


def preview_report_links(
    config: AppConfig,
    *,
    report: str = "latest",
    host: str = DEFAULT_FEEDBACK_HOST,
    report_port: int = DEFAULT_REPORT_PORT,
    feedback_port: int = DEFAULT_FEEDBACK_PORT,
) -> PreviewLinks:
    html_path = _select_html_report(config, report)
    markdown_path = html_path.with_suffix(".md")
    markdown_url = _file_url(config.root, markdown_path, host, report_port) if markdown_path.exists() else None
    return PreviewLinks(
        html_url=_file_url(config.root, html_path, host, report_port),
        markdown_url=markdown_url,
        feedback_url=f"http://{host}:{int(feedback_port)}/",
    )


def start_preview_server(
    config: AppConfig,
    *,
    host: str = DEFAULT_FEEDBACK_HOST,
    report_port: int = DEFAULT_REPORT_PORT,
    feedback_port: int = DEFAULT_FEEDBACK_PORT,
    report: str = "latest",
) -> dict[str, Any]:
    links = preview_report_links(config, report=report, host=host, report_port=report_port, feedback_port=feedback_port)
    already_running = _url_ok(links.html_url) and _url_ok(links.feedback_url)
    pid = None
    if not already_running:
        config.logs_dir.mkdir(parents=True, exist_ok=True)
        log_path = config.logs_dir / "preview-server.log"
        log_file = log_path.open("a", encoding="utf-8")
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "daily_agent.cli",
                "preview",
                "serve",
                "--root",
                str(config.root),
                "--host",
                host,
                "--report-port",
                str(int(report_port)),
                "--feedback-port",
                str(int(feedback_port)),
            ],
            cwd=str(config.root),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        pid = process.pid
        _wait_until_ready(links.html_url)
    return {
        "pid": pid,
        "already_running": already_running,
        **links.to_dict(),
    }


def serve_preview(
    config: AppConfig,
    *,
    host: str = DEFAULT_FEEDBACK_HOST,
    report_port: int = DEFAULT_REPORT_PORT,
    feedback_port: int = DEFAULT_FEEDBACK_PORT,
) -> None:
    report_server = ThreadingHTTPServer((host, int(report_port)), _static_handler(config.root))
    feedback_server = ThreadingHTTPServer((host, int(feedback_port)), make_feedback_handler(config))
    feedback_thread = Thread(target=feedback_server.serve_forever, daemon=True)
    feedback_thread.start()
    try:
        report_server.serve_forever()
    finally:
        report_server.shutdown()
        report_server.server_close()
        feedback_server.shutdown()
        feedback_server.server_close()
        feedback_thread.join(timeout=3)


def _static_handler(root: Path):
    class QuietStaticHandler(SimpleHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:
            return

    return partial(QuietStaticHandler, directory=str(root))


def _select_html_report(config: AppConfig, report: str) -> Path:
    reports_dir = config.reports_dir
    if report not in {"latest", "daily", "weekly"}:
        raise ValueError("report must be latest, daily, or weekly")
    if report == "daily":
        candidates = [path for path in reports_dir.glob("daily-agent-????-??-??.html") if path.is_file()]
    elif report == "weekly":
        candidates = [path for path in reports_dir.glob("daily-agent-*-W??.html") if path.is_file()]
    else:
        candidates = [path for path in reports_dir.glob("daily-agent-*.html") if path.is_file()]
    if not candidates:
        raise ValueError(f"No {report} HTML report found under {reports_dir}")
    return max(candidates, key=lambda path: path.stat().st_mtime)


def _file_url(root: Path, path: Path, host: str, port: int) -> str:
    relative = path.resolve().relative_to(root.resolve()).as_posix()
    return f"http://{host}:{int(port)}/{quote(relative)}"


def _wait_until_ready(url: str, timeout_seconds: float = 3.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if _url_ok(url):
            return
        time.sleep(0.1)


def _url_ok(url: str) -> bool:
    try:
        request = Request(url, method="HEAD")
        with urlopen(request, timeout=0.5) as response:
            return 200 <= response.status < 400
    except Exception:
        return False
