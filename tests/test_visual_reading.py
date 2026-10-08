import hashlib
import json
from pathlib import Path
import pytest
from daily_agent.paper_document import extract_document,attach_document,build_document
from daily_agent.visual_reading import read_visuals
from daily_agent.reading import read_papers,verify_draft
from test_trusted_reading import paper,config,document,responder,draft


def test_blank_lines_do_not_create_unreadable_chunks():
    r=paper();d=build_document(r,[{'page':1,'text':'\n'*200+'Methods\nMethod text.'}],r.url,'pdf',{'chunk_chars':256})
    assert d['chunks'] and all(c['text'].strip() for c in d['chunks'])


def test_ocr_extracts_real_rasterized_page(tmp_path):
    fitz=pytest.importorskip('fitz')
    import shutil
    binary=shutil.which('tesseract')
    if not binary:pytest.skip('optional local Tesseract unavailable')
    with fitz.open() as source:
        page=source.new_page();page.insert_text((60,80),'Quantum Routing Study\nMethods\nWe compare routing algorithms.\nResults\nDepth falls 22 percent.',fontsize=18)
        pix=page.get_pixmap(matrix=fitz.Matrix(2,2))
        with fitz.open() as scanned:
            scanned.new_page().insert_image(page.rect,stream=pix.tobytes('png'))
            data=scanned.tobytes()
    r=paper()
    d=extract_document(data,r,r.url,{'ocr_enabled':True,'ocr_command':binary,'page_image_dir':str(tmp_path/'images')})
    assert d['pages'][0]['text_source']=='tesseract'
    assert 'routing' in d['pages'][0]['text'].lower()
    assert Path(d['pages'][0]['image_path']).exists()
    assert '公式' in ' '.join(d['limitations'])


def test_visual_attachment_mismatch_and_missing_image_gate(tmp_path):
    cfg=config(tmp_path);r=paper();attach_document(r,document(r));read_papers([r],cfg,responder)
    p=r.paper_document['pages'][0];path=tmp_path/'page.png';path.write_bytes(b'image fixture')
    p.update(visual_required=True,image_path=str(path),image_hash=hashlib.sha256(path.read_bytes()).hexdigest())
    calls=[]
    def invoke(prompt,timeout,image):
        calls.append(image)
        return {'page':1,'summary':'图表与公式核对','figures':['图中坐标未抽取'],'tables':[],'formulas':['\\sum_i x_i'],
                'text_matches_image':False,'issues':['求和符号丢失']}
    read_visuals([r],cfg,invoke)
    assert calls==[str(path)]
    assert not r.paper_text_status['sufficient_for_deep_summary']
    d=draft(r);verify_draft(d,r);assert d.verification['status']=='limited'
    path.unlink();read_visuals([r],cfg,invoke)
    assert not r.reading['visual']['complete'] and len(calls)==1


def test_two_column_blocks_are_not_interleaved():
    fitz=pytest.importorskip('fitz')
    from daily_agent.page_evidence import ordered_text
    with fitz.open() as pdf:
        page=pdf.new_page(width=600,height=800)
        for y,i in [(100,1),(200,2),(300,3)]:
            page.insert_text((30,y),f'Left paragraph {i}')
            page.insert_text((330,y),f'Right paragraph {i}')
        text=ordered_text(page)
    assert text.index('Left paragraph 3')<text.index('Right paragraph 1')


def test_ocr_failure_is_visible(tmp_path):
    fitz=pytest.importorskip('fitz')
    with fitz.open() as pdf:
        pdf.new_page();data=pdf.tobytes()
    r=paper();d=extract_document(data,r,r.url,{'ocr_enabled':True,'ocr_command':'/nonexistent/tesseract'})
    assert d['document_kind']=='unavailable'
    assert 'OCR 失败' in ' '.join(d['limitations'])


def test_visual_cli_receives_image_argument(tmp_path,monkeypatch):
    from daily_agent.editorial import draft_report_items
    cfg=config(tmp_path);cfg.sources["report_writing"]["source_screenshots_enabled"]=False;r=paper();attach_document(r,document(r))
    path=tmp_path/'page.png';path.write_bytes(b'image')
    r.paper_document['pages'][0].update(visual_required=True,image_path=str(path),image_hash=hashlib.sha256(b'image').hexdigest())
    called=[]
    def run(args,**kwargs):
        if args[-1].startswith('核对附带'):
            called.append(args)
            return type('R',(),{'stdout':json.dumps({'page':1,'summary':'核对页面','figures':[],'tables':[],'formulas':[], 'text_matches_image':True,'issues':[]})})()
        if args[-1].startswith('阅读论文'):
            return type('R',(),{'stdout':json.dumps(responder(args[-1],1))})()
        return type('R',(),{'stdout':'[]'})()
    monkeypatch.setattr('daily_agent.editorial.subprocess.run',run)
    draft_report_items(cfg,[r],True)
    assert len(called)==1
    assert called[0][called[0].index('--image')+1]==str(path)
    assert called[0][-2]=='--'  # --image is variadic; prompt must not become an image filename


def test_semantic_failure_cannot_pass_limited_review(tmp_path):
    from daily_agent.editorial import review_draft
    cfg=config(tmp_path);r=paper();attach_document(r,document(r));d=draft(r)
    d.verification={'status':'limited','valid_fields':['problem','method'],'semantic_support':'review_failed','issues':['failed']}
    assert review_draft(cfg,[d])[0].verdict=='FAIL'


def test_asymmetric_journal_sidebar_does_not_interrupt_body():
    fitz=pytest.importorskip('fitz')
    from daily_agent.page_evidence import ordered_text
    with fitz.open() as pdf:
        page=pdf.new_page(width=600,height=800)
        for y,i in [(100,1),(130,2),(160,3),(190,4)]:
            page.insert_text((45,y),f'Sidebar {i}')
            page.insert_text((220,y),f'Body paragraph {i} continues across the larger column')
        text=ordered_text(page)
    assert text.index('Sidebar 4') < text.index('Body paragraph 1')
    assert 'Body paragraph 1' in text and 'Body paragraph 4' in text


def test_parallel_visual_reads_preserve_page_order_and_reuse_cache(tmp_path):
    cfg=config(tmp_path);cfg.sources['reading']['concurrent_reads']=3
    r=paper();attach_document(r,document(r));calls=[]
    for p in r.paper_document['pages']:
        path=tmp_path/f"page-{p['page']}.png";path.write_bytes(str(p['page']).encode())
        p.update(visual_required=True,image_path=str(path),image_hash=hashlib.sha256(path.read_bytes()).hexdigest())
    def invoke(prompt,timeout,image):
        p=json.loads(prompt.split('输入：\n',1)[1])['page'];calls.append(p)
        return {'page':p,'summary':'图片核对完成','figures':[],'tables':[],'formulas':[], 'text_matches_image':True,'issues':[]}
    read_visuals([r],cfg,invoke)
    assert r.reading['visual']['passed']
    assert [n['page'] for n in r.reading['visual']['notes']]==[1,2]
    read_visuals([r],cfg,invoke)
    assert len(calls)==2
    assert json.loads((tmp_path/'data/reading/visual/failures.json').read_text())==[]


def test_identical_pages_have_independent_visual_cache(tmp_path):
    cfg=config(tmp_path);r=paper();attach_document(r,document(r));calls=[]
    path=tmp_path/'repeat.png';path.write_bytes(b'same image')
    for p in r.paper_document['pages']:
        p.update(text='Repeated page',visual_required=True,image_path=str(path),image_hash=hashlib.sha256(path.read_bytes()).hexdigest())
    def invoke(prompt,timeout,image):
        page=json.loads(prompt.split('输入：\n',1)[1])['page'];calls.append(page)
        return {'page':page,'summary':'核对完成','figures':[],'tables':[],'formulas':[], 'text_matches_image':True,'issues':[]}
    read_visuals([r],cfg,invoke)
    read_visuals([r],cfg,None)
    assert calls==[1,2]
    assert r.reading['visual']['passed']


def test_html_table_cells_preserve_number_boundaries():
    r=paper()
    doc=extract_document(b'<html><body><table><tr><th>Baseline</th><th>Result</th></tr><tr><td>12</td><td>34</td></tr></table></body></html>',r,r.url,{})
    text=doc['pages'][0]['text']
    assert '1234' not in text
    assert 'Baseline\tResult' in text
    assert '12\t34' in text


@pytest.mark.parametrize('damage',['fabricated','missing','offset'])
def test_document_cache_rejects_corrupt_chunk_provenance(damage):
    from daily_agent.paper_document import valid_document
    r=paper();doc=document(r)
    assert valid_document(doc,doc['identity'])
    if damage=='fabricated': doc['chunks'][0]['text']='Fabricated result.'
    elif damage=='missing': doc['chunks'].pop()
    else: doc['chunks'][0]['offset']+=1
    assert not valid_document(doc,doc['identity'])


def test_strict_fidelity_requires_explicit_modality_claims(tmp_path):
    cfg=config(tmp_path);r=paper();attach_document(r,document(r));p=r.paper_document['pages'][0]
    path=tmp_path/'formula.png';path.write_bytes(b'formula');p.update(visual_required=True,image_path=str(path),image_hash=hashlib.sha256(path.read_bytes()).hexdigest())
    def invoke(prompt,timeout,image):
        return {'page':1,'summary':'公式观察','figures':[],'tables':[],'formulas':['x^2'], 'text_matches_image':True,'issues':[]}
    read_visuals([r],cfg,invoke)
    assert r.reading['visual']['complete'] and not r.reading['visual']['strict_fidelity']


def test_pdf_bold_headings_identify_nonstandard_article_sections(tmp_path):
    fitz = pytest.importorskip('fitz')
    r = paper()
    with fitz.open() as pdf:
        for heading in ['Implementation of the circuit', 'Gate benchmarking', 'Discussion and conclusion']:
            page = pdf.new_page()
            page.insert_text((60, 60), r.title)
            page.insert_text((60, 90), heading, fontname='hebo')
            for n in range(30):
                page.insert_text((60, 120 + n * 18), 'We analyze quantum routing with fixed circuit and noise conditions.')
        content = pdf.tobytes()
    d = extract_document(content, r, r.url, {'min_body_chars': 3000})
    assert d['document_kind'] == 'full_text'
    assert d['source_page_count'] == 3
    assert {'method', 'results', 'discussion'} <= set(d['coverage']['sections_found'])


def test_bold_word_in_body_is_not_a_section_heading():
    fitz = pytest.importorskip('fitz')
    from daily_agent.page_evidence import typographic_headings
    with fitz.open() as pdf:
        page = pdf.new_page()
        page.insert_text((60, 60), 'Methods', fontname='hebo')
        page.insert_text((120, 60), 'are compared in the following paragraph.')
        assert 'Methodsare compared in the following paragraph.' not in typographic_headings(page)
