from pathlib import Path
import httpx

from daily_agent.config import load_config
from daily_agent.connectors.paper_text import enrich_paper_texts
from daily_agent.models import MaterialRecord


class _StreamResponse:
    status_code = 200

    def __init__(self, chunks):
        self.chunks = chunks
        self.read_count = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def raise_for_status(self):
        return None

    def iter_bytes(self):
        for chunk in self.chunks:
            self.read_count += 1
            yield chunk


class _Client:
    def __init__(self, response):
        self.response = response

    def stream(self, method, url):
        assert method == "GET"
        assert url == "https://paper.test/large.pdf"
        return self.response


def test_download_limited_stops_reading_after_byte_budget():
    from daily_agent.connectors.paper_text import _download_limited

    response = _StreamResponse([b"1234567890", b"abcdefghij", b"SHOULD_NOT_READ"])
    content = _download_limited(_Client(response), "https://paper.test/large.pdf", max_bytes=15)

    assert content == b"1234567890abcde"
    assert response.read_count == 2


def test_download_limited_stops_reading_after_deadline(monkeypatch):
    from daily_agent.connectors.paper_text import _download_limited

    response = _StreamResponse([b"first", b"second", b"SHOULD_NOT_READ"])
    monotonic_values = iter([0.0, 6.0])

    monkeypatch.setattr("daily_agent.connectors.paper_text.time.monotonic", lambda: next(monotonic_values))

    content = _download_limited(_Client(response), "https://paper.test/large.pdf", max_bytes=100, deadline=5.0)

    assert content == b"firstsecond"
    assert response.read_count == 2


def test_download_limited_returns_none_for_blocked_status():
    from daily_agent.connectors.paper_text import _download_limited

    response = _StreamResponse([b"unused"])
    response.status_code = 403

    assert _download_limited(_Client(response), "https://paper.test/large.pdf", max_bytes=15) is None
    assert response.read_count == 0


def test_enrich_paper_texts_limits_urls_per_paper(tmp_path, monkeypatch):
    config = load_config(str(Path(__file__).resolve().parents[1]))
    object.__setattr__(config, "root", tmp_path)
    config.sources["paper_text"] = {
        "enabled": True,
        "max_papers_per_run": 1,
        "max_urls_per_paper": 2,
        "html_fallback_enabled": False,
        "max_excerpt_chars": 1000,
    }
    material = MaterialRecord(
        key="doi:10.1234/url-budget",
        source="openalex",
        item_type="paper",
        title="URL budget paper",
        url="https://publisher.test/paper",
        pdf_url="https://publisher.test/first.pdf",
        evidence={
            "sources": {
                "openalex": {"pdf_url": "https://publisher.test/second.pdf"},
                "semantic_scholar": {"pdf_url": "https://publisher.test/third.pdf"},
            }
        },
    )
    attempted: list[str] = []

    def fake_download(client, url, max_bytes, deadline=None):
        attempted.append(url)
        return b"not a pdf"

    monkeypatch.setattr("daily_agent.connectors.paper_text._download_limited", fake_download)

    enrich_paper_texts([material], config)

    assert attempted == ["https://publisher.test/first.pdf", "https://publisher.test/second.pdf"]
    assert material.paper_text_status["status"] == "unavailable"


def test_enrich_paper_texts_marks_remaining_records_skipped_when_run_budget_expires(tmp_path, monkeypatch):
    config = load_config(str(Path(__file__).resolve().parents[1]))
    object.__setattr__(config, "root", tmp_path)
    config.sources["paper_text"] = {
        "enabled": True,
        "max_papers_per_run": 3,
        "run_budget_seconds": 10,
        "max_urls_per_paper": 1,
        "html_fallback_enabled": False,
        "max_excerpt_chars": 1000,
    }
    records = [
        MaterialRecord(
            key=f"doi:10.1234/budget-{index}",
            source="openalex",
            item_type="paper",
            title=f"Budget paper {index}",
            url=f"https://publisher.test/{index}",
            pdf_url=f"https://publisher.test/{index}.pdf",
        )
        for index in range(3)
    ]
    monotonic_values = iter([0.0, 0.0, 0.0, 11.0, 11.0, 11.0])
    attempted: list[str] = []

    monkeypatch.setattr("daily_agent.connectors.paper_text.time.monotonic", lambda: next(monotonic_values))

    def fake_download(client, url, max_bytes, deadline=None):
        attempted.append(url)
        return b"not a pdf"

    monkeypatch.setattr("daily_agent.connectors.paper_text._download_limited", fake_download)

    enrich_paper_texts(records, config)

    assert attempted == ["https://publisher.test/0.pdf"]
    assert records[0].paper_text_status["status"] == "unavailable"
    assert records[1].paper_text_status["status"] == "skipped"
    assert records[1].paper_text_status["reason"] == "run_budget_exceeded"
    assert records[2].paper_text_status["status"] == "skipped"


def test_landing_metadata_follows_pdf_within_url_budget(tmp_path, monkeypatch):
    from daily_agent.connectors.paper_text import _landing_pdf_urls
    from daily_agent.paper_document import build_document
    cfg = load_config(str(Path(__file__).resolve().parents[1]))
    object.__setattr__(cfg, 'root', tmp_path)
    cfg.sources['paper_text'].update(max_urls_per_paper=2)
    record = MaterialRecord(key='paper:linked', source='openalex', item_type='paper',
                          title='Linked Paper', url='https://publisher.test/article')
    html = b'<html><meta name="citation_pdf_url" content="/paper.pdf"></html>'
    assert _landing_pdf_urls(html, record.url) == ['https://publisher.test/paper.pdf']
    assert _landing_pdf_urls(b'<meta name="citation_pdf_url" content="file:///secret.pdf">', record.url) == []
    calls = []
    def download(client, url, **kwargs):
        calls.append(url)
        return html if url == record.url else b'%PDF-fixture'
    def extract(content, record, url, settings):
        if content.startswith(b'%PDF'):
            text = 'Linked Paper\nMethods\n' + 'We compare models. ' * 200 + '\nResults\nResults are measured.\nDiscussion\nLimited results.'
            return build_document(record, [{'page':1, 'text':text}], url, 'pdf')
        return build_document(record, [{'page':None, 'text':'Short abstract'}], url, 'html')
    monkeypatch.setattr('daily_agent.connectors.paper_text._download_limited', download)
    monkeypatch.setattr('daily_agent.paper_document.extract_document', extract)
    enrich_paper_texts([record], cfg)
    assert calls == [record.url, 'https://publisher.test/paper.pdf']
    assert record.paper_document['document_kind'] == 'full_text'


def test_abstract_cache_retries_and_retains_evidence_on_failed_fetch(tmp_path, monkeypatch):
    from daily_agent.paper_document import build_document, atomic_json, digest, version_identity
    cfg = load_config(str(Path(__file__).resolve().parents[1]))
    object.__setattr__(cfg, 'root', tmp_path)
    record = MaterialRecord(key='paper:cached', source='openalex', item_type='paper',
                            title='Cached Paper', url='https://publisher.test/article')
    doc = build_document(record, [{'page':None, 'text':'Cached Paper abstract.'}], record.url, 'html')
    path = tmp_path/'data/paper_text/v3'/(digest([version_identity(record), cfg.sources['paper_text'], 4])+'.json')
    atomic_json(path, doc)
    calls = []
    def download(client, url, **kwargs):
        calls.append(url)
        return None
    monkeypatch.setattr('daily_agent.connectors.paper_text._download_limited', download)
    enrich_paper_texts([record], cfg)
    assert calls == [record.url]
    assert record.paper_document['content_hash'] == doc['content_hash']
    assert record.paper_document['source_type'] == 'html'


def test_parser_upgrade_reuses_hash_verified_local_pdf(tmp_path, monkeypatch):
    import hashlib
    from daily_agent.paper_document import build_document, atomic_json, digest, version_identity
    cfg = load_config(str(Path(__file__).resolve().parents[1])); object.__setattr__(cfg, 'root', tmp_path)
    record = MaterialRecord(key='paper:local', source='openalex', item_type='paper',
                            title='Local Paper', url='https://publisher.test/article')
    pdf = tmp_path/'old.pdf'; pdf.write_bytes(b'%PDF-fixture')
    doc = build_document(record, [{'page':1, 'text':'Local Paper'}], record.url, 'pdf')
    doc.update(schema_version=0, source_pdf_sha256=hashlib.sha256(pdf.read_bytes()).hexdigest(),
               source_pdf_path=str(pdf))
    path = tmp_path/'data/paper_text/v3'/(digest([version_identity(record), cfg.sources['paper_text'], 4])+'.json')
    atomic_json(path, doc)
    def extract(content, record, url, settings):
        assert content == b'%PDF-fixture'
        text = 'Local Paper\nMethods\n' + 'We compare models. '*200 + '\nResults\nMeasured results.\nDiscussion\nLimits.'
        return build_document(record, [{'page':1,'text':text}], url, 'pdf')
    def forbidden(*args, **kwargs):
        raise AssertionError('Do not refetch an intact local PDF merely because the parser changed')
    monkeypatch.setattr('daily_agent.paper_document.extract_document', extract)
    monkeypatch.setattr('daily_agent.connectors.paper_text._download_limited', forbidden)
    enrich_paper_texts([record], cfg)
    assert record.paper_document['document_kind'] == 'full_text'
