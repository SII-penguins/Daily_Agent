"""Trusted report -> selected-only import plan. No network, secrets or model calls.

Trusted digests and artifact bytes must come from the authenticated server's immutable
report registry, NEVER from a browser request. Checksums establish that binding;
they are not signatures and do not independently establish scientific truth.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hashlib
import json
import re
import unicodedata
from urllib.parse import unquote, urlsplit

SCHEMA = "daily-agent-zotero-catalog-v1"
MAX_PDF_BYTES = 64 * 1024 * 1024
TOPICS = frozenset({"quantum-error-correction", "neural-quantum-states", "quantum-optimization",
                    "quantum-machine-learning", "ai-for-science", "agents", "world-models",
                    "embodied-ai", "other-reviewed"})
PUBLICATIONS = frozenset({"published_verified", "preprint", "unverified"})


class ImportRejected(ValueError):
    """Fail closed before any library operation."""


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def digest(value, field="digest"):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ImportRejected(f"Invalid {field}")
    return value


def short_text(value, field, maximum=1000, allow_empty=False):
    if not isinstance(value, str) or (not value.strip() and not allow_empty) or len(value) > maximum:
        raise ImportRejected(f"Invalid {field}")
    if any(ord(c) < 32 for c in value):
        raise ImportRejected(f"Control character in {field}")
    return value.strip()


def normalize_doi(value):
    if not value:
        return ""
    value = short_text(value, "DOI", 300)
    value = re.sub(r"^doi:\s*", "", value, flags=re.I)
    if re.match(r"^https?://", value, re.I):
        parts = urlsplit(value)
        if parts.hostname not in {"doi.org", "dx.doi.org"} or parts.query or parts.fragment:
            raise ImportRejected("Invalid DOI URL")
        value = unquote(parts.path.lstrip("/"))
    if not re.fullmatch(r"10\.\d{4,9}/[^\s]+", value, re.I):
        raise ImportRejected("Invalid DOI")
    # Do not strip punctuation: DOI suffix punctuation can be significant.
    return value.lower()


def normalize_arxiv(value):
    if not value:
        return ""
    value = short_text(value, "arXiv ID", 200)
    value = re.sub(r"^arxiv:\s*", "", value, flags=re.I)
    if re.match(r"^https?://", value, re.I):
        parts = urlsplit(value)
        if parts.hostname not in {"arxiv.org", "www.arxiv.org"} or parts.query or parts.fragment:
            raise ImportRejected("Invalid arXiv URL")
        value = re.sub(r"^/(?:abs|pdf)/", "", parts.path)
        value = re.sub(r"\.pdf$", "", value, flags=re.I)
    value = re.sub(r"v[1-9]\d*$", "", value, flags=re.I).lower()
    if not re.fullmatch(r"(?:\d{4}\.\d{4,5}|[a-z][a-z.\-]*/\d{7})", value):
        raise ImportRejected("Invalid arXiv ID")
    return value


def normalized_text(value):
    # Deliberately no fuzzy matching, punctuation stripping, or author initials.
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def safe_source_url(value):
    """Metadata only, never used as a fetch target by this package."""
    value = short_text(value, "source URL", 2000)
    try:
        p = urlsplit(value)
        if (p.scheme != "https" or not p.hostname or p.username or p.password or p.port not in {None, 443}
                or "\\" in value or "%" in p.netloc or p.fragment or p.query):
            raise ImportRejected("Invalid source URL")
        host = p.hostname.lower()
        if "." not in host or host.endswith((".local", ".localhost", ".internal")) or re.fullmatch(r"[\d.]+", host):
            raise ImportRejected("Invalid source URL host")
    except ValueError as exc:
        raise ImportRejected("Invalid source URL") from exc
    return value


@dataclass(frozen=True)
class PdfRef:
    blob_id: str
    sha256: str
    size: int

    @classmethod
    def load(cls, row):
        if not isinstance(row, dict) or set(row) != {"blob_id", "sha256", "size"}:
            raise ImportRejected("PDF reference permits only blob ID, digest and size")
        digest(row["sha256"], "PDF digest")
        if row["blob_id"] != row["sha256"]:
            raise ImportRejected("PDF blob must be content-addressed")
        if type(row["size"]) is not int or not 8 <= row["size"] <= MAX_PDF_BYTES:
            raise ImportRejected("PDF size outside permitted bounds")
        return cls(**row)


def check_pdf(ref: PdfRef, content: bytes):
    if not isinstance(content, bytes) or len(content) != ref.size or len(content) > MAX_PDF_BYTES:
        raise ImportRejected("PDF size mismatch")
    if not content.startswith(b"%PDF-") or sha256(content) != ref.sha256:
        raise ImportRejected("PDF signature or hash mismatch")
    return hashlib.md5(content, usedforsecurity=False).hexdigest()


@dataclass(frozen=True)
class Paper:
    paper_id: str
    title: str
    source_url: str
    doi: str
    arxiv: str
    authors: tuple[str, ...]
    year: str
    venue: str
    item_type: str
    publication: str
    publication_evidence_sha256: str
    topics: tuple[str, ...]
    pdf: PdfRef | None

    @property
    def fallback_identity(self):
        if self.doi or self.arxiv or not self.authors or not self.year or not self.venue:
            return ""
        return sha256(canonical_json([normalized_text(self.title),
                                      [normalized_text(a) for a in self.authors],
                                      self.year, normalized_text(self.venue)]).encode())

    @property
    def identities(self):
        return tuple(x for x in ("doi:" + self.doi if self.doi else "",
                                 "arxiv:" + self.arxiv if self.arxiv else "",
                                 "title:" + self.fallback_identity if self.fallback_identity else "") if x)


@dataclass(frozen=True)
class VerifiedManifest:
    report_sha256: str
    catalog_sha256: str
    day: str
    papers: tuple[Paper, ...]

    def by_id(self):
        return {p.paper_id: p for p in self.papers}


def load_verified_manifest(report_bytes: bytes, html_bytes: bytes, catalog_bytes: bytes, *,
                           trusted_report_sha256: str, trusted_catalog_sha256: str) -> VerifiedManifest:
    """Load a server-pinned corrected report and reviewed metadata sidecar.

    This function does not promote scraped/browser metadata to verified metadata.
    The sidecar is an explicit server-reviewed artifact, separately digest-pinned.
    """
    digest(trusted_report_sha256); digest(trusted_catalog_sha256)
    if sha256(report_bytes) != trusted_report_sha256 or sha256(catalog_bytes) != trusted_catalog_sha256:
        raise ImportRejected("Untrusted or changed report/catalog")
    try:
        report = json.loads(report_bytes)
        catalog = json.loads(catalog_bytes)
        if report["schema"] != "paper-first-corrected-edition-v1" or catalog["schema"] != SCHEMA:
            raise ImportRejected("Unsupported report/catalog schema")
        if report["html_sha256"] != sha256(html_bytes) or report["html_bytes"] != len(html_bytes):
            raise ImportRejected("Report HTML does not match its seal")
        if catalog["report_sha256"] != trusted_report_sha256:
            raise ImportRejected("Catalog is bound to another report")
        day = date.fromisoformat(report["date"]).isoformat()
        if day != report["date"]:
            raise ImportRejected("Noncanonical report date")
        rows = report["paper_records"]
        ids = [short_text(r["key"], "paper ID", 300) for r in rows]
        if len(ids) != len(set(ids)) or len(rows) != report["qualified_papers"]:
            raise ImportRejected("Duplicate or inconsistent report allowlist")
        catalog_rows = catalog["papers"]
        if len(catalog_rows) != len(rows) or {r["paper_id"] for r in catalog_rows} != set(ids):
            raise ImportRejected("Catalog must match the complete report paper allowlist")
        index = {r["paper_id"]: r for r in catalog_rows}
        papers = []
        for row in rows:
            data = index[row["key"]]
            for field in ("title", "source_url", "review_sha256", "qualification_receipt_sha256", "source_pdf_sha256"):
                if data[field] != row[field]:
                    raise ImportRejected("Catalog/report evidence mismatch")
            for field in ("review_sha256", "qualification_receipt_sha256", "source_pdf_sha256"):
                digest(data[field], field)
            publication = data["publication"]
            evidence = data.get("publication_evidence_sha256", "")
            if publication not in PUBLICATIONS:
                raise ImportRejected("Unknown publication status")
            if publication == "published_verified":
                digest(evidence, "official publication evidence")
            elif evidence:
                digest(evidence, "publication evidence")
            item_type = data["item_type"]
            if item_type not in {"journalArticle", "conferencePaper", "preprint", "document"}:
                raise ImportRejected("Unsupported Zotero item type")
            if publication != "published_verified" and item_type in {"journalArticle", "conferencePaper"}:
                raise ImportRejected("Unverified publication cannot become a formal-publication item")
            if publication == "preprint" and item_type != "preprint":
                raise ImportRejected("Preprint must retain its status")
            topics = data["topics"]
            if not topics or not isinstance(topics, list) or not all(t in TOPICS for t in topics):
                raise ImportRejected("Classification must use reviewed topic IDs")
            if not isinstance(data["authors"], list):
                raise ImportRejected("Authors must be an ordered list")
            authors = tuple(short_text(a, "author", 200) for a in data["authors"])
            year = data["year"]
            if year and not re.fullmatch(r"\d{4}", year):
                raise ImportRejected("Invalid publication year")
            pdf = PdfRef.load(data["pdf"]) if data.get("pdf") else None
            if pdf and pdf.sha256 != row["source_pdf_sha256"]:
                raise ImportRejected("PDF is not the reviewed source")
            paper = Paper(row["key"], short_text(data["title"], "title"), safe_source_url(data["source_url"]),
                          normalize_doi(data.get("doi")), normalize_arxiv(data.get("arxiv")), authors, year,
                          short_text(data["venue"], "venue", allow_empty=True), item_type, publication,
                          evidence, tuple(sorted(set(topics))), pdf)
            if not paper.identities:
                raise ImportRejected("Insufficient stable identity; manual review required")
            papers.append(paper)
        return VerifiedManifest(trusted_report_sha256, trusted_catalog_sha256, day, tuple(papers))
    except (KeyError, TypeError, json.JSONDecodeError, AttributeError) as exc:
        raise ImportRejected("Malformed report/catalog") from exc


@dataclass(frozen=True)
class ImportPlan:
    job_id: str
    manifest: VerifiedManifest
    papers: tuple[Paper, ...]
    include_pdf: bool
    library_scope: str


def plan_import(manifest: VerifiedManifest, request: dict, *, personal_library_scope: str) -> ImportPlan:
    """Request comes only from an explicit Save click, after server auth/CSRF checks.

    No browser-supplied URLs, metadata, library IDs, topic IDs or secrets accepted.
    The explicit action is an intent guard, not an authentication mechanism.
    """
    if not isinstance(request, dict) or set(request) != {"action", "report_sha256", "selected_paper_ids", "include_pdf"}:
        raise ImportRejected("Unexpected import request fields")
    if request["action"] != "save_selected" or request["report_sha256"] != manifest.report_sha256:
        raise ImportRejected("Explicit save action for this report is required")
    if not re.fullmatch(r"personal:[A-Za-z0-9_\-]{1,100}", personal_library_scope):
        raise ImportRejected("Only a server-bound personal library is supported")
    selected = request["selected_paper_ids"]
    if not isinstance(selected, list) or not selected or len(selected) > 50 or not all(isinstance(x, str) for x in selected):
        raise ImportRejected("Select between 1 and 50 paper IDs")
    if len(selected) != len(set(selected)) or any(x not in manifest.by_id() for x in selected):
        raise ImportRejected("Selection outside verified paper allowlist or duplicate selection")
    if type(request["include_pdf"]) is not bool:
        raise ImportRejected("include_pdf must be boolean")
    papers = tuple(manifest.by_id()[key] for key in sorted(selected))
    identity = {"scope": personal_library_scope, "report": manifest.report_sha256,
                "catalog": manifest.catalog_sha256, "selected": [p.paper_id for p in papers],
                "include_pdf": request["include_pdf"], "schema": 1}
    return ImportPlan(sha256(canonical_json(identity).encode()), manifest, papers, request["include_pdf"], personal_library_scope)


def classification(paper: Paper, day: str):
    paths = [("DailyAgent", day[:4], day)]
    paths += [("DailyAgent", "Topics", topic) for topic in paper.topics]
    tags = ["DailyAgent", "daily:" + day, "publication:" + paper.publication]
    tags += ["topic:" + topic for topic in paper.topics]
    return paths, tags


def match_paper(paper: Paper, candidates: list[dict]):
    """Exact library-wide identity match. Ambiguities and trash fail closed."""
    matches = []
    for item in candidates:
        identities = item.get("identity", {})
        invalid_identity = False
        try:
            doi = normalize_doi(identities.get("doi"))
        except ImportRejected:
            doi = ""
            invalid_identity = True
        try:
            arxiv = normalize_arxiv(identities.get("arxiv"))
        except ImportRejected:
            arxiv = ""
            invalid_identity = True
        exact = bool(paper.doi and doi == paper.doi or paper.arxiv and arxiv == paper.arxiv)
        if exact and (invalid_identity or paper.doi and doi and paper.doi != doi or paper.arxiv and arxiv and paper.arxiv != arxiv):
            raise ImportRejected("Conflicting strong identifiers require manual resolution")
        if not exact and not invalid_identity and not (paper.doi or paper.arxiv or doi or arxiv) and paper.fallback_identity:
            other = [normalized_text(item.get("title", "")),
                     [normalized_text(a) for a in identities.get("authors", [])],
                     identities.get("year", ""), normalized_text(identities.get("venue", ""))]
            exact = sha256(canonical_json(other).encode()) == paper.fallback_identity
        if exact:
            if item.get("deleted"):
                raise ImportRejected("Matching item is in Trash; user decision required")
            matches.append(item)
    if len(matches) > 1:
        raise ImportRejected("Multiple matching library items require manual resolution")
    return matches[0] if matches else None


def metadata(paper: Paper):
    fields = {"itemType": paper.item_type, "title": paper.title, "url": paper.source_url,
              "creators": [{"creatorType": "author", "name": name} for name in paper.authors],
              "date": paper.year}
    if paper.doi:
        if paper.item_type in {"journalArticle", "conferencePaper", "preprint"}:
            fields["DOI"] = paper.doi
        else:
            fields["extra"] = "DOI: " + paper.doi
    if paper.venue and paper.item_type in {"journalArticle", "conferencePaper"}:
        fields["publicationTitle" if paper.item_type == "journalArticle" else "proceedingsTitle"] = paper.venue
    if paper.arxiv:
        fields["extra"] = (fields.get("extra", "") + "\narXiv: " + paper.arxiv).strip()
    return fields


def paper_identity(paper: Paper):
    return {"doi": paper.doi, "arxiv": paper.arxiv, "authors": list(paper.authors),
            "year": paper.year, "venue": paper.venue}
