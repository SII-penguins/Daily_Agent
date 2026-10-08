"""Portable, dependency-free editorial edition from already approved content.

No model calls, remote assets, local file links, forms, or invented summaries.
The existing local-preview renderer remains available for its feedback server.
"""
from __future__ import annotations

from datetime import date
import ipaddress
import re
from html import escape
from urllib.parse import urlsplit

from daily_agent.models import ApprovedItem
from daily_agent.rendering.composition import paper_paragraphs, featured_keys, reader_text
from daily_agent.rendering.notes import reading_label, card_gaps
from daily_agent.rendering.markdown import _prefix_label

RENDERER_VERSION = 'editorial-v4'


def text(value):
    value = re.sub(r'(?<![A-Za-z0-9:/])(?:file://[^\s<>\"\']+|/(?:workspace|home|Users|tmp|root|mnt|private)/[^\s<>\"\']+)', '[本地路径已省略]', str(value or ''))
    return escape(value, quote=True)


def link(label, url):
    try:
        parsed = urlsplit(str(url))
        safe = parsed.scheme in {'https', 'http'} and parsed.hostname and not parsed.username and not parsed.password
        host = (parsed.hostname or '').lower().rstrip('.')
        safe = safe and not (host == 'localhost' or host.endswith('.localhost') or host.endswith('.local'))
        safe = safe and not any(ord(c) < 32 for c in str(url)) and '%' not in parsed.netloc and '\\' not in parsed.netloc
        try:
            address = ipaddress.ip_address(host)
            safe = safe and address.is_global
        except ValueError:
            # Browsers normalize legacy integer/octal/short IPv4 forms. Do not
            # let these turn apparently external source links into local ones.
            safe = safe and not re.fullmatch(r'(?:0x[0-9a-f]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|[0-9]+))*', host)
    except ValueError:
        safe = False
    return f'<a href="{text(url)}" rel="noopener noreferrer">{text(label)}</a>' if safe else f'<span>{text(label)}（链接不可用）</span>'


def paragraph(label, value, cls=''):
    return f'<div class="reading-block {cls}"><h4>{text(label)}</h4><p>{text(value)}</p></div>'


def scientific_block(label, paragraphs, cls=''):
    """Render coherent prose with explicit, non-repeating provenance labels."""
    prose = []
    for part in paragraphs:
        provenance = (f'<strong class="provenance-label">{text(part["label"])}：</strong> '
                      if part['label'] else '')
        claims = ' '.join(f'<span class="scientific-claim" data-claim-id="{text(c["id"])}">{text(c["text"])}</span>'
                          for c in part['claims'])
        prose.append(f'<p class="analysis-paragraph" data-provenance="{text(part["kind"])}">{provenance}{claims}</p>')
    return f'<div class="reading-block scientific-prose {cls}"><h4>{text(label)}</h4>{"".join(prose)}</div>'


def _research_context(material):
    """Render only stored, source-labelled context; never infer affiliation."""
    context = material.raw.get('research_context') or {}
    if not isinstance(context, dict):
        context = {}
    authors = context.get('authors') or []
    blocks = []
    shared_evidence = {}
    for author in authors:
        if not isinstance(author, dict):
            continue
        roles = [dict(first_author='第一署名作者', corresponding_author='通讯作者', co_first_author='共同第一作者', lead_author='主要作者').get(r, '') for r in author.get('roles', [])]
        roles = ' · '.join(r for r in roles if r)
        lines = ['<strong>' + text(author.get('name', '姓名未记录')) + '</strong>（' + text(roles or '角色未核验') + '）']
        for institution in author.get('institutions', []):
            if isinstance(institution, dict):
                lines.append('机构：' + text(institution.get('name')) + ' · ' + text({'verified_primary':'原始来源已核验','source_metadata_unverified':'来源元数据 · 关系未独立核验'}.get(institution.get('status'),'未核验')) + ' ' + link('机构来源', institution.get('source_url')))
        for evidence in author.get('evidence', []):
            if isinstance(evidence, dict):
                key = (str(evidence.get('source_url') or ''), str(evidence.get('excerpt') or ''))
                if key not in shared_evidence:
                    shared_evidence[key] = len(shared_evidence) + 1
                lines.append(link('作者来源 ' + str(shared_evidence[key]), evidence.get('source_url')))
        blocks.append('<p class="author-context-line">' + '；'.join(lines) + '</p>')
    for (url, excerpt), number in shared_evidence.items():
        blocks.append('<p class="context-source">' + link('作者来源 ' + str(number), url) + ' · ' + text(excerpt) + '</p>')
    if not authors:
        blocks.append(paragraph('作者', '；'.join(material.authors) if material.authors else '作者资料未记录'))
        blocks.append(paragraph('作者角色与机构', '第一作者、通讯作者及机构关系未独立核验'))
    labs = [lab for lab in context.get('labs', []) if isinstance(lab, dict) and lab.get('status') == 'verified_official']
    if labs:
        for lab in labs:
            blocks.append('<div class="reading-block"><h4>实验室 / 团队 · 官方来源核验</h4><p>' + text(lab.get('name')) + ' ' + link('官方页面', lab.get('url')) + '</p><p>' + text({'paper_listed_by_group':'团队官方页面列出本论文；不据此推断每位作者的隶属关系', 'explicit_author_affiliation':'原始资料明确列出的作者隶属关系'}.get(lab.get('relationship'), '关系类型未记录')) + '</p></div>')
    else:
        blocks.append(paragraph('实验室 / 团队', '尚未找到已核验的实验室或团队归属；不根据邮箱、机构名或作者顺序推断'))
    for line in context.get('research_lines', []):
        if isinstance(line, dict):
            blocks.append('<div class="reading-block"><h4>' + ('历史研究背景' if line.get('scope') == 'historical_background' else '本文研究方向') + '</h4><p>' + text(line.get('text')) + ' ' + link('来源', line.get('source_url')) + '</p></div>')
    for uncertainty in context.get('uncertainties', []):
        blocks.append(paragraph('待核验', uncertainty))
    return '<details class="context-details"><summary>作者、机构与研究背景</summary><div class="details-body">' + ''.join(blocks) + '</div></details>'


def _publication_label(material):
    evidence = material.raw.get('publication_evidence') or {}
    status = evidence.get('status')
    if status == 'published':
        return '正式发表 · 已有官方发表记录'
    if status == 'preprint_only' or material.source == 'arxiv':
        return 'arXiv / 预印本 · 不代表同行评审通过'
    if status == 'metadata_only':
        return '发表信息来自元数据 · 官方记录尚未核验'
    return '发表状态尚未核验'


def _paper(item, rank, featured):
    from daily_agent.paper_visual_assets import visual_assets_html
    visuals = visual_assets_html(item.material)
    figure_jump = ''
    if '<figure ' in visuals:
        visuals = visuals.replace('class="paper-visual-assets"', f'class="paper-visual-assets" id="figures-{rank}"', 1)
        figure_jump = f'<a class="figure-jump" href="#figures-{rank}">查看原论文图表</a>'
    from daily_agent.scientific_analysis import GAP
    from daily_agent.rendering.scientific import analysis_reading_blocks, analysis_sources
    analysis = analysis_reading_blocks(item)
    insight = scientific_block('最有价值的科学启发', analysis['insight'], 'scientific-insight') if analysis else paragraph('科学问题与论证深读', GAP, 'analysis-gap')
    deep_analysis = ('<div class="scientific-reading">'
        + scientific_block('关键思路', analysis.get('explanation', []))
        + scientific_block('证据与边界', analysis.get('argument', []))
        + '</div>') if analysis else ''
    parts = paper_paragraphs(item, featured)
    intro = next((p.text for p in parts if not p.label), '')
    limitations = next((p.text for p in parts if p.label == '局限'), '')
    body = ''.join(paragraph(p.label, p.text) for p in parts if p.label and p.label != '局限')
    fields = item.final_fields
    extras = []
    for key, label in [('method_steps', '完整方法步骤'), ('technical_route', '技术路线')]:
        value = reader_text(fields.get(key))
        if value:
            extras.append(paragraph(label, value))
    evidence = paragraph('阅读状态', reading_label(item.material)) + paragraph('证据缺口', card_gaps(item.material))
    evidence += paragraph('置信度', fields.get('confidence', '未记录'))
    if analysis:
        evidence += paragraph('原文证据位置', analysis_sources(item))
    legacy_intro = paragraph('研究问题与方法', intro) if analysis else ''
    visible_intro = '' if analysis else f'<p class="lead">{text(intro)}</p>'
    legacy_boundary = paragraph('适用边界', limitations, 'boundary')
    visible_boundary = '' if analysis else legacy_boundary
    links = link('阅读原文', item.url)
    if item.material.pdf_url:
        links += link('原始 PDF', item.material.pdf_url)
    return f'''<article class="story paper" id="item-{rank}" aria-labelledby="title-{rank}">
<div class="story-meta"><span class="eyebrow">PAPER / {rank:02d}</span><span class="badge">{'重点解读' if featured else '论文简讯'}</span><span>{text(item.source)}</span></div>
<h3 id="title-{rank}">{text(_prefix_label(item))}{text(item.title)}</h3><p class="publication-status">{text(_publication_label(item.material))}</p><p class="publication-date">首次发表：{text(item.material.raw.get("published_at") or "日期未记录")}</p>{visible_intro}
{figure_jump}
{insight}
{deep_analysis}
{visible_boundary}
{visuals}
<details class="content-details supplementary-details"><summary>方法细节与来源核验 <span aria-hidden="true">＋</span></summary><div class="details-body">{legacy_intro}{body}{''.join(extras)}{legacy_boundary if analysis else ''}{_research_context(item.material)}<section class="evidence-notes" aria-label="阅读状态与证据边界">{evidence}</section></div></details>
<footer class="story-footer"><div class="source-links">{links}<a href="#issue-contents">返回目录</a></div><span>第 {rank} 条</span></footer></article>'''


def _repo(item, rank):
    fields = item.final_fields
    body = ''.join(paragraph(label, reader_text(fields.get(key)) or '本次未核验') for key, label in [
        ('core_capabilities','核心能力'), ('typical_use_cases','典型场景'), ('architecture_or_api','架构 / API'), ('reusable_point','可复用点')])
    material = item.material
    return f'''<article class="story repo" id="item-{rank}" aria-labelledby="title-{rank}">
<div class="story-meta"><span class="eyebrow">OPEN SOURCE / {rank:02d}</span><span class="badge neutral">仓库解读</span><span>{text(material.language or '语言未记录')}</span></div>
<h3 id="title-{rank}">{text(_prefix_label(item))}{text(item.title)}</h3><p class="lead">{text(reader_text(fields.get('what_it_is')))}</p>
{paragraph('成熟度与核验边界', reader_text(fields.get('maturity_signal')) or '本次未核验', 'boundary')}
<details class="content-details"><summary>展开能力、接口与可复用点 <span aria-hidden="true">＋</span></summary><div class="details-body">{body}
{paragraph('仓库快照', f"Stars {material.stars if material.stars is not None else '未记录'} · 代码更新时间 {material.source_updated_at or '未记录'}；快照不代表当前实时状态")}</div></details>
<footer class="story-footer"><div class="source-links">{link('查看 GitHub 仓库', item.url)}<a href="#issue-contents">返回目录</a></div><span>第 {rank} 条</span></footer></article>'''


def render_editorial_html(items: list[ApprovedItem], run_date: date, *, kind='report', coverage='', excluded_count=0, approval_sha256='', body_sha256='', identity='') -> str:
    """Return a complete standalone document; exact source text remains inspectable."""
    papers = sum(i.item_type == 'paper' for i in items)
    repos = sum(i.item_type == 'repo' for i in items)
    featured = featured_keys(items)
    pilot = kind == 'pilot'
    label = '迁移验收样例 · 非今日新闻' if pilot else ('本期状态 · 暂无合格内容' if not items else '公共来源版 · 非全源')
    intro = '用一页建立全貌，按需展开方法与证据。' if items else '本期没有满足来源、全文核验和结论支持要求的内容。'
    contents = ''.join(f'<a class="contents-item" href="#item-{rank}"><span>{rank:02d}</span><div><small>{"论文" if i.item_type == "paper" else "开源项目"}</small><strong>{text(i.title)}</strong></div></a>' for rank, i in enumerate(items,1))
    navigation = f'<nav class="contents" aria-label="本期内容">{contents or "<p>暂无获准刊登的条目</p>"}</nav>'
    stories = []
    for rank,item in enumerate(items,1):
        stories.append(_paper(item,rank,item.key in featured) if item.item_type == 'paper' else _repo(item,rank))
    empty = '<div class="empty-state"><h3>保持空白，也是一种质量控制</h3><p>本期不补入未经核验的内容。来源覆盖与运行信息可在下方检查。</p></div>'
    return f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark"><meta name="referrer" content="no-referrer">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<meta name="daily-agent-approval-sha256" content="{text(approval_sha256)}"><meta name="daily-agent-body-sha256" content="{text(body_sha256)}"><meta name="daily-agent-identity" content="{text(identity)}">
<title>Daily Agent · {run_date.isoformat()} · 研究日报</title><style>{CSS}</style></head><body>
<a class="skip-link" href="#stories">跳到正文</a>
<div class="page"><header class="masthead"><a class="brand" href="#top" aria-label="Daily Agent 首页">DA<span>DAILY AGENT<small>RESEARCH BRIEFING</small></span></a><div class="edition">研究日报<br><time datetime="{run_date.isoformat()}">{run_date.strftime('%Y / %m / %d')}</time></div></header>
<main id="top"><section class="hero compact-hero" aria-labelledby="issue-title"><h1 id="issue-title">研究日报</h1><div class="issue-meta"><span>{papers} 篇论文 · {repos} 个项目</span><span class="issue-label">{text(label)}</span><a href="#coverage">覆盖与核验 ↓</a></div></section>
<div class="reading-layout"><aside class="issue-navigation" id="issue-contents"><div class="desktop-contents"><div class="section-heading"><h2>本期导航</h2><span>INDEX</span></div>{navigation}</div><details class="contents-disclosure"><summary><h2>本期导航 · {len(items)} 条</h2><span class="contents-toggle">展开 / 收起</span></summary>{navigation}</details></aside><section id="stories" aria-label="论文与开源项目">{''.join(stories) or empty}</section></div>
<section class="coverage" id="coverage"><div><p class="eyebrow">COVERAGE & PROVENANCE</p><h2>本期覆盖与核验</h2></div><div><p>{'这是已审核内容的呈现验收样例，不代表当日新发表内容，也不进入正式发布历史。' if pilot else '仅收录本期获准呈现的内容；条目数量不代表来源完整性。'}</p><p>本期 {len(items)} 条获准呈现 · {int(excluded_count)} 条因来源或全文证据不足未列入</p><details><summary>查看原始日报文本与运行记录</summary><pre>{text(coverage)}</pre></details><details><summary>内容绑定与文件说明</summary><p>独立 HTML 文件，无外部脚本、字体或追踪。所有展开区域均可离线阅读。原始资料链接需要网络。反馈请在当前对话中提及条目编号。</p><p class="hash">正文 SHA-256<br>{text(body_sha256)}<br>审核内容 SHA-256<br>{text(approval_sha256)}</p></details></div></section>
</main><footer class="page-footer"><strong>DAILY AGENT</strong><a href="#top">回到顶部 ↑</a></footer></div></body></html>'''


CSS = '''
:root{color-scheme:light dark;--bg:#f6f4ed;--card:#fffef9;--ink:#172c29;--muted:#5d6c66;--line:#ccd3c8;--accent:#14634c;--soft:#e9eee4;--warm:#f2ecd9;--focus:#925025}
*{box-sizing:border-box}html{scroll-behavior:smooth;scroll-padding-top:24px}body{margin:0;background:var(--bg);color:var(--ink);font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;font-size:16px;line-height:1.8}a{color:var(--accent);text-underline-offset:5px}a:hover{text-decoration-thickness:2px}a:focus-visible,summary:focus-visible{outline:3px solid var(--focus);outline-offset:5px}.page{max-width:1180px;margin:auto;padding:0 48px}h1,h2,h3,h4,p{margin:0}h1,h2,h3{line-height:1.3}p+p{margin-top:12px}.masthead{display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid var(--ink);padding:30px 0 22px}.brand{display:flex;gap:12px;align-items:center;color:var(--ink);text-decoration:none;font:700 36px Georgia,serif}.brand>span{border-left:1px solid var(--line);padding-left:14px;font:700 13px/1.6 sans-serif;letter-spacing:2px}.brand small{display:block;font-size:12px;letter-spacing:1.6px;color:var(--muted)}.edition{text-align:right;font-size:12px;color:var(--muted);letter-spacing:1px}.hero{display:flex;justify-content:space-between;gap:32px;padding:56px 0 46px;align-items:center}.eyebrow{font:600 12px/1.5 sans-serif;letter-spacing:2px;color:var(--accent)}h1{font-size:clamp(36px,5vw,60px);font-weight:600;letter-spacing:-2px;margin:18px 0 20px}h1 span{font-weight:400;color:var(--muted)}.hero-intro{color:var(--muted);font-size:15px}.issue-label{display:inline-block;margin-top:22px!important;font-size:12px;padding:4px 12px;border:1px solid var(--line);border-radius:30px}.issue-count{border-left:1px solid var(--line);padding-left:54px;min-width:210px}.issue-count>span{font:100px/1 Georgia,serif;letter-spacing:-7px;color:var(--accent)}.issue-count p{font-size:12px;color:var(--muted);margin:12px 0}.issue-count div{font-size:13px}.issue-count i{font-style:normal;color:var(--line);margin:0 9px}.section-heading{display:flex;justify-content:space-between;align-items:center;border-top:1px solid var(--ink);padding:16px 0}.section-heading h2{font-size:15px}.section-heading>span{font-size:12px;letter-spacing:2px;color:var(--muted)}.contents{display:grid;grid-template-columns:1fr 1fr;border-bottom:1px solid var(--line);gap:30px;padding:8px 0 30px}.contents-item{display:flex;gap:16px;text-decoration:none;color:var(--ink);align-items:baseline;min-width:0}.contents-item>span:first-child{font:18px Georgia,serif;color:var(--accent)}.contents-item>span:last-child{margin-left:auto}.contents-item small{display:block;color:var(--muted);font-size:12px;margin-bottom:6px}.contents-item strong{font-size:14px;line-height:1.6;display:block;font-weight:500;overflow-wrap:anywhere}.reading-layout{display:grid;grid-template-columns:210px minmax(0,1fr);gap:50px;padding:44px 0}.reader-note h2{font-size:22px;margin:14px 0 18px}.reader-note p{font-size:12px;color:var(--muted);line-height:1.9}.reader-note a{display:inline-block;font-size:12px;margin-top:22px}.story{background:var(--card);border:1px solid var(--line);padding:30px 32px;margin-bottom:24px;border-radius:3px;overflow-wrap:anywhere;box-shadow:0 4px 12px #172c2904}.story-meta{display:flex;gap:10px;align-items:center;flex-wrap:wrap;font-size:12px;color:var(--muted)}.badge{background:var(--soft);color:var(--accent);padding:1px 8px;border-radius:3px;font-size:12px}.badge.neutral{background:var(--warm);color:var(--muted)}h3{font-size:26px;letter-spacing:-.6px;margin:18px 0}.publication-date{font-size:12px;color:var(--muted);margin:-6px 0 16px}.lead{font-size:15px;line-height:1.95}.reading-block{margin:20px 0}.reading-block h4{font-size:14px;color:var(--accent);letter-spacing:.5px;margin-bottom:7px}.reading-block p{font-size:14px;line-height:1.95;white-space:pre-line}.boundary{background:var(--soft);padding:14px 18px;border-left:2px solid var(--accent);margin:22px 0}.boundary p{font-size:12px;color:var(--muted)}details{border-top:1px solid var(--line)}summary{cursor:pointer;font-size:14px;min-height:48px;padding:12px 0;list-style:none;display:flex;justify-content:space-between;align-items:center;gap:16px}summary::-webkit-details-marker{display:none}summary::before{content:'›';font-size:20px;color:var(--accent);margin-right:5px}summary>span{margin-left:auto}details[open]>summary::before{transform:rotate(90deg)}details[open]>summary>span{transform:rotate(45deg)}.details-body{padding:0 2px 4px}.evidence-details summary{color:var(--muted)}.story-footer{border-top:1px solid var(--line);display:flex;justify-content:space-between;gap:16px;align-items:center;padding-top:18px;margin-top:2px}.source-links{display:flex;gap:24px;flex-wrap:wrap}.source-links a{font-size:14px;text-decoration:none;font-weight:600;min-height:28px;display:inline-flex;align-items:center;gap:6px}.story-footer>span{color:var(--muted);font-size:12px;white-space:nowrap}.coverage{border-top:1px solid var(--ink);display:grid;grid-template-columns:210px minmax(0,1fr);gap:50px;padding:30px 0 36px}.coverage h2{font-size:20px;margin-top:12px}.coverage p{font-size:12px;color:var(--muted)}.coverage details{margin-top:14px}.coverage pre{white-space:pre-wrap;overflow-wrap:anywhere;font-family:inherit;font-size:12px;line-height:1.9;max-height:600px;overflow:auto;padding:16px;background:var(--card)}.hash{overflow-wrap:anywhere;font-family:monospace}.page-footer{border-top:1px solid var(--line);padding:24px 0 36px;display:flex;align-items:center;gap:24px;font-size:12px;letter-spacing:1px;color:var(--muted)}.page-footer a{margin-left:auto}.empty-state{padding:30px;border:1px dashed var(--line)}.empty-state h3{font-size:24px}.empty-state p{font-size:14px;color:var(--muted)}.skip-link{position:absolute;top:-100px;left:20px;padding:8px;background:var(--card);z-index:2}.skip-link:focus{top:12px}
@media(prefers-color-scheme:dark){:root{--bg:#142320;--card:#1c2e29;--ink:#e6ece0;--muted:#adbdb3;--line:#41564b;--accent:#a7d3ae;--soft:#263c32;--warm:#36382b;--focus:#eab98a}}
@media(max-width:760px){.page{padding:0 24px}.hero{padding:36px 0;gap:22px}.issue-count{min-width:116px;padding-left:24px}.issue-count>span{font-size:70px;letter-spacing:-4px}.issue-count div{font-size:12px}.issue-count i{margin:0 3px}.reading-layout,.coverage{grid-template-columns:1fr;gap:24px}.reader-note{display:none}.story{padding:24px}.contents{gap:24px}.coverage{padding:26px 0}.coverage>div:first-child{display:flex;justify-content:space-between;align-items:center}.coverage h2{font-size:17px;margin:0}h3{font-size:23px}.brand{font-size:30px}.brand>span{font-size:12px}.brand small{font-size:12px}}
@media(max-width:480px){.page{padding:0 18px}.masthead{padding-top:22px}.brand{gap:8px}.brand>span{padding-left:9px}.edition{font-size:12px}.hero{align-items:flex-end;gap:14px}h1{font-size:35px;letter-spacing:-1px;margin-top:14px}.hero-intro{font-size:12px;max-width:210px}.issue-count{min-width:78px;padding-left:15px;margin-bottom:3px}.issue-count>span{font-size:53px}.issue-count div{max-width:65px;line-height:1.9}.issue-count i{display:none}.issue-count p{font-size:12px}.eyebrow{font-size:12px;letter-spacing:1.3px}.issue-label{font-size:12px;padding:3px 8px}.contents{grid-template-columns:1fr;gap:18px;padding-bottom:24px}.contents-item{gap:13px}.reading-layout{padding:26px 0 12px}.story{padding:20px 18px;margin-bottom:18px}h3{font-size:23px}.lead{font-size:14px}.boundary{padding:12px}.story-footer{gap:10px}.source-links{gap:18px}.coverage>div:first-child{display:block}.coverage h2{margin-top:8px}.page-footer{gap:12px;flex-wrap:wrap}.page-footer>span{display:none}}
.paper-visual-assets{margin:22px 0}.paper-visual-assets h4{font-size:14px;color:var(--accent)}.scientific-asset{margin:18px 0;padding:12px;border:1px solid var(--line);background:#fff;color:#273b33}.scientific-asset img{display:block;width:auto;max-width:100%;height:auto;margin:auto}.scientific-asset figcaption,.visual-scope,.visual-gap,.context-source{font-size:12px;overflow-wrap:anywhere}.scientific-asset figcaption{margin-top:10px}.publication-status{font-size:14px;color:var(--accent);margin:0 0 12px}.publication-status+.publication-date{margin-top:0}
/* Compact issue header: the first article is the primary content. */
.masthead{padding:20px 0 16px}.compact-hero{display:flex;align-items:center;justify-content:space-between;gap:18px;padding:22px 0;border-bottom:1px solid var(--line)}.compact-hero h1{font-size:26px;letter-spacing:-.5px;margin:0;white-space:nowrap}.issue-meta{display:flex;gap:18px;align-items:center;flex-wrap:wrap;font-size:12px;color:var(--muted)}.compact-hero .issue-label{margin:0!important;padding:3px 10px}.issue-meta a{font-size:14px}.reading-layout{padding:26px 0;gap:40px}.issue-navigation .section-heading{border-top:0;padding:0 0 16px}.issue-navigation .contents{grid-template-columns:1fr;border-bottom:0;padding:0;gap:22px}.issue-navigation .contents-item{gap:12px}.issue-navigation .contents-item>span:last-child{display:none}
@media(max-width:760px){.compact-hero{align-items:flex-start;flex-direction:column;gap:10px;padding:18px 0}.compact-hero h1{font-size:24px}.issue-meta{gap:8px 14px;font-size:12px}.issue-navigation .section-heading{padding-bottom:10px}.issue-navigation .contents{grid-template-columns:1fr 1fr;gap:18px}.issue-navigation .contents-item small{margin-bottom:2px}.reading-layout{padding:20px 0;gap:20px}.contents-item strong{font-size:12px}.issue-navigation .contents-item>span:first-child{font-size:15px}}
@media(max-width:480px){.issue-navigation .contents{grid-template-columns:1fr;gap:10px}.issue-navigation .contents-item div{display:flex;align-items:baseline;gap:10px}.issue-navigation .contents-item small{white-space:nowrap}.issue-navigation .contents-item strong{line-height:1.5}.compact-hero .issue-label{font-size:12px}}
/* One quiet reading column: provenance transitions, not nested cards. */
.scientific-prose{max-width:46rem;margin:26px 0}.reading-block h4{font-size:14px;font-weight:650;line-height:1.6;margin-bottom:10px}.scientific-prose h4{font-size:14px;letter-spacing:.2px}.scientific-prose p{font-size:14px;line-height:1.95}.scientific-prose .analysis-paragraph+.analysis-paragraph{margin-top:15px}.provenance-label{font-weight:650;color:var(--ink)}.scientific-insight{padding-top:0;border-top:0}.scientific-reading{max-width:46rem}.scientific-reading .scientific-prose{margin:24px 0}.scientific-reading .scientific-prose+.scientific-prose{padding-top:0;border-top:0}.supplementary-details .evidence-notes{border-top:1px solid var(--line);margin-top:20px}.supplementary-details{margin-top:28px}.author-context-line{font-size:12px;line-height:1.85}.author-context-line strong{font-weight:600}.context-details .context-source{margin-top:12px;color:var(--muted)}.paper-visual-assets{border-top:1px solid var(--line);padding-top:24px}.paper-visual-assets>h4{font-size:14px}.scientific-asset{padding:14px 0;border:0;border-bottom:1px solid var(--line);background:transparent;color:var(--ink)}.scientific-asset img{background:white}.scientific-asset figcaption{font-size:14px;line-height:1.8;color:var(--muted)}.scientific-analysis .scientific-prose+.scientific-prose{padding-top:22px;border-top:1px solid var(--line)}.scientific-analysis>.details-body{padding-top:1px}.scientific-analysis>summary{justify-content:flex-start}.scientific-analysis>summary::before{flex:none}.scientific-analysis .reading-block:last-child h4{color:var(--muted)}.scientific-analysis .reading-block:last-child p{font-size:12px;color:var(--muted)}
@media(max-width:480px){.scientific-prose{margin:22px 0}.scientific-prose .analysis-paragraph+.analysis-paragraph{margin-top:14px}.scientific-insight{padding-top:0}.scientific-analysis .scientific-prose+.scientific-prose{padding-top:18px}}
/* Responsive reading controls remain native and usable without scripts. */
.issue-navigation{align-self:start;position:sticky;top:24px;max-height:calc(100vh - 48px);overflow:auto;scrollbar-gutter:stable;padding:3px 8px 8px 3px}
.contents-disclosure{display:none;border-top:0}.contents-disclosure>summary{justify-content:flex-start;padding:0 0 12px;min-height:44px;gap:8px}.contents-disclosure>summary h2{font-size:15px}.contents-disclosure>summary::before{flex:none}.contents-disclosure>summary>.contents-toggle{font-size:14px;color:var(--muted);margin-left:auto;transform:none}.issue-navigation .contents{gap:8px}.issue-navigation .contents-item{padding:10px 3px;min-height:44px}.contents-item:hover,.contents-item:focus-visible{background:var(--soft)}
.story:target{border-color:var(--accent)}.story,.paper-visual-assets,.issue-navigation{scroll-margin-top:24px}.scientific-prose p,.reading-block p,.lead{font-size:16px;line-height:1.9}.boundary p{font-size:14px}.scientific-prose .analysis-paragraph+.analysis-paragraph{margin-top:18px}.source-links{gap:4px 20px}.source-links a,.figure-jump,.figure-source,.page-footer a{display:inline-flex;align-items:center;min-height:44px;font-size:14px}.figure-jump{margin:0 0 2px}.scientific-asset>a:first-child{display:block}.figure-source{font-weight:600}.scientific-asset figcaption{font-size:14px;line-height:1.85}.scientific-asset a:focus-visible{outline-offset:3px}
@media(max-width:760px){.desktop-contents{display:none}.contents-disclosure{display:block}.issue-navigation{position:static;max-height:none;overflow:visible;padding:0}.issue-navigation .contents{grid-template-columns:1fr;gap:2px}.issue-navigation .contents-item{gap:12px;padding:10px 3px}.issue-navigation .contents-item div{display:block}.issue-navigation .contents-item strong{font-size:15px;line-height:1.6}.issue-navigation .contents-item small{font-size:12px;margin-bottom:2px}.contents-disclosure{border-bottom:1px solid var(--line);padding-bottom:8px}.contents-disclosure>summary{padding-bottom:8px}.story{padding:24px}.reading-layout{gap:24px}.scientific-prose{margin:24px 0}.story-footer{align-items:flex-start}.page-footer a{letter-spacing:0}.issue-meta a{display:inline-flex;align-items:center;min-height:44px;font-size:14px}}
@media(max-width:480px){.page{padding:0 16px}.story{padding:22px 18px}.story-footer{flex-wrap:wrap}.story-footer>span{margin-left:auto}.scientific-prose p,.reading-block p,.lead{font-size:16px}.boundary p{font-size:14px}.scientific-asset{margin:18px 0;padding:12px 0}.paper-visual-assets{padding-top:20px}.contents-disclosure>summary h2{font-size:15px}}
@media(prefers-reduced-motion:reduce){html{scroll-behavior:auto}}
@media print{.desktop-contents{display:block}.contents-disclosure{display:none}.issue-navigation{position:static;max-height:none;overflow:visible}.figure-jump,.contents-toggle{display:none}body{background:white;color:black}.page{max-width:none;padding:0}.reader-note,.skip-link{display:none}.reading-layout,.coverage{display:block}.story{break-inside:avoid;box-shadow:none}details> *{display:block!important}details::details-content{content-visibility:visible;display:block}.coverage pre{max-height:none;overflow:visible}.story,.coverage{border-color:#aaa}.page-footer{margin-top:20px}}
'''
