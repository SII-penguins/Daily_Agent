"""Local reading notes and shared trust presentation for both report formats."""
from __future__ import annotations
from html import escape
from pathlib import Path
import hashlib
import json
import shutil
from daily_agent.paper_document import digest
from daily_agent.source_evidence_policy import record_policy, NATIVE, native_text_complete, source_text_gaps
from daily_agent.visual_inventory import verified_inventory, inventory_assets
from daily_agent.paper_visual_assets import prepare_report_visuals, visual_assets_html, visual_assets_markdown
from daily_agent.source_screenshots import prepare_screenshots, screenshot_html, screenshot_markdown

LABELS = {'problem':'解决问题','method':'方法','why_it_works':'为什么有效','novelty_or_difference':'新意/差异',
          'technical_route':'技术路线','method_steps':'关键步骤','key_result':'结果/发现',
          'possible_use_or_impact':'可能用途/影响','limitations':'局限'}


def fidelity_gaps(visual):
    pages = visual.get('fidelity', {}).get('pages', [])
    counts = {}
    for page in pages:
        if not page.get('passed'):
            reason = page.get('reason')
            counts[reason] = counts.get(reason, 0) + 1
    labels = {'FidelityBudgetExhausted': '核验预算耗尽',
              'TimeoutExpired': '模型调用超时',
              '逐页重建/独立复核未通过': '转写或独立复核不通过',
              'TimeoutError': '核验超时或预算耗尽'}
    return [f'页面保真核验：{count} 页{labels[reason]}'
            for reason, count in counts.items() if reason in labels]


def reading_label(material):
    doc, reading = material.paper_document, material.reading
    if not doc: return '未核验的旧版阅读记录'
    if record_policy(material) == NATIVE:
        status = '原生全文文本已分块阅读' if native_text_complete(material) else '原生全文文本阅读未完成'
        assets = material.reading.get('paper_visual_assets', {}).get('assets', [])
        return (f"{status}（{len(reading.get('read_chunk_ids', []))}/{reading.get('total_chunks', 0)} 块）；"
                f"精选原图 {len(assets)} 项；" + reading.get('verification', {}).get('label', '引用结论尚未独立核验'))
    if doc.get('document_kind') == 'abstract_only': status = '摘要速览'
    elif reading.get('complete') and doc.get('document_kind') == 'full_text': status = '全文文本已分块阅读'
    elif reading.get('read_chunk_ids'): status = '部分正文阅读'
    else: status = '未完成阅读'
    visual = reading.get('visual', {})
    if visual.get('required_pages'):
        inventory = verified_inventory(visual)
        if inventory:
            status += f"；已复核资产目录 {len(inventory['pages'])}/{visual['required_pages']} 页"
        else:
            status += f"；页面视觉 {len(visual.get('notes', []))}/{visual['required_pages']} 页"
    return f"{status}（{len(reading.get('read_chunk_ids',[]))}/{reading.get('total_chunks',0)} 块）；{reading.get('verification',{}).get('label','尚未核验结论')}"


def _native_gaps(material):
    values = [v for v in material.paper_document.get('limitations', [])
              if v != '图表图像、公式视觉保真及外部补充材料未核验']
    values.append('原生文本阅读覆盖不等于逐字保真；仅精选原图与引用结论经像素核验，其他视觉内容及外部补充材料未全面核验')
    for gap in source_text_gaps(material):
        values.append(f"第 {gap['page']} 页有局部文本抽取缺口；未据此猜补，相关结论须以原图支持")
    if material.reading.get('failures'):
        values.append(f"{len(material.reading['failures'])} 个原文块未完成阅读")
    if material.reading.get('synthesis_error'):
        values.append('整篇综合未完成')
    values.extend(material.reading.get('verification', {}).get('issues', []))
    values.extend(material.reading.get('native_visual_evidence', {}).get('gaps', []))
    return '；'.join(dict.fromkeys(values))


def native_render_allowed(value):
    material = getattr(value, 'material', value)
    policy = record_policy(material)
    if policy == 'strict_fidelity_v1':
        return True
    if policy != NATIVE:
        return False
    from daily_agent.source_evidence_policy import audit_reading
    return audit_reading([value.to_dict()])['all_passed']


def assert_native_renderable(items):
    if any(item.item_type == 'paper' and not native_render_allowed(item) for item in items):
        raise ValueError('Native published fields or evidence differ from independent review')


def gaps(material):
    if record_policy(material) == NATIVE:
        return _native_gaps(material)
    values = list(material.paper_document.get('limitations',[]))
    if not material.paper_document: values.append('旧版记录没有阅读覆盖与定位证据')
    if material.reading.get('failures'): values.append(f"{len(material.reading['failures'])} 个块未完成阅读")
    if material.reading.get('synthesis_error'): values.append(material.reading['synthesis_error'])
    visual=material.reading.get('visual',{})
    values.extend(fidelity_gaps(visual))
    if visual.get('inventory_failure', {}).get('reason') == 'source_page_coverage_incomplete':
        values.append('原始 PDF 页数覆盖不完整，未生成全页资产目录')
    if visual.get('strict_fidelity'):
        values=[v for v in values if v != '图表图像、公式视觉保真及外部补充材料未核验']
        values.append('使用逐页重建并经独立模型图片复核的文本；原生抽取差异保留，非数学正确性认证，外部补充材料未核验')
    if visual.get('passed'):
        values=[v for v in values if v != '图表图像、公式视觉保真及外部补充材料未核验']
        values.append('页面图表和公式经模型核对；非人工保真认证，外部补充材料未核验')
    if visual.get('complete') and not visual.get('passed') and not visual.get('strict_fidelity'):
        values=[v for v in values if v != '图表图像、公式视觉保真及外部补充材料未核验']
        values.append('页面已逐页核对，图表/公式抽取有差异；外部补充材料未核验')
        values.append('差异页 '+str(visual.get('mismatch_pages',[])))
    elif visual.get('required_pages') and not visual.get('passed') and not visual.get('strict_fidelity'):
        values.append('页面视觉核对未完成')
    values.extend(material.reading.get('verification',{}).get('issues',[]))
    if material.reading.get('source_screenshots', {}).get('passed'):
        values = [v for v in values if v != '图表图像、公式视觉保真及外部补充材料未核验']
        values = [v.replace('图表/公式尚未通过严格保真验收', '公式及表格的文本转写未通过严格核验')
                   .replace('页面视觉核对未完成；图表/公式与外部补充材料未核验', '图表/公式的模型语义核对与外部补充材料未核验') for v in values]
        values.append('图表和公式以原PDF截图呈现；文本转写与语义核验状态独立保留')
    return '；'.join(dict.fromkeys(values)) or '未记录'


def must_read(item):
    if item.item_type == 'paper' and not native_render_allowed(item):
        return False
    return item.item_type != 'paper' or (item.material.reading.get('verification',{}).get('status')=='located'
                                       and item.final_fields.get('confidence')!='low')


def card_gaps(material):
    """Reader-facing gaps; detailed rejection text stays in the complete note."""
    if record_policy(material) == NATIVE:
        return _native_gaps(material)
    values = list(material.paper_document.get('limitations', []))
    if not material.paper_document:
        values.append('旧版记录没有阅读覆盖与定位证据')
    reading = material.reading
    if reading.get('failures'):
        values.append(f"{len(reading['failures'])} 个块未完成阅读")
    if reading.get('synthesis_error'):
        values.append('整篇综合未完成')
    visual = reading.get('visual', {})
    values.extend(fidelity_gaps(visual))
    if visual.get('inventory_failure', {}).get('reason') == 'source_page_coverage_incomplete':
        values.append('原始 PDF 页数覆盖不完整，未生成全页资产目录')
    if visual.get('required_pages'):
        values = [v for v in values if v != '图表图像、公式视觉保真及外部补充材料未核验']
        if visual.get('strict_fidelity'):
            values.append('页面经独立模型复核，非数学正确性认证；外部补充材料未核验')
        elif visual.get('complete'):
            values.append('图表/公式尚未通过严格保真验收；外部补充材料未核验')
        else:
            values.append('页面视觉核对未完成；图表/公式与外部补充材料未核验')
    issues = reading.get('verification', {}).get('issues', [])
    failed = [LABELS[field] for field in LABELS if field in {'problem','method','key_result','limitations'} and any(v.startswith(field+':') for v in issues)]
    if failed:
        values.append('、'.join(failed)+'部分内容未通过证据审核，详见完整阅读笔记')
    if reading.get('verification', {}).get('semantic_support') == 'review_failed':
        values.append('独立语义审核未完成')
    if material.reading.get('source_screenshots', {}).get('passed'):
        values = [v for v in values if v != '图表图像、公式视觉保真及外部补充材料未核验']
        values = [v.replace('图表/公式尚未通过严格保真验收', '公式及表格的文本转写未通过严格核验')
                   .replace('页面视觉核对未完成；图表/公式与外部补充材料未核验', '图表/公式的模型语义核对与外部补充材料未核验') for v in values]
        values.append('图表和公式以原PDF截图呈现；文本转写与语义核验状态独立保留')
    return '；'.join(dict.fromkeys(values)) or '未记录'


def write_reading_notes(config, items, run_date, dry_run=False):
    folder = config.reports_dir / 'notes' / (run_date.isoformat()+('-dry-run' if dry_run else ''))
    for item in items:
        if item.item_type != 'paper': continue
        if not native_render_allowed(item):
            raise ValueError('Native note fields or evidence differ from independent review')
        visual_cfg = config.sources.get('report_writing', {})
        # Native selections and captions are already bound by the independent
        # claim review before completion. Rendering cannot admit new jobs or
        # change those reviewed assets outside their original finite slots.
        if record_policy(item.material) != NATIVE:
            prepare_report_visuals(item.material, config.reports_dir, visual_cfg, root=config.root)
            if not item.material.reading.get('paper_visual_assets'):
                prepare_screenshots(item.material, config.reports_dir, visual_cfg)
        folder.mkdir(parents=True,exist_ok=True)
        name = digest([item.key,item.material.paper_document.get('content_hash')])[:20]
        relative = f'notes/{folder.name}/{name}'
        item.material.raw['reading_note_url'] = relative+'.md'
        item.material.raw['reading_note_html_url'] = relative+'.html'
        material = item.material
        lines = [f'# {item.title}', '', f'阅读状态：{reading_label(material)}', '',
                 f'置信度：{item.final_fields.get("confidence","low")}', '', f'证据缺口：{gaps(material)}', '',
                 f'原文：{material.url}', '', '## 完整阅读笔记', '']
        from daily_agent.scientific_analysis import analysis_paragraphs, GAP
        analysis = analysis_paragraphs(item)
        lines.extend(['## 科学问题与论证深读', ''])
        for key, value in analysis:
            label = {'insight': '最有价值的科学启发', 'explanation': '问题、瓶颈与必要复杂性', 'argument': '论证推进、关键证据与未解问题'}[key]
            lines.extend([f'### {label}', '', value, ''])
        if not analysis: lines.extend([GAP, ''])
        for key,label in LABELS.items():
            value = item.final_fields.get(key,'not_stated')
            if isinstance(value,list): value='；'.join(value)
            lines.extend([f'### {label}', '', str(value), ''])
        lines.extend(['## 页面视觉核对', '', '以下为模型对页面图片的观察和转写；存在差异时不视为已核验结论。', ''])
        page_links = {}
        pages = {p.get('page'): p for p in material.paper_document.get('pages', [])}
        def append_page_image(number):
            page = pages.get(number, {})
            image = Path(page.get('image_path') or '')
            if image.is_file() and hashlib.sha256(image.read_bytes()).hexdigest() == page.get('image_hash'):
                target = folder / 'assets' / (page['image_hash'] + '.png')
                target.parent.mkdir(exist_ok=True)
                if not target.exists(): shutil.copyfile(image, target)
                href = 'assets/' + target.name
                label = f"查看第 {number} 页原图"
                link = f'[{label}]({href})'
                page_links[link] = (label, href)
                lines.extend([link, ''])
        for note in material.reading.get('visual',{}).get('notes',[]):
            lines.extend([f"### 第 {note['page']} 页", '', note['summary'], ''])
            append_page_image(note['page'])
            for key,label in [('figures','图'),('tables','表'),('formulas','公式'),('issues','差异/缺口')]:
                lines.extend([label+'：'+str(value) for value in note[key]])
            lines.append('')
        inventory = verified_inventory(material.reading.get('visual', {}))
        if inventory:
            lines.extend(['## 独立图片复核后的资产目录', '', inventory['scope'], ''])
            for entry in inventory['pages']:
                lines.extend([f"### 第 {entry['page']} 页 · 已复核资产目录", ''])
                append_page_image(entry['page'])
                assets = inventory_assets(material.reading['visual'], entry)
                for asset in assets:
                    label = {'figure': '图', 'table': '表', 'formula': '公式'}[asset['kind']]
                    lines.extend([label+' '+asset['id']+'：'+json.dumps(asset['content'], ensure_ascii=False), ''])
                if not assets:
                    lines.extend(['本页已逐页独立复核，候选转写未列出独立图、表或公式资产', ''])
        lines.extend(['## 结论证据', ''])
        if material.paper_document.get('evidence_basis') == 'image_transcription_reviewed':
            lines.extend(['以下引句来自经独立模型图片复核的重建文本；并非原生 PDF 文本层。原始页面供回查。', ''])
        chunks = {c['id']:c for c in material.paper_document.get('chunks',[])}
        for claim in material.reading.get('claim_evidence',[]):
            chunk = chunks.get(claim['chunk_id'],{})
            url = material.paper_document.get('source_url') or material.url
            if chunk.get('page'): url = url.split('#')[0]+f"#page={chunk['page']}"
            lines.extend([f"### {claim['field']} — {claim['chunk_id']}", '',
                f"位置：{chunk.get('section','未知章节')} / 页码 {chunk.get('page') or '不适用'} / {url}", '',
                f"证据性质：{claim['evidence_kind']}；条件：{claim['conditions']}", '',
                '> '+claim['quote'].replace('\n','\n> '), ''])
        lines.extend(['## 分块阅读与覆盖', ''])
        notes = {n['chunk_id']:n for n in material.reading.get('notes',[])}
        for cid,chunk in chunks.items():
            note=notes.get(cid)
            lines.extend([f"### {cid} — {chunk['section']} — 页码 {chunk.get('page') or '不适用'}", '',
                          note['summary'] if note else '未完成阅读', ''])
            if note:
                lines.extend(['> '+q.replace('\n','\n> ') for q in note['quotes']]); lines.append('')
        selected_visuals = visual_assets_markdown(material, prefix='../../') if native_render_allowed(item) else ''
        if selected_visuals:
            lines.extend(['## 原论文精选图表与核心公式', '', selected_visuals, ''])
        screenshots = '' if material.reading.get('paper_visual_assets') else screenshot_markdown(material, prefix='../../')
        if screenshots:
            lines.extend(['## 原文图表与公式（完整原页）', '', screenshots, ''])
        markdown='\n'.join(lines)
        (folder/(name+'.md')).write_text(markdown,encoding='utf-8')
        # Escape all source/model text; preserve the same information as Markdown.
        blocks = []
        for line in lines:
            if selected_visuals and line == selected_visuals:
                blocks.append(visual_assets_html(material) if native_render_allowed(item) else '<p>原图引用证据未通过完整核验，未展示</p>')
            elif screenshots and line == screenshots:
                blocks.append(screenshot_html(material))
            elif line in page_links:
                label, href = page_links[line]
                blocks.append('<p><a href="'+escape(href,quote=True)+'">'+escape(label)+'</a></p>')
            elif line.startswith('### '): blocks.append('<h3>'+escape(line[4:])+'</h3>')
            elif line.startswith('## '): blocks.append('<h2>'+escape(line[3:])+'</h2>')
            elif line.startswith('# '): blocks.append('<h1>'+escape(line[2:])+'</h1>')
            elif line.startswith('> '): blocks.append('<blockquote>'+escape(line[2:])+'</blockquote>')
            elif line: blocks.append('<p>'+escape(line)+'</p>')
        html='<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>'+escape(item.title)+'</title><style>body{max-width:900px;margin:2em auto;padding:1em;font:16px/1.7 system-ui;overflow-wrap:anywhere}blockquote{border-left:3px solid #888;padding-left:1em}</style><body>'+''.join(blocks)+'</body></html>'
        (folder/(name+'.html')).write_text(html,encoding='utf-8')


def result_conditions(material):
    claims = [c for c in material.reading.get('claim_evidence', []) if c.get('field')=='key_result']
    labels = {'experiment':'实验','simulation':'模拟','theory':'理论','prediction':'预测','not_stated':'未说明'}
    values = [labels.get(c.get('evidence_kind'),'未说明')+' / '+c.get('conditions','未说明') for c in claims]
    # Conditions may change the meaning of a number; never drop later ones
    # merely to shorten a scan card. Only exact duplicates can be removed.
    return '；'.join(dict.fromkeys(values)) or '未核验'
