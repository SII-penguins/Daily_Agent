from copy import deepcopy
from datetime import date
import json
import pytest

from daily_agent.models import ApprovedItem
from daily_agent.paper_document import digest
from daily_agent.scientific_analysis import (FACETS, analyze_papers, validate_analysis,
    validate_review, analysis_paragraphs, reviewed_analysis, GAP)
from daily_agent.deferred_review_cache import draft_report_items
from test_deferred_review_cache import setup


def fixture(tmp_path):
    config, record, calls, original = setup(tmp_path)
    draft = draft_report_items(config, [record], True, original)[0]
    quote = 'Compared with the baseline, circuit depth falls on simulated quantum circuits.'
    cid = next(c['id'] for c in record.paper_document['chunks'] if quote in c['text'])
    candidate = {'schema_version': 1, 'paragraphs': []}
    facets = [('insight', ['valuable_insight', 'precise_problem']),
              ('explanation', ['bottleneck', 'minimal_idea', 'complexity']),
              ('argument', ['narrative', 'decisive_evidence', 'unresolved'])]
    for key, tags in facets:
        candidate['paragraphs'].append({'id': key, 'claims': [{'id': key, 'kind': 'interpretation',
            'text': '模拟比较提供了线路设计的证据。', 'facets': tags,
            'evidence': [{'chunk_id': cid, 'quote': quote, 'conditions': 'simulation versus baseline', 'evidence_kind': 'simulation'}]}]})
    invoked = []
    def invoke(prompt, timeout, *, stage):
        invoked.append(stage)
        if stage == 'draft': return deepcopy(candidate)
        data = json.loads(prompt.split('INPUT:\n')[1])
        return {'input_sha256': data['input_sha256'],
                'checks': [{'id': p['claims'][0]['id'], 'supported': True, 'reason': 'Exact evidence supports this qualified inference'} for p in candidate['paragraphs']],
                'facets': [{'facet': f, 'adequate': True, 'reason': 'Specific evidence-grounded answer'} for f in sorted(FACETS)]}
    return config, record, draft, candidate, calls, original, invoke, invoked


def item(record, draft):
    record.reading['claim_evidence'] = deepcopy(draft.claim_evidence)
    record.reading['verification'] = deepcopy(draft.verification)
    return ApprovedItem(draft.key, 'paper', draft.title, record.source, record.url, draft.draft_fields, record)


def test_stage_is_additive_cached_and_source_bound(tmp_path):
    config, record, draft, candidate, calls, original, invoke, invoked = fixture(tmp_path)
    scientific_fields = deepcopy(draft.draft_fields)
    baseline = deepcopy(calls)
    verification = deepcopy(draft.verification)
    analyze_papers(config, [record], [draft], invoke=invoke)
    assert invoked == ['draft', 'review']
    assert {k: v for k,v in draft.draft_fields.items() if k != 'scientific_analysis'} == scientific_fields
    assert draft.verification == verification
    approved = item(record, draft)
    assert reviewed_analysis(approved) == candidate
    assert len(analysis_paragraphs(approved)) == 3
    # Analysis never changes full text or visual reading identities.
    assert calls == baseline
    analyze_papers(config, [record], [draft], invoke=invoke)
    assert invoked == ['draft', 'review']
    assert reviewed_analysis(approved) == candidate
    approved.final_fields['scientific_analysis']['paragraphs'][0]['claims'][0]['text'] = '篡改'
    assert reviewed_analysis(approved) is None


def test_new_interpretation_setting_reuses_all_reading(tmp_path):
    config, record, draft, candidate, calls, original, invoke, invoked = fixture(tmp_path)
    analyze_papers(config, [record], [draft], invoke=invoke)
    baseline = deepcopy(calls)
    config.sources['scientific_analysis'] = {'editorial_revision': 'new scientific framing'}
    second = draft_report_items(config, [record], True, original)[0]
    analyze_papers(config, [record], [second], invoke=invoke)
    assert calls == baseline
    assert invoked == ['draft', 'review', 'draft', 'review']


@pytest.mark.parametrize('mutation', ['incomplete', 'unchecked', 'missing_checks', 'bad_quote', 'visual'])
def test_existing_approval_cannot_be_faked(tmp_path, mutation):
    config, record, draft, candidate, calls, original, invoke, invoked = fixture(tmp_path)
    if mutation == 'incomplete': record.reading['complete'] = False
    elif mutation == 'unchecked': draft.verification['semantic_support'] = 'not_independently_verified'
    elif mutation == 'missing_checks': draft.verification['semantic_checks'] = []
    elif mutation == 'bad_quote': draft.claim_evidence[0]['quote'] = 'This quote was never in the source document'
    else: record.paper_document['source_type'] = 'pdf'
    analyze_papers(config, [record], [draft], invoke=invoke)
    assert not invoked
    assert 'scientific_analysis' not in draft.draft_fields
    assert record.reading['scientific_analysis']['status'] == 'not_reviewed'


def test_rejection_is_bounded_and_visible_without_downgrading_paper(tmp_path):
    config, record, draft, candidate, calls, original, invoke, invoked = fixture(tmp_path)
    before = deepcopy(draft.verification)
    def reject(prompt, timeout, *, stage):
        response = invoke(prompt, timeout, stage=stage)
        if stage == 'review': response['checks'][0]['supported'] = False
        return response
    analyze_papers(config, [record], [draft], invoke=reject)
    analyze_papers(config, [record], [draft], invoke=reject)
    assert invoked == ['draft', 'review']
    assert draft.verification == before
    assert record.reading['scientific_analysis']['status'] == 'failed'
    from daily_agent.rendering.editorial import render_editorial_html
    html = render_editorial_html([item(record, draft)], date(2026,10,8))
    assert GAP in html
    assert candidate['paragraphs'][0]['claims'][0]['text'] not in html


@pytest.mark.parametrize('mutation', ['quote', 'numbers', 'facet', 'duplicate', 'unknown_kind'])
def test_candidate_contract_rejects_ungrounded_or_incomplete(tmp_path, mutation):
    config, record, draft, candidate, *_ = fixture(tmp_path)
    claim = candidate['paragraphs'][0]['claims'][0]
    if mutation == 'quote': claim['evidence'][0]['quote'] = 'An invented original quotation'
    elif mutation == 'numbers': claim['text'] += '收益增加99%。'
    elif mutation == 'facet': claim['facets'].remove('valuable_insight')
    elif mutation == 'duplicate': candidate['paragraphs'][1]['claims'][0]['id'] = claim['id']
    else: claim['kind'] = ['author_claim']
    assert not validate_analysis(candidate, record)


def test_all_renderers_show_reviewed_prose_and_lead_before_figures(tmp_path):
    config, record, draft, candidate, calls, original, invoke, invoked = fixture(tmp_path)
    analyze_papers(config, [record], [draft], invoke=invoke)
    approved = item(record, draft)
    from daily_agent.rendering.editorial import render_editorial_html
    from daily_agent.rendering.html import _render_papers as html_papers
    from daily_agent.rendering.markdown import _render_papers as md_papers
    for text in [render_editorial_html([approved],date(2026,10,8)),
                 html_papers([approved],{approved.key:1},date(2026,10,8)),
                 '\n'.join(md_papers([approved],{approved.key:1},date(2026,10,8)))]:
        assert '最有价值的科学启发' in text
        assert '证据支持的解读' in text
        assert GAP not in text
        assert 'Compared with the baseline' not in text
    record.paper_document['chunks'][0]['text'] += ' Source mutation'
    assert reviewed_analysis(approved) is None


def test_pending_review_resumes_without_rewriting_or_rereading(tmp_path):
    from daily_agent.parent_writer import PendingResponse
    config, record, draft, candidate, calls, original, invoke, invoked = fixture(tmp_path)
    baseline = deepcopy(calls)
    def pending(prompt, timeout, *, stage):
        if stage == 'review': raise PendingResponse('review-job')
        return invoke(prompt, timeout, stage=stage)
    with pytest.raises(PendingResponse):
        analyze_papers(config, [record], [draft], invoke=pending)
    assert 'scientific_analysis' not in draft.draft_fields
    analyze_papers(config, [record], [draft], invoke=invoke)
    assert invoked == ['draft', 'review']
    assert calls == baseline
    assert reviewed_analysis(item(record, draft)) == candidate


def test_expired_analysis_does_not_block_qualified_paper(tmp_path):
    from daily_agent.parent_writer import ExpiredResponse
    config, record, draft, candidate, calls, original, invoke, invoked = fixture(tmp_path)
    before = deepcopy(draft.verification)
    attempted = []
    def expired(prompt, timeout, *, stage):
        attempted.append(stage)
        raise ExpiredResponse('expired-job')
    analyze_papers(config, [record], [draft], invoke=expired)
    analyze_papers(config, [record], [draft], invoke=expired)
    assert attempted == ['draft']
    assert draft.verification == before
    assert record.reading['scientific_analysis']['status'] == 'failed'


def test_analysis_contract_requires_exact_review_hash_and_every_facet(tmp_path):
    config, record, draft, candidate, calls, original, invoke, invoked = fixture(tmp_path)
    analyze_papers(config, [record], [draft], invoke=invoke)
    approved = item(record, draft)
    stored = record.reading['scientific_analysis']
    before = deepcopy(stored['review'])
    stored['review']['facets'].pop()
    assert reviewed_analysis(approved) is None
    stored['review'] = before
    stored['review']['input_sha256'] = '0' * 64
    assert reviewed_analysis(approved) is None


def test_presentation_changes_do_not_invalidate_semantic_or_reading_cache(tmp_path, monkeypatch):
    import daily_agent.scientific_analysis as module
    import daily_agent.rendering.scientific as presentation
    config, record, draft, candidate, calls, original, invoke, invoked = fixture(tmp_path)
    analyze_papers(config, [record], [draft], invoke=invoke)
    before = module.semantic_protocol()
    upstream = deepcopy(calls)
    monkeypatch.setitem(module.LABELS, 'author_claim', '作者报告')
    monkeypatch.setattr(module, 'GAP', '新展示文字')
    monkeypatch.setattr(presentation, 'analysis_paragraphs', lambda item: [])
    monkeypatch.setattr(presentation, 'PARAGRAPH_TARGET', 180)
    assert module.semantic_protocol() == before
    analyze_papers(config, [record], [draft], invoke=invoke)
    assert invoked == ['draft', 'review']
    assert calls == upstream


def test_real_editorial_policy_change_only_recomputes_analysis(tmp_path, monkeypatch):
    import daily_agent.scientific_analysis as module
    config, record, draft, candidate, calls, original, invoke, invoked = fixture(tmp_path)
    analyze_papers(config, [record], [draft], invoke=invoke)
    before = module.semantic_protocol()
    upstream = deepcopy(calls)
    monkeypatch.setattr(module, 'WRITER_PROMPT', module.WRITER_PROMPT.replace('约450至850', '约500至900'))
    assert module.semantic_protocol() != before
    reused = draft_report_items(config, [record], True, original)[0]
    analyze_papers(config, [record], [reused], invoke=invoke)
    assert invoked == ['draft', 'review', 'draft', 'review']
    assert calls == upstream


def test_validator_change_invalidates_analysis_protocol(monkeypatch):
    import daily_agent.scientific_analysis as module
    before = module.semantic_protocol()
    original = module.validate_analysis
    def changed_validation(value, record):
        return original(value, record)
    monkeypatch.setattr(module, 'validate_analysis', changed_validation)
    assert module.semantic_protocol() != before


def test_new_semantic_helper_is_bound_without_updating_allowlist(monkeypatch):
    import daily_agent.scientific_analysis as module
    before = module.semantic_protocol()
    original = module.Path.read_text
    def changed_source(path, *args, **kwargs):
        value = original(path, *args, **kwargs)
        if path == module.Path(module.__file__):
            value += '\n\ndef new_semantic_helper():\n    return "new validation behavior"\n'
        return value
    monkeypatch.setattr(module.Path, 'read_text', changed_source)
    assert module.semantic_protocol() != before


def test_transitive_imported_validator_is_bound(monkeypatch):
    import daily_agent.scientific_analysis as module
    import daily_agent.reading as reading
    before = module.semantic_protocol()
    original = module.inspect.getsource
    def changed_source(value):
        source = original(value)
        if value is reading.compact:
            return source.replace('.lower()', '.upper()')
        return source
    monkeypatch.setattr(module.inspect, 'getsource', changed_source)
    assert module.semantic_protocol() != before


def test_semantic_ast_excludes_only_named_presentation_nodes():
    import ast
    from daily_agent.scientific_analysis import _semantic_tree
    first = 'LABELS = {"a":"old"}\nGAP = "old"\ndef analysis_paragraphs(item):\n return "old"\ndef validator():\n return True\n'
    second = first.replace('old', 'new')
    assert ast.dump(_semantic_tree(first)) == ast.dump(_semantic_tree(second))
    assert ast.dump(_semantic_tree(first)) != ast.dump(_semantic_tree(second.replace('return True', 'return False')))
