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
    timeout = float(source_config.get("timeout_seconds", 30))
    run_budget_seconds = float(source_config.get("run_budget_seconds", 0) or 0)
    max_urls_per_paper = int(source_config.get("max_urls_per_paper", 0) or 0)
    html_fallback = bool(source_config.get("html_fallback_enabled", True))
    from daily_agent.paper_document import (attach_document, build_document, extract_document,
        version_identity, load_json, atomic_json, digest, valid_document)
    cache_dir = config.root / "data" / "paper_text" / "v3"
    deadline = time.monotonic() + run_budget_seconds if run_budget_seconds > 0 else None
    ttl = float(source_config.get("cache_ttl_hours", 168)) * 3600
    count = 0
    with httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": "Daily-Agent/0.1"}) as client:
        for record in records:
            if record.item_type != "paper":
                continue
            identity = version_identity(record)
            path = cache_dir / (digest([identity, source_config, 4]) + ".json")
            cached = load_json(path)
            if (valid_document(cached, identity) and time.time()-path.stat().st_mtime < ttl):
                attach_document(record, cached)
                continue
            # Existing unstructured snippets are never promoted to full text.
            fallback = build_document(record, [{"page": None, "text": record.paper_text_excerpt or record.abstract or ""}],
                                      record.url, "legacy" if record.paper_text_excerpt else "abstract", source_config)
            if count >= max_papers or _deadline_exceeded(deadline):
                fallback["limitations"].append("本轮正文获取预算不足")
                attach_document(record, fallback)
                record.paper_text_status.update(status="skipped", reason="run_budget_exceeded")
                continue
            count += 1
            best = fallback
            urls = _candidate_pdf_urls(record)
            if html_fallback:
                arxiv_id = _arxiv_id(record)
                if arxiv_id:
                    urls.append(f"https://arxiv.org/html/{arxiv_id}")
                urls.extend(_candidate_html_urls(record))
            for index, url in enumerate(dict.fromkeys(urls)):
                if _url_budget_exceeded(index, max_urls_per_paper) or _deadline_exceeded(deadline):
                    break
                content = _download_limited(client, url, max_bytes=max(max_bytes, max_html_bytes), deadline=deadline)
                if not content:
                    continue
                try:
                    doc = extract_document(content, record, url, {**source_config, "page_image_dir": str(cache_dir / "pages" / digest([identity, __import__("hashlib").sha256(content).hexdigest()]))})
                except Exception:
                    best["limitations"].append("一个候选正文解析失败")
                    continue
                if len(content) >= max(max_bytes, max_html_bytes) or _deadline_exceeded(deadline):
                    doc["limitations"].append("下载可能因大小或时间预算截断")
                    if doc["document_kind"] == "full_text":
                        doc["document_kind"] = "partial_text"
                order = {"unavailable": 0, "abstract_only": 1, "partial_text": 2, "full_text": 3}
                if (order[doc["document_kind"]], doc["coverage"]["char_count"]) > (order[best["document_kind"]], best["coverage"]["char_count"]):
                    if doc.get("source_pdf_sha256"):
                        pdf_path = cache_dir / "pdfs" / (doc["source_pdf_sha256"] + ".pdf")
                        pdf_path.parent.mkdir(parents=True, exist_ok=True)
                        pdf_path.write_bytes(content)
                        doc["source_pdf_path"] = str(pdf_path.resolve())
                    best = doc
                if best["document_kind"] == "full_text":
                    break
            if best["source_type"] == "legacy" and best["coverage"]["char_count"]:
                best["limitations"].append("本轮未获取到可确认的新版正文")
            attach_document(record, best)
            # Failed/legacy fetches must be retried, not immortalized as valid cache.
            if best["source_type"] in {"pdf", "html"} and best["coverage"]["char_count"]:
                atomic_json(path, best)
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
    versioned = next((url for url in [record.pdf_url, _arxiv_pdf_from_url(record.url)]
                      if url and re.search(r"arxiv\.org/pdf/[^/?#]+v\d+", url)), None)
    if versioned:
        add(versioned)
    elif arxiv_id:
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
