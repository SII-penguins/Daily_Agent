from __future__ import annotations

from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

from daily_agent.config import AppConfig
from daily_agent.feedback.ingest import build_feedback_event
from daily_agent.models import FeedbackEvent
from daily_agent.storage import load_feedback_events, resolve_published_item, upsert_feedback_event


DEFAULT_FEEDBACK_HOST = "127.0.0.1"
DEFAULT_FEEDBACK_PORT = 8765


def feedback_form_action(host: str = DEFAULT_FEEDBACK_HOST, port: int = DEFAULT_FEEDBACK_PORT) -> str:
    return f"http://{host}:{port}/feedback"


def feedback_url(run_date, rank: int, signal: str, host: str = DEFAULT_FEEDBACK_HOST, port: int = DEFAULT_FEEDBACK_PORT) -> str:
    query = urlencode({"date": str(run_date), "rank": int(rank), "signal": signal})
    return f"{feedback_form_action(host, port)}?{query}"


def record_feedback_request(config: AppConfig, params: dict[str, str]) -> FeedbackEvent:
    date_selector = str(params.get("date") or "latest")
    rank = _parse_rank(params.get("rank"))
    signal = str(params.get("signal") or "")
    if signal not in {"like", "dislike"}:
        raise ValueError(f"Unsupported feedback signal: {signal}")
    run_date, item = resolve_published_item(config, date_selector, rank)
    note = str(params.get("note") or f"html button {signal}")
    external_id = f"{run_date}:{rank}:{signal}"
    event = build_feedback_event(
        item=item,
        run_date=run_date,
        rank=rank,
        signal=signal,
        channel="html",
        keywords=[],
        note=note,
        origin="html_button",
        external_id=external_id,
    )
    upsert_feedback_event(config, event)
    return _load_recorded_event(config, external_id, rank, signal)


def make_feedback_handler(config: AppConfig) -> type[BaseHTTPRequestHandler]:
    class FeedbackHandler(BaseHTTPRequestHandler):
        server_version = "DailyAgentFeedback/1.0"

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path in {"", "/"}:
                self._send_html(200, _home_page())
                return
            if parsed.path == "/health":
                self._send_text(200, "ok\n")
                return
            if parsed.path != "/feedback":
                self._send_html(404, _error_page("Not found"))
                return
            self._handle_feedback(_flatten_query(parse_qs(parsed.query, keep_blank_values=True)))

        def do_HEAD(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path in {"", "/", "/health"}:
                self._send_head(200, "text/plain; charset=utf-8")
                return
            if parsed.path == "/feedback":
                self._send_head(405, "text/plain; charset=utf-8")
                return
            self._send_head(404, "text/plain; charset=utf-8")

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path != "/feedback":
                self._send_html(404, _error_page("Not found"))
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw_body = self.rfile.read(length).decode("utf-8", errors="replace")
            self._handle_feedback(_flatten_query(parse_qs(raw_body, keep_blank_values=True)))

        def _handle_feedback(self, params: dict[str, str]) -> None:
            try:
                event = record_feedback_request(config, params)
            except Exception as exc:
                self._send_html(400, _error_page(str(exc)))
                return
            self._send_html(200, _success_page(event))

        def _send_text(self, status: int, body: str) -> None:
            payload = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _send_html(self, status: int, body: str) -> None:
            payload = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _send_head(self, status: int, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args) -> None:
            return

    return FeedbackHandler


def serve_feedback(config: AppConfig, host: str = DEFAULT_FEEDBACK_HOST, port: int = DEFAULT_FEEDBACK_PORT) -> None:
    server = ThreadingHTTPServer((host, int(port)), make_feedback_handler(config))
    try:
        server.serve_forever()
    finally:
        server.server_close()


def _parse_rank(value: str | None) -> int:
    try:
        rank = int(str(value or ""))
    except ValueError as exc:
        raise ValueError(f"Invalid feedback rank: {value}") from exc
    if rank <= 0:
        raise ValueError(f"Invalid feedback rank: {value}")
    return rank


def _flatten_query(values: dict[str, list[str]]) -> dict[str, str]:
    return {key: items[-1] for key, items in values.items() if items}


def _load_recorded_event(config: AppConfig, external_id: str, rank: int, signal: str) -> FeedbackEvent:
    for event in reversed(load_feedback_events(config)):
        if event.origin == "html_button" and event.external_id == external_id and event.rank == rank and event.signal == signal:
            return event
    raise ValueError("Feedback event was not recorded")


def _home_page() -> str:
    return _page("Daily Agent 反馈服务", "<p>服务已启动。回到日报 HTML 或飞书文档点击反馈按钮即可记录反馈。</p>")


def _success_page(event: FeedbackEvent) -> str:
    signal = "有用" if event.signal == "like" else "不相关"
    body = (
        "<p>反馈已记录。</p>"
        f"<p>日期：{escape(event.run_date)}；条目：第 {event.rank} 条；反馈：{escape(signal)}</p>"
        f"<p>{escape(event.title)}</p>"
    )
    return _page("反馈已记录", body)


def _error_page(message: str) -> str:
    return _page("反馈记录失败", f"<p>{escape(message)}</p>")


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{escape(title)}</title>"
        "<style>body{font-family:-apple-system,BlinkMacSystemFont,\"Segoe UI\",sans-serif;line-height:1.6;max-width:720px;margin:40px auto;padding:0 18px;color:#20242a}"
        "a{color:#315efb}</style></head><body>"
        f"<h1>{escape(title)}</h1>{body}"
        "</body></html>"
    )
