"""Durable, auditable review deferrals; independent of publication/delivery state."""
from __future__ import annotations

from datetime import date
from daily_agent.paper_document import version_identity
from daily_agent.source_evidence_policy import audit_reading
from daily_agent.scoring.dedup import _identity_keys


def retain_quota_deferred(config, library, shortlist, drafts, reviews, approved, run_date):
    """Only fully supported, reviewed papers excluded by a full quota are deferred.

    Unreviewed candidates remain in the library without a quality endorsement.
    A failed review, stale version, delivered paper, or delivery reservation must
    never acquire a quota-deferred label merely because it was not selected.
    """
    from daily_agent.cloud_workflow import reserved_delivery_identities
    reserved = reserved_delivery_identities(config, run_date)
    selected = {item.key for item in approved}
    delivered = {identity for record in library.values() if record.published_dates and not record.update_label
                 for identity in _identity_keys(record.to_digest_item())}
    passed = {review.key for review in reviews if review.verdict == 'PASS'}
    drafted = {draft.key for draft in drafts}
    quality = {row['key'] for row in audit_reading([r.to_dict() for r in shortlist])['papers'] if row['quality_passed']}
    full = len(approved) >= int(config.quota.get('max_items', 10))
    papers_full = sum(item.item_type == 'paper' for item in approved) >= int(config.quota.get('paper_target', 8))
    reviewed = {record.key for record in shortlist}
    for record in library.values():
        old = record.raw.get('pool_deferral') or {}
        identities = {record.key, *_identity_keys(record.to_digest_item())}
        disallowed = (record.key in selected or record.published_dates or identities & reserved
                      or (identities & delivered and not record.update_label)
                      or record.quality_status in {'rejected', 'archived'}
                      or old.get('version_identity') not in (None, version_identity(record)))
        if disallowed:
            record.raw.pop('pool_deferral', None)
            continue
        if record.key not in reviewed:
            review_state = record.raw.get('pool_review') or {}
            if review_state.get('status') == 'unsupported_or_failed' and review_state.get('version_identity') == version_identity(record):
                record.raw.pop('pool_deferral', None)
                continue
            if old.get('status') == 'quota_deferred' and old.get('version_identity') == version_identity(record):
                # A previous exact-version approval is not downgraded merely
                # because today's quota was filled before revisiting it.
                if full or papers_full:
                    old['last_deferred_on'] = run_date.isoformat()
                continue
            # Screening-only overflow is kept separately from reviewed/approved
            # papers. It still needs every expensive evidence stage next time.
            from daily_agent.discovery_window import within_discovery_window
            from daily_agent.scoring.relevance import passes_topic_gate
            if (record.item_type == 'paper' and (full or papers_full)
                    and passes_topic_gate(record, config) and record.score >= float(config.sources.get('selection', {}).get('historical_min_score', 15))
                    and within_discovery_window(record, config, run_date)):
                record.raw['pool_deferral'] = {
                    'status': 'screening_deferred', 'reason': 'daily_count_limit_before_review',
                    'first_deferred_on': old.get('first_deferred_on') or run_date.isoformat(),
                    'last_deferred_on': run_date.isoformat(), 'version_identity': version_identity(record),
                    'quality_basis': 'topic_and_source_screening_only', 'review_required': True,
                    'revisit_policy': 'rescore_each_issue_no_freshness_expiry',
                }
            continue
        if record.item_type != 'paper' or record.key not in passed & drafted & quality:
            record.raw.pop('pool_deferral', None)
            if record.item_type == 'paper':
                record.raw['pool_review'] = {'status': 'unsupported_or_failed',
                    'version_identity': version_identity(record), 'reviewed_on': run_date.isoformat()}
            continue
        record.raw.pop('pool_review', None)
        if full or papers_full:
            record.raw['pool_deferral'] = {
                'status': 'quota_deferred', 'reason': 'daily_count_limit',
                'first_deferred_on': old.get('first_deferred_on') or run_date.isoformat(),
                'last_deferred_on': run_date.isoformat(),
                'version_identity': version_identity(record),
                'quality_basis': 'full_reading_and_supported_claims',
                'revisit_policy': 'rescore_each_issue_no_freshness_expiry',
            }


def deferred_revisit_score(record, config, run_date):
    data = record.raw.get('pool_deferral') or {}
    valid_state = (data.get('status'), data.get('reason')) in {('quota_deferred', 'daily_count_limit'), ('screening_deferred', 'daily_count_limit_before_review')}
    if (not valid_state
            or data.get('version_identity') != version_identity(record)):
        return 0.0
    try:
        days = max(0, (run_date - date.fromisoformat(data['first_deferred_on'])).days)
    except (ValueError, KeyError, TypeError):
        return 0.0
    settings = config.sources.get('selection', {})
    return min(max(0., float(settings.get('deferred_revisit_bonus_cap', 6))),
               days * max(0., float(settings.get('deferred_revisit_bonus_per_day', .5))))


def same_source_version(incoming, existing):
    """Keep expensive work only when available source revision signals agree."""
    if version_identity(incoming) != version_identity(existing):
        return False
    for field in ('source_updated_at', 'title', 'abstract'):
        left, right = getattr(incoming, field, None), getattr(existing, field, None)
        if left and right and str(left).strip() != str(right).strip():
            return False
    return True
