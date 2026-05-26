from __future__ import annotations

import re

from daily_agent.models import DigestItem


def deduplicate_items(items: list[DigestItem]) -> list[DigestItem]:
    merged: dict[str, DigestItem] = {}
    identity_index: dict[str, str] = {}
    for item in items:
        _ensure_multisource_raw(item)
        keys = _identity_keys(item)
        key = next((identity_index[value] for value in keys if value in identity_index), item.canonical_key())
        if key not in merged:
            merged[key] = item
            for value in keys:
                identity_index[value] = key
            continue
        merged[key] = _merge_items(merged[key], item)
        for value in _identity_keys(merged[key]):
            identity_index[value] = key
    return list(merged.values())


def _merge_items(existing: DigestItem, incoming: DigestItem) -> DigestItem:
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
        if item.raw.get("openalex_id"):
            keys.append(f"openalex:{item.raw['openalex_id']}")
        if item.raw.get("semantic_scholar_id"):
            keys.append(f"semantic_scholar:{item.raw['semantic_scholar_id']}")
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
    priorities = {"arxiv": 0, "openreview": 1, "pmlr": 2, "neurips": 3, "ieee": 4, "openalex": 5, "semantic_scholar": 6, "crossref": 7}
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
    if item.source == "crossref" and item.doi:
        aliases.setdefault("crossref", item.doi.lower())
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
            "abstract": item.abstract,
            "doi": item.doi,
            "venue": item.raw.get("venue"),
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
