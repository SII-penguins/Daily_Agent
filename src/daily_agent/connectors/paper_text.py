from __future__ import annotations

from io import BytesIO
from html.parser import HTMLParser
import re
import time
import zlib
from pathlib import Path
from urllib.parse import urlparse

import httpx

from daily_agent.config import AppConfig
from daily_agent.models import MaterialRecord

PDF_TEXT_RE = re.compile(rb"\((?:\\.|[^\\()])*\)\s*Tj|\[(.*?)\]\s*TJ", re.DOTALL)
PDF_STRING_RE = re.compile(rb"\((?:\\.|[^\\()])*\)")
PDF_OCTAL_ESCAPE_RE = re.compile(rb"\\([0-7]{1,3})")
SECTION_MARKER_GROUPS = [
    ("abstract", ["abstract"]),
    ("method", ["methods", "method", "approach"]),
    ("results", ["results", "result", "experiments", "experiment", "evaluation"]),
    ("limitations", ["limitations", "limitation"]),
    ("introduction", ["introduction"]),
    ("conclusion", ["conclusion", "discussion"]),
]
REQUIRED_DEEP_SUMMARY_SECTIONS = ["method", "results", "limitations"]
HTML_SKIP_TAGS = {"script", "style", "nav", "header", "footer", "aside", "form", "svg", "noscript"}
HTML_BREAK_TAGS = {"article", "main", "section", "div", "p", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr"}
PDF_GLYPH_ESCAPES = {
    b"000": b"-",
    b"001": b"fi",
    b"002": b"fi",
    b"003": b"fl",
    b"013": b"ff",
    b"014": b"ffi",
    b"015": b"fl",
    b"016": b"ffi",
    b"050": b"(",
    b"051": b")",
    b"055": b"-",
    b"226": b"-",
}


def enrich_paper_texts(records: list[MaterialRecord], config: AppConfig) -> list[MaterialRecord]:
    source_config = config.sources.get("paper_text", {}) or {}
    if not source_config.get("enabled", True):
        return records
    max_papers = int(source_config.get("max_papers_per_run", 7))
    max_bytes = int(source_config.get("max_pdf_bytes", 8_000_000))
    max_html_bytes = int(source_config.get("max_html_bytes", max_bytes))
    max_chars = int(source_config.get("max_excerpt_chars", 12_000))
    max_raw_chars = max(max_chars, int(source_config.get("max_raw_text_chars", max_chars * 3)))
    section_notes_enabled = bool(source_config.get("section_notes_enabled", True))
    max_section_note_chars = int(source_config.get("max_section_note_chars", 1200))
    timeout = float(source_config.get("timeout_seconds", 30))
    run_budget_seconds = float(source_config.get("run_budget_seconds", 0) or 0)
    max_urls_per_paper = int(source_config.get("max_urls_per_paper", 0) or 0)
    html_fallback = bool(source_config.get("html_fallback_enabled", True))
    cache_dir = config.root / "data" / "paper_text" / "v2"
    for record in records:
        if record.item_type == "paper" and record.paper_text_excerpt and not record.paper_text_status:
            record.paper_text_status = analyze_paper_text_coverage(
                record.paper_text_excerpt,
                source_type="existing_excerpt",
                source_url=None,
                section_notes_enabled=section_notes_enabled,
                max_section_note_chars=max_section_note_chars,
            )
    paper_records = [
        record
        for record in records
        if record.item_type == "paper" and (_candidate_pdf_urls(record) or (html_fallback and _candidate_html_urls(record))) and not record.paper_text_excerpt
    ]
    if not paper_records or max_papers <= 0:
        return records
    cache_dir.mkdir(parents=True, exist_ok=True)
    started_at = time.monotonic()
    deadline = started_at + run_budget_seconds if run_budget_seconds > 0 else None
    with httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": "Daily-Agent/0.1"}) as client:
        candidates = paper_records[:max_papers]
        for index, record in enumerate(candidates):
            if _deadline_exceeded(deadline):
                for skipped in candidates[index:]:
                    skipped.paper_text_status = _skipped_text_status("run_budget_exceeded")
                break
            excerpt = _load_cached_excerpt(cache_dir, record.key)
            if excerpt:
                record.paper_text_excerpt = excerpt[:max_chars]
                record.paper_text_status = analyze_paper_text_coverage(
                    excerpt,
                    source_type="cache",
                    source_url=None,
                    section_notes_enabled=section_notes_enabled,
                    max_section_note_chars=max_section_note_chars,
                )
                continue
            text = ""
            source_type = ""
            source_url = ""
            attempts = 0
            for pdf_url in _candidate_pdf_urls(record):
                if _url_budget_exceeded(attempts, max_urls_per_paper):
                    break
                attempts += 1
                content = _download_limited(client, pdf_url, max_bytes=max_bytes, deadline=deadline)
                if not content:
                    continue
                text = extract_pdf_text(content, max_chars=max_raw_chars)
                if text:
                    source_type = "pdf"
                    source_url = pdf_url
                    break
            if not text and html_fallback:
                for html_url in _candidate_html_urls(record):
                    if _url_budget_exceeded(attempts, max_urls_per_paper):
                        break
                    attempts += 1
                    content = _download_limited(client, html_url, max_bytes=max_html_bytes, deadline=deadline)
                    if not content:
                        continue
                    text = extract_html_text(content, max_chars=max_raw_chars)
                    if text:
                        source_type = "html"
                        source_url = html_url
                        break
            if not text:
                record.paper_text_status = _unavailable_text_status()
                continue
            excerpt = select_paper_excerpt(text, max_chars=max_chars)
            if excerpt:
                record.paper_text_excerpt = excerpt
                record.paper_text_status = analyze_paper_text_coverage(
                    text,
                    source_type=source_type,
                    source_url=source_url,
                    excerpt=excerpt,
                    section_notes_enabled=section_notes_enabled,
                    max_section_note_chars=max_section_note_chars,
                )
                _write_cached_excerpt(cache_dir, record.key, excerpt)
    return records


def _url_budget_exceeded(attempts: int, max_urls_per_paper: int) -> bool:
    return max_urls_per_paper > 0 and attempts >= max_urls_per_paper


def _deadline_exceeded(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() >= deadline


def _download_limited(client: httpx.Client, url: str, max_bytes: int, deadline: float | None = None) -> bytes | None:
    try:
        if not hasattr(client, "stream"):
            response = client.get(url)
            if response.status_code in {403, 404, 429}:
                return None
            response.raise_for_status()
            return response.content[:max_bytes]
        with client.stream("GET", url) as response:
            if response.status_code in {403, 404, 429}:
                return None
            response.raise_for_status()
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_bytes():
                if not chunk:
                    continue
                remaining = max_bytes - total
                if remaining <= 0:
                    break
                if len(chunk) > remaining:
                    chunks.append(chunk[:remaining])
                    break
                chunks.append(chunk)
                total += len(chunk)
                if _deadline_exceeded(deadline):
                    break
            return b"".join(chunks)
    except httpx.HTTPError:
        return None


def _candidate_pdf_urls(record: MaterialRecord) -> list[str]:
    urls: list[str] = []

    def add(url: str | None) -> None:
        if url and url not in urls:
            urls.append(url)

    def add_many(values) -> None:  # type: ignore[no-untyped-def]
        if not isinstance(values, list):
            return
        for value in values:
            if isinstance(value, str):
                add(value)

    arxiv_id = _arxiv_id(record)
    if arxiv_id:
        add(f"https://arxiv.org/pdf/{arxiv_id}")
    if record.url:
        add(_arxiv_pdf_from_url(record.url))
    add(record.pdf_url)
    add_many((record.raw or {}).get("pdf_urls"))
    sources = (record.evidence or {}).get("sources", {}) or {}
    for source in ["arxiv", "openalex", "unpaywall", "semantic_scholar", "google_scholar", "crossref", "core", "dblp", "ieee"]:
        source_data = sources.get(source) or {}
        add(_arxiv_pdf_from_url(source_data.get("url")))
        add(source_data.get("pdf_url"))
        add_many(source_data.get("pdf_urls"))
    return urls


def _candidate_html_urls(record: MaterialRecord) -> list[str]:
    urls: list[str] = []

    def add(url: str | None) -> None:
        if url and _is_http_url(url) and not _looks_like_pdf_url(url) and url not in urls:
            urls.append(url)

    add(record.url)
    add(record.pdf_url)
    sources = (record.evidence or {}).get("sources", {}) or {}
    for source in ["arxiv", "openreview", "pmlr", "neurips", "openalex", "unpaywall", "semantic_scholar", "google_scholar", "crossref", "core", "dblp", "ieee"]:
        source_data = sources.get(source) or {}
        add(source_data.get("url"))
        add(source_data.get("pdf_url"))
        add(source_data.get("landing_url"))
        add(source_data.get("open_access_url"))
    return urls


def _is_http_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _looks_like_pdf_url(url: str) -> bool:
    parsed = urlparse(url)
    path = parsed.path.lower()
    return path.endswith(".pdf") or "/pdf/" in path


def _arxiv_id(record: MaterialRecord) -> str | None:
    if record.key.startswith("arxiv:"):
        return record.key.split(":", 1)[1]
    if record.doi and record.doi.lower().startswith("10.48550/arxiv."):
        match = re.search(r"10\.48550/arxiv\.([0-9.]+(?:v\d+)?)", record.doi, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def _arxiv_pdf_from_url(url: str | None) -> str | None:
    if not url:
        return None
    match = re.search(r"arxiv\.org/(?:abs|pdf)/([^?#/]+)", url)
    if match:
        return f"https://arxiv.org/pdf/{match.group(1)}"
    doi_match = re.search(r"10\.48550/arxiv\.([0-9.]+)(v\d+)?", url, flags=re.IGNORECASE)
    if doi_match:
        return f"https://arxiv.org/pdf/{doi_match.group(1)}{doi_match.group(2) or ''}"
    return None


def extract_pdf_text(content: bytes, max_chars: int = 12_000) -> str:
    if not content.startswith(b"%PDF"):
        return ""
    for extractor in [_extract_with_pymupdf, _extract_with_pypdf, _extract_from_pdf_streams]:
        text = extractor(content, max_chars=max_chars)
        if text:
            return text
    return ""


def extract_html_text(content: bytes, max_chars: int = 12_000) -> str:
    sample = content[:1024].lower()
    if content.startswith(b"%PDF") or (b"<html" not in sample and b"<article" not in sample and b"<main" not in sample and b"<body" not in sample):
        return ""
    try:
        html = content.decode("utf-8", errors="ignore")
    except Exception:
        return ""
    parser = _PaperHTMLTextParser(max_chars=max_chars * 3)
    try:
        parser.feed(html)
        parser.close()
    except Exception:
        return ""
    return _clean_text(parser.text())[:max_chars]


class _PaperHTMLTextParser(HTMLParser):
    def __init__(self, max_chars: int) -> None:
        super().__init__(convert_charrefs=True)
        self.max_chars = max_chars
        self.parts: list[str] = []
        self.skip_stack: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in HTML_SKIP_TAGS:
            self.skip_stack.append(tag)
            return
        if tag in HTML_BREAK_TAGS:
            self._append(" ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self.skip_stack and self.skip_stack[-1] == tag:
            self.skip_stack.pop()
            return
        if tag in HTML_BREAK_TAGS:
            self._append(" ")

    def handle_data(self, data: str) -> None:
        if self.skip_stack:
            return
        self._append(data)

    def text(self) -> str:
        return "".join(self.parts)

    def _append(self, value: str) -> None:
        if sum(len(part) for part in self.parts) >= self.max_chars:
            return
        self.parts.append(value)


def _extract_with_pymupdf(content: bytes, max_chars: int) -> str:
    try:
        import fitz  # type: ignore
    except Exception:
        return ""
    _quiet_pymupdf(fitz)
    try:
        doc = fitz.open(stream=content, filetype="pdf")
    except Exception:
        return ""
    chunks: list[str] = []
    try:
        for page in doc:
            chunks.append(page.get_text("text"))
            if sum(len(chunk) for chunk in chunks) >= max_chars * 2:
                break
    finally:
        doc.close()
    return _clean_text(" ".join(chunks))[:max_chars]


def _quiet_pymupdf(fitz_module) -> None:
    tools = getattr(fitz_module, "TOOLS", None)
    if not tools:
        return
    for name in ["mupdf_display_errors", "mupdf_display_warnings"]:
        toggle = getattr(tools, name, None)
        if callable(toggle):
            try:
                toggle(False)
            except Exception:
                continue


def _extract_with_pypdf(content: bytes, max_chars: int) -> str:
    try:
        from pypdf import PdfReader  # type: ignore
    except Exception:
        return ""
    try:
        reader = PdfReader(BytesIO(content))
    except Exception:
        return ""
    chunks: list[str] = []
    for page in reader.pages:
        try:
            chunks.append(page.extract_text() or "")
        except Exception:
            continue
        if sum(len(chunk) for chunk in chunks) >= max_chars * 2:
            break
    return _clean_text(" ".join(chunks))[:max_chars]


def _extract_from_pdf_streams(content: bytes, max_chars: int) -> str:
    chunks: list[bytes] = []
    for match in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", content, re.DOTALL):
        raw = match.group(1).strip(b"\r\n")
        for candidate in _stream_candidates(raw):
            text = _extract_text_from_stream(candidate)
            if text:
                chunks.append(text.encode("utf-8", errors="ignore"))
                if sum(len(chunk) for chunk in chunks) >= max_chars * 2:
                    break
        if sum(len(chunk) for chunk in chunks) >= max_chars * 2:
            break
    return _clean_text(b" ".join(chunks).decode("utf-8", errors="ignore"))[:max_chars]


def select_paper_excerpt(text: str, max_chars: int = 12_000) -> str:
    cleaned = _clean_text(text)
    if len(cleaned) <= max_chars:
        return cleaned
    lowered = cleaned.lower()
    found: list[tuple[str, int]] = []
    for label, markers in SECTION_MARKER_GROUPS:
        index = _first_marker_index(lowered, markers)
        if index != -1:
            found.append((label, index))
    if not found:
        return cleaned[:max_chars]
    max_sections = max(1, min(len(found), max_chars // 90))
    selected_by_priority = found[:max_sections]
    selected_by_position = sorted(selected_by_priority, key=lambda section: section[1])
    separator = " [...] "
    available = max_chars - len(separator) * max(0, len(selected_by_position) - 1)
    per_section = max(60, available // max(1, len(selected_by_position)))
    pieces = []
    for _, index in selected_by_position:
        pieces.append(cleaned[index : min(index + per_section, len(cleaned))])
    return _clean_text(" [...] ".join(pieces))[:max_chars]


def analyze_paper_text_coverage(
    text: str,
    source_type: str | None,
    source_url: str | None,
    excerpt: str | None = None,
    section_notes_enabled: bool = True,
    max_section_note_chars: int = 1200,
) -> dict[str, object]:
    cleaned = _clean_text(text)
    excerpt_text = _clean_text(excerpt or cleaned)
    sections_found = _sections_found(cleaned)
    section_notes = build_section_notes(cleaned, max_chars_per_section=max_section_note_chars) if section_notes_enabled else {}
    missing_sections = [section for section in REQUIRED_DEEP_SUMMARY_SECTIONS if section not in sections_found]
    return {
        "available": bool(cleaned),
        "source_type": source_type,
        "source_url": source_url,
        "sections_found": sections_found,
        "missing_sections": missing_sections,
        "section_notes": section_notes,
        "section_note_count": len(section_notes),
        "sufficient_for_deep_summary": bool(cleaned) and not missing_sections,
        "raw_char_count": len(cleaned),
        "excerpt_char_count": len(excerpt_text),
    }


def _unavailable_text_status() -> dict[str, object]:
    return {
        "status": "unavailable",
        "available": False,
        "source_type": None,
        "source_url": None,
        "sections_found": [],
        "missing_sections": list(REQUIRED_DEEP_SUMMARY_SECTIONS),
        "section_notes": {},
        "section_note_count": 0,
        "sufficient_for_deep_summary": False,
        "raw_char_count": 0,
        "excerpt_char_count": 0,
    }


def _skipped_text_status(reason: str) -> dict[str, object]:
    status = _unavailable_text_status()
    status["status"] = "skipped"
    status["reason"] = reason
    return status


def _sections_found(text: str) -> list[str]:
    lowered = text.lower()
    found: list[str] = []
    for label, markers in SECTION_MARKER_GROUPS:
        if _first_marker_index(lowered, markers) != -1:
            found.append(label)
    return found


def build_section_notes(text: str, max_chars_per_section: int = 1200) -> dict[str, str]:
    cleaned = _clean_text(text)
    if not cleaned:
        return {}
    lowered = cleaned.lower()
    positions: list[tuple[str, int]] = []
    seen_labels: set[str] = set()
    for label, markers in SECTION_MARKER_GROUPS:
        index = _first_marker_index(lowered, markers)
        if index != -1 and label not in seen_labels:
            positions.append((label, index))
            seen_labels.add(label)
    if not positions:
        return {}
    ordered = sorted(positions, key=lambda item: item[1])
    notes: dict[str, str] = {}
    for index, (label, start) in enumerate(ordered):
        end = ordered[index + 1][1] if index + 1 < len(ordered) else len(cleaned)
        note = cleaned[start:end].strip()
        if note:
            notes[label] = note[:max_chars_per_section].strip()
    return notes


def _first_marker_index(text: str, markers: list[str]) -> int:
    indexes = [text.find(marker) for marker in markers]
    indexes = [index for index in indexes if index != -1]
    return min(indexes) if indexes else -1


def _stream_candidates(raw: bytes) -> list[bytes]:
    candidates = [raw]
    try:
        candidates.append(zlib.decompress(raw))
    except zlib.error:
        pass
    return candidates


def _extract_text_from_stream(stream: bytes) -> str:
    parts: list[str] = []
    for match in PDF_TEXT_RE.finditer(stream):
        if match.group(1):
            for item in PDF_STRING_RE.findall(match.group(1)):
                decoded = _decode_pdf_string(item)
                if decoded:
                    parts.append(decoded)
        else:
            decoded = _decode_pdf_string(match.group(0).rsplit(b")", 1)[0] + b")")
            if decoded:
                parts.append(decoded)
    return " ".join(parts)


def _decode_pdf_string(value: bytes) -> str:
    if value.startswith(b"(") and value.endswith(b")"):
        value = value[1:-1]
    value = re.sub(rb"\\([nrtbf()\\])", _replace_escape, value)
    value = PDF_OCTAL_ESCAPE_RE.sub(_replace_octal_escape, value)
    value = re.sub(rb"\\\r?\n", b"", value)
    decoded = value.decode("utf-8", errors="ignore") or value.decode("latin-1", errors="ignore")
    return _clean_text(decoded)


def _replace_escape(match: re.Match[bytes]) -> bytes:
    mapping = {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b" ", b"f": b" ", b"(": b"(", b")": b")", b"\\": b"\\"}
    return mapping.get(match.group(1), match.group(1))


def _replace_octal_escape(match: re.Match[bytes]) -> bytes:
    raw = match.group(1)
    if raw in PDF_GLYPH_ESCAPES:
        return PDF_GLYPH_ESCAPES[raw]
    try:
        value = int(raw, 8)
    except ValueError:
        return b" "
    if value < 32 or value == 127:
        return b" "
    return bytes([value])


def _clean_text(value: str) -> str:
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", value)
    value = re.sub(r"(?<=[A-Za-z])\s+(?=[.,;:%])", "", value)
    return re.sub(r"\s+", " ", value).strip()


def _cache_path(cache_dir: Path, key: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", key)
    return cache_dir / f"{safe}.txt"


def _load_cached_excerpt(cache_dir: Path, key: str) -> str | None:
    path = _cache_path(cache_dir, key)
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _write_cached_excerpt(cache_dir: Path, key: str, excerpt: str) -> None:
    try:
        _cache_path(cache_dir, key).write_text(excerpt, encoding="utf-8")
    except OSError:
        pass
