import hashlib
import json
from copy import deepcopy

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
