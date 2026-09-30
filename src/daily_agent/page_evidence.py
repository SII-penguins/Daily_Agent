"""Local page evidence. OCR output is evidence, never a fidelity certificate."""
from __future__ import annotations
import hashlib
from pathlib import Path
import subprocess
import tempfile
import time


def ordered_text(page):
    blocks = []
    for block in page.get_text('dict')['blocks']:
        for line in block.get('lines', []):
            text = ''.join(span['text'] for span in line['spans'])
            blocks.append((*line['bbox'], text, 0, 0))
    width = page.rect.width
    # Infer the second column from repeated line starts, including narrow
    # journal sidebars; a fixed mid-page split interleaves their body text.
    from collections import Counter
    starts = Counter(round(b[0] / 5) * 5 for b in blocks)
    anchors = [(x, n) for x, n in starts.items() if width*.28 < x < width*.65 and n >= 3]
    split = max(anchors, key=lambda pair: pair[1])[0] - 5 if anchors else width*.5
    left = [b for b in blocks if b[2] <= split]
    right = [b for b in blocks if b[0] >= split]
    # Only split a clear two-column layout; full-width tables/figures remain barriers.
    if len(left)<3 or len(right)<3:
        return page.get_text('text',sort=True)
    wide = sorted([b for b in blocks if b not in left and b not in right],key=lambda b:b[1])
    pending=[b for b in blocks if b not in wide]; result=[]
    for barrier in wide:
        before=[b for b in pending if b[3] <= barrier[1]]
        result.extend(sorted(before,key=lambda b:(b[0]>=split,b[1])))
        pending=[b for b in pending if b not in before]
        result.append(barrier)
    result.extend(sorted(pending,key=lambda b:(b[0]>=split,b[1])))
    return '\n'.join(b[4] for b in result)


def extract_pages(pdf, settings):
    pages,issues=[],[]
    started=time.monotonic()
    for index,page in enumerate(pdf):
        text=ordered_text(page)
        item={'page':index+1,'text':text,'text_source':'native','visual_required':True}
        low_text=len(text.strip()) < int(settings.get('ocr_min_chars',80))
        output=settings.get('page_image_dir')
        image=None
        if output:
            folder=Path(output);folder.mkdir(parents=True,exist_ok=True)
            image=folder/f'page-{index+1:04d}.png'
            try:
                import fitz
                page.get_pixmap(matrix=fitz.Matrix(float(settings.get('render_scale',2)),float(settings.get('render_scale',2)))).save(image)
                item['image_path']=str(image.resolve())
                item['image_hash']=hashlib.sha256(image.read_bytes()).hexdigest()
            except Exception:
                issues.append(f'第 {index+1} 页渲染失败')
        if low_text and settings.get('ocr_enabled',False):
            try:
                remaining=float(settings.get('ocr_budget_seconds',120))-(time.monotonic()-started)
                if remaining<=0: raise TimeoutError('OCR budget')
                with tempfile.TemporaryDirectory() as temporary:
                    if image is None:
                        import fitz
                        image=Path(temporary)/'page.png'
                        page.get_pixmap(matrix=fitz.Matrix(2,2)).save(image)
                    result=subprocess.run([str(settings.get('ocr_command','tesseract')),str(image),'stdout','-l',str(settings.get('ocr_language','eng'))],
                        check=True,capture_output=True,text=True,timeout=min(remaining,float(settings.get('ocr_timeout_seconds',30))))
                    if not result.stdout.strip(): raise ValueError('empty OCR')
                    item.update(text=result.stdout,text_source='tesseract',ocr_status='extracted')
            except Exception as exc:
                item['ocr_status']='failed'
                issues.append(f'第 {index+1} 页 OCR 失败：{type(exc).__name__}')
        elif low_text:
            item['ocr_status']='needed'
        pages.append(item)
    return pages,issues
