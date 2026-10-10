"""Default UI contract tests; synthetic fixtures, not visual or semantic QA."""
from __future__ import annotations

import base64
from copy import deepcopy
from hashlib import sha256
from html import escape
import json
import re

import pytest

from daily_agent import paper_first as workflow
from daily_agent.rendering import paper_first_editorial as editorial
from daily_agent.workflow_state import StateCorrupt
from test_paper_first import (_issue, _draft, _qualify, _state, _job_files,
                              _answer, _review)


def _old_claim_bytes(claim, pdf_url):
    links = ' '.join(f'<a href="{escape(pdf_url, quote=True)}#page={a["page"]}">p.{a["page"]}</a>'
                     for a in claim['anchors'])
    prefix = '编者推断：' if claim['basis'] == 'editor_inference' else ''
    text = f'<p>{prefix}{escape(claim["text"])} <span class="refs">{links}</span></p>'
    if ' '.join(claim['conditions'].split()) not in ' '.join(claim['text'].split()):
        text += f'<p class="conditions">适用条件：{escape(claim["conditions"])}</p>'
    return text


def _extra_draft():
    draft = _draft()
    for index, section in [(7, 'method'), (8, 'results'), (9, 'limits')]:
        claim = deepcopy(draft['claims'][index - 7])
        claim.update(id=f'c{index}', section=section,
                     text=f'补充段落 {index}：保留完整方法及附加比较，属于同一已审核稿件，不能提升为新的独立结论。')
        draft['claims'].append(claim)
    return draft


def test_default_render_and_seal_use_approved_editorial_shell(tmp_path):
    root = _issue(tmp_path)
    _qualify(root)
    preview = workflow.render(root).read_text()
    report = workflow.render(root, seal=True).read_text()
    assert preview == report
    assert editorial.RENDERER_VERSION in report
    css = re.search(r'<style>(.*?)</style>', report, re.S).group(1)
    assert sha256(css.encode()).hexdigest() == editorial.APPROVED_CSS_SHA256
    assert 'class="reading-layout"' in report
    assert 'class="story paper"' in report
    assert 'background:#f4f6f8' not in report
    assert 'daily-agent-approval-sha256' not in report


@pytest.mark.parametrize('command', ['render', 'seal'])
def test_default_cli_uses_editorial_export(tmp_path, capsys, command):
    root = _issue(tmp_path)
    _qualify(root)
    workflow.main([command, '--root', str(root)])
    value = json.loads(capsys.readouterr().out)
    from pathlib import Path
    output = Path(value['result']['html'])
    assert editorial.RENDERER_VERSION in output.read_text()
    assert output.name == ('report.html' if command == 'seal' else 'preview.html')


def test_c1_to_c6_visible_extra_claims_secondary_exact_once(tmp_path):
    root = _issue(tmp_path)
    draft = _extra_draft()
    _qualify(root, draft=draft)
    html = workflow.render(root).read_text()
    article = re.search(r'<article\b.*?</article>', html, re.S).group()
    visible, details = article.split('<details class="content-details supplementary-details">')
    for claim in draft['claims']:
        marker = f'data-claim-id="{claim["id"]}"'
        expected = _old_claim_bytes(claim, _state(root)['inputs'][0]['pdf_url'])
        assert html.count(marker) == html.count(expected) == 1
        assert (expected in visible) == (int(claim['id'][1:]) <= 6)
        assert (expected in details) == (int(claim['id'][1:]) > 6)
    assert 'c7' in details and details.index('c7') < details.index('c8') < details.index('c9')
    assert '核心问题与启发' in visible and '关键思路' in visible and '证据与边界' in visible


def test_conditions_present_in_text_are_not_duplicated_or_hidden(tmp_path):
    root = _issue(tmp_path)
    draft = _draft()
    draft['claims'][4]['conditions'] = '合成基准上的结果不能证明真实环境中的表现'
    _qualify(root, draft=draft)
    html = workflow.render(root).read_text()
    assert _old_claim_bytes(draft['claims'][4], _state(root)['inputs'][0]['pdf_url']) in html
    assert html.count(draft['claims'][4]['conditions']) == 1


def test_author_affiliation_unknowns_visible_exactly_once(tmp_path):
    root = _issue(tmp_path)
    draft = _draft()
    _qualify(root, draft=draft)
    html = workflow.render(root).read_text()
    expected = f'<p class="authors">{escape(draft["author_context"]["text"])}</p>'
    assert html.count(expected) == 1
    assert expected in html.split('<details class="content-details supplementary-details">')[0]
    assert '团队历史与通讯身份未知' in html
    assert '作者与机构' in html


@pytest.mark.parametrize('figure_count', [0, 1, 3])
def test_zero_to_three_exact_pngs_captions_and_page_links(tmp_path, figure_count):
    root = _issue(tmp_path)
    draft = _draft(figures=bool(figure_count))
    if figure_count == 3:
        draft['figures'] = [dict(draft['figures'][0], id=f'f{index}',
                                 role=role, label=f'Figure 1(d), source region {index}')
                            for index, role in enumerate(('framework', 'result', 'formula'), 1)]
    _qualify(root, draft=draft)
    state = _state(root)
    meta, record = state['inputs'][0], state['papers']['alpha']
    html = workflow.render(root, seal=True).read_text()
    assert len(re.findall(r'<figure\b', html)) == figure_count
    images = re.findall(r'<img\b[^>]*src="data:image/png;base64,([^"]+)"[^>]*>', html)
    for figure, image, asset in zip(draft['figures'], images, record['assets']):
        assert base64.b64decode(image) == (root / asset['crop']).read_bytes()
        caption = (f'<figcaption>{escape(figure["label"])} · {escape(figure["caption"])} '
                   f'<a href="{escape(meta["pdf_url"], quote=True)}#page={figure["page"]}">原文 p.{figure["page"]}</a></figcaption>')
        assert html.count(caption) == 1
        assert f'href="{meta["pdf_url"]}#page={figure["page"]}"' in html
        assert asset['crop'].split('/')[-1] in html
    if figure_count == 0:
        assert draft['no_figure_reason'] in html
        assert 'class="figure-jump"' not in html
        assert 'id="figures-' not in html


def test_repeated_render_never_reads_source_again_or_dispatches_jobs(tmp_path, monkeypatch):
    root = _issue(tmp_path)
    _qualify(root)
    before = {p.name: p.read_bytes() for p in _job_files(root)}
    def forbidden(*args, **kwargs):
        raise AssertionError('No semantic/source-render operation is allowed while formatting')
    monkeypatch.setattr(workflow, '_source', forbidden)
    monkeypatch.setattr(workflow, '_request', forbidden)
    first = workflow.render(root).read_bytes()
    assert workflow.render(root).read_bytes() == first
    assert {p.name: p.read_bytes() for p in _job_files(root)} == before


def test_static_mobile_navigation_focus_and_offline_controls(tmp_path):
    root = _issue(tmp_path)
    _qualify(root)
    html = workflow.render(root).read_text()
    assert '<details class="contents-disclosure">' in html
    assert '<details class="contents-disclosure" open' not in html
    assert html.count('<nav class="contents"') == 2
    assert 'position:sticky;top:24px' in html
    assert '@media(max-width:760px)' in html
    assert '.desktop-contents{display:none}.contents-disclosure{display:block}' in html
    assert 'min-height:44px' in html
    assert 'a:focus-visible,summary:focus-visible' in html
    assert 'max-width:100%;height:auto' in html
    assert '@media(prefers-reduced-motion:reduce)' in html
    assert '@media(prefers-color-scheme:dark)' in html
    assert '@media print' in html
    assert "default-src 'none'; img-src data:" in html
    assert not re.search(r'<(?:script|iframe|form)\b', html)
    ids = re.findall(r'\bid="([^"]+)"', html)
    assert len(ids) == len(set(ids))
    assert set(re.findall(r'href="#([^"]+)"', html)) <= set(ids)


@pytest.mark.parametrize('target', ['crop', 'receipt', 'draft', 'review', 'queue_answer'])
def test_default_editorial_export_fails_closed_on_bound_bytes(tmp_path, target):
    root = _issue(tmp_path)
    _qualify(root)
    record = _state(root)['papers']['alpha']
    if target == 'crop':
        path = root / record['assets'][0]['crop']
    elif target == 'queue_answer':
        path = root / 'data/writer-queue' / (record['calls']['review:0'] + '.answer.json')
    else:
        path = root / record[target]
    path.write_bytes(path.read_bytes() + b'tampered')
    with pytest.raises((StateCorrupt, json.JSONDecodeError)):
        workflow.render(root, seal=True)
    assert not (root / 'report.html').exists()
    assert _state(root)['sealed'] is None


def test_missing_core_contract_enters_repair_before_review_or_render(tmp_path):
    root = _issue(tmp_path)
    workflow.advance(root)
    draft = _draft()
    draft['claims'][0]['id'] = 'c10'
    _answer(root, 'alpha', 'draft', draft)
    status = workflow.advance(root)
    assert status['papers']['alpha']['status'] == 'ready'
    assert status['papers']['alpha']['round'] == 1
    assert 'review:0' not in _state(root)['papers']['alpha']['calls']


def test_renderer_refuses_wrong_crop_page_even_with_supplied_bytes(tmp_path):
    root = _issue(tmp_path)
    _qualify(root)
    state = _state(root)
    meta, record = state['inputs'][0], state['papers']['alpha']
    receipt, draft = workflow._verified_receipt(root, meta, record)
    crops = {a['id']: workflow._get(root, a['crop']) for a in receipt['assets']}
    receipt['assets'][0]['page'] = 1
    with pytest.raises(StateCorrupt, match='source page'):
        editorial.render_paper_first_editorial(state, [(meta, receipt, draft, crops)], workflow.status(root))


def test_stylesheet_drift_fails_closed(tmp_path, monkeypatch):
    root = _issue(tmp_path)
    _qualify(root)
    monkeypatch.setattr(editorial, 'CSS', editorial.CSS + 'body{color:red}')
    with pytest.raises(StateCorrupt, match='stylesheet changed'):
        workflow.render(root)


def test_empty_preview_preserves_honest_status_and_has_no_fake_cards(tmp_path):
    root = _issue(tmp_path)
    html = workflow.render(root).read_text()
    assert editorial.RENDERER_VERSION in html
    assert '<article' not in html
    assert '本期尚无合格内容' in html
    assert '已独立核验 0 篇论文' in html


def test_active_or_private_source_urls_fail_closed():
    for url in ('javascript:alert(1)', 'https://localhost/source',
                'https://127.0.0.1/source', 'https://user:secret@example.org/source'):
        with pytest.raises(StateCorrupt):
            editorial._source_url(url)
