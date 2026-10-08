import copy
import hashlib
from pathlib import Path
import pytest
from daily_agent.models import MaterialRecord
from daily_agent.paper_visual_assets import (page_review_image,prepare_visual_assets,verified_assets,visual_assets_html,request_visual_selection)

@pytest.fixture
def example(tmp_path):
    fitz=pytest.importorskip('fitz')
    path=tmp_path/'paper.pdf'
    with fitz.open() as pdf:
        page=pdf.new_page(width=250,height=220)
        page.insert_text((25,40),'Table 1. Success rate (%)')
        page.insert_text((25,80),'Ours 81.2   Baseline 72.0')
        page.insert_text((25,120),'(1) E = mc2')
        pdf.save(path)
    sha=hashlib.sha256(path.read_bytes()).hexdigest()
    with fitz.open(path) as pdf: image_sha=hashlib.sha256(page_review_image(pdf[0])).hexdigest()
    m=MaterialRecord(key='x',source='arxiv',item_type='paper',title='x',url='https://example.org/paper',
        paper_document={'source_pdf_path':str(path),'source_pdf_sha256':sha,'source_url':'https://example.org/paper.pdf'})
    m.raw['paper_visual_selection']={'schema_version':1,'source_pdf_sha256':sha,'assets':[
      {'kind':'result_table','number':'Table 1','page':1,'bbox':[20,20,230,90],
       'page_image_sha256':image_sha,'caption':'Success rates','conditions':'Simulation, success rate (%)',
       'review':{'labels_checked':True,'conditions_checked':True}}], 'gaps':['No framework in this test paper']}
    return m,tmp_path/'reports'

def test_original_crop_and_bounded_self_contained_html(example):
    m,root=example;prepare_visual_assets(m,root)
    assert m.reading['paper_visual_assets']['status']=='ready'
    asset,data=verified_assets(m)[0]
    import fitz
    with fitz.open(m.paper_document['source_pdf_path']) as pdf:
        expected=pdf[0].get_pixmap(matrix=fitz.Matrix(2,2),clip=fitz.Rect(20,20,230,90),alpha=False).tobytes('png')
    assert data==expected
    html=visual_assets_html(m)
    assert 'data:image/png;base64,' in html and str(root) not in html
    assert '未独立复现' in html and 'Simulation' in html
    assert not m.reading.get('verification')
    manifest=next(root.rglob('manifest.json')).read_text()
    assert str(root) not in manifest and asset['source_pdf_sha256'] in manifest

@pytest.mark.parametrize('field,value',[
 ('page',0),('page',True),('bbox',[0,0,999,999]),('bbox',[0,0,float('nan'),100]),
 ('bbox',[100,90,20,20]),('kind','generated_diagram'),('page_image_sha256','f'*64),
 ('caption',''),('review',{'labels_checked':'true','conditions_checked':True})])
def test_reject_adversarial_selection(example,field,value):
    m,root=example;m.raw['paper_visual_selection']['assets'][0][field]=value
    prepare_visual_assets(m,root)
    assert m.reading['paper_visual_assets']['status']=='invalid'
    assert verified_assets(m)==[] and 'data:image' not in visual_assets_html(m)

def test_asset_and_metadata_tamper_and_source_change(example):
    m,root=example;prepare_visual_assets(m,root)
    original=copy.deepcopy(m.reading)
    m.reading['paper_visual_assets']['assets'][0]['caption']='changed claim'
    assert verified_assets(m)==[]
    m.reading=original
    Path(m.reading['paper_visual_assets']['assets'][0]['path']).write_bytes(b'bad')
    assert verified_assets(m)==[]
    prepare_visual_assets(m,root)
    Path(m.paper_document['source_pdf_path']).write_bytes(b'%PDF changed')
    assert verified_assets(m)==[]

def test_selection_mutation_and_wrong_pdf_fail_closed(example):
    m,root=example;prepare_visual_assets(m,root)
    m.raw['paper_visual_selection']['gaps'].append('changed')
    assert verified_assets(m)==[]
    m.raw['paper_visual_selection']['source_pdf_sha256']='a'*64
    prepare_visual_assets(m,root)
    assert m.reading['paper_visual_assets']['status']=='invalid'

def test_empty_and_unavailable_are_explicit_not_pass(example):
    m,root=example;m.raw['paper_visual_selection']['assets']=[]
    prepare_visual_assets(m,root)
    assert m.reading['paper_visual_assets']['status']=='empty'
    assert 'No framework' in visual_assets_html(m)
    del m.raw['paper_visual_selection']
    prepare_visual_assets(m,root,{'paper_visual_assets_enabled':True})
    assert m.reading['paper_visual_assets']['status']=='unavailable'
    assert '生成插图' in visual_assets_html(m)

def test_too_many_and_duplicate_assets(example):
    m,root=example
    item=m.raw['paper_visual_selection']['assets'][0]
    for count in [2,6]:
        m.raw['paper_visual_selection']['assets']=[copy.deepcopy(item) for _ in range(count)]
        prepare_visual_assets(m,root)
        assert m.reading['paper_visual_assets']['status']=='invalid'

def test_html_escapes_captions(example):
    m,root=example
    m.raw['paper_visual_selection']['assets'][0]['caption']='<script>alert(1)</script>'
    m.paper_document['source_url']='javascript:alert(1)'
    m.url='file:///etc/passwd'
    prepare_visual_assets(m,root)
    html=visual_assets_html(m)
    assert '<script>' not in html and 'javascript:' not in html and 'file://' not in html

def test_selection_queue_is_bounded_and_no_candidates_do_not_fake_success(example,monkeypatch):
    m,root=example
    del m.raw['paper_visual_selection']
    result=request_visual_selection(m,root.parent)
    assert result['assets']==[] and result['gaps']
    captured=[]
    m.reading={'visual':{'notes':[{'page':1,'figures':['Table visible'],'tables':[],'formulas':[]}]}}
    def fake(root,prompt,timeout,image_path,stage):
        captured.append((prompt,image_path,stage));return result
    monkeypatch.setattr('daily_agent.parent_writer.request',fake)
    request_visual_selection(m,root.parent)
    assert len(captured[0][1])==1 and captured[0][2]=='visual_selection'
    assert 'ORIGINAL PDF POINTS' in captured[0][0]

@pytest.mark.parametrize('source',['http://localhost/x','http://127.1/x','http://2130706433/x','http://0x7f000001/x','http://192.168.1.1/x','http://[::1]/x','https://user:secret@example.org/x','http://bad.local/x','http://127%2e0%2e0%2e1/x'])
def test_private_or_credential_source_links_not_exposed(example,source):
    m,root=example;m.paper_document['source_url']=source;m.pdf_url=None;m.url=source
    prepare_visual_assets(m,root)
    html=visual_assets_html(m)
    assert 'href=' not in html and source not in html

def test_missing_pdf_and_byte_budget_clear_stale_assets(example,monkeypatch):
    m,root=example;prepare_visual_assets(m,root)
    monkeypatch.setattr('daily_agent.paper_visual_assets.MAX_TOTAL_BYTES',1)
    prepare_visual_assets(m,root)
    assert m.reading['paper_visual_assets']['status']=='invalid'
    assert verified_assets(m)==[]
    Path(m.paper_document['source_pdf_path']).unlink()
    prepare_visual_assets(m,root)
    assert m.reading['paper_visual_assets']['status']=='invalid'

def test_empty_selection_without_gap_is_invalid(example):
    m,root=example;m.raw['paper_visual_selection'].update(assets=[],gaps=[])
    prepare_visual_assets(m,root)
    assert m.reading['paper_visual_assets']['status']=='invalid'

def test_local_paths_in_editorial_text_do_not_leak(example):
    m,root=example
    m.raw['paper_visual_selection']['assets'][0]['caption']='Figure at /workspace/private/source.pdf'
    m.raw['paper_visual_selection']['gaps']=['See file:///tmp/source.pdf']
    prepare_visual_assets(m,root)
    html=visual_assets_html(m)
    assert '/workspace/' not in html and 'file:///' not in html

def test_equation_crop_keeps_original_pixels_without_latex_invention(example):
    m,root=example
    m.raw['paper_visual_selection']['assets'][0].update(kind='equation',number='Equation (1)',bbox=[20,100,230,135],caption='Original equation (1)',conditions='Original printed equation; no semantic or mathematical certification')
    prepare_visual_assets(m,root)
    a,data=verified_assets(m)[0]
    assert a['kind']=='equation' and a['number']=='Equation (1)'
    assert 'Equation (1)' in visual_assets_html(m)
    assert 'latex' not in a and 'transcription' not in a

def test_corrupt_matching_pdf_and_huge_page_fail_closed(example):
    m,root=example
    path=Path(m.paper_document['source_pdf_path']);path.write_bytes(b'%PDF invalid')
    h=hashlib.sha256(path.read_bytes()).hexdigest()
    m.paper_document['source_pdf_sha256']=h;m.raw['paper_visual_selection']['source_pdf_sha256']=h
    prepare_visual_assets(m,root)
    assert m.reading['paper_visual_assets']['status']=='invalid'
    import fitz
    with fitz.open() as pdf:
        p=pdf.new_page(width=100000,height=100000)
        with pytest.raises(ValueError,match='pixel budget'):page_review_image(p)

def test_workflow_hook_propagates_pending_but_missing_pdf_is_gap(example,monkeypatch):
    from daily_agent.paper_visual_assets import prepare_report_visuals
    from daily_agent.parent_writer import PendingResponse
    m,root=example;del m.raw['paper_visual_selection']
    cfg={'paper_visual_assets_enabled':True,'paper_visual_selection_provider':'parent_queue'}
    def pending(*args,**kwargs):raise PendingResponse('a'*64)
    monkeypatch.setattr('daily_agent.paper_visual_assets.request_visual_selection',pending)
    with pytest.raises(PendingResponse):prepare_report_visuals(m,root,cfg,root=root.parent)
    def missing(*args,**kwargs):raise FileNotFoundError()
    monkeypatch.setattr('daily_agent.paper_visual_assets.request_visual_selection',missing)
    prepare_report_visuals(m,root,cfg,root=root.parent)
    assert m.reading['paper_visual_assets']['status']=='unavailable'
    assert m.reading['paper_visual_assets']['assets']==[]

def test_reading_note_hook_embeds_selected_assets_and_provenance(example):
    from types import SimpleNamespace
    from datetime import date
    from daily_agent.rendering.notes import write_reading_notes
    m,root=example
    cfg=SimpleNamespace(root=root.parent,reports_dir=root,sources={'report_writing':{'paper_visual_assets_enabled':True,'source_screenshots_enabled':True}})
    item=SimpleNamespace(item_type='paper',material=m,key=m.key,title=m.title,final_fields={'confidence':'high'})
    write_reading_notes(cfg,[item],date(2026,10,8))
    html=next((root/'notes').rglob('*.html')).read_text()
    md=next((root/'notes').rglob('*.md')).read_text()
    assert 'data:image/png;base64,' in html and 'PDF SHA-256' in html
    assert '../../assets/paper-visuals/' in md
    assert '完整原页' not in html and 'source_screenshots' not in m.reading
    assert str(root.parent) not in html

def test_reading_note_pending_selection_stops_before_output(example,monkeypatch):
    from types import SimpleNamespace
    from datetime import date
    from daily_agent.rendering.notes import write_reading_notes
    from daily_agent.parent_writer import PendingResponse
    m,root=example
    cfg=SimpleNamespace(root=root.parent,reports_dir=root,sources={'report_writing':{}})
    item=SimpleNamespace(item_type='paper',material=m,key=m.key,title=m.title,final_fields={})
    def pending(*args,**kwargs):raise PendingResponse('a'*64)
    monkeypatch.setattr('daily_agent.rendering.notes.prepare_report_visuals',pending)
    with pytest.raises(PendingResponse):write_reading_notes(cfg,[item],date(2026,10,8))
    assert not (root/'notes').exists()

def test_real_parent_queue_resumes_identical_selection_without_new_job(example):
    from daily_agent.parent_writer import PendingResponse, claim, import_response, pending
    from daily_agent.paper_visual_assets import prepare_report_visuals
    m,reports=example;root=reports.parent
    answer=m.raw.pop('paper_visual_selection')
    answer.update(selection_policy_version=2,experiment_plot_coverage={'reviewed':True,'available':False,'key_plot_numbers':[],'absence_reason':'This inspected fixture has only a table and equation'})
    m.reading={'visual':{'notes':[{'page':1,'summary':'Table 1','tables':['Success rate (%) table'],'figures':[],'formulas':[]}]}}
    cfg={'paper_visual_assets_enabled':True,'paper_visual_selection_provider':'parent_queue'}
    with pytest.raises(PendingResponse) as p:prepare_report_visuals(m,reports,cfg,root=root)
    job_id=p.value.job_id
    jobs=pending(root)
    assert len(jobs)==1 and jobs[0]['stage']=='visual_selection'
    lease=claim(root,job_id,'visual-test-worker')
    import_response(root,job_id,answer,'visual-test-worker',claim_token=lease['token'])
    prepare_report_visuals(m,reports,cfg,root=root)
    assert m.reading['paper_visual_assets']['status']=='ready'
    m.raw.pop('paper_visual_selection')
    prepare_report_visuals(m,reports,cfg,root=root)
    assert len(list((root/'data/writer-queue').glob('*.job.json')))==1
    assert pending(root)==[]


def plot_policy(m, *, available=True, keys=None, **extra):
    m.raw['paper_visual_selection'].update(selection_policy_version=2,experiment_plot_coverage={
        'reviewed':True,'available':available,'key_plot_numbers':keys if keys is not None else ['Figure 2'],**extra})


def test_v2_table_only_selection_cannot_silently_omit_available_experiment_plot(example):
    m,root=example;plot_policy(m)
    prepare_visual_assets(m,root)
    assert m.reading['paper_visual_assets']['status']=='invalid'
    plot_policy(m,omission_reason='The inspected original plot is illegible at source resolution; preserve this explicit gap')
    prepare_visual_assets(m,root)
    assert m.reading['paper_visual_assets']['status']=='ready'
    assert '关键实验数据图未完整纳入' in visual_assets_html(m)
    assert 'illegible' in visual_assets_html(m)


def test_v2_no_plot_paper_requires_inspected_absence_not_forced_plot(example):
    m,root=example;plot_policy(m,available=False,keys=[])
    prepare_visual_assets(m,root)
    assert m.reading['paper_visual_assets']['status']=='invalid'
    plot_policy(m,available=False,keys=[],absence_reason='Inspected source contains tables and a theoretical equation, no key experimental plot')
    prepare_visual_assets(m,root)
    assert m.reading['paper_visual_assets']['status']=='ready'


def test_v2_prose_cannot_silently_crowd_out_second_key_plot(example):
    m,root=example;a=m.raw['paper_visual_selection']['assets'][0]
    a.update(kind='result_figure',number='Figure 1')
    prose=copy.deepcopy(a);prose.update(kind='objective_excerpt',number='Objective',bbox=[20,100,230,135])
    m.raw['paper_visual_selection']['assets'].append(prose)
    plot_policy(m,keys=['Figure 1','Figure 2'])
    prepare_visual_assets(m,root)
    assert m.reading['paper_visual_assets']['status']=='invalid'


def test_experiment_plot_candidates_not_starved_by_many_framework_pages():
    from daily_agent.paper_visual_assets import _candidate_pages
    notes=[{'page':n,'figures':['Architecture diagram with nodes'],'tables':[],'formulas':[]} for n in range(1,15)]
    notes.append({'page':15,'figures':['Figure 9. Ablation learning curve experiment'],'tables':[],'formulas':[]})
    selected=_candidate_pages(notes)
    assert len(selected)==8 and selected[0]==15


def test_v2_refreshes_unsealed_legacy_selection_but_keeps_direct_legacy_render(example,monkeypatch):
    from daily_agent.paper_visual_assets import prepare_report_visuals
    m,root=example;prepare_visual_assets(m,root)
    assert m.reading['paper_visual_assets']['status']=='ready'
    calls=[]
    def upgrade(material,*args,**kwargs):
        calls.append(True)
        plot_policy(material,available=False,keys=[],absence_reason='Inspected source has only table/equation')
        return material.raw['paper_visual_selection']
    monkeypatch.setattr('daily_agent.paper_visual_assets.request_visual_selection',upgrade)
    cfg={'paper_visual_assets_enabled':True,'paper_visual_selection_provider':'parent_queue'}
    prepare_report_visuals(m,root,cfg,root=root.parent)
    prepare_report_visuals(m,root,cfg,root=root.parent)
    assert len(calls)==1 and m.raw['paper_visual_selection']['selection_policy_version']==2


def test_no_nominated_pages_is_unknown_not_fake_pixel_review(example):
    m,root=example;m.raw.pop('paper_visual_selection')
    selection=request_visual_selection(m,root.parent)
    assert selection['experiment_plot_coverage']['reviewed'] is False
    assert selection['experiment_plot_coverage']['available'] is None
    prepare_visual_assets(m,root)
    assert m.reading['paper_visual_assets']['status']=='empty'
    assert '不能把未检查当作不存在' in visual_assets_html(m)


def test_figure_inspection_links_name_original_pdf_and_preserve_assets(example):
    m, root = example
    prepare_visual_assets(m, root)
    original = copy.deepcopy(m.to_dict())
    assets_before = [(asset['artifact_sha256'], data) for asset, data in verified_assets(m)]
    html = visual_assets_html(m)
    assert 'class="figure-source" href="https://example.org/paper.pdf#page=1"' in html
    assert '查看原始 PDF · 第 1 页' in html
    assert 'aria-label="查看 Table 1 的原始 PDF，第 1 页"' in html
    assert m.to_dict() == original
    assert [(asset['artifact_sha256'], data) for asset, data in verified_assets(m)] == assets_before
