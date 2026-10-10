"""Offline regressions for optional author schema failures within a shared batch."""
from copy import deepcopy
import json

import pytest

from daily_agent import editorial, reading
from daily_agent.author_research import (
    _research_prompt, _valid_author_proposal, enrich_selected_author_contexts,
    validate_author_evidence,
)
from daily_agent.batch_execution import Execution, candidate
from daily_agent.parent_writer import (
    PendingResponse, claim, digest, import_response, pending,
)
from daily_agent.scientific_analysis import analyze_papers
from test_author_research import setup as author_setup, source
from test_scientific_analysis import fixture as science_fixture
from test_stage_execution import StageClock, admit_fake, normalized, run


def proposal(record, *, invalid=False):
    evidence = source(record)
    if invalid:
        # The diagnostic's exact structural failure: an unknown lab URL was null.
        evidence['labs'] = [{'name': 'UNREVIEWED LAB', 'url': None,
                             'relationship': 'explicit_author_affiliation'}]
    return {'papers': [{'key': record.key, 'sources': [evidence], 'uncertainties': []}]}


@pytest.mark.parametrize('failure_stage', ['author_research', 'author_review'])
def test_author_schema_failure_keeps_drafting_and_independent_review_available(tmp_path, monkeypatch, failure_stage):
    config, record, original_draft, _, _, _, science_reply, science_calls = science_fixture(tmp_path)
    normalized(config)
    frozen = [candidate(record)]
    calls = []
    def author_request(root, prompt, timeout, **kwargs):
        operation = kwargs['operation']
        admit_fake(kwargs['execution'], operation, prompt, role=kwargs['stage'])
        calls.append(operation[2])
        if operation[2] == 'author_research':
            result = proposal(record, invalid=failure_stage == 'author_research')
            if failure_stage == 'author_review':
                # The science fixture has no author front matter. Give this
                # reviewer-schema test an eligible synthetic official source;
                # an empty source set must now skip review instead.
                entry = result['papers'][0]['sources'][0]
                entry.update(source_kind='official_author',
                             source_url='https://authors.example.edu/alice',
                             excerpt='\n'.join(c['quote'] for c in entry['claims']))
            return result
        return {'input_sha256': 'wrong', 'approved_source_hashes': []}
    def draft_request(root, prompt, timeout, **kwargs):
        admit_fake(kwargs['execution'], kwargs['operation'], prompt, role=kwargs['stage'])
        calls.append(kwargs['operation'][2])
        fields = deepcopy(original_draft.draft_fields)
        fields['evidence_from_source'] = '模拟比较中的来源引句'
        return [{'key': record.key, 'draft_fields': fields,
                 'evidence_used': [record.url], 'claim_evidence': original_draft.claim_evidence}]
    def semantic_reply(prompt, timeout, **kwargs):
        admit_fake(kwargs['execution'], kwargs['operation'], prompt, role='review')
        calls.append(kwargs['operation'][2])
        return {'checks': [{'field': field, 'supported': True, 'reason': 'Exact source evidence checked'}
                           for field in draft.verification['valid_fields']]}
    monkeypatch.setattr('daily_agent.author_research.request', author_request)
    monkeypatch.setattr('daily_agent.parent_writer.request', draft_request)
    with run(config, frozen) as execution:
        document, reading_before = deepcopy(record.paper_document), deepcopy(record.reading)
        enrich_selected_author_contexts([record], config, execution=execution)
        assert execution.snapshot()['writer_circuit'] is None
        evidence = record.raw['author_research_evidence']
        assert evidence['status'] == 'not_reviewed'
        assert evidence['failure'] == {'kind': 'schema', 'scope': 'author_context',
            'stage': failure_stage, 'detail': 'Invalid author research response' if failure_stage == 'author_research'
                                           else 'Invalid independent author review response'}
        assert not validate_author_evidence(record, evidence)
        assert not record.raw.get('author_research_sources')
        assert 'UNREVIEWED LAB' not in json.dumps(record.raw['research_context'])
        assert not list((tmp_path / 'data/author_context/research-v2').glob('*.json'))
        assert record.paper_document == document and record.reading == reading_before
        draft, = editorial._draft_with_llm_batch([record], execution=execution,
                                                 settings=editorial._llm_writer_settings(config))
        assert draft.writer_notes.startswith('LLM写手草稿')
        reading.verify_draft(draft, record)
        assert draft.verification.get('semantic_support') != 'model_checked'
        reading.semantic_review(draft, record, semantic_reply, 120, execution=execution)
        assert draft.verification['semantic_support'] == 'model_checked'
        analyze_papers(config, [record], [draft], execution=execution, invoke=science_reply)
        assert record.reading['scientific_analysis']['status'] == 'passed'
        assert science_calls == ['draft', 'review']
        assert calls == (['author_research', 'author_review'] if failure_stage == 'author_review'
                         else ['author_research']) + ['draft', 'semantic']
        assert execution.snapshot()['writer_circuit'] is None


def test_invalid_author_is_immutable_and_does_not_block_another_paper(tmp_path):
    config, first = author_setup(tmp_path)
    normalized(config)
    second = deepcopy(first)
    second.key, second.title = 'pmlr:second', 'Second Quantum Learning'
    frozen = [candidate(first), candidate(second)]
    with run(config, frozen) as execution:
        with pytest.raises(PendingResponse):
            enrich_selected_author_contexts([first], config, execution=execution)
    first_job, = pending(tmp_path)
    bad = proposal(first, invalid=True)
    lease = claim(tmp_path, first_job['job_id'], 'author-researcher')
    import_response(tmp_path, first_job['job_id'], bad, 'author-researcher', claim_token=lease['token'])
    with run(config, frozen) as execution:
        enrich_selected_author_contexts([first], config, execution=execution)
        assert execution.snapshot()['writer_circuit'] is None
        with pytest.raises(PendingResponse):
            enrich_selected_author_contexts([second], config, execution=execution)
    second_job, = pending(tmp_path)
    valid = proposal(second)
    lease = claim(tmp_path, second_job['job_id'], 'author-researcher')
    import_response(tmp_path, second_job['job_id'], valid, 'author-researcher', claim_token=lease['token'])
    with run(config, frozen) as execution:
        with pytest.raises(PendingResponse):
            enrich_selected_author_contexts([second], config, execution=execution)
    review_job, = pending(tmp_path)
    payload = json.loads(review_job['prompt'].split('\nREVIEW_INPUT:', 1)[1])
    review = {'input_sha256': digest(payload), 'approved_source_hashes': [digest(source(second))],
              'uncertainties': {}}
    with pytest.raises(ValueError, match='Independent review'):
        import_response(tmp_path, review_job['job_id'], review, 'author-researcher')
    lease = claim(tmp_path, review_job['job_id'], 'independent-reviewer')
    with pytest.raises(ValueError, match='claim'):
        import_response(tmp_path, review_job['job_id'], review, 'author-researcher', claim_token=lease['token'])
    import_response(tmp_path, review_job['job_id'], review, 'independent-reviewer', claim_token=lease['token'])
    answer_path = tmp_path / 'data/writer-queue' / (first_job['job_id'] + '.answer.json')
    answer_before = answer_path.read_bytes()
    with run(config, frozen) as execution:
        enrich_selected_author_contexts([first, second], config, execution=execution)
        assert not validate_author_evidence(first, first.raw['author_research_evidence'])
        assert validate_author_evidence(second, second.raw['author_research_evidence'])
        assert execution.snapshot()['writer_circuit'] is None
        ops = [op['identity'] for op in execution.snapshot()['operations'].values()]
        assert sorted(ops) == sorted([[first.key, 'primary_writer', 'author_research', 0],
            [second.key, 'primary_writer', 'author_research', 0],
            [second.key, 'primary_writer', 'author_review', 0]])
    assert answer_path.read_bytes() == answer_before
    assert len(list((tmp_path / 'data/writer-queue').glob('*.job.json'))) == 3
    assert not pending(tmp_path)
    assert len(list((tmp_path / 'data/author_context/research-v2').glob('*.json'))) == 1


@pytest.mark.parametrize('failure_stage', ['draft', 'review'])
def test_invalid_science_stays_local_after_optional_author_failure(tmp_path, monkeypatch, failure_stage):
    config, record, draft, _, _, _, reply, _ = science_fixture(tmp_path)
    normalized(config)
    monkeypatch.setattr('daily_agent.author_research.request', lambda *a, **kw: {'papers': {}})
    def invalid_science(prompt, timeout, *, stage):
        return {'invalid': True} if stage == failure_stage else reply(prompt, timeout, stage=stage)
    with run(config, [candidate(record)]) as execution:
        enrich_selected_author_contexts([record], config, execution=execution)
        assert execution.snapshot()['writer_circuit'] is None
        analyze_papers(config, [record], [draft], execution=execution, invoke=invalid_science)
        assert record.reading['scientific_analysis']['status'] == 'failed'
        assert 'scientific_analysis' not in draft.draft_fields
        assert execution.snapshot()['writer_circuit'] is None
        with pytest.raises(PendingResponse):
            editorial._draft_with_llm_batch([record], execution=execution,
                                           settings=editorial._llm_writer_settings(config))
        assert pending(config.root)[0]['stage'] == 'draft'


def test_author_local_failure_keeps_budget_spent_and_exhausted(tmp_path, monkeypatch):
    config, record = author_setup(tmp_path)
    normalized(config)
    config.sources['llm_writer']['run_budget_seconds'] = 4
    clock, calls = StageClock(), []
    def invalid(root, prompt, timeout, **kwargs):
        calls.append(timeout)
        clock.advance(5)
        return {'papers': {}}
    monkeypatch.setattr('daily_agent.author_research.request', invalid)
    with Execution(tmp_path / 'issue', 'batch', [candidate(record)], config,
                   protocol='offline-contract', clock=clock) as execution:
        enrich_selected_author_contexts([record], config, execution=execution)
        state = execution.snapshot()
        assert state['writer_circuit'] is None
        assert state['pools']['primary_writer']['charged'] == 4
        assert state['pools']['primary_writer']['measured'] == 5
        assert state['pools']['primary_writer']['remaining'] == 0
        assert not state['reservations']
        enrich_selected_author_contexts([record], config, execution=execution)
        assert editorial._draft_with_llm_batch([record], execution=execution,
                                              settings=editorial._llm_writer_settings(config)) == []
    assert calls == [4]


def test_author_cancellation_propagates_without_circuit_or_budget_refund(tmp_path, monkeypatch):
    config, record = author_setup(tmp_path)
    normalized(config)
    clock = StageClock()
    def cancel(*args, **kwargs):
        clock.advance(3)
        raise KeyboardInterrupt('cancelled')
    monkeypatch.setattr('daily_agent.author_research.request', cancel)
    with Execution(tmp_path / 'issue', 'batch', [candidate(record)], config,
                   protocol='offline-contract', clock=clock) as execution:
        with pytest.raises(KeyboardInterrupt):
            enrich_selected_author_contexts([record], config, execution=execution)
        state = execution.snapshot()
        assert state['writer_circuit'] is None and not state['reservations']
        assert state['pools']['primary_writer']['charged'] == 3
        assert record.raw['author_research_evidence']['status'] == 'not_reviewed'


def test_lab_unknown_url_is_explicitly_omitted_never_guessed_or_normalized(tmp_path):
    _, record = author_setup(tmp_path)
    invalid = proposal(record, invalid=True)
    original = deepcopy(invalid)
    assert not _valid_author_proposal(invalid, [record.key])
    assert invalid == original
    del invalid['papers'][0]['sources'][0]['labs'][0]['url']
    assert _valid_author_proposal(invalid, [record.key])
    prompt = _research_prompt([])
    assert 'omit url when unknown; never use null or guess a URL' in prompt
