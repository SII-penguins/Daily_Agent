"""New native daily-selection contracts require independently reviewed science."""
from copy import deepcopy
from datetime import date

import pytest

from daily_agent.batch_execution import Execution, candidate, existing_review_validator
from daily_agent.cloud_workflow import _qualifying_rows
from daily_agent.deferred_review_cache import _load, _supported, _core_cache_supported, draft_report_items
from daily_agent.editorial import approve_publication
from daily_agent.models import EditorialReview
from daily_agent.scientific_analysis import analyze_papers, scientific_analysis_valid
from daily_agent.scheduling import _validate_native_approvals, StageFailure
from daily_agent.source_evidence_policy import (
    audit_reading, native_quality, native_review_supported, configured_policy,
    native_reading_config, native_qualification_contract,
)
from test_native_claim_pixel_review import example
from test_native_scientific_pixels import native_science, complete_science, as_item


def rows(record, draft):
    record.detail = deepcopy(draft.draft_fields)
    return [as_item(record, draft).to_dict()]


def publication_config(config):
    config.delivery = {}
    config.quota = {'max_items': 1, 'paper_target': 1, 'github_target': 0}
    return config


def test_core_only_is_reusable_but_never_complete_selection_or_ready(native_science):
    record, draft, config, _ = native_science
    config = publication_config(config)
    assert native_quality(record, draft)
    assert record.reading['source_evidence']['schema_version'] == 2
    assert record.reading['source_evidence']['qualification_contract'] == native_qualification_contract()
    assert not native_review_supported(record, draft) and not _supported(record, draft, config=config)
    assert _core_cache_supported(record, draft, config=config)
    approved_rows = rows(record, draft)
    report = audit_reading(approved_rows)
    assert not report['all_passed'] and report['quality_passed_count'] == 0
    assert report['papers'][0]['completion_scope'] == 'core_only'
    assert not _qualifying_rows(approved_rows, config=config)[0]
    assert not approve_publication(config, [record], [draft], [EditorialReview(record.key, 'PASS')])
    with pytest.raises(StageFailure): _validate_native_approvals(approved_rows, config)
    with pytest.raises(StageFailure): _validate_native_approvals(approved_rows)  # sealed/configless recheck
    calls = []
    def original(config, values, use_llm, **kwargs):
        calls.append(len(values))
        return [deepcopy(draft)]
    draft_report_items(config, [record], True, original)
    restored = _load(config, record)
    assert restored is not None and _core_cache_supported(*restored, config=config)
    assert not _supported(*restored, config=config)
    draft_report_items(config, [record], True, original)
    assert calls == [1]  # intermediate reuse avoids rereading/rewriting CORE


@pytest.mark.parametrize('mode', ['missing', 'writer_only', 'no_review', 'wrong_pixels', 'forged_scope'])
def test_every_final_sink_rejects_missing_or_invalid_scientific_proof(native_science, mode):
    record, draft, config, analysis = native_science
    config = publication_config(config)
    if mode in {'no_review', 'wrong_pixels'}:
        complete_science(native_science)
        if mode == 'no_review': record.reading['scientific_analysis'].pop('review')
        else: record.reading['scientific_analysis']['review']['checks'][0]['image_sha256s'] = []
    elif mode == 'writer_only':
        draft.draft_fields['scientific_analysis'] = analysis
        record.reading['scientific_analysis'] = {'status': 'passed', 'analysis': analysis}
    elif mode == 'forged_scope':
        record.reading['scientific_analysis'] = {'status': 'passed', 'completion_scope': 'core_and_scientific_analysis'}
    data = rows(record, draft)
    assert not scientific_analysis_valid(record, draft)
    assert not _supported(record, draft, config=config)
    assert not audit_reading(data)['all_passed']
    assert not _qualifying_rows(data, config=config)[0]
    assert not approve_publication(config, [record], [draft], [EditorialReview(record.key, 'PASS')])
    with pytest.raises(StageFailure): _validate_native_approvals(data)


def test_valid_independent_science_passes_complete_selection_and_configless_ready(native_science):
    record, draft, config, _ = native_science
    config = publication_config(config)
    complete_science(native_science)
    data = rows(record, draft)
    assert scientific_analysis_valid(record, draft)
    assert native_review_supported(record, draft) and _supported(record, draft, config=config)
    assert audit_reading(data)['all_passed']
    assert audit_reading(data)['papers'][0]['completion_scope'] == 'core_and_scientific_analysis'
    assert _qualifying_rows(data, config=config)[0] == data
    assert approve_publication(config, [record], [draft], [EditorialReview(record.key, 'PASS')])
    _validate_native_approvals(data, config)
    _validate_native_approvals(data)


@pytest.mark.parametrize('value', [False, 1, 'true', None])
def test_native_config_cannot_opt_out_of_required_science(native_science, value):
    record, draft, config, _ = native_science
    config.sources['reading']['require_scientific_analysis'] = value
    with pytest.raises(ValueError): configured_policy(config)
    with pytest.raises(ValueError):
        with Execution(config.root / 'issue', 'required-science', [candidate(record)], config, protocol='offline-required'):
            pass


@pytest.mark.parametrize('mutation', ['missing', 'false', 'int_true', 'string', 'schema_bool', 'old_schema', 'deleted_marker'])
def test_missing_or_typed_false_requirement_cannot_downgrade_new_native(native_science, mutation):
    record, draft, config, _ = native_science
    marker = record.reading['source_evidence']
    if mutation == 'missing': marker.pop('qualification_contract')
    elif mutation == 'false': marker['qualification_contract']['require_scientific_analysis'] = False
    elif mutation == 'int_true': marker['qualification_contract']['require_scientific_analysis'] = 1
    elif mutation == 'string': marker['qualification_contract']['require_scientific_analysis'] = 'true'
    elif mutation == 'schema_bool': marker['qualification_contract']['schema_version'] = True
    elif mutation == 'old_schema': marker['schema_version'] = 1
    else: record.reading.pop('source_evidence')
    assert not native_quality(record, draft)
    assert not native_review_supported(record, draft)
    assert not _core_cache_supported(record, draft, config=config)
    assert not audit_reading(rows(record, draft))['all_passed']


def test_native_requirement_frozen_in_execution_but_excluded_from_raw_reader(native_science):
    record, draft, config, _ = native_science
    config.sources['reading']['require_scientific_analysis'] = True
    raw = native_reading_config(config)
    assert 'require_scientific_analysis' not in raw.sources['reading']
    assert config.sources['reading']['require_scientific_analysis'] is True
    with Execution(config.root / 'issue', 'required-science', [candidate(record)], config, protocol='offline-required') as active:
        assert active.contract['settings']['native_qualification_contract'] == native_qualification_contract()


@pytest.mark.parametrize('mutation', ['missing', 'invalid', 'contradictory'])
def test_noncloud_selection_rejects_missing_invalid_or_contradictory_native_policy(native_science, mutation):
    record, draft, config, _ = native_science
    config = publication_config(config)
    complete_science(native_science)
    original_review = EditorialReview(record.key, 'PASS')
    assert approve_publication(config, [record], [draft], [original_review])
    if mutation == 'missing': record.reading.pop('source_evidence')
    elif mutation == 'invalid': record.reading['source_evidence']['policy'] = 'unknown_policy'
    else: config.sources['reading']['source_evidence_policy'] = 'strict_fidelity_v1'
    for _ in range(2):
        assert not approve_publication(config, [record], [draft], [original_review])
        assert not _qualifying_rows(rows(record, draft), config=config)[0]
        assert not _supported(record, draft, config=config)
        with pytest.raises(StageFailure): _validate_native_approvals(rows(record, draft), config)
