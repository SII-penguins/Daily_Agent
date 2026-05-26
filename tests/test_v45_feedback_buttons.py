from datetime import date
from threading import Thread
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from daily_agent.config import load_config
from daily_agent.feedback.server import make_feedback_handler, record_feedback_request
from daily_agent.models import ApprovedItem, MaterialRecord, RunStatus
from daily_agent.rendering.html import render_daily_html
from daily_agent.rendering.markdown import render_daily_markdown
from daily_agent.storage import load_feedback_events, write_published_index


def _approved_paper():
    material = MaterialRecord(
        key="arxiv:2401.00001",
        source="arxiv",
        item_type="paper",
        title="Quantum paper",
        url="https://arxiv.org/abs/2401.00001",
        pdf_url="https://arxiv.org/pdf/2401.00001",
        abstract="abstract",
        tags=["quantum_ai"],
    )
    return ApprovedItem(
        key=material.key,
        item_type="paper",
        title=material.title,
        source=material.source,
        url=material.url,
        final_fields={
            "problem": "p",
            "method": "m",
            "method_steps": ["s"],
            "key_result": "r",
            "possible_use_or_impact": "i",
            "limitations": "l",
        },
        material=material,
    )


def _config(tmp_path):
    config = load_config("/Users/wuzixie/Daily_Agent")
    object.__setattr__(config, "root", tmp_path)
    return config


def test_html_feedback_block_contains_local_post_buttons():
    html = render_daily_html([_approved_paper()], date(2026, 5, 27), RunStatus())

    assert '<form class="feedback-actions" method="post" action="http://127.0.0.1:8765/feedback">' in html
    assert 'name="date" value="2026-05-27"' in html
    assert 'name="rank" value="1"' in html
    assert '<button type="submit" name="signal" value="like">有用</button>' in html
    assert '<button type="submit" name="signal" value="dislike">不相关</button>' in html
    assert "onclick" not in html
    assert "javascript:" not in html


def test_markdown_feedback_line_contains_local_click_links_for_feishu_docs():
    markdown = render_daily_markdown([_approved_paper()], date(2026, 5, 27), RunStatus())

    assert "[有用](http://127.0.0.1:8765/feedback?date=2026-05-27&rank=1&signal=like)" in markdown
    assert "[不相关](http://127.0.0.1:8765/feedback?date=2026-05-27&rank=1&signal=dislike)" in markdown


def test_record_feedback_request_writes_idempotent_html_button_event(tmp_path):
    config = _config(tmp_path)
    approved = _approved_paper()
    write_published_index(config, [approved], date(2026, 5, 27))

    first = record_feedback_request(config, {"date": "2026-05-27", "rank": "1", "signal": "like"})
    second = record_feedback_request(config, {"date": "2026-05-27", "rank": "1", "signal": "like"})

    events = load_feedback_events(config)
    assert len(events) == 1
    assert first.event_id == second.event_id == events[0].event_id
    assert events[0].origin == "html_button"
    assert events[0].channel == "html"
    assert events[0].external_id == "2026-05-27:1:like"
    assert events[0].key == "arxiv:2401.00001"


def test_feedback_http_handler_accepts_form_post(tmp_path):
    from http.server import ThreadingHTTPServer

    config = _config(tmp_path)
    write_published_index(config, [_approved_paper()], date(2026, 5, 27))
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_feedback_handler(config))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        body = urlencode({"date": "2026-05-27", "rank": "1", "signal": "dislike"}).encode("utf-8")
        request = Request(
            f"http://{host}:{port}/feedback",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urlopen(request, timeout=3) as response:
            html = response.read().decode("utf-8")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)

    assert response.status == 200
    assert "反馈已记录" in html
    events = load_feedback_events(config)
    assert [(event.rank, event.signal, event.origin) for event in events] == [(1, "dislike", "html_button")]
