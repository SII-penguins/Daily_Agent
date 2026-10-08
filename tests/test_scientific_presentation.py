from copy import deepcopy
from html import escape
from datetime import date

from daily_agent.rendering import scientific
from daily_agent.rendering.editorial import scientific_block, render_editorial_html
from test_scientific_analysis import fixture, item
from daily_agent.scientific_analysis import analyze_papers


def test_claim_boundaries_and_provenance_runs_are_lossless(monkeypatch):
    claims = [{'id':str(i), 'kind':kind, 'text':word*size, 'evidence':[{'chunk_id':'c0001'}]}
              for i,(kind,word,size) in enumerate([
                  ('interpretation','甲',140), ('interpretation','乙',130),
                  ('interpretation','丙',100), ('author_claim','丁',40),
                  ('unresolved','戊',70), ('unresolved','己',50)])]
    original = deepcopy(claims)
    monkeypatch.setattr(scientific, 'reviewed_analysis', lambda _: {'paragraphs':[{'id':'insight','claims':claims}]})
    paragraphs = scientific.analysis_reading_blocks(None)['insight']
    assert [len(p['claims']) for p in paragraphs] == [1,2,1,2]
    assert [p['label'] for p in paragraphs] == ['证据支持的解读','','作者主张','尚待回答']
    assert [c for p in paragraphs for c in p['claims']] == original
    assert claims == original
    assert all(len({c['kind'] for c in p['claims']}) == 1 for p in paragraphs)


def test_long_claim_is_not_split_and_html_is_escaped(monkeypatch):
    claim={'id':'<id>', 'kind':'author_claim', 'text':'<script>'+ '甲'*600, 'evidence':[]}
    monkeypatch.setattr(scientific, 'reviewed_analysis', lambda _: {'paragraphs':[{'id':'insight','claims':[claim]}]})
    paragraphs=scientific.analysis_reading_blocks(None)['insight']
    html=scientific_block('科学启发', paragraphs)
    assert html.count('<p ') == 1
    assert '<script>' not in html and 'data-claim-id="&lt;id&gt;"' in html
    assert escape(claim['text']) in html


def test_verified_claims_and_cache_identity_unchanged_by_layout(tmp_path):
    config, record, draft, candidate, calls, original, invoke, invoked = fixture(tmp_path)
    analyze_papers(config, [record], [draft], invoke=invoke)
    approved=item(record,draft)
    before=deepcopy(approved.to_dict())
    identity=record.reading['scientific_analysis']['identity']
    html=render_editorial_html([approved], date(2026,10,8))
    for section in candidate['paragraphs']:
        for claim in section['claims']:
            assert escape(claim['text']) in html
            assert f'data-claim-id="{claim["id"]}"' in html
    assert approved.to_dict() == before
    analyze_papers(config, [record], [draft], invoke=invoke)
    assert invoked == ['draft','review']
    assert record.reading['scientific_analysis']['identity'] == identity
    # The existing full source/review gate is still the only entrance.
    record.paper_document['chunks'][0]['text'] += ' changed source'
    assert scientific.analysis_reading_blocks(approved) == {}


def test_reviewed_narrative_is_main_flow_without_duplicate_legacy_lead(tmp_path):
    config, record, draft, candidate, calls, original, invoke, invoked = fixture(tmp_path)
    analyze_papers(config, [record], [draft], invoke=invoke)
    approved = item(record, draft)
    html = render_editorial_html([approved], date(2026,10,8))
    assert '<p class="lead">' not in html
    assert 'class="scientific-reading"' in html
    assert 'class="scientific-analysis"' not in html  # no accordion hides the core reading
    assert html.index('class="scientific-reading"') < html.index('class="content-details supplementary-details"')
    assert html.index('研究问题与方法') > html.index('方法细节与来源核验')
    assert '研究问题与方法' in html and '适用边界' in html
