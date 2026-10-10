"""Small, explicit official-candidate handoff for future paper-first hosts.

Only public title/abstract/metadata acquisition. No PDF acquisition, scientific
approval, issue enrollment, publication, delivery or archive completeness claim.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from pathlib import Path

from daily_agent.config import load_config
from daily_agent.connectors.crossref import fetch_preferred_journals
from daily_agent.connectors.nature import fetch_nature
from daily_agent.connectors.pmlr import fetch_pmlr
from daily_agent.connectors.official_metadata import MetadataBudget, discovery_bounds, exact_date_in_window, fingerprint
from daily_agent.scoring.relevance import passes_topic_gate
from daily_agent.scoring.publication import publication_dois, _same_title
from daily_agent.workflow_state import atomic_json, atomic_bytes


def collect_official_candidates(config, target_date):
    coverage = {"window": [str(x) for x in discovery_bounds(config, target_date)],
                "mode": "official_metadata_only", "complete_archive": False,
                "pmlr": {}, "journal_leads": {}, "nature": {}, "failures": []}

    def collect(name, callback):
        try:
            return callback()
        except Exception as exc:
            # Failure remains visible; cached/partial evidence stays usable.
            coverage["failures"].append({"source": name, "type": type(exc).__name__,
                "message": str(exc), "targets": getattr(exc, "failures", [])})
            return getattr(exc, "partial_items", [])

    pmlr = collect("pmlr", lambda: fetch_pmlr(config, target_date, coverage=coverage["pmlr"]))
    # One shared cap: at most16 journal queries +8RSS +24new landing requests.
    # Journal broad search does not run here and cannot consume this budget.
    budget = MetadataBudget(max_requests=48, max_bytes=24 * 1024**2, seconds=150)
    leads = collect("journal_leads", lambda: fetch_preferred_journals(config, target_date,
        coverage=coverage["journal_leads"], budget=budget))
    nature = collect("nature", lambda: fetch_nature(config, target_date,
        discovery_seeds=leads, coverage=coverage["nature"], budget=budget))
    coverage["nature_and_journals_total"] = {"requests": budget.requests, "bytes": budget.bytes}
    coverage["returned"] = len(pmlr) + len(nature)
    return [*pmlr, *nature], coverage


def official_publication_date_verified(item):
    """Registry/RSS dates stay hints until the exact official record proves them."""
    raw = item.raw
    if not isinstance(item.published_at, str) or len(item.published_at) != 10:
        return False
    try:
        date.fromisoformat(item.published_at)
    except ValueError:
        return False
    if raw.get("publication_date_precision") != "day" or len(publication_dois(item)) > 1:
        return False
    proof = raw.get("primary_verification") or {}
    if (item.source in {"pmlr", "nature"} and raw.get("primary_landing_verified") is True
            and proof.get("verified") is True and proof.get("url") == item.url
            and _same_title(proof.get("title"), item.title)
            and proof.get("venue") == raw.get("venue")
            and proof.get("published_at") == item.published_at):
        return True
    index = raw.get("index_evidence") or {}
    return bool(item.source == "pmlr" and raw.get("official_metadata_status") == "official_index_verified"
        and raw.get("publication_date_basis") == "official_volume_publication"
        and raw.get("publication_date") == item.published_at and raw.get("publication_date_evidence")
        and index.get("url") == raw.get("publication_date_source_url")
        and index.get("response_sha256") and _same_title(index.get("title"), item.title))


def export_candidate_handoff(config, target_date, output_root):
    """Export candidates and primary citation extracts, not a ready-to-run manifest."""
    root = Path(output_root).resolve()
    items, coverage = collect_official_candidates(config, target_date)
    bounds = discovery_bounds(config, target_date)
    rows = []
    for item in items:
        row = item.to_dict()
        official_date = official_publication_date_verified(item)
        row["publication_date_status"] = "official_verified" if official_date else "date_unresolved"
        if not official_date:
            row["discovery_date_hint"] = item.published_at
        row["recent_topic_eligible"] = official_date and exact_date_in_window(item.published_at, bounds) and passes_topic_gate(item, config)
        # A formal-looking URL or indexer record is insufficient for this handoff.
        proof = item.raw.get("primary_verification") or {}
        evidence = item.raw.get("metadata_evidence") or {}
        excerpt = evidence.get("citation_excerpt")
        if (official_date and item.raw.get("primary_landing_verified") is True and proof.get("verified") is True
                and proof.get("url") == item.url and excerpt
                and item.raw.get("publication_stage") != "accepted_manuscript"):
            packet = ("PRIMARY PUBLISHER CITATION EXTRACT (not full page or full text)\n"
                      f"URL: {item.url}\nResponse SHA256: {evidence.get('response_sha256', '')}\n\n" + excerpt)
            content = packet.encode()
            if len(content) <= 100000:
                path = root / "primary-evidence" / (fingerprint([item.url, packet]) + ".txt")
                atomic_bytes(path, content)
                row["publication"] = {"venue": item.raw.get("venue"), "url": item.url,
                    "evidence_path": str(path), "quote": excerpt}
        row["handoff_status"] = ("metadata_candidate_requires_full_reading" if row["recent_topic_eligible"]
            else "discovery_lead_requires_official_verification" if not official_date else "outside_window_or_topic")
        rows.append(row)
    atomic_json(root / "candidates.json", rows)
    atomic_json(root / "coverage.json", coverage)
    return rows, coverage


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="Configuration and persistent metadata-cache root")
    parser.add_argument("--date", required=True)
    parser.add_argument("--output", required=True, help="New candidate/evidence output directory")
    args = parser.parse_args()
    target = datetime.fromisoformat(args.date).replace(tzinfo=timezone.utc)
    export_candidate_handoff(load_config(args.root), target, args.output)


if __name__ == "__main__":
    main()
