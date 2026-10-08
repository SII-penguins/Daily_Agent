import hashlib
import json
from copy import deepcopy
from pathlib import Path

from daily_agent.paper_document import attach_document
from daily_agent.visual_fidelity import repair_visuals, accepted
from test_trusted_reading import config, paper, document


def test_rebuilt_document_is_reread_before_synthesis(tmp_path, monkeypatch):
    from daily_agent.editorial import draft_report_items
    cfg=config(tmp_path);cfg.sources["report_writing"]["source_screenshots_enabled"]=False;r=paper();attach_document(r,document(r));old=r.paper_document['content_hash'];calls=[]
    def read(records, config, invoke):
        for record in records:
            calls.append(record.paper_document['content_hash'])
            record.reading={'complete':True, 'fingerprint':record.paper_document['content_hash']}
    def repair(records, config, invoke):
        for record in records:
            record.paper_document={**record.paper_document,'content_hash':'rebuilt','evidence_basis':'image_transcription_reviewed'}
            record.reading['visual']={'required_pages':2,'strict_fidelity':True}
    monkeypatch.setattr('daily_agent.reading.read_papers',read)
    monkeypatch.setattr('daily_agent.visual_reading.read_visuals',lambda *a:None)
    monkeypatch.setattr('daily_agent.visual_fidelity.repair_visuals',repair)
    draft_report_items(cfg,[r],use_llm=False)
    assert calls==[old,'rebuilt']
    assert r.reading['fingerprint']=='rebuilt'
    assert r.reading['visual']['strict_fidelity']


def test_fidelity_requires_complete_independent_review():
    candidate={'page':1,'unresolved':[], 'assets':[{'id':'eq1'}]}
    review={'page':1,'text_supported':True,'inventory_complete':True,'checks':[], 'issues':[]}
    assert not accepted(candidate,review)
    review['checks']=[{'id':'eq1','supported':True,'reason':'symbol checked'}]
    assert accepted(candidate,review)
    candidate['unresolved']=['unclear subscript']
    assert not accepted(candidate,review)


def test_partial_repair_never_replaces_native_document(tmp_path):
    cfg=config(tmp_path);cfg.sources['reading']['fidelity_enabled']=True
    r=paper();attach_document(r,document(r));original=deepcopy(r.paper_document)
    for p in r.paper_document['pages']:
        path=tmp_path/f"{p['page']}.png";path.write_bytes(b'fixture')
        p.update(visual_required=True,image_path=str(path),image_hash=hashlib.sha256(b'fixture').hexdigest())
    def invoke(prompt,timeout,image):
        value=json.loads(prompt.split('输入：\n',1)[1]);page=value['page']
        if 'native_text' in value:
            return {'page':page,'text':value['native_text'],'assets':[],'unresolved':[]}
        return {'page':page,'text_supported':True,'inventory_complete':page==1,'checks':[], 'issues':[] if page==1 else ['missing formula']}
    repair_visuals([r],cfg,invoke)
    assert r.paper_document['content_hash']==original['content_hash']
    assert not r.reading['visual']['strict_fidelity']
    assert [p['passed'] for p in r.reading['visual']['fidelity']['pages']]==[True,False]


def test_repair_receives_previous_candidate_and_preserves_pdf_identity(tmp_path):
    cfg = config(tmp_path); cfg.sources['reading']['fidelity_enabled'] = True
    r = paper(); doc = document(r); doc['pages'] = doc['pages'][:1]
    doc['source_pdf_sha256'] = 'original-pdf-hash'; attach_document(r, doc)
    page = r.paper_document['pages'][0]
    path = tmp_path/'page.png'; path.write_bytes(b'fixture')
    page.update(visual_required=True, image_path=str(path), image_hash=hashlib.sha256(b'fixture').hexdigest())
    attempts = []
    def invoke(prompt, timeout, image):
        value = json.loads(prompt.split('输入：\n', 1)[1])
        if 'native_text' in value:
            attempts.append(value)
            return {'page':1, 'text':value['native_text'], 'assets':[], 'unresolved':[]}
        return {'page':1, 'text_supported':len(attempts)==2, 'inventory_complete':True,
                'checks':[], 'issues':[] if len(attempts)==2 else ['missing symbol']}
    repair_visuals([r], cfg, invoke)
    assert attempts[1]['previous_candidate']['text'] == page['text']
    assert attempts[1]['previous_review']['issues'] == ['missing symbol']
    assert r.reading['visual']['strict_fidelity']
    assert r.paper_document['source_pdf_sha256'] == 'original-pdf-hash'


def test_review_timeout_resumes_saved_transcription_without_promotion(tmp_path):
    import subprocess
    cfg = config(tmp_path)
    cfg.sources['reading']['fidelity_enabled'] = True
    r = paper(); doc = document(r); doc['pages'] = doc['pages'][:1]
    attach_document(r, doc)
    page = r.paper_document['pages'][0]
    image = tmp_path / 'page.png'; image.write_bytes(b'fixture')
    page.update(visual_required=True, image_path=str(image),
                image_hash=hashlib.sha256(b'fixture').hexdigest())
    original = r.paper_document['content_hash']
    calls = []
    def invoke(prompt, timeout, image):
        value = json.loads(prompt.split('输入：\n', 1)[1])
        if 'native_text' in value:
            calls.append('transcribe')
            return {'page':1, 'text':value['native_text'], 'assets':[], 'unresolved':[]}
        calls.append('review')
        if calls.count('review') == 1:
            raise subprocess.TimeoutExpired('model', timeout)
        return {'page':1, 'text_supported':True, 'inventory_complete':True,
                'checks':[], 'issues':[]}
    repair_visuals([r], cfg, invoke)
    assert not r.reading['visual']['strict_fidelity']
    assert r.paper_document['content_hash'] == original
    repair_visuals([r], cfg, invoke)
    assert calls == ['transcribe', 'review', 'review']
    assert r.reading['visual']['strict_fidelity']


def test_budget_exhaustion_does_not_invoke_model(tmp_path):
    cfg = config(tmp_path)
    cfg.sources['reading'].update(fidelity_enabled=True, fidelity_budget_seconds=0)
    r = paper(); attach_document(r, document(r))
    for page in r.paper_document['pages']:
        image = tmp_path / f"{page['page']}.png"; image.write_bytes(b'fixture')
        page.update(visual_required=True, image_path=str(image),
                    image_hash=hashlib.sha256(b'fixture').hexdigest())
    def invoke(*args):
        raise AssertionError('No model calls after budget exhaustion')
    repair_visuals([r], cfg, invoke)
    assert not r.reading['visual']['strict_fidelity']
    assert all(p['reason'] == 'FidelityBudgetExhausted'
               for p in r.reading['visual']['fidelity']['pages'])


def test_complete_image_review_can_recover_ocr_but_not_missing_pages(tmp_path):
    for missing in (False, True):
        cfg = config(tmp_path / str(missing)); cfg.sources['reading']['fidelity_enabled'] = True
        r = paper(); doc = document(r)
        doc.update(document_kind='partial_text', source_pdf_sha256='original-pdf',
                   source_page_count=len(doc['pages']) + int(missing),
                   limitations=['第 2 页 OCR 失败：ValueError'])
        attach_document(r, doc)
        for page in r.paper_document['pages']:
            image = tmp_path / f"{page['page']}.png"; image.write_bytes(b'fixture')
            page.update(visual_required=True, image_path=str(image),
                        image_hash=hashlib.sha256(b'fixture').hexdigest())
        def invoke(prompt, timeout, image):
            value = json.loads(prompt.split('输入：\n', 1)[1])
            if 'native_text' in value:
                return {'page':value['page'], 'text':value['native_text'], 'assets':[], 'unresolved':[]}
            return {'page':value['page'], 'text_supported':True, 'inventory_complete':True,
                    'checks':[], 'issues':[]}
        repair_visuals([r], cfg, invoke)
        assert r.paper_document['document_kind'] == ('partial_text' if missing else 'full_text')
        assert r.paper_document['native_document']['limitations'] == ['第 2 页 OCR 失败：ValueError']


def test_failed_page_repair_uses_original_and_overlapping_detail_images(tmp_path):
    import pytest
    fitz = pytest.importorskip('fitz')
    cfg = config(tmp_path); cfg.sources['reading']['fidelity_enabled'] = True
    r = paper(); doc = document(r); doc['pages'] = doc['pages'][:1]; attach_document(r, doc)
    with fitz.open() as pdf:
        page = pdf.new_page(width=400, height=600)
        page.insert_text((200, 310), 'Tiny gate parameter')
        image = tmp_path/'page.png'; page.get_pixmap().save(image)
    r.paper_document['pages'][0].update(visual_required=True, image_path=str(image),
                                      image_hash=hashlib.sha256(image.read_bytes()).hexdigest())
    seen = []
    def invoke(prompt, timeout, images):
        seen.append(images)
        data = json.loads(prompt.split('输入：\n', 1)[1])
        if 'native_text' in data:
            return {'page':1, 'text':data['native_text'], 'assets':[], 'unresolved':[]}
        passed = isinstance(images, list)
        return {'page':1, 'text_supported':passed, 'inventory_complete':True,
                'checks':[], 'issues':[] if passed else ['Small label is unclear']}
    repair_visuals([r], cfg, invoke)
    assert r.reading['visual']['strict_fidelity']
    assert len(seen) == 4
    assert seen[:2] == [str(image), str(image)]
    assert seen[2] == seen[3] and len(seen[2]) == 5
    assert all(Path(p).is_file() for p in seen[2])


def test_fidelity_cache_survives_runtime_budget_changes(tmp_path):
    cfg = config(tmp_path); cfg.sources['reading']['fidelity_enabled'] = True
    r = paper(); doc = document(r); doc['pages'] = doc['pages'][:1]; attach_document(r, doc)
    image = tmp_path/'page.png'; image.write_bytes(b'fixture')
    r.paper_document['pages'][0].update(visual_required=True, image_path=str(image),
                                      image_hash=hashlib.sha256(image.read_bytes()).hexdigest())
    def invoke(prompt, timeout, image):
        data = json.loads(prompt.split('输入：\n', 1)[1])
        if 'native_text' in data:
            return {'page':1, 'text':data['native_text'], 'assets':[], 'unresolved':[]}
        return {'page':1, 'text_supported':True, 'inventory_complete':True, 'checks':[], 'issues':[]}
    repair_visuals([r], cfg, invoke)
    assert r.reading['visual']['strict_fidelity']
    cfg.sources['reading'].update(fidelity_budget_seconds=0, fidelity_timeout_seconds=1, concurrent_reads=4)
    repair_visuals([r], cfg, None)
    assert r.reading['visual']['strict_fidelity']
