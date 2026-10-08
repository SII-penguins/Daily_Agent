from copy import deepcopy
from datetime import date
from pathlib import Path
import hashlib
import json

from daily_agent.config import load_config
from daily_agent.models import MaterialRecord, EditorialDraft
from daily_agent.paper_document import build_document, attach_document, digest, atomic_json
from daily_agent.reading import CORE, read_papers, verify_draft, semantic_review
from daily_agent.deferred_review_cache import draft_report_items, protected_artifacts
from daily_agent.cloud_cache import prepare_batch


def setup(tmp_path):
    config = load_config(Path(__file__).resolve().parents[1])
    object.__setattr__(config, 'root', tmp_path)
    config.sources['reading'] = {'enabled': True, 'run_budget_seconds': 60}
    record = MaterialRecord(key='arxiv:2609.00001', source='arxiv', item_type='paper',
                            title='Quantum Circuit Search', url='https://arxiv.org/abs/2609.00001v1')
    quote = 'Compared with the baseline, circuit depth falls on simulated quantum circuits.'
    text = 'Quantum Circuit Search\nMethods\n' + (quote + '\n') * 50 + '\nResults\n' + quote + '\nDiscussion\nMeasured on simulations only.'
    attach_document(record, build_document(record, [{'page': 1, 'text': text}], record.url, 'html'))
    record.raw['pool_deferral'] = {'status': 'quota_deferred'}
    calls = {'reading': 0, 'visual': 0, 'semantic': 0, 'draft': 0, 'enrich': 0}
    def original(config, records, use_llm=True):
        drafts = []
        for r in records:
            def read(prompt, timeout):
                calls['reading'] += 1
                chunk = json.loads(prompt.split('输入：\n')[1])
                return {'chunk_id': chunk['id'], 'summary': '文中研究量子线路搜索',
                        'quotes': [chunk['text'].strip()[:200]], 'conditions': 'simulation versus baseline', 'evidence_kind': 'simulation'}
            read_papers([r], config, read)
            calls['visual'] += 1
            r.reading['visual'] = {'required_pages': 0}
            calls['draft'] += 1
            fields = {field: '作者报告量子线路搜索结果。' for field in CORE}
            fields['method_steps'] = ['执行量子线路搜索']
            fields['confidence'] = 'medium'
            claims = [{'field': field, 'chunk_id': next(c['id'] for c in r.paper_document['chunks'] if quote in c['text']),
                       'quote': quote, 'conditions': 'simulation versus baseline', 'evidence_kind': 'simulation'} for field in CORE]
            d = EditorialDraft(r.key, 'paper', r.title, fields, claim_evidence=claims)
            verify_draft(d, r)
            def review(prompt, timeout):
                calls['semantic'] += 1
                return {'checks': [{'field': field, 'supported': True, 'reason': 'Supported by the exact evidence'} for field in d.verification['valid_fields']]}
            semantic_review(d, r, review, 10)
            drafts.append(d)
        return drafts
    return config, record, calls, original


def test_next_day_quota_and_topic_changes_make_zero_expensive_calls(tmp_path):
    config, record, calls, original = setup(tmp_path)
    first = draft_report_items(config, [record], True, original)
    assert first[0].verification['status'] == 'located'
    assert all(calls[key] for key in ('reading', 'visual', 'semantic'))
    previous = deepcopy(calls)
    config.quota['paper_target'] = 99
    config.sources['selection']['deferred_revisit_bonus_per_day'] = 2
    record.score = 999
    record.tags = ['changed selection topic']
    record.raw['research_context'] = {'lab': 'newly verified public lab'}
    def enrich(records):
        calls['enrich'] += 1
        return records
    next_batch = prepare_batch(config, date(2026, 10, 9), [record], enrich)
    second = draft_report_items(config, next_batch, True, original)
    assert calls == previous
    assert second == first and record.score == 999


def test_changed_actual_evidence_without_hash_update_invalidates(tmp_path):
    config, record, calls, original = setup(tmp_path)
    draft_report_items(config, [record], True, original)
    before = deepcopy(calls)
    record.paper_document['chunks'][0]['text'] += '\nNew limitation in the actual evidence.'
    draft_report_items(config, [record], True, original)
    assert calls['reading'] > before['reading']
    assert calls['semantic'] == before['semantic'] + 1


def test_draft_context_change_only_recomputes_downstream_stages(tmp_path):
    config, record, calls, original = setup(tmp_path)
    draft_report_items(config, [record], True, original)
    before = deepcopy(calls)
    record.raw['citation_context'] = {'verified_citations': ['new source']}
    draft_report_items(config, [record], True, original)
    assert calls['reading'] == before['reading']
    assert calls['semantic'] == before['semantic'] + 1


def test_changed_model_and_reading_protocol_invalidate(tmp_path):
    config, record, calls, original = setup(tmp_path)
    draft_report_items(config, [record], True, original)
    before = deepcopy(calls)
    config.sources['llm_writer']['model'] = 'different-model'
    def no_refetch(rows):
        raise AssertionError('Model change must not refetch immutable evidence')
    assert prepare_batch(config, date(2026, 10, 9), [record], no_refetch) == [record]
    draft_report_items(config, [record], True, original)
    assert calls['reading'] > before['reading']
    before = deepcopy(calls)
    config.sources['reading']['protocol_version'] = 2
    draft_report_items(config, [record], True, original)
    assert calls['reading'] > before['reading']


def test_tampered_envelope_or_unsupported_review_never_passes(tmp_path):
    config, record, calls, original = setup(tmp_path)
    draft_report_items(config, [record], True, original)
    for path in (tmp_path / 'data/deferred-reviews/reviews').glob('*.json'):
        value = json.loads(path.read_text())
        value['payload']['draft']['draft_fields']['key_result'] = 'Fabricated benefit'
        path.write_text(json.dumps(value))
    draft_report_items(config, [record], True, original)
    assert calls['semantic'] == 2
    # Even a checksummed envelope must satisfy the independent review contract.
    for path in (tmp_path / 'data/deferred-reviews/reviews').glob('*.json'):
        value = json.loads(path.read_text())
        value['payload']['draft']['verification']['semantic_checks'] = []
        value['sha256'] = digest(value['payload'])
        path.write_text(json.dumps(value))
    draft_report_items(config, [record], True, original)
    assert calls['semantic'] == 3


def test_missing_or_tampered_assets_restore_exact_bytes_and_survive_retention(tmp_path):
    config, record, calls, original = setup(tmp_path)
    asset = tmp_path / 'data/pdfs/2026-01-01/evidence.png'
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b'immutable original pixels')
    record.paper_document['pages'][0].update(image_path=str(asset), image_hash=hashlib.sha256(asset.read_bytes()).hexdigest())
    draft_report_items(config, [record], True, original)
    before = deepcopy(calls)
    asset.unlink()
    draft_report_items(config, [record], True, original)
    assert asset.read_bytes() == b'immutable original pixels' and calls == before
    asset.write_bytes(b'tampered')
    draft_report_items(config, [record], True, original)
    assert asset.read_bytes() == b'immutable original pixels' and calls == before
    from daily_agent.storage import write_material_library, cleanup_retention
    write_material_library(config, {record.key: record})
    assert asset.resolve() in protected_artifacts(config)
    cleanup_retention(config, date(2026, 10, 9))
    assert asset.exists()


def test_unrepairable_assets_force_real_checks_not_stored_pass(tmp_path):
    config, record, calls, original = setup(tmp_path)
    asset = tmp_path / 'evidence.png'; asset.write_bytes(b'original')
    record.paper_document['pages'][0].update(image_path=str(asset), image_hash=hashlib.sha256(asset.read_bytes()).hexdigest())
    draft_report_items(config, [record], True, original)
    asset.unlink()
    for blob in (tmp_path / 'data/deferred-reviews/blobs').iterdir():
        blob.unlink()
    draft_report_items(config, [record], True, original)
    assert calls['semantic'] == 2


def test_version_change_and_no_llm_never_restore_old_pass(tmp_path):
    config, record, calls, original = setup(tmp_path)
    draft_report_items(config, [record], True, original)
    record.url = record.url.replace('v1', 'v2')
    draft_report_items(config, [record], True, original)
    assert calls['semantic'] == 2
    observed = []
    def offline(config, rows, use_llm=True):
        observed.append(use_llm)
        return []
    assert draft_report_items(config, [record], False, offline) == []
    assert observed == [False]


def test_real_visual_pipeline_and_pdf_reuse_without_next_day_model_calls(tmp_path, monkeypatch):
    import pytest
    fitz = pytest.importorskip('fitz')
    from daily_agent import editorial
    config, record, _, _ = setup(tmp_path)
    config.sources['reading'].update(fidelity_enabled=True, visual_enabled=True)
    config.sources['llm_writer']['provider'] = 'codex'
    config.sources['report_writing']['source_screenshots_enabled'] = False
    text = record.paper_document['pages'][0]['text']
    pdf_path = tmp_path / 'original.pdf'
    image_path = tmp_path / 'page.png'
    with fitz.open() as pdf:
        page = pdf.new_page()
        page.insert_text((30, 30), text, fontsize=8)
        pdf_path.write_bytes(pdf.tobytes())
        page.get_pixmap().save(str(image_path))
    doc = record.paper_document
    doc.update(source_type='pdf', source_pdf_path=str(pdf_path), source_pdf_sha256=hashlib.sha256(pdf_path.read_bytes()).hexdigest())
    doc['pages'][0].update(visual_required=True, image_path=str(image_path), image_hash=hashlib.sha256(image_path.read_bytes()).hexdigest())
    calls = {'reading': 0, 'visual': 0, 'fidelity': 0, 'semantic': 0, 'draft': 0}
    def fake_run(args, **kwargs):
        prompt = args[-1]
        value = json.loads(prompt.split('输入：\n', 1)[1])
        if prompt.startswith('阅读论文的一个原文块'):
            calls['reading'] += 1
            output = {'chunk_id': value['id'], 'summary': '量子线路原文阅读',
                      'quotes': [value['text'].strip()[:200]], 'conditions': 'simulation', 'evidence_kind': 'simulation'}
        elif prompt.startswith('核对附带的论文页面图片'):
            calls['visual'] += 1
            output = {'page': value['page'], 'summary': '页面与正文一致', 'figures': [], 'tables': [], 'formulas': [], 'text_matches_image': True, 'issues': []}
        elif prompt.startswith('你是论文页面转写员'):
            calls['fidelity'] += 1
            output = {'page': value['page'], 'text': value['native_text'], 'assets': [], 'unresolved': []}
        elif prompt.startswith('你是独立的页面保真复核员'):
            calls['fidelity'] += 1
            output = {'page': value['page'], 'text_supported': True, 'inventory_complete': True, 'checks': [], 'issues': []}
        elif prompt.startswith('你是独立证据核验员'):
            calls['semantic'] += 1
            output = {'checks': [{'field': field, 'supported': True, 'reason': 'Exact source support'} for field in value['fields']]}
        else:
            raise AssertionError(prompt[:100])
        return type('Result', (), {'stdout': json.dumps(output, ensure_ascii=False)})()
    def fake_draft(records):
        output = []
        for r in records:
            calls['draft'] += 1
            fields = {field: '作者报告量子线路搜索结果。' for field in CORE}
            fields.update(method_steps=['执行搜索'], confidence='medium')
            quote = 'Compared with the baseline, circuit depth falls on simulated quantum circuits.'
            cid = next(c['id'] for c in r.paper_document['chunks'] if quote in c['text'])
            claims = [{'field': field, 'chunk_id': cid, 'quote': quote, 'conditions': 'simulation versus baseline', 'evidence_kind': 'simulation'} for field in CORE]
            output.append(EditorialDraft(r.key, 'paper', r.title, fields, claim_evidence=claims))
        return output
    monkeypatch.setattr(editorial.subprocess, 'run', fake_run)
    monkeypatch.setattr(editorial, '_draft_with_llm', fake_draft)
    monkeypatch.setattr('daily_agent.rendering.composition.review_result_presentation', lambda *args: None)
    first = editorial.draft_report_items(config, [record], True)
    assert first[0].verification['status'] == 'located'
    assert record.reading['visual']['strict_fidelity']
    assert all(calls.values())
    before = deepcopy(calls)
    second = editorial.draft_report_items(config, [record], True)
    assert calls == before
    assert first == second
    image_path.unlink()
    pdf_path.write_bytes(b'corrupted PDF')
    third = editorial.draft_report_items(config, [record], True)
    assert calls == before and third == first
    assert pdf_path.read_bytes().startswith(b'%PDF') and image_path.is_file()

    # If neither the artifact nor its immutable backup is recoverable, live
    # visual validation must downgrade the draft instead of replaying PASS.
    image_path.unlink()
    for blob in (tmp_path / 'data/deferred-reviews/blobs').iterdir():
        blob.write_bytes(b'damaged backup')
    monkeypatch.setattr(editorial, '_draft_with_llm_batch', lambda *args, **kwargs: [])
    failed = editorial.draft_report_items(config, [record], True)
    assert failed[0].verification['status'] == 'limited'
    assert not record.reading['visual']['strict_fidelity']


def test_verified_cache_never_overrides_delivered_or_reserved_eligibility(tmp_path, monkeypatch):
    from daily_agent.storage import select_library_candidates
    config, record, calls, original = setup(tmp_path)
    draft_report_items(config, [record], True, original)
    record.published_dates = ['2026-10-08']
    assert select_library_candidates(config, {record.key: record}, date(2026, 10, 9)) == []
    record.published_dates = []
    monkeypatch.setattr('daily_agent.cloud_workflow.reserved_delivery_identities', lambda *args: {record.key})
    assert select_library_candidates(config, {record.key: record}, date(2026, 10, 9)) == []
    assert calls['semantic'] == 1


def test_stage_protocol_hashes_invalidate_only_corresponding_model_cache(tmp_path, monkeypatch):
    from daily_agent import reading, visual_reading, visual_fidelity
    config, record, calls, original = setup(tmp_path)
    original(config, [record])
    before = calls['reading']
    # A change in unrelated author context or topic weights is not evidence.
    record.raw['research_context'] = {'lab': 'newly verified public lab'}
    config.quota['paper_target'] += 1
    original(config, [record])
    assert calls['reading'] == before
    monkeypatch.setattr(reading, 'PROTOCOL_SHA256', 'changed text-stage protocol')
    original(config, [record])
    assert calls['reading'] > before
    image = tmp_path / 'page.png'; image.write_bytes(b'fixture pixels')
    page = record.paper_document['pages'][0]
    page.update(visual_required=True, image_path=str(image), image_hash=hashlib.sha256(image.read_bytes()).hexdigest())
    visual_calls = []
    def visual(prompt, timeout, image_path):
        visual_calls.append(prompt)
        return {'page': 1, 'summary': '页面检查', 'figures': [], 'tables': [], 'formulas': [], 'text_matches_image': True, 'issues': []}
    visual_reading.read_visuals([record], config, visual)
    visual_reading.read_visuals([record], config, visual)
    assert len(visual_calls) == 1
    monkeypatch.setattr(visual_reading, 'PROTOCOL_SHA256', 'changed visual-stage protocol')
    visual_reading.read_visuals([record], config, visual)
    assert len(visual_calls) == 2
    config.sources['reading']['fidelity_enabled'] = True
    fidelity_calls = []
    def fidelity(prompt, timeout, image_path):
        value = json.loads(prompt.split('输入：\n')[1])
        fidelity_calls.append(prompt)
        if 'native_text' in value:
            return {'page': 1, 'text': value['native_text'], 'assets': [], 'unresolved': []}
        return {'page': 1, 'text_supported': True, 'inventory_complete': True, 'checks': [], 'issues': []}
    visual_fidelity.repair_visuals([record], config, fidelity)
    visual_fidelity.repair_visuals([record], config, fidelity)
    assert len(fidelity_calls) == 2
    monkeypatch.setattr(visual_fidelity, 'PROTOCOL_SHA256', 'changed fidelity-stage protocol')
    visual_fidelity.repair_visuals([record], config, fidelity)
    assert len(fidelity_calls) == 4
