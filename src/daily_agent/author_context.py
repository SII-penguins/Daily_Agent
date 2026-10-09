"""Public professional author context, with conservative provenance and local tracking.

No author-order -> PI inference, institution -> lab inference, social follow, or
external subscriptions. Reviewed evidence is data, never executable instructions.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

import httpx

from daily_agent.models import DigestItem, utc_now_iso

SCHEMA_VERSION = 1
PRIMARY_HOSTS = ("arxiv.org", "proceedings.mlr.press", "openreview.net", "nature.com",
                 "science.org", "acm.org", "ieee.org", "springer.com", "springeropen.com",
                 "sciencedirect.com", "cell.com", "neurips.cc", "aps.org", "quantum-journal.org")


def _norm(value):
    return re.sub(r"[^\w]", "", str(value).casefold())


def _public_url(url):
    host = urlparse(str(url)).hostname or ""
    return urlparse(str(url)).scheme == "https" and bool(host) and not re.fullmatch(r"[\d.:]+", host) and host != "localhost" and "." in host


def _primary_url(url):
    host = urlparse(str(url)).hostname or ""
    return _public_url(url) and any(host == h or host.endswith("." + h) for h in PRIMARY_HOSTS)


def _read(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic within the pipeline lock; no new external state.
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


class _CitationMeta(HTMLParser):
    def __init__(self):
        super().__init__()
        self.values = {}
        self.authors = []

    def handle_starttag(self, tag, attrs):
        if tag != "meta":
            return
        a = dict(attrs)
        key, value = (a.get("name") or "").lower(), a.get("content", "").strip()
        if not value:
            return
        self.values.setdefault(key, []).append(value)
        if key == "citation_author":
            self.authors.append({"name": value, "institutions": []})
        elif key == "citation_author_institution" and self.authors:
            self.authors[-1]["institutions"].append(value)


def publisher_evidence(item, html, url, checked_at=None):
    """Only exact normalized title or DOI match makes metadata attributable."""
    parser = _CitationMeta()
    parser.feed(html)
    values = parser.values
    titles = values.get("citation_title", [])
    dois = values.get("citation_doi", [])
    match = any(_norm(t) == _norm(item.title) for t in titles)
    if item.doi:
        match |= any(d.lower().removeprefix("https://doi.org/") == item.doi.lower().removeprefix("https://doi.org/") for d in dois)
    if item.doi and dois and not any(d.lower().removeprefix("https://doi.org/") == item.doi.lower().removeprefix("https://doi.org/") for d in dois):
        match = False
    if not _primary_url(url) or not match:
        return {}
    return {"title": item.title, "doi": item.doi, "source_url": url,
            "source_kind": "publisher_metadata", "checked_at": checked_at or utc_now_iso(),
            "authors": parser.authors, "excerpt": titles[0] if titles else str(item.doi)}


def _evidence_matches(item, entry):
    # A conflicting DOI cannot be rescued by a common or similar title.
    if item.doi and entry.get("doi"):
        return item.doi.lower().removeprefix("https://doi.org/") == entry["doi"].lower().removeprefix("https://doi.org/")
    return bool(entry.get("title")) and _norm(item.title) == _norm(entry["title"])


def build_author_context(item, evidence_sources=(), checked_at=None):
    """Build context from source metadata and separately reviewed primary evidence.

    Reviewed entries require exact title/DOI identity, source URL, kind, dated
    evidence, and explicit author/lab assertions. They are never inferred from
    the mere co-occurrence of an institution and author on a page.
    """
    now = checked_at or utc_now_iso()
    context = {"schema_version": SCHEMA_VERSION, "checked_at": now, "authors": [],
               "institutions": [], "labs": [], "research_lines": [], "uncertainties": []}
    authors = {}

    def add_author(name):
        key = _norm(name)
        if key and key not in authors:
            authors[key] = {"name": str(name), "position": len(authors) + 1, "roles": [],
                            "institutions": [], "evidence": [], "status": "source_metadata"}
        return authors.get(key)

    for name in item.authors:
        add_author(name)
    # Aggregator claims stay explicitly unverified, including ORCID identity.
    for row in item.raw.get("authorships", []) or []:
        a = add_author(row.get("name", ""))
        if not a:
            continue
        if row.get("orcid"):
            a["orcid"] = row["orcid"]
            a["orcid_status"] = "source_metadata_unverified"
        for name in row.get("institutions", []) or []:
            inst = {"name": str(name), "status": "source_metadata_unverified", "source_url": item.url}
            if inst not in a["institutions"]:
                a["institutions"].append(inst)
    for entry in evidence_sources:
        if not isinstance(entry, dict) or not _evidence_matches(item, entry):
            continue
        kind = entry.get("source_kind")
        url = entry.get("source_url", "")
        if kind not in {"paper_pdf", "publisher_metadata", "official_project", "official_lab", "official_author"}:
            continue
        if not _public_url(url) or not entry.get("checked_at") or not entry.get("excerpt"):
            continue
        if kind != "publisher_metadata" and entry.get("review_status") != "verified":
            continue
        if kind == "publisher_metadata" and not _primary_url(url):
            continue
        ev = {k: entry[k] for k in ("source_url", "source_kind", "excerpt", "checked_at")}
        if entry.get("page"):
            ev["page"] = entry["page"]
        for index, row in enumerate(entry.get("authors", [])):
            a = add_author(row.get("name", ""))
            if not a:
                continue
            # Name conflicts with an existing stable identifier do not merge.
            if a.get("orcid") and row.get("orcid") and a["orcid"] != row["orcid"]:
                context["uncertainties"].append(f"{a['name']} 的 ORCID 冲突，需人工核对")
                continue
            a["status"] = "verified_primary"
            if row.get("orcid"):
                a.update(orcid=row["orcid"], orcid_status="verified_primary")
            a["evidence"].append(ev)
            # First-listed is an ordering fact; lead author/equal contribution is not inferred.
            if index == 0 and entry.get("author_order_complete", True) and kind in {"paper_pdf", "publisher_metadata", "official_project"} and "first_author" not in a["roles"]:
                a["roles"].append("first_author")
            for role in row.get("roles", []):
                if role in {"corresponding_author", "co_first_author", "lead_author"} and role not in a["roles"]:
                    a["roles"].append(role)
            for name in row.get("institutions", []):
                a["institutions"] = [x for x in a["institutions"] if _norm(x["name"]) != _norm(name)]
                a["institutions"].append({"name": name, "status": "verified_primary", "source_url": url})
        for lab in entry.get("labs", []):
            relation = lab.get("relationship")
            if kind not in {"official_lab", "official_author", "paper_pdf", "official_project"} or relation not in {"paper_listed_by_group", "explicit_author_affiliation"}:
                continue
            if not lab.get("name") or not _public_url(lab.get("url", url)):
                continue
            group = {"name": lab["name"], "url": lab.get("url", url),
                "status": "verified_official", "relationship": relation, "evidence": [ev]}
            existing = next((g for g in context["labs"] if (g["name"], g["url"], g["relationship"]) == (group["name"], group["url"], group["relationship"])), None)
            if existing is None:
                context["labs"].append(group)
            elif ev not in existing["evidence"]:
                existing["evidence"].append(ev)
        for line in entry.get("research_lines", []):
            if line.get("text") and line.get("scope") in {"paper", "historical_background"}:
                context["research_lines"].append({"text": line["text"], "scope": line["scope"], "source_url": url})
    context["authors"] = list(authors.values())
    for author in context["authors"]:
        for inst in author["institutions"]:
            if inst not in context["institutions"]:
                context["institutions"].append(inst)
    if not context["authors"]:
        context["uncertainties"].append("作者名单尚未核实")
    if not any("corresponding_author" in a["roles"] for a in context["authors"]):
        context["uncertainties"].append("通讯作者尚未核实；不按末位作者推定")
    if not context["institutions"]:
        context["uncertainties"].append("机构归属尚未核实")
    if not context["labs"]:
        context["uncertainties"].append("具体课题组尚未核实；机构相同不等于同一课题组")
    return context


def _fetch_text(client, url, max_bytes, deadline=None):
    """Fetch only caller-reviewed public URLs, with byte and elapsed-time bounds."""
    if not _public_url(url):
        return ""
    try:
        configured_timeout = float(client.timeout.read or 4.0)
        timeout = max(.1, min(configured_timeout, deadline - time.monotonic())) if deadline else configured_timeout
        with client.stream("GET", url, timeout=timeout) as response:
            response.raise_for_status()
            if response.status_code != 200:
                return ""
            content = bytearray()
            for chunk in response.iter_bytes():
                content.extend(chunk)
                if len(content) > max_bytes or (deadline and time.monotonic() > deadline):
                    return ""
        return content.decode("utf-8", errors="replace")
    except (httpx.HTTPError, ValueError):
        return ""


def _fetch_publisher(client, item, max_bytes, deadline=None):
    # No redirects to unreviewed hosts; no arbitrary URLs from paper body.
    if not _primary_url(item.url):
        return {}
    html = _fetch_text(client, item.url, max_bytes, deadline)
    return publisher_evidence(item, html, item.url) if html else {}


def lab_listing_evidence(item, lab, html, checked_at):
    """A reviewed lab publication page can establish a paper listing, not membership."""
    if lab.get("review_status") != "verified" or not lab.get("name") or not _public_url(lab.get("url", "")):
        return {}
    # Remove markup before normalizing; require a substantive exact title.
    text = re.sub(r"<[^>]+>", " ", html)
    title = _norm(item.title)
    if len(title) < 12 or title not in _norm(text):
        return {}
    return {"title":item.title, "source_url":lab["url"], "source_kind":"official_lab",
        "review_status":"verified", "checked_at":checked_at, "excerpt":item.title,
        "labs":[{"name":lab["name"], "url":lab["url"], "relationship":"paper_listed_by_group"}]}


def update_watchlist(path, items, limit=1000):
    """Candidates for future public paper discovery only; no outbound action."""
    old = _read(path)
    entries = old.get("entries", {})
    now = utc_now_iso()
    for item in items:
        context = item.raw.get("research_context", {})
        for author in context.get("authors", []):
            # Name alone is not a global identity; avoid merging homonyms.
            key = "author:" + (author.get("orcid") if author.get("orcid_status") == "verified_primary" else item.canonical_key() + ":" + _norm(author["name"]))
            value = entries.get(key, {})
            entries[key] = {"kind": "author", "name": author["name"], "status": "candidate",
                "identity_status": "verified_primary" if value.get("identity_status") == "verified_primary" else author.get("status"), "orcid": author.get("orcid") or value.get("orcid"),
                "institutions": author.get("institutions") or value.get("institutions", []),
                "papers": list(dict.fromkeys([*value.get("papers", []), item.url]))[-30:],
                "evidence": author["evidence"] or value.get("evidence", []), "last_seen_at": now}
        for lab in context.get("labs", []):
            key = "lab:" + lab["url"] + ":" + _norm(lab["name"])
            value = entries.get(key, {})
            entries[key] = {"kind": "lab", "name": lab["name"], "url": lab["url"],
                "status": "candidate", "papers": list(dict.fromkeys([*value.get("papers", []), item.url]))[-30:],
                "evidence": lab["evidence"], "last_seen_at": now}
    entries = dict(sorted(entries.items(), key=lambda kv: kv[1].get("last_seen_at", ""), reverse=True)[:max(0, limit)])
    _write(path, {"schema_version": 1, "purpose": "local_public_paper_discovery_candidates",
                  "external_actions": False, "entries": entries})


def enrich_author_contexts(items, config):
    options = config.sources.get("author_context", {}) or {}
    if not options.get("enabled", True):
        return items
    root = config.root
    registry_path = Path(options.get("evidence_registry", "config/research-context-evidence.json"))
    if not registry_path.is_absolute():
        registry_path = root / registry_path
    registry_data = _read(registry_path)
    registry = registry_data.get("sources", [])
    cache_dir = root / "data" / "author_context" / "v1"
    ttl = max(0, float(options.get("cache_ttl_hours", 168))) * 3600
    network_left = max(0, int(options.get("max_pages_per_run", 3)))
    deadline = time.monotonic() + max(0, float(options.get("run_budget_seconds", 12)))
    enriched = []
    lab_pages = []
    with httpx.Client(timeout=max(.1, float(options.get("timeout_seconds", 4))), follow_redirects=False) as client:
        for lab in registry_data.get("lab_pages", [])[:max(0, int(options.get("max_lab_pages_per_run", 1)))]:
            if lab.get("review_status") != "verified" or not _public_url(lab.get("url", "")):
                continue
            lab_path = cache_dir / ("lab-" + hashlib.sha256(lab["url"].encode()).hexdigest() + ".json")
            page = _read(lab_path)
            if not page or time.time() - lab_path.stat().st_mtime >= ttl:
                page = {}
                if network_left and time.monotonic() < deadline:
                    network_left -= 1
                    html = _fetch_text(client, lab["url"], int(options.get("max_page_bytes", 512000)), deadline)
                    if html:
                        page = {"html": html, "checked_at": utc_now_iso()}
                        _write(lab_path, page)
            if page:
                lab_pages.append((lab, page))
        for item in items:
            if item.item_type != "paper":
                continue
            fingerprint = [SCHEMA_VERSION, item.title, item.doi, item.url, item.authors, item.raw.get("authorships"), registry, lab_pages]
            key = hashlib.sha256(json.dumps(fingerprint, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            path = cache_dir / (key + ".json")
            cached = _read(path)
            if cached and path.exists() and time.time() - path.stat().st_mtime < ttl:
                item.raw["research_context"] = cached
            else:
                sources = list(registry)
                sources.extend(lab_listing_evidence(item, lab, page["html"], page["checked_at"]) for lab, page in lab_pages)
                if network_left and time.monotonic() < deadline and _primary_url(item.url):
                    network_left -= 1
                    metadata = _fetch_publisher(client, item, max(1024, int(options.get("max_page_bytes", 512000))), deadline)
                    if metadata:
                        sources.append(metadata)
                context = build_author_context(item, sources)
                item.raw["research_context"] = context
                # Negative results short-lived so later source availability can recover.
                if any(a.get("status") == "verified_primary" for a in context["authors"]):
                    _write(path, context)
            enriched.append(item)
    if options.get("watchlist_enabled", True):
        update_watchlist(cache_dir.parent / "watchlist.json", enriched, int(options.get("max_watchlist_entries", 1000)))
    return items


def research_context_lines(context):
    """Renderer-neutral short Markdown lines; never suppress unknown provenance."""
    lines = []
    authors = context.get("authors", [])
    first = [a["name"] for a in authors if "first_author" in a.get("roles", [])]
    corresponding = [a["name"] for a in authors if "corresponding_author" in a.get("roles", [])]
    if authors:
        lines.append("作者：" + "、".join(a["name"] + ("（元数据，未独立核实）" if a.get("status") != "verified_primary" else "") for a in authors))
    if first:
        lines.append("首位作者：" + "、".join(first) + "（按论文署名顺序，不推定贡献大小）")
    if corresponding:
        lines.append("通讯作者：" + "、".join(corresponding))
    for inst in context.get("institutions", []):
        status = "论文/官方来源" if inst["status"] == "verified_primary" else "元数据提供，未独立核实"
        lines.append(f"机构：[{inst['name']}]({inst['source_url']})（{status}）")
    for lab in context.get("labs", []):
        relation = "官网列有本论文，非全部作者归属声明" if lab["relationship"] == "paper_listed_by_group" else "来源明确署名归属"
        lines.append(f"课题组：[{lab['name']}]({lab['url']})（{relation}）")
    for line in context.get("research_lines", []):
        scope = "历史研究背景，不受本期三个月选文窗口限制" if line["scope"] == "historical_background" else "本篇研究方向"
        lines.append(f"{scope}：[{line['text']}]({line['source_url']})")
    provenance = {}
    for author in authors:
        for ev in author.get("evidence", []):
            provenance.setdefault(ev["source_url"], ev)
    if provenance:
        lines.append("作者核验来源：" + "；".join(
            f"[{('论文第 ' + str(ev['page']) + ' 页') if ev.get('page') else '官方元数据'}]({url})（{ev['checked_at'][:10]} 核验）"
            for url, ev in list(provenance.items())[:3]))
    lines.extend(context.get("uncertainties", []))
    return lines


def enrich_selected_author_contexts(records, config, *, execution=None):
    from daily_agent.author_research import enrich_selected_author_contexts as enrich
    if execution is None:
        return enrich(records, config)
    return enrich(records, config, execution=execution)


def watchlist_discovery_queries(config, limit=2):
    from daily_agent.author_research import watchlist_discovery_queries as queries
    return queries(config, limit)
