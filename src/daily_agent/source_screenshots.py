"""Faithful PDF page presentation, independent of text/OCR/model acceptance."""
from __future__ import annotations

import base64
import hashlib
from html import escape
from pathlib import Path
import re

from daily_agent.paper_document import atomic_json, digest


def prepare_screenshots(material, reports_dir: Path, settings: dict | None = None):
    cfg = settings or {}
    if not cfg.get('source_screenshots_enabled', False):
        return
    # Clear stale success before attempting a new PDF identity.
    material.reading['source_screenshots'] = {'passed': False, 'pages': []}
    state = material.reading['source_screenshots']
    try:
        import fitz
        document = material.paper_document.get('native_document', material.paper_document)
        pdf_path = Path(document.get('source_pdf_path') or material.raw.get('local_pdf_path') or '')
        data = pdf_path.read_bytes()
        pdf_hash = hashlib.sha256(data).hexdigest()
        if document.get('source_pdf_sha256') and document['source_pdf_sha256'] != pdf_hash:
            raise ValueError('PDF identity differs from reading input')
        scale = max(1.0, min(4.0, float(cfg.get('screenshot_scale', 2))))
        identity = digest([pdf_hash, scale, 'full-page-png-v1'])[:24]
        folder = reports_dir / 'assets' / identity
        folder.mkdir(parents=True, exist_ok=True)
        with fitz.open(stream=data, filetype='pdf') as pdf:
            pages = []
            for index, page in enumerate(pdf):
                target = folder / f'page-{index+1:04d}.png'
                # Render directly, never re-use unverified old image bytes.
                pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
                png = pix.tobytes('png')
                target.write_bytes(png)
                text = page.get_text()
                pages.append({'page': index+1, 'path': str(target.resolve()),
                    'url': target.relative_to(reports_dir).as_posix(),
                    'image_sha256': hashlib.sha256(png).hexdigest(),
                    'pixel_sha256': hashlib.sha256(pix.samples).hexdigest(),
                    'width': pix.width, 'height': pix.height,
                    'bbox': list(page.rect), 'source_url': ((document.get('source_url') if document.get('source_type') == 'pdf' else None) or material.pdf_url or material.url).split('#')[0]+f'#page={index+1}',
                    'has_figure_or_table': bool(re.search(r'(?im)^\s*(?:fig(?:ure)?\.?|table)\s*\d+', text)),
                    'has_numbered_formula': bool(re.search(r'(?m)^\s*\(\d+[a-z]?\)\s*$', text))})
        if not pages:
            raise ValueError('Empty PDF')
        candidates = [p for p in pages if p['has_figure_or_table'] or p['has_numbered_formula']]
        # These are navigation highlights only; complete pages stay in the note.
        selected = []
        for field in ['has_figure_or_table', 'has_numbered_formula']:
            match = next((p for p in candidates if p[field] and p['page'] not in selected), None)
            if match: selected.append(match['page'])
        for p in candidates or pages:
            if len(selected) >= max(2, int(cfg.get('screenshot_preview_pages', 2))): break
            if p['page'] not in selected: selected.append(p['page'])
        requested = material.raw.get('source_preview_pages')
        if isinstance(requested, list) and requested and all(type(n) is int and 1 <= n <= len(pages) for n in requested):
            selected = list(dict.fromkeys(requested))
        state.update(passed=True, basis='original_pdf_full_page_raster', pdf_sha256=pdf_hash,
                     page_count=len(pages), scale=scale, pages=pages, preview_pages=selected,
                     scope='原PDF完整页面图像；不代表文本转写、语义或外部补充材料通过核验')
        atomic_json(folder/'manifest.json', state)
    except Exception as exc:
        state.update(passed=False, error=type(exc).__name__)


def verified_pages(material, preview=False):
    state = material.reading.get('source_screenshots', {})
    if not state.get('passed'): return []
    pages = state.get('pages', [])
    if preview: pages = [p for p in pages if p['page'] in state.get('preview_pages', [])]
    result = []
    for page in pages:
        try:
            data = Path(page['path']).read_bytes()
            if hashlib.sha256(data).hexdigest() == page['image_sha256']:
                result.append((page, data))
        except (OSError, KeyError):
            continue
    return result


def screenshot_html(material, preview=False):
    blocks = []
    for page, data in verified_pages(material, preview):
        caption = f"原文第 {page['page']} 页 · 图表／公式原页截图（点击查看原文）"
        source = page['source_url']
        if not source.startswith(('https://', 'http://')): source = '#'
        blocks.append('<figure class="source-page" style="margin:1em 0"><a href="'+escape(source, quote=True)+'">'
            '<img style="display:block;max-width:100%;height:auto" loading="lazy" alt="'+escape(caption, quote=True)+'" '
            'width="'+str(page['width'])+'" height="'+str(page['height'])+'" src="data:image/png;base64,'
            +base64.b64encode(data).decode()+'"></a><figcaption>'+escape(caption)+'</figcaption></figure>')
    return '\n'.join(blocks)


def screenshot_markdown(material, preview=False, prefix=''):
    return '\n\n'.join(f"![原文第 {p['page']} 页截图]({prefix}{p['url']})\n\n来源：{p['source_url']}"
                         for p, _ in verified_pages(material, preview))
