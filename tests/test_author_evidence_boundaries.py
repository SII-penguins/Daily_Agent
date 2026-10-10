"""Strict author evidence boundaries, including synthetic native rejection cases."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from daily_agent.author_context import (
    build_author_context, research_context_lines, safe_author_uncertainties,
)
from daily_agent.author_research import (
    MAX_AUTHOR_SOURCE_CHARACTERS, MAX_AUTHOR_SOURCES,
    _author_review_payload, _author_review_prompt, _research_prompt,
    _valid_author_proposal, _valid_author_review, _validated_source_report,
    _validated_sources, enrich_selected_author_contexts, validate_author_evidence,
)
from daily_agent.batch_execution import candidate
from daily_agent.models import MaterialRecord
from daily_agent.parent_writer import PendingResponse, digest, import_response, pending
from daily_agent.rendering.editorial import _research_context
from test_author_research import setup, source
from test_stage_execution import normalized, run


TRIAL = json.loads((Path(__file__).parent / 'fixtures/author_trial_rejections.json').read_text())['papers']


def trial_record(row):
    return MaterialRecord(key=row['key'], source='pmlr', item_type='paper',
        title=row['title'], doi=row['doi'], authors=deepcopy(row['authors']),
        url='https://proceedings.mlr.press/v306/' + row['key'].split(':')[1] + '.html',
        paper_document=deepcopy(row['paper_document']))


@pytest.mark.parametrize('row', TRIAL, ids=lambda r: r['key'])
def test_synthetic_frozen_proposals_still_fail_closed(row):
    record = trial_record(row)
    proposal_before = deepcopy(row['proposed'])
    assert _valid_author_proposal(row['proposed'], [record.key])
    valid, report = _validated_source_report(record, row['proposed']['papers'][0]['sources'])
    assert valid == []
    assert report == {'proposed_count': 1, 'eligible_count': 0,
                      'rejected': [{'index': 0, 'reason': row['expected_rejection']}]}
    payload = _author_review_payload(record, [], row['proposed'])
    empty_approval = {**row['review'], 'input_sha256': digest(payload)}
    assert not _valid_author_review(empty_approval, payload)
    assert row['proposed'] == proposal_before


@pytest.mark.parametrize('row', TRIAL, ids=lambda r: r['key'])
def test_synthetic_unbound_prose_cannot_leak_through_either_renderer(row):
    record = trial_record(row)
    record.raw['research_context'] = deepcopy(row['retained_context'])
    context_before = deepcopy(record.raw['research_context'])
    outputs = ['\n'.join(research_context_lines(context_before)), _research_context(record)]
    unbound = [s for s in context_before['uncertainties'] if s[:1].isascii()]
    assert unbound
    for rendered in outputs:
        assert '机构归属尚未核实' in rendered
        assert '具体课题组尚未核实' in rendered
        assert 'Example Hidden Lab' not in rendered
        assert 'Fictional Research Group' not in rendered
        assert 'Project lead' not in rendered and 'project-lead' not in rendered
        assert all(s not in rendered for s in unbound)
    assert record.raw['research_context'] == context_before


def test_prompt_matches_size_count_and_exact_quote_contract():
    prompt = _research_prompt([])
    assert f'at most {MAX_AUTHOR_SOURCES} source entries' in prompt
    assert f'at most {MAX_AUTHOR_SOURCE_CHARACTERS} characters' in prompt
    assert 'json.dumps(source, ensure_ascii=False)' in prompt
    assert 'one exact contiguous verbatim substring' in prompt
    assert 'Do not silently concatenate separated passages' in prompt
    assert 'Do not transcribe the document page by page' in prompt
    assert 'Equal contribution alone is not co_first_author' in prompt
    assert 'project lead alone is not lead_author' in prompt
    review = _author_review_prompt({'proposed_sources': {}})
    assert 'do not add or repair claims through freeform uncertainties' in review


def test_source_size_and_count_limits_are_not_relaxed(tmp_path):
    _, record = setup(tmp_path)
    entry = source(record)
    entry['padding'] = ''
    entry['padding'] = 'x' * (MAX_AUTHOR_SOURCE_CHARACTERS - len(json.dumps(entry, ensure_ascii=False)))
    assert len(json.dumps(entry, ensure_ascii=False)) == 5000
    assert _validated_sources(record, [entry])
    entry['padding'] += 'x'
    assert _validated_source_report(record, [entry])[1]['rejected'][0]['reason'] == 'source_size_limit'
    valid, report = _validated_source_report(record, [source(record)] * 5)
    assert len(valid) == 4
    assert report['rejected'] == [{'index': 4, 'reason': 'source_limit'}]


@pytest.mark.parametrize('mutation', ['joined_passages', 'removed_symbol', 'rewritten_claim'])
def test_frozen_native_quotes_cannot_be_reconstructed(tmp_path, mutation):
    _, record = setup(tmp_path)
    entry = source(record)
    if mutation == 'joined_passages':
        entry['excerpt'] = entry['excerpt'].replace('Correspondence to: Bob Example\n', '')
    elif mutation == 'removed_symbol':
        entry['excerpt'] = entry['excerpt'].replace(',', '')
    else:
        entry['claims'][0]['quote'] = 'AliceExample'
    assert _valid_author_proposal({'papers': [{'key': record.key, 'sources': [entry]}]}, [record.key])
    assert not _validated_sources(record, [entry])


def test_research_line_also_requires_source_bound_quote(tmp_path):
    _, record = setup(tmp_path)
    entry = source(record)
    line = {'text': 'The paper concerns quantum learning.', 'scope': 'paper'}
    entry['research_lines'] = [line]
    assert _validated_source_report(record, [entry])[1]['rejected'][0]['reason'] == 'missing_assertion_claim'
    entry['claims'].append({'kind': 'research_line', 'subject': line['text'], 'quote': record.title})
    assert _valid_author_proposal({'papers': [{'key': record.key, 'sources': [entry]}]}, [record.key])
    assert _validated_sources(record, [entry])


@pytest.mark.parametrize('row', TRIAL, ids=lambda r: r['key'])
def test_trial_rejection_skips_review_and_preserves_metadata_unknowns(tmp_path, monkeypatch, row):
    config, _ = setup(tmp_path)
    normalized(config)
    record = trial_record(row)
    record.raw['research_context'] = deepcopy(row['retained_context'])
    before = deepcopy(record.paper_document)
    calls = []
    def request(root, prompt, timeout, **kwargs):
        calls.append(kwargs['operation'][2])
        assert kwargs['stage'] == 'author_research'
        return deepcopy(row['proposed'])
    monkeypatch.setattr('daily_agent.author_research.request', request)
    with run(config, [candidate(record)]) as execution:
        enrich_selected_author_contexts([record], config, execution=execution)
        assert execution.snapshot()['writer_circuit'] is None
    evidence = record.raw['author_research_evidence']
    assert calls == ['author_research']
    assert evidence['status'] == 'no_supported_sources' and 'review' not in evidence
    assert evidence['proposed'] == row['proposed']
    assert not validate_author_evidence(record, evidence)
    assert record.raw['author_research_sources'] == []
    assert record.raw['research_context']['institutions'] == []
    assert record.raw['research_context']['labs'] == []
    assert '未进入独立核验' in ' '.join(record.raw['research_context']['uncertainties'])
    assert not list((tmp_path / 'data/author_context/research-v2').glob('*.json'))
    assert record.paper_document == before
    assert 'Example Hidden Lab' not in json.dumps(record.raw['research_context'], ensure_ascii=False)
    assert 'Fictional Research Group' not in json.dumps(record.raw['research_context'], ensure_ascii=False)


def test_real_queue_empty_proposal_resumes_without_an_empty_review_job(tmp_path):
    config, record = setup(tmp_path)
    normalized(config)
    frozen = [candidate(record)]
    with run(config, frozen) as execution:
        with pytest.raises(PendingResponse):
            enrich_selected_author_contexts([record], config, execution=execution)
    job, = pending(tmp_path)
    response = {'papers': [{'key': record.key, 'sources': [],
                           'uncertainties': ['UNVERIFIED UNIVERSITY is confirmed.']}]}
    import_response(tmp_path, job['job_id'], response, 'researcher')
    answer_path = tmp_path / 'data/writer-queue' / (job['job_id'] + '.answer.json')
    answer_before = answer_path.read_bytes()
    for _ in range(2):
        with run(config, frozen) as execution:
            enrich_selected_author_contexts([record], config, execution=execution)
            assert execution.snapshot()['writer_circuit'] is None
            assert [op['identity'][2] for op in execution.snapshot()['operations'].values()] == ['author_research']
    assert not pending(tmp_path)
    assert len(list((tmp_path / 'data/writer-queue').glob('*.job.json'))) == 1
    assert answer_path.read_bytes() == answer_before
    assert record.raw['author_research_evidence']['status'] == 'no_supported_sources'
    assert 'UNVERIFIED UNIVERSITY' not in json.dumps(record.raw['research_context'])


@pytest.mark.parametrize('approved', [True, False])
def test_model_prose_is_audit_only_even_after_review_and_cache(tmp_path, monkeypatch, approved):
    config, record = setup(tmp_path)
    normalized(config)
    frozen = [candidate(record)]
    calls = []
    def request(root, prompt, timeout, **kwargs):
        calls.append(kwargs['stage'])
        if kwargs['stage'] == 'author_research':
            return {'papers': [{'key': record.key, 'sources': [source(record)],
                                'uncertainties': ['UNVERIFIED UNIVERSITY is confirmed.']}]}
        payload = json.loads(prompt.split('\nREVIEW_INPUT:', 1)[1])
        return {'input_sha256': digest(payload), 'approved_source_hashes': [digest(source(record))] if approved else [],
                'uncertainties': {record.key: ['UNVERIFIED LAB and project-lead marker confirmed.']}}
    monkeypatch.setattr('daily_agent.author_research.request', request)
    with run(config, frozen) as execution:
        enrich_selected_author_contexts([record], config, execution=execution)
        evidence = record.raw['author_research_evidence']
        assert validate_author_evidence(record, evidence)
        enrich_selected_author_contexts([record], config, execution=execution)
    assert calls == ['author_research', 'review']
    assert 'UNVERIFIED LAB' in json.dumps(record.raw['author_research_evidence'])
    assert 'UNVERIFIED UNIVERSITY' in json.dumps(record.raw['author_research_evidence'])
    assert 'UNVERIFIED' not in json.dumps(record.raw['research_context'])
    assert ('Institute X' in _research_context(record)) == approved
    assert '仍有未核实事项' in _research_context(record)
    cached, = (tmp_path / 'data/author_context/research-v2').glob('*.json')
    assert 'UNVERIFIED' not in json.dumps(json.loads(cached.read_text())['uncertainties'])


def test_legacy_path_skips_empty_review_and_filters_cached_prose(tmp_path, monkeypatch):
    config, record = setup(tmp_path)
    calls = []
    def request(root, prompt, timeout, **kwargs):
        calls.append(kwargs['stage'])
        return {'papers': [{'key': record.key, 'sources': [],
                            'uncertainties': ['UNVERIFIED LAB affiliation confirmed.']}]}
    monkeypatch.setattr('daily_agent.author_research.request', request)
    enrich_selected_author_contexts([record], config)
    assert calls == ['author_research']
    assert record.raw['author_research_audit']['status'] == 'no_supported_sources'
    assert 'UNVERIFIED' not in json.dumps(record.raw['research_context'])
    assert not [p for p in (tmp_path / 'data/author_context/research-v1').glob('*.json') if not p.name.startswith('input-')]


def test_unverified_metadata_cannot_gain_roles_via_renderer(tmp_path):
    _, record = setup(tmp_path)
    context = build_author_context(record.to_digest_item())
    context['authors'][-1]['roles'] = ['corresponding_author', 'lead_author']
    context['uncertainties'] = ['Marker says PI at UNVERIFIED LAB']
    record.raw['research_context'] = context
    assert '通讯作者尚未核实' in ' '.join(safe_author_uncertainties(context))
    assert '通讯作者：Bob Example' not in '\n'.join(research_context_lines(context))
    html = _research_context(record)
    assert '元数据，未独立核实' in html
    assert '主要作者' not in html and 'UNVERIFIED LAB' not in html
