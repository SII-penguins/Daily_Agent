from datetime import date
import hashlib
import json
import os
from pathlib import Path
import pytest

from daily_agent.config import load_config
from daily_agent.models import ApprovedItem, MaterialRecord
from daily_agent.connectors.pdf_cache import cache_selected_pdfs
from daily_agent.storage import cleanup_retention
from daily_agent.workflow_state import StateCorrupt

DAY = date(2026, 10, 8)
PDF = b'%PDF-1.7\nreviewed source'


def setup(tmp_path):
    config = load_config(Path(__file__).resolve().parents[1])
    object.__setattr__(config, 'root', tmp_path)
    config.sources['pdf_cache'] = {'enabled': True, 'max_pdf_bytes': 1024, 'max_urls_per_paper': 2}
    material = MaterialRecord(key='paper:1', source='arxiv', item_type='paper', title='Verified paper',
                              url='https://paper.test', pdf_url='https://paper.test/file.pdf')
    item = ApprovedItem(key=material.key, item_type='paper', title=material.title, source=material.source,
                        url=material.url, final_fields={}, material=material)
    return config, item


def test_cache_reuses_reviewed_bytes_without_network(tmp_path, monkeypatch):
    config, item = setup(tmp_path)
    source = tmp_path / 'source.pdf'
    source.write_bytes(PDF)
    item.material.paper_document = {'source_pdf_path': str(source), 'source_pdf_sha256': hashlib.sha256(PDF).hexdigest()}
    monkeypatch.setattr('daily_agent.connectors.pdf_cache._download_limited', lambda *a, **k: pytest.fail('unneeded network'))
    cache_selected_pdfs([item], config, DAY)
    assert Path(item.material.raw['local_pdf_path']).read_bytes() == PDF
    assert item.material.raw['pdf_cache_status']['matches_reviewed_pdf']
    assert not Path(item.material.raw['local_pdf_relative_path']).is_absolute()


def test_changed_remote_pdf_cannot_replace_reviewed_evidence(tmp_path, monkeypatch):
    config, item = setup(tmp_path)
    item.material.paper_document = {'source_pdf_sha256': hashlib.sha256(PDF).hexdigest()}
    item.material.raw.update(local_pdf_path=str(tmp_path / 'missing.pdf'), local_pdf_report_url='stale')
    monkeypatch.setattr('daily_agent.connectors.pdf_cache._download_limited', lambda *a, **k: b'%PDF-wrong-version')
    cache_selected_pdfs([item], config, DAY)
    assert 'local_pdf_path' not in item.material.raw
    assert 'local_pdf_report_url' not in item.material.raw
    assert item.material.raw['pdf_cache_status']['status'] == 'unavailable'


def test_corrupt_cached_pdf_is_repaired(tmp_path, monkeypatch):
    config, item = setup(tmp_path)
    old = tmp_path / 'stale.pdf'
    old.write_bytes(b'%PDF-corrupt')
    item.material.raw.update(local_pdf_path=str(old), pdf_cache_status={'sha256': hashlib.sha256(PDF).hexdigest()})
    monkeypatch.setattr('daily_agent.connectors.pdf_cache._download_limited', lambda *a, **k: PDF)
    cache_selected_pdfs([item], config, DAY)
    assert Path(item.material.raw['local_pdf_path']).read_bytes() == PDF


def test_legacy_valid_cache_remains_compatible(tmp_path, monkeypatch):
    config, item = setup(tmp_path)
    old = tmp_path / 'legacy.pdf'
    old.write_bytes(PDF)
    item.material.raw['local_pdf_path'] = str(old)
    monkeypatch.setattr('daily_agent.connectors.pdf_cache._download_limited', lambda *a, **k: pytest.fail('unneeded network'))
    cache_selected_pdfs([item], config, DAY)
    assert item.material.raw['local_pdf_path'] == str(old)
    assert item.material.raw['pdf_cache_status']['sha256'] == hashlib.sha256(PDF).hexdigest()


def test_retention_preserves_seals_and_their_assets(tmp_path):
    config, _ = setup(tmp_path)
    config.reports_dir.mkdir(parents=True)
    sealed = config.reports_dir / 'daily-agent-2025-01-01.hash.ready.md'
    orphan = config.reports_dir / 'daily-agent-2025-01-02.md'
    pdf = tmp_path / 'data/pdfs/2025-01-01/retained.pdf'
    disposable = tmp_path / 'data/pdfs/2025-01-02/disposable.pdf'
    for path in [sealed, orphan, pdf, disposable]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('retained evidence')
        os.utime(path, (1, 1))
    folder = config.state_dir / 'ready-reports'
    folder.mkdir(parents=True)
    (folder / '2025-01-01.json').write_text(json.dumps({'report': str(sealed), 'approval': [{'material': {'raw': {'local_pdf_path': str(pdf)}}}]}))
    (folder / '2025-01-01.outbox.json').write_text(json.dumps({'status': 'uncertain'}))
    cleanup_retention(config, DAY)
    assert sealed.exists() and pdf.exists()
    assert not orphan.exists() and not disposable.exists()


def test_corrupt_manifest_stops_retention_before_deletion(tmp_path):
    config, _ = setup(tmp_path)
    folder = config.state_dir / 'ready-reports'
    folder.mkdir(parents=True)
    (folder / '2025-01-01.json').write_text('{bad')
    old = config.reports_dir / 'daily-agent-2025-01-01.ready.md'
    old.parent.mkdir(parents=True)
    old.write_text('evidence')
    os.utime(old, (1, 1))
    with pytest.raises(StateCorrupt):
        cleanup_retention(config, DAY)
    assert old.exists()


def test_cloud_delivery_protects_relative_artifacts(tmp_path):
    config, _ = setup(tmp_path)
    report = config.reports_dir / 'daily-agent-2025-01-01.hash.ready.md'
    report.parent.mkdir(parents=True)
    report.write_text('accepted message evidence')
    os.utime(report, (1, 1))
    folder = config.state_dir / 'cloud-delivery'
    folder.mkdir(parents=True)
    (folder / '2025-01-01.json').write_text(json.dumps({
        'state': 'accepted', 'artifacts': [{'path': report.relative_to(tmp_path).as_posix()}]}))
    cleanup_retention(config, DAY)
    assert report.exists()
