"""Approved editorial-v4 presentation for already-qualified paper-first inputs.

Pure deterministic formatting. The workflow verifies receipts/queue answers and
supplies original crop bytes before entering here. No reads, models, publication,
semantic rewriting, inferred metadata, or new legacy approvals are performed.
"""
from __future__ import annotations

import base64
from datetime import date
from hashlib import sha256
from html import escape
import re

from daily_agent.rendering.editorial import CSS, link
from daily_agent.workflow_state import StateCorrupt

RENDERER_VERSION = 'editorial-v4-paper-first-v1'
APPROVED_CSS_SHA256 = 'e7216039f612c7a8d4f1acf0707d022493769cdfdd180ed6160e404a6c10d58c'
SECTIONS = ('problem', 'method', 'insight', 'results', 'limits', 'reuse')
LABELS = dict(zip(SECTIONS, ('研究问题', '方法与机制', '核心洞见', '关键结果', '边界与不足', '值得借鉴')))
PRIMARY = (
    ('核心问题与启发', ('c1', 'c3'), 'scientific-insight'),
    ('关键思路', ('c2',), ''),
    ('证据与边界', ('c4', 'c5', 'c6'), ''),
)

# Class bindings only. Approved typography, palette, layout, responsive controls,
# focus, reduced motion and print rules remain byte-for-byte in CSS above.
ADAPTER_CSS = '''
.scientific-prose p.conditions{font-size:14px;line-height:1.85;color:var(--muted);border-left:2px solid var(--line);padding-left:14px;margin-top:10px;margin-bottom:22px}
.scientific-claim-block+.scientific-claim-block{margin-top:18px}
.refs{font-size:12px;line-height:1.85;color:var(--muted)}.refs a{display:inline-block;margin-right:4px}
.author-context-summary{margin:12px 0 18px}.author-context-summary h4{font-size:14px;font-weight:600;color:var(--accent);margin-bottom:5px}
.authors{font-size:14px;line-height:1.9;color:var(--muted)}
.evidence-notes{padding-top:18px}.evidence-notes>p{font-size:14px;line-height:1.9}
.meta,.asset-provenance .hash{font-size:12px;color:var(--muted)}
.asset-provenance{margin-top:8px}.asset-provenance summary{font-size:14px;color:var(--muted)}.asset-provenance .hash{padding-bottom:16px}
.source-links>p{margin:0;display:flex;gap:4px 16px;align-items:center;flex-wrap:wrap}
.visual-gap{margin:22px 0;font-size:14px;color:var(--muted)}
'''


def _require(condition, message):
    if not condition:
        raise StateCorrupt(message)


def _source_url(value):
    # Reuse the approved renderer's URL safety policy, while retaining the exact
    # original serialization for allowed scientific source links.
    _require(isinstance(value, str) and value.startswith('https://')
             and link('source', value).startswith('<a '), 'Unsafe paper-first source URL')
    return escape(value, quote=True)


def claim_html(claim, pdf_url):
    """Exactly the previous renderer's full paragraph/conditions serialization."""
    url = _source_url(pdf_url)
    links = ' '.join(f'<a href="{url}#page={a["page"]}">p.{a["page"]}</a>' for a in claim['anchors'])
    prefix = '编者推断：' if claim['basis'] == 'editor_inference' else ''
    body = f'<p>{prefix}{escape(claim["text"])} <span class="refs">{links}</span></p>'
    if ' '.join(claim['conditions'].split()) not in ' '.join(claim['text'].split()):
        body += f'<p class="conditions">适用条件：{escape(claim["conditions"])}</p>'
    return body


def _claim_block(claim, pdf_url):
    return (f'<div class="scientific-claim-block" data-claim-id="{escape(claim["id"])}" '
            f'data-source-section="{escape(claim["section"])}" '
            f'aria-label="{LABELS[claim["section"]]}">{claim_html(claim, pdf_url)}</div>')


def _section(label, content, cls=''):
    return f'<section class="reading-block scientific-prose {cls}"><h4>{label}</h4>{content}</section>'


def _figures(meta, receipt, draft, crop_bytes, rank):
    figures = draft['figures']
    _require(len(figures) <= 3, 'Paper-first presentation accepts zero to three figures')
    if not figures:
        _require(not receipt['assets'] and not crop_bytes and draft.get('no_figure_reason', '').strip(),
                 'No-figure contract changed')
        return ('<p class="visual-gap">本篇未附原图：'
                + escape(draft['no_figure_reason']) + '</p>'), ''
    assets = {a['id']: a for a in receipt['assets']}
    _require(len(assets) == len(receipt['assets']) == len(figures)
             and set(assets) == {f['id'] for f in figures} == set(crop_bytes),
             'Figure/receipt set changed')
    rows = []
    for figure in figures:
        asset, pixels = assets[figure['id']], crop_bytes[figure['id']]
        _require(asset['page'] == figure['page'] and asset['bbox'] == figure['bbox'],
                 'Figure source page or crop changed')
        crop_hash = sha256(pixels).hexdigest()
        _require(crop_hash == asset['crop'].split('/')[-1] and pixels.startswith(b'\x89PNG\r\n\x1a\n'),
                 'Figure bytes do not match qualified PNG crop')
        url = _source_url(meta['pdf_url']) + '#page=' + str(figure['page'])
        # img and figcaption bytes match the previous paper-first serializer.
        image = f'<img alt="{escape(figure["label"], quote=True)}" src="data:image/png;base64,{base64.b64encode(pixels).decode()}">'
        caption = (f'<figcaption>{escape(figure["label"])} · {escape(figure["caption"])} '
                   f'<a href="{url}">原文 p.{figure["page"]}</a></figcaption>')
        rows.append(f'<figure class="scientific-asset"><a href="{url}" rel="noopener noreferrer">{image}</a>{caption}'
                    f'<a class="figure-source" href="{url}" rel="noopener noreferrer">打开原始 PDF · 第 {figure["page"]} 页 ↗</a>'
                    '<details class="asset-provenance"><summary>原图裁切与来源绑定</summary>'
                    f'<p class="visual-scope">原页第 {figure["page"]} 页 · 归一化裁切框 {escape(str(asset["bbox"]))}</p>'
                    f'<p class="hash">原始 PDF SHA-256<br>{escape(meta["pdf"].split("/")[-1])}'
                    f'<br>原页 SHA-256<br>{escape(asset["page_image"].split("/")[-1])}'
                    f'<br>裁切 SHA-256<br>{crop_hash}</p></details></figure>')
    return (f'<section class="paper-visual-assets" id="figures-{rank}" aria-label="原论文图表">'
            '<h4>原论文图表</h4>' + ''.join(rows) + '</section>',
            f'<a class="figure-jump" href="#figures-{rank}">查看原论文图表</a>')


def _paper(meta, receipt, draft, crop_bytes, rank):
    claims = {c['id']: c for c in draft['claims']}
    _require(len(claims) == len(draft['claims']), 'Duplicate scientific claim identifier')
    for number, section in enumerate(SECTIONS, 1):
        _require(claims.get(f'c{number}', {}).get('section') == section,
                 'Missing or misplaced c1–c6 visible-core contract')
    _require(all(re.fullmatch(r'c[1-9][0-9]*', key) and claim['section'] in SECTIONS
                 for key, claim in claims.items()), 'Invalid scientific claim layout')
    primary = ''.join(_section(label, ''.join(_claim_block(claims[key], meta['pdf_url'])
                                              for key in ids), cls)
                      for label, ids, cls in PRIMARY)
    core_ids = {f'c{i}' for i in range(1, 7)}
    secondary = ''.join(_section(LABELS[section], ''.join(_claim_block(c, meta['pdf_url'])
                         for c in draft['claims'] if c['section'] == section and c['id'] not in core_ids))
                         for section in SECTIONS
                         if any(c['section'] == section and c['id'] not in core_ids for c in draft['claims']))
    visuals, figure_jump = _figures(meta, receipt, draft, crop_bytes, rank)
    venue = (meta.get('publication') or {}).get('venue', '预印本／正式发表状态未独立确认')
    authors = f'<p class="authors">{escape(draft["author_context"]["text"])}</p>'
    links = (f'<p><a href="{_source_url(meta["url"])}">论文原文</a> · '
             f'<a href="{_source_url(meta["pdf_url"])}">PDF</a></p>')
    original_meta = (f'<p class="meta">{escape(meta["source_date"])} · {escape(meta["version"])} · '
                     f'{escape(venue)}</p>')
    return f'''<article class="story paper" id="paper-{rank}" aria-labelledby="title-{rank}">
<div class="story-meta"><span class="eyebrow">PAPER / {rank:02d}</span><span class="badge">论文解读</span></div>
<h3 id="title-{rank}">{escape(meta['title'])}</h3><p class="publication-status">{escape(venue)}</p><p class="publication-date">来源日期：{escape(meta['source_date'])}</p>
<section class="author-context-summary" aria-label="作者、机构与研究背景"><h4>作者与机构</h4>{authors}</section>
{figure_jump}{primary}{visuals}
<details class="content-details supplementary-details"><summary>方法细节、补充证据与来源核验 <span aria-hidden="true">＋</span></summary><div class="details-body">{secondary}
<section class="evidence-notes" aria-label="阅读状态与证据边界">{original_meta}<p>已通过本流程的独立全文与所选原图核验；排版不重读、不改写科学结论，不代表论文实验已独立复现。</p><p class="hash">稿件 SHA-256<br>{escape(receipt['draft'].split('/')[-1])}<br>独立审查 SHA-256<br>{escape(receipt['review'].split('/')[-1])}</p></section></div></details>
<footer class="story-footer"><div class="source-links">{links}<a href="#issue-contents">返回目录</a></div><span>第 {rank} 条</span></footer></article>'''


def render_paper_first_editorial(state, entries, status):
    """Default paper-first HTML from verified (meta, receipt, draft, crops) tuples."""
    _require(sha256(CSS.encode()).hexdigest() == APPROVED_CSS_SHA256,
             'Approved editorial-v4 stylesheet changed; review the presentation version')
    day = date.fromisoformat(state['issue_date'])
    title = f'{day.isoformat()} 研究简报' + (' · 隔离验证' if state['diagnostic'] else '')
    toc, stories = [], []
    for rank, (meta, receipt, draft, crops) in enumerate(entries, 1):
        stories.append(_paper(meta, receipt, draft, crops, rank))
        toc.append(f'<a class="contents-item" href="#paper-{rank}"><span>{rank:02d}</span><div>'
                   f'<small>论文</small><strong>{escape(meta["title"])}</strong></div></a>')
    count = len(entries)
    _require(count == status['qualified'], 'Rendered count differs from qualified status')
    navigation = '<nav class="contents" aria-label="本期内容">' + (''.join(toc) or '<p>暂无获准刊登的条目</p>') + '</nav>'
    empty = '<div class="empty-state"><h3>本期尚无合格内容</h3><p>待完成或未通过的论文不会展示为获准内容。</p></div>'
    label = '隔离验证 · 不进入正式发布历史' if state['diagnostic'] else '公共来源版 · 非全源'
    coverage = (f'已独立核验 {count} 篇论文；目标 {state["target"]} 篇。另有 '
                f'{status["pending"]} 篇待完成、{status["rejected"]} 篇未通过。本页仅呈现合格内容。')
    return f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark"><meta name="referrer" content="no-referrer">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<meta name="daily-agent-presentation" content="{RENDERER_VERSION}"><meta name="daily-agent-issue-binding" content="{escape(state['binding'])}">
<title>{escape(title)}</title><style>{CSS}</style><style>{ADAPTER_CSS}</style></head><body>
<a class="skip-link" href="#stories">跳到正文</a><div class="page"><header class="masthead"><a class="brand" href="#top" aria-label="Daily Agent 首页">DA<span>DAILY AGENT<small>RESEARCH BRIEFING</small></span></a><div class="edition">研究日报<br><time datetime="{day.isoformat()}">{day.strftime('%Y / %m / %d')}</time></div></header>
<main id="top"><section class="hero compact-hero" aria-labelledby="issue-title"><h1 id="issue-title">研究日报</h1><div class="issue-meta"><span>{count} 篇论文</span><span class="issue-label">{label}</span><a href="#coverage">覆盖与核验 ↓</a></div></section>
<div class="reading-layout"><aside class="issue-navigation" id="issue-contents"><div class="desktop-contents"><div class="section-heading"><h2>本期导航</h2><span>INDEX</span></div>{navigation}</div><details class="contents-disclosure"><summary><h2>本期导航 · {count} 条</h2><span class="contents-toggle">展开 / 收起</span></summary>{navigation}</details></aside><section id="stories" aria-label="论文解读">{''.join(stories) or empty}</section></div>
<section class="coverage" id="coverage"><div><p class="eyebrow">COVERAGE & PROVENANCE</p><h2>本期覆盖与核验</h2></div><div><p>{coverage}</p><details><summary>内容绑定与文件说明</summary><p>c1–c6 为经独立审查的可见主线；其余完整段落保存在补充详情中。所有条件、作者说明、图注和原图均按获准稿件保留。</p><p>独立 HTML 文件，无外部脚本、字体或追踪。展开区域、正文与原图可离线阅读，原文链接需要网络。</p><p class="hash">本期输入绑定 SHA-256<br>{escape(state['binding'])}<br>程序 SHA-256<br>{escape(state['implementation_sha256'])}</p></details></div></section>
</main><footer class="page-footer"><strong>DAILY AGENT</strong><a href="#top">回到顶部 ↑</a></footer></div></body></html>'''
