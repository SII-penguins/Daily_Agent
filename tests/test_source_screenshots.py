from pathlib import Path
import hashlib
import pytest
from daily_agent.models import MaterialRecord
from daily_agent.source_screenshots import prepare_screenshots, verified_pages, screenshot_html


def test_pdf_pixels_preserved_and_corrupt_image_not_rendered(tmp_path):
    fitz = pytest.importorskip('fitz')
    pdf = fitz.open(); p = pdf.new_page(width=240, height=200)
    p.insert_text((20, 40), 'Formula (1): E = mc^2')
    p.draw_rect(fitz.Rect(20,60,180,130), color=(1,0,0))
    path = tmp_path/'paper.pdf'; pdf.save(path); pdf.close()
    m = MaterialRecord(key='x', source='arxiv', item_type='paper', title='x', url='https://example.org/paper', raw={'local_pdf_path':str(path)})
    prepare_screenshots(m, tmp_path/'reports', {'source_screenshots_enabled':True})
    state = m.reading['source_screenshots']
    assert state['passed'] and state['page_count']==1
    with fitz.open(path) as source:
        expected = source[0].get_pixmap(matrix=fitz.Matrix(2,2), alpha=False)
    image = fitz.Pixmap(state['pages'][0]['path'])
    assert image.samples == expected.samples
    assert 'data:image/png;base64,' in screenshot_html(m)
    assert not m.reading.get('visual',{}).get('strict_fidelity')
    Path(state['pages'][0]['path']).write_bytes(b'bad')
    assert verified_pages(m)==[] and screenshot_html(m)==''


def test_missing_pdf_clears_stale_success(tmp_path):
    m = MaterialRecord(key='x',source='arxiv',item_type='paper',title='x',url='https://example.org',reading={'source_screenshots':{'passed':True}})
    prepare_screenshots(m,tmp_path,{'source_screenshots_enabled':True})
    assert m.reading['source_screenshots']['passed'] is False


def test_screenshots_do_not_skip_evidence_verification(tmp_path, monkeypatch):
    from daily_agent.config import load_config
    from daily_agent.editorial import draft_report_items
    cfg = load_config(Path(__file__).resolve().parents[1])
    object.__setattr__(cfg, 'root', tmp_path)
    cfg.sources['report_writing']['source_screenshots_enabled'] = True
    m = MaterialRecord(key='x', source='arxiv', item_type='paper', title='x',
                       url='https://example.org', paper_document={'pages': []})
    calls = []
    monkeypatch.setattr('daily_agent.reading.read_papers', lambda *args: None)
    monkeypatch.setattr('daily_agent.visual_reading.read_visuals', lambda *args: calls.append('visual'))
    monkeypatch.setattr('daily_agent.visual_fidelity.repair_visuals', lambda *args: calls.append('fidelity'))
    draft_report_items(cfg, [m], use_llm=False)
    assert calls == ['visual', 'fidelity']


def test_screenshot_rejects_pdf_changed_since_reading(tmp_path):
    path=tmp_path/'changed.pdf';path.write_bytes(b'%PDF different bytes')
    m=MaterialRecord(key='x',source='arxiv',item_type='paper',title='x',url='https://example.org',
        raw={'local_pdf_path':str(path)},paper_document={'source_pdf_sha256':'not-the-same'})
    prepare_screenshots(m,tmp_path/'reports',{'source_screenshots_enabled':True})
    assert m.reading['source_screenshots']['passed'] is False
    assert m.reading['source_screenshots']['error']=='ValueError'
