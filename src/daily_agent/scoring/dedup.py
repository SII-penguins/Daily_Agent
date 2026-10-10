from __future__ import annotations

import re

from daily_agent.models import DigestItem
from daily_agent.scoring.publication import publication_dois


def deduplicate_items(items: list[DigestItem]) -> list[DigestItem]:
    merged: dict[str, DigestItem] = {}
    # One weak title/year key may identify several incompatible DOI clusters.
    # Retain each mapping so a later DOI-less row cannot bridge them arbitrarily.
    identity_index: dict[str, list[str]] = {}
    for item in items:
        _ensure_multisource_raw(item)
        keys = _identity_keys(item)
        matches = list(dict.fromkeys(key for value in keys for key in identity_index.get(value, [])))
        strong = list(dict.fromkeys(key for value in keys if not value.startswith("title:")
                                   for key in identity_index.get(value, [])))
        compatible = [key for key in (strong or matches)
                      if not _publication_identity_conflict(merged[key], item)]
        # A strong exact identifier may disambiguate a weak title collision.
        # A title alone cannot select between conflicting candidate identities.
        key = compatible[0] if len(compatible) == 1 and (strong or len(matches) == 1) else None
        if key is None:
            key = item.canonical_key()
            if key in merged:
                suffix = 1
                while f"{key}#identity-conflict-{suffix}" in merged:
                    suffix += 1
                key = f"{key}#identity-conflict-{suffix}"
            merged[key] = item
        else:
            merged[key] = _merge_items(merged[key], item)
        for value in _merge_lists(keys, _identity_keys(merged[key])):
            destinations = identity_index.setdefault(value, [])
            if key not in destinations:
                destinations.append(key)
    return list(merged.values())


def _publication_identity_conflict(existing: DigestItem, incoming: DigestItem) -> bool:
    if existing.item_type != "paper" or incoming.item_type != "paper":
        return False
    left, right = publication_dois(existing), publication_dois(incoming)
    return len(left) > 1 or len(right) > 1 or bool(left and right and left != right)


def _merge_items(existing: DigestItem, incoming: DigestItem) -> DigestItem:
    if _publication_identity_conflict(existing, incoming):
        raise ValueError("Contradictory publication DOIs cannot be merged")
    _ensure_multisource_raw(existing)
    _ensure_multisource_raw(incoming)

    if _prefer_incoming(existing, incoming):
        base, other = incoming, existing
    else:
        base, other = existing, incoming

    base.source_tags = _merge_lists(base.source_tags, other.source_tags)
    base.fixed_tags = _merge_lists(base.fixed_tags, other.fixed_tags)
    base.llm_tags = _merge_lists(base.llm_tags, other.llm_tags)
    base.categories = _merge_lists(base.categories, other.categories)
    base.authors = _merge_lists(base.authors, other.authors)
    base.score_breakdown.update(other.score_breakdown)
    base.raw = _merge_raw(other.raw, base.raw)
    base.pdf_url = base.pdf_url or other.pdf_url
    base.code_url = base.code_url or other.code_url
    base.abstract = base.abstract or other.abstract
    base.repo_description = base.repo_description or other.repo_description
    base.doi = base.doi or other.doi
    base.arxiv_id = base.arxiv_id or other.arxiv_id
    base.arxiv_version = base.arxiv_version or other.arxiv_version
    base.published_at = base.published_at or other.published_at
    base.updated_at = max(base.updated_at or "", other.updated_at or "") or None
    base.update_label = base.update_label or other.update_label

    if base.source == "arxiv" and other.source == "arxiv" and base.arxiv_id == other.arxiv_id and base.arxiv_version != other.arxiv_version:
        if _version_number(other.arxiv_version) > _version_number(base.arxiv_version):
            base.arxiv_version = other.arxiv_version
        base.update_label = "version_update"
    if base.source == "github":
        if other.stars and (not base.stars or other.stars > base.stars):
            base.stars = other.stars
        if other.forks and (not base.forks or other.forks > base.forks):
            base.forks = other.forks
    return base


def _identity_keys(item: DigestItem) -> list[str]:
    keys = [item.canonical_key()]
    if item.item_type == "paper":
        if item.arxiv_id:
            keys.append(f"arxiv:{item.arxiv_id}")
        if item.doi:
            keys.append(f"doi:{item.doi.lower()}")
            match = re.fullmatch(r"10\.48550/arxiv\.(.+?)(?:v\d+)?", item.doi, re.I)
            if match:
                keys.append(f"arxiv:{match.group(1).lower()}")
        if item.raw.get("openalex_id"):
            keys.append(f"openalex:{item.raw['openalex_id']}")
        if item.raw.get("semantic_scholar_id"):
            keys.append(f"semantic_scholar:{item.raw['semantic_scholar_id']}")
        if item.raw.get("google_scholar_id"):
            keys.append(f"google_scholar:{item.raw['google_scholar_id']}")
        if item.raw.get("dblp_key"):
            keys.append(f"dblp:{item.raw['dblp_key']}")
        if item.raw.get("core_id"):
            keys.append(f"core:{item.raw['core_id']}")
        if item.raw.get("ieee_article_number"):
            keys.append(f"ieee:{item.raw['ieee_article_number']}")
        if item.raw.get("openreview_id"):
            keys.append(f"openreview:{item.raw['openreview_id']}")
        if item.raw.get("pmlr_id"):
            keys.append(f"pmlr:{item.raw['pmlr_id']}")
        if item.raw.get("neurips_id"):
            keys.append(f"neurips:{item.raw['neurips_id']}")
        title_key = _title_year_key(item)
        if title_key:
            keys.append(title_key)
    return _merge_lists([], keys)


def _title_year_key(item: DigestItem) -> str | None:
    if not item.title:
        return None
    normalized = re.sub(r"[^a-z0-9]+", " ", item.title.lower()).strip()
    if not normalized:
        return None
    year = (item.published_at or item.updated_at or "")[:4]
    return f"title:{normalized}:{year}" if year else f"title:{normalized}"


def _prefer_incoming(existing: DigestItem, incoming: DigestItem) -> bool:
    if existing.source == "arxiv" and incoming.source == "arxiv" and existing.arxiv_id == incoming.arxiv_id:
        return _version_number(incoming.arxiv_version) > _version_number(existing.arxiv_version)
    if existing.item_type != "paper" or incoming.item_type != "paper":
        return False
    return _source_priority(incoming.source) < _source_priority(existing.source)


def _source_priority(source: str) -> int:
    priorities = {"arxiv": 0, "openreview": 1, "pmlr": 2, "neurips": 3, "ieee": 4, "dblp": 5, "core": 6, "openalex": 7, "semantic_scholar": 8, "google_scholar": 9, "crossref": 10}
    return priorities.get(source, 10)


def _ensure_multisource_raw(item: DigestItem) -> None:
    item.raw.setdefault("source_aliases", {})
    item.raw.setdefault("evidence", {"sources": {}})
    aliases = item.raw["source_aliases"]
    if item.source == "arxiv" and item.arxiv_id:
        aliases.setdefault("arxiv", item.arxiv_id)
    if item.source == "openalex" and item.raw.get("openalex_id"):
        aliases.setdefault("openalex", str(item.raw["openalex_id"]))
    if item.source == "semantic_scholar" and item.raw.get("semantic_scholar_id"):
        aliases.setdefault("semantic_scholar", str(item.raw["semantic_scholar_id"]))
    if item.source == "google_scholar" and item.raw.get("google_scholar_id"):
        aliases.setdefault("google_scholar", str(item.raw["google_scholar_id"]))
    if item.source == "crossref" and item.doi:
        aliases.setdefault("crossref", item.doi.lower())
    if item.source == "dblp" and item.raw.get("dblp_key"):
        aliases.setdefault("dblp", str(item.raw["dblp_key"]))
    if item.source == "core" and item.raw.get("core_id"):
        aliases.setdefault("core", str(item.raw["core_id"]))
    if item.source == "ieee" and item.raw.get("ieee_article_number"):
        aliases.setdefault("ieee", str(item.raw["ieee_article_number"]))
    if item.source == "openreview" and item.raw.get("openreview_id"):
        aliases.setdefault("openreview", str(item.raw["openreview_id"]))
    if item.source == "pmlr" and item.raw.get("pmlr_id"):
        aliases.setdefault("pmlr", str(item.raw["pmlr_id"]))
    if item.source == "neurips" and item.raw.get("neurips_id"):
        aliases.setdefault("neurips", str(item.raw["neurips_id"]))
    item.raw["evidence"].setdefault("sources", {}).setdefault(
        item.source,
        {
            "title": item.title,
            "url": item.url,
            "pdf_url": item.pdf_url,
            "pdf_urls": item.raw.get("pdf_urls") or [],
            "abstract": item.abstract,
            "doi": item.doi,
            "venue": item.raw.get("venue"),
            "publication_type": item.raw.get("publication_type"),
            "publication_types": item.raw.get("publication_types"),
            "publication_status": item.raw.get("publication_status"),
            "journal_ref": item.raw.get("journal_ref"),
            "primary_verification": item.raw.get("primary_verification"),
            "primary_landing_verified": item.raw.get("primary_landing_verified"),
                "publication_date": item.published_at,
                "publication_date_precision": item.raw.get("publication_date_precision"),
                "publication_date_basis": item.raw.get("publication_date_basis"),
                "publication_date_source_url": item.raw.get("publication_date_source_url"),
                "publication_stage": item.raw.get("publication_stage"),
                "abstract_source": item.raw.get("abstract_source"),
                "abstract_status": item.raw.get("abstract_status"),
                "official_metadata_status": item.raw.get("official_metadata_status"),
                "metadata_evidence": item.raw.get("metadata_evidence"),
        },
    )


def _merge_raw(left: dict, right: dict) -> dict:
    merged = {**left, **right}
    merged["source_aliases"] = {**(left.get("source_aliases") or {}), **(right.get("source_aliases") or {})}
    left_evidence = (left.get("evidence") or {}).get("sources", {})
    right_evidence = (right.get("evidence") or {}).get("sources", {})
    merged["evidence"] = {"sources": {**left_evidence, **right_evidence}}
    return merged


def _merge_lists(left: list[str], right: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in [*left, *right]:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _version_number(value: str | None) -> int:
    if not value:
        return 0
    try:
        return int(value.lower().lstrip("v"))
    except ValueError:
        return 0
