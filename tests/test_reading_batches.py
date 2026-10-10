"""Offline bounded-reading contracts. All queues, caches, and ledgers use tmp_path."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from daily_agent import parent_writer, reading, reading_batches as batches
from daily_agent.batch_execution import Execution, ExecutionConflict, candidate, digest as ledger_digest
from daily_agent.models import MaterialRecord
from daily_agent.paper_document import attach_document, build_document, digest, valid_document, version_identity
from daily_agent.workflow_state import StateCorrupt, atomic_json, read_json

BASE_READING_SHA = 'f7d7533884c147285f97fbfb2b82d6720fff45281d58c2e8aacdb06eb674b02e'


def fixture(tmp_path, *, chunks=4, limit=80):
    record = MaterialRecord(key='arxiv:2609.00001', source='arxiv', item_type='paper',
                            title='Offline Source', url='https://example.test/paper')
    labels = ['1. Introduction', '2. Methods', '3. Results', '4. References', '5. Appendix']
    text = ''.join(labels[i % len(labels)] + '\n' + f'Exact source for chunk {i}: ' + 'unique evidence. ' * 5 + '\n' for i in range(chunks))
    document = build_document(record, [{'page': 1, 'text': text}], record.url, 'html', {'min_body_chars': 10})
    assert len(document['chunks']) == chunks
    attach_document(record, document)
    config = SimpleNamespace(root=tmp_path, sources={
        'reading': {'run_budget_seconds': 100, 'visual_budget_seconds': 20,
                    'fidelity_budget_seconds': 100, 'max_chunks_per_paper': limit},
        'llm_writer': {'run_budget_seconds': 100, 'provider': 'parent_queue'},
    })
    clock = SimpleNamespace(now=100.)
    return SimpleNamespace(root=tmp_path, record=record, config=config, clock=clock,
                           candidates=[candidate(record)])


def execution(f):
    return Execution(f.root / 'issue', 'original-batch', f.candidates, f.config,
                     protocol='offline-reading-batch-test', clock=lambda: f.clock.now)


def note(chunk):
    return {'chunk_id': chunk['id'], 'summary': '该块原文内容摘要',
            'quotes': [chunk['text'].strip()[:100]], 'conditions': 'not_stated',
            'evidence_kind': 'not_stated'}


def response(prompt):
    value = json.loads(prompt.split('输入：\n', 1)[1])
    if 'contract' not in value:
        return note(value)
    return {'contract': value['contract'], 'results': [
        {'chunk_id': c['id'], 'text_sha256': batches.text_sha256(c['text']), 'note': note(c)}
        for c in value['chunks']]}


def invoke(f):
    def run(prompt, timeout, **kwargs):
        return parent_writer.request(f.root, prompt, timeout, **kwargs)
    return run


def pending(f, run):
    with pytest.raises(parent_writer.PendingResponse) as caught:
        batches.read_papers([f.record], f.config, invoke(f), execution=run)
    return caught.value.job_id


def job(f, job_id):
    return read_json(f.root / 'data' / 'writer-queue' / (job_id + '.job.json'))


def answer(f, job_id, value=None):
    value = response(job(f, job_id)['prompt']) if value is None else value
    return parent_writer.import_response(f.root, job_id, value, 'offline-reader')


def manifests(f):
    return sorted((f.root / 'issue').rglob('manifest.json'))


def cache(f):
    return f.root / 'data' / 'reading' / batches.fingerprints(f.record.paper_document, f.config)[0]


def consume(f, run):
    jobs = []
    for _ in range(20):
        try:
            batches.read_papers([f.record], f.config, invoke(f), execution=run)
            return jobs
        except parent_writer.PendingResponse as exc:
            jobs.append(exc.job_id)
            answer(f, exc.job_id)
    raise AssertionError('unexpected number of jobs')


def test_original_protocol_is_byte_unchanged_and_prompts_exact(tmp_path):
    assert hashlib.sha256(Path(reading.__file__).read_bytes()).hexdigest() == BASE_READING_SHA
    assert reading.PROTOCOL_SHA256 == BASE_READING_SHA
    f = fixture(tmp_path)
    prompts = []
    def invalid(prompt, timeout):
        prompts.append(prompt)
        return {}
    reading.read_papers([f.record], f.config, invalid)
    for i, chunk in enumerate(f.record.paper_document['chunks']):
        assert prompts[2*i] == batches.single_prompt(chunk)
        assert prompts[2*i+1] == batches.single_prompt(chunk, repair=True)


def test_static_synthetic_span_metadata_reproduces_121_to_70():
    source = json.loads((Path(__file__).parent / 'fixtures' / 'reading_pack_audit_20261009.json').read_text())
    counts = []
    for document in source['documents']:
        chunks = [{**c, 'text': 'x' * c['chars']} for c in document['chunks']]
        grouped = batches.group_chunks(chunks)
        assert sum(len(c['text']) for c in chunks) == document['source_chars']
        assert len(grouped) == document['expected_groups']
        assert [c['id'] for g in grouped for c in g] == [c['id'] for c in chunks]
        assert all(len(g) <= 4 and len({c['page'] for c in g}) == 1
                   and sum(len(c['text']) for c in g) <= 10000 for g in grouped)
        counts.append((len(chunks), len(grouped)))
    assert counts == [(20, 16), (39, 24), (62, 30)]
    assert sum(c[0] for c in counts) == 121 and sum(c[1] for c in counts) == 70


def test_grouping_preserves_boundaries_text_and_all_kinds(tmp_path):
    f = fixture(tmp_path, chunks=5)
    original = deepcopy(f.record.paper_document)
    chunks = original['chunks']
    groups = batches.group_chunks(chunks)
    assert list(map(len, groups)) == [4, 1]
    assert [c for group in groups for c in group] == chunks
    assert valid_document(original, version_identity(f.record))
    huge = deepcopy(chunks[0]); huge['text'] = 'x' * 10001
    with pytest.raises(ValueError):
        batches.group_chunks([huge])
    groups = batches.group_chunks([{**chunks[0], 'text': 'x'*7000}, {**chunks[1], 'text': 'x'*4000}])
    assert list(map(len, groups)) == [1, 1]
    groups = batches.group_chunks([chunks[0], {**chunks[1], 'page': 2}])
    assert list(map(len, groups)) == [1, 1]


@pytest.mark.parametrize('mutation', ['missing', 'extra', 'duplicate', 'document', 'phase', 'group_hash', 'truncated', 'oversize'])
def test_bad_global_envelope_rejects_all_rows(tmp_path, mutation):
    f = fixture(tmp_path)
    chunks = f.record.paper_document['chunks']
    contract = batches._group({'document_sha256': 'doc', 'phase': 'native'}, chunks)
    result = response(batches.group_prompt(contract, chunks))
    if mutation == 'missing': result['results'].pop()
    if mutation == 'extra': result['results'].append(deepcopy(result['results'][0]))
    if mutation == 'duplicate': result['results'][1] = deepcopy(result['results'][0])
    if mutation == 'document': result['contract']['document_sha256'] = 'wrong'
    if mutation == 'phase': result['contract']['phase'] = 'repaired'
    if mutation == 'group_hash': result['contract']['group_sha256'] = 'wrong'
    if mutation == 'truncated': result = {'contract': result['contract']}
    if mutation == 'oversize': result['junk'] = 'x'*20001
    accepted, failed = batches.validate_response(result, contract, chunks, f.config.sources['reading'])
    assert not accepted and failed == [c['id'] for c in chunks]


@pytest.mark.parametrize('mutation', ['neighbor_quote', 'joined_quote', 'swapped_id', 'wrong_hash', 'wrong_note_id', 'bad_type'])
def test_each_row_retains_own_schema_and_quote_ownership(tmp_path, mutation):
    f = fixture(tmp_path)
    chunks = f.record.paper_document['chunks']
    contract = batches._group({'document_sha256': 'doc', 'phase': 'native'}, chunks)
    result = response(batches.group_prompt(contract, chunks))
    row = result['results'][0]
    if mutation == 'neighbor_quote': row['note']['quotes'] = [chunks[1]['text'][:70]]
    if mutation == 'joined_quote': row['note']['quotes'] = [chunks[0]['text'][-30:] + chunks[1]['text'][:30]]
    if mutation == 'swapped_id':
        result['results'][0]['chunk_id'], result['results'][1]['chunk_id'] = result['results'][1]['chunk_id'], result['results'][0]['chunk_id']
    if mutation == 'wrong_hash': row['text_sha256'] = 'x'*64
    if mutation == 'wrong_note_id': row['note']['chunk_id'] = chunks[1]['id']
    if mutation == 'bad_type': row['note']['summary'] = ['中文']
    accepted, failed = batches.validate_response(result, contract, chunks, f.config.sources['reading'])
    assert chunks[0]['id'] not in accepted
    assert chunks[0]['id'] in failed
    assert len(accepted) == (2 if mutation == 'swapped_id' else 3)


def test_four_members_consume_four_original_slots_and_resume_one_job(tmp_path):
    f = fixture(tmp_path)
    original = deepcopy(f.record.paper_document)
    with execution(f) as run:
        jid = pending(f, run)
        first_manifest = manifests(f)[0].read_bytes()
        state = run.snapshot()
        assert len(state['operations']) == 4
        assert {op['queue_job_id'] for op in state['operations'].values()} == {jid}
        assert {op['identity'][3] for op in state['operations'].values()} == {0}
        assert job(f, jid)['stage'] == 'reading'
        assert pending(f, run) == jid
        assert manifests(f)[0].read_bytes() == first_manifest
    with execution(f) as run:
        assert pending(f, run) == jid
        answer(f, jid)
        batches.read_papers([f.record], f.config, invoke(f), execution=run)
        assert f.record.reading['complete']
        assert len(f.record.reading['notes']) == 4
        assert run.snapshot()['writer_circuit'] is None
        assert len(run.snapshot()['operations']) == 4
        batches.read_papers([f.record], f.config, lambda *a, **k: pytest.fail('cache must avoid transport'), execution=run)
    assert f.record.paper_document == original
    assert manifests(f)[0].read_bytes() == first_manifest


def test_exact_legacy_cache_mixed_with_old_single_and_new_batch(tmp_path):
    f = fixture(tmp_path, chunks=5)
    chunks = f.record.paper_document['chunks']
    # The old reader writes precisely the same cache fingerprint.
    reading.read_papers([f.record], f.config, lambda p, t: note(json.loads(p.split('输入：\n', 1)[1])))
    assert cache(f).name == f.record.reading['fingerprint']
    for chunk in chunks[1:]:
        (cache(f) / (chunk['id'] + '.json')).unlink()
    with execution(f) as run:
        op = (f.record.key, 'native', 'chunk:' + chunks[1]['id'], 0)
        with run.stage('native'), pytest.raises(parent_writer.PendingResponse) as caught:
            parent_writer.request(f.root, batches.single_prompt(chunks[1]), 20, execution=run, operation=op)
        old_jid = caught.value.job_id
        saved_job = job(f, old_jid)
        assert pending(f, run) == old_jid
        manifest = read_json(manifests(f)[0])['payload']
        assert [e['mode'] for e in manifest['entries']] == ['cache', 'single', 'group', 'group', 'group']
        answer(f, old_jid)
        new_jid = pending(f, run)
        assert new_jid != old_jid
        assert len(json.loads(job(f, new_jid)['prompt'].split('输入：\n')[1])['chunks']) == 3
        answer(f, new_jid)
        batches.read_papers([f.record], f.config, invoke(f), execution=run)
        assert f.record.reading['complete']
        assert job(f, old_jid)['prompt'] == saved_job['prompt']
        assert len(run.snapshot()['operations']) == 4  # cached chunk consumed no extra slot


@pytest.mark.parametrize('change', ['content', 'layout', 'model', 'evidence'])
def test_cache_identity_changes_are_not_compatible(tmp_path, change):
    f = fixture(tmp_path)
    before = batches.fingerprints(f.record.paper_document, f.config)
    if change == 'content': f.record.paper_document['content_hash'] = 'changed'
    if change == 'layout': f.record.paper_document['chunks'][0]['offset'] += 1
    if change == 'model': f.config.sources['llm_writer']['model'] = 'new-model'
    if change == 'evidence': f.config.sources['reading']['prompt_version'] = 2
    assert batches.fingerprints(f.record.paper_document, f.config)[0] != before[0]


def test_valid_rows_project_and_only_failed_members_use_ordinal_one(tmp_path):
    f = fixture(tmp_path)
    with execution(f) as run:
        jid = pending(f, run)
        bad = response(job(f, jid)['prompt'])
        bad['results'][1]['note']['quotes'] = ['fabricated source quote']
        bad['results'][3]['text_sha256'] = 'wrong'
        answer(f, jid, bad)
        repair_jid = pending(f, run)
        repair = json.loads(job(f, repair_jid)['prompt'].split('输入：\n')[1])
        ids = [c['id'] for c in f.record.paper_document['chunks']]
        assert [c['id'] for c in repair['chunks']] == [ids[1], ids[3]]
        assert repair['contract']['failed_response_sha256'] == digest(bad)
        assert (cache(f)/(ids[0]+'.json')).exists() and (cache(f)/(ids[2]+'.json')).exists()
        assert len(run.snapshot()['operations']) == 6
        answer(f, repair_jid)
        batches.read_papers([f.record], f.config, invoke(f), execution=run)
        assert f.record.reading['complete']
        assert len(run.snapshot()['operations']) == 6
        assert len(run.snapshot()['settlements']) == 3


def test_bad_repair_never_creates_third_inference(tmp_path):
    f = fixture(tmp_path)
    with execution(f) as run:
        jid = pending(f, run); answer(f, jid, {})
        retry = pending(f, run); answer(f, retry, {})
        for _ in range(3):
            batches.read_papers([f.record], f.config, invoke(f), execution=run)
            assert not f.record.reading['complete']
            assert len(run.snapshot()['operations']) == 8
        assert len(list((f.root/'data/writer-queue').glob('*.job.json'))) == 2


@pytest.mark.parametrize('point', ['manifest', 'response', 'first_note'])
def test_cancellation_replays_same_manifest_response_and_members(tmp_path, monkeypatch, point):
    f = fixture(tmp_path)
    real = batches.atomic_json
    triggered = False
    def crash(path, value):
        nonlocal triggered
        real(path, value)
        match = ((point == 'manifest' and path.name == 'manifest.json') or
                 (point == 'response' and path.name.endswith('.response.json')) or
                 (point == 'first_note' and path.parent == cache(f) and path.name == 'c0001.json'))
        if not triggered and match:
            triggered = True
            raise KeyboardInterrupt('simulated cancellation')
    with execution(f) as run:
        if point != 'manifest':
            jid = pending(f, run); answer(f, jid)
        monkeypatch.setattr(batches, 'atomic_json', crash)
        with pytest.raises(KeyboardInterrupt):
            batches.read_papers([f.record], f.config, invoke(f), execution=run)
        saved = manifests(f)[0].read_bytes()
        monkeypatch.setattr(batches, 'atomic_json', real)
        consume(f, run)
        assert f.record.reading['complete']
        assert manifests(f)[0].read_bytes() == saved
        assert len(run.snapshot()['operations']) == 4
        assert len(list((f.root/'data/writer-queue').glob('*.job.json'))) == 1


@pytest.mark.parametrize('target', ['manifest', 'response', 'repair', 'missing_manifest'])
def test_corrupt_durable_records_fail_closed(tmp_path, target):
    f = fixture(tmp_path)
    with execution(f) as run:
        jid = pending(f, run)
        answer(f, jid, {} if target == 'repair' else None)
        if target == 'repair':
            pending(f, run)
        elif target == 'response':
            batches.read_papers([f.record], f.config, invoke(f), execution=run)
            (cache(f)/'c0001.json').unlink()
        manifest = manifests(f)[0]
        if target == 'missing_manifest': manifest.unlink()
        else:
            path = manifest if target == 'manifest' else next(manifest.parent.glob('*.' + target + '.json'))
            path.write_text('{invalid json')
        with pytest.raises(StateCorrupt):
            batches.read_papers([f.record], f.config, invoke(f), execution=run)
        assert len(list((f.root/'data/writer-queue').glob('*.job.json'))) == (2 if target == 'repair' else 1)


def test_partial_cache_never_changes_manifest_members_or_prompt(tmp_path):
    f = fixture(tmp_path)
    with execution(f) as run:
        jid = pending(f, run)
        frozen = manifests(f)[0].read_bytes()
        prompt = job(f, jid)['prompt']
        atomic_json(cache(f)/'c0002.json', note(f.record.paper_document['chunks'][1]))
        assert pending(f, run) == jid
        assert job(f, jid)['prompt'] == prompt
        assert manifests(f)[0].read_bytes() == frozen
        answer(f, jid)
        batches.read_papers([f.record], f.config, invoke(f), execution=run)
        assert f.record.reading['complete']


def test_same_manifest_concurrency_has_one_group_job_and_no_duplicate_slots(tmp_path):
    f = fixture(tmp_path)
    records = [MaterialRecord.from_dict(f.record.to_dict()) for _ in range(2)]
    with execution(f) as run:
        def work(record):
            try:
                batches.read_papers([record], f.config, invoke(f), execution=run)
            except parent_writer.PendingResponse as exc:
                return exc.job_id
        with ThreadPoolExecutor(max_workers=2) as pool:
            ids = list(pool.map(work, records))
        assert len(set(ids)) == 1
        assert len(manifests(f)) == 1
        assert len(run.snapshot()['operations']) == 4
        answer(f, ids[0])
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert list(pool.map(work, records)) == [None, None]
        assert all(r.reading['complete'] for r in records)
        assert len(list((f.root/'data/writer-queue').glob('*.job.json'))) == 1


def test_max_chunks_still_counts_original_chunks_not_groups(tmp_path):
    f = fixture(tmp_path, chunks=5, limit=3)
    with execution(f) as run:
        assert len(consume(f, run)) == 1
        assert not f.record.reading['complete']
        assert f.record.reading['read_chunk_ids'] == ['c0001','c0002','c0003']
        assert len(run.snapshot()['operations']) == 3
        assert f.record.reading['total_chunks'] == 5


def test_wrong_source_span_fails_before_any_queue_admission(tmp_path):
    f = fixture(tmp_path)
    f.record.paper_document['chunks'][0]['offset'] += 1
    with execution(f) as run, pytest.raises(StateCorrupt):
        batches.read_papers([f.record], f.config, invoke(f), execution=run)
    assert not list((f.root/'data/writer-queue').glob('*.job.json'))


def test_synthetic_documents_use_real_manifest_planner_121_to_70(tmp_path):
    import gzip
    source = json.loads(gzip.decompress((Path(__file__).parent / 'fixtures' / 'reading_actual_documents_20261009.json.gz').read_bytes()))
    counts = []
    for index, value in enumerate(source['documents']):
        f = fixture(tmp_path / str(index))
        record = MaterialRecord.from_dict(value['record'])
        f.candidates = [candidate(record)]
        f.record = record
        with execution(f) as run:
            record.paper_document = deepcopy(value['document'])
            exact = deepcopy(record.paper_document)
            assert valid_document(exact, version_identity(record))
            run.bind_repaired(record)
            batches.read_papers([record], f.config, None, execution=run, phase='repaired')
            manifest = read_json(manifests(f)[0])['payload']
            groups = manifest['groups']
            expected_ids = [c['id'] for c in exact['chunks']]
            seen = [member['chunk_id'] for group in groups for member in group['members']]
            assert seen == expected_ids and len(set(seen)) == len(expected_ids)
            by_id = {c['id']: c for c in exact['chunks']}
            for group in groups:
                assert len(group['members']) <= 4
                chunks = [by_id[m['chunk_id']] for m in group['members']]
                assert len({c['page'] for c in chunks}) == 1
                assert sum(len(c['text']) for c in chunks) <= 10000
                assert group['members'] == [batches._span(c) for c in chunks]
                for chunk in chunks:
                    assert reading.valid_note(note(chunk), chunk, f.config.sources['reading'])
            assert len(groups) == value['expected_groups']
            assert record.paper_document == exact
            assert not run.snapshot()['operations']
            counts.append((len(expected_ids), len(groups)))
    assert counts == [(20,16), (39,24), (62,30)]
    assert reading.PROTOCOL_SHA256 == BASE_READING_SHA


def test_existing_answered_single_is_reused_without_recreating_job(tmp_path):
    f = fixture(tmp_path)
    chunk = f.record.paper_document['chunks'][0]
    with execution(f) as run:
        with run.stage('native'), pytest.raises(parent_writer.PendingResponse) as caught:
            parent_writer.request(f.root, batches.single_prompt(chunk), 10, execution=run,
                                  operation=(f.record.key,'native','chunk:'+chunk['id'],0))
        original_id = caught.value.job_id
        answer(f, original_id)
        path = f.root/'data/writer-queue'/(original_id+'.job.json')
        saved = path.read_bytes()
        next_id = pending(f, run)
        assert next_id != original_id
        assert (cache(f)/(chunk['id']+'.json')).exists()
        assert path.read_bytes() == saved
        assert run.snapshot()['operations'][ledger_digest([f.record.key,'native','chunk:'+chunk['id'],0])]['queue_job_id'] == original_id
        assert len(list(path.parent.glob('*.job.json'))) == 2


def test_all_member_admission_fails_atomically_when_one_slot_exceeds_budget(tmp_path):
    f = fixture(tmp_path, limit=3)
    chunks = f.record.paper_document['chunks']
    group = batches._group({'document_sha256':'test','phase':'native'},chunks)
    with execution(f) as run:
        with run.stage('native'):
            before = run.path.read_bytes()
            from daily_agent.batch_execution import BudgetExhausted
            with pytest.raises(BudgetExhausted):
                parent_writer.request(f.root,batches.group_prompt(group,chunks),20,execution=run,
                                      operations=[(f.record.key,'native','chunk:'+c['id'],0) for c in chunks])
            assert run.path.read_bytes() == before
            assert not run.snapshot()['operations']
        assert not list((f.root/'data/writer-queue').glob('*.job.json'))


@pytest.mark.parametrize('saved_projection', [False, True])
def test_zero_remaining_budget_replays_answer_without_transport(tmp_path, monkeypatch, saved_projection):
    f = fixture(tmp_path)
    with execution(f) as run:
        jid = pending(f, run); answer(f, jid)
        if saved_projection:
            original = batches.atomic_json
            def crash(path,value):
                original(path,value)
                if path.parent == cache(f) and path.name == 'c0001.json':
                    raise KeyboardInterrupt('crash after one row')
            monkeypatch.setattr(batches,'atomic_json',crash)
            with pytest.raises(KeyboardInterrupt):
                batches.read_papers([f.record],f.config,invoke(f),execution=run)
            monkeypatch.setattr(batches,'atomic_json',original)
        with run.stage('native'):
            f.clock.now += 100
        assert run.remaining('native') == 0
        batches.read_papers([f.record],f.config,lambda *a,**k: pytest.fail('no inference allowed'),execution=run)
        assert f.record.reading['complete']
        assert len(run.snapshot()['operations']) == 4
        assert run.snapshot()['pools']['native']['charged'] == 100
        assert len(list((f.root/'data/writer-queue').glob('*.job.json'))) == 1


def test_fullread_does_not_bypass_claim_or_independent_review(tmp_path):
    f = fixture(tmp_path)
    with execution(f) as run:
        consume(f, run)
    assert f.record.reading['complete']
    audit = reading.audit_reading([f.record.to_dict()])
    assert not audit['all_passed']
    assert not audit['papers'][0]['claims_passed']
    assert f.record.reading.get('verification', {}).get('semantic_support') != 'model_checked'


def test_corrupt_receipt_not_hidden_by_all_valid_projected_notes(tmp_path):
    f = fixture(tmp_path)
    with execution(f) as run:
        consume(f, run)
        receipt = next(manifests(f)[0].parent.glob('*.response.json'))
        receipt.write_text('{corrupt')
        with pytest.raises(StateCorrupt):
            batches.read_papers([f.record],f.config,invoke(f),execution=run)


def test_queue_answer_wrong_hash_cannot_be_replayed_as_cache(tmp_path):
    f = fixture(tmp_path)
    with execution(f) as run:
        jid = pending(f, run); answer(f, jid)
        path = f.root/'data/writer-queue'/(jid+'.answer.json')
        corrupt = read_json(path); corrupt['response_sha256'] = 'wrong'
        atomic_json(path, corrupt)
        with pytest.raises(StateCorrupt):
            batches.read_papers([f.record],f.config,invoke(f),execution=run)
        assert not (cache(f)/'c0001.json').exists()


def test_cache_hole_preserves_each_source_span(tmp_path):
    f = fixture(tmp_path)
    chunks = f.record.paper_document['chunks']
    atomic_json(cache(f)/'c0002.json', note(chunks[1]))
    with execution(f) as run:
        jid = pending(f, run)
        payload = json.loads(job(f,jid)['prompt'].split('输入：\n')[1])
        assert [c['id'] for c in payload['chunks']] == ['c0001','c0003','c0004']
        assert payload['chunks'] == [chunks[0],chunks[2],chunks[3]]
        answer(f,jid)
        batches.read_papers([f.record],f.config,invoke(f),execution=run)
        assert f.record.reading['read_chunk_ids'] == [c['id'] for c in chunks]
        assert len(run.snapshot()['operations']) == 3


@pytest.mark.parametrize('change', ['subset','superset','disjoint','reverse','scalar_subset','scalar_extension'])
def test_same_actual_job_cannot_change_exact_member_set_or_order(tmp_path, change):
    f = fixture(tmp_path, chunks=5)
    chunks = f.record.paper_document['chunks']
    operations = [(f.record.key,'native','chunk:'+c['id'],0) for c in chunks]
    group = batches._group({'phase':'native','document_sha256':'fixture'}, chunks[:2])
    prompt = batches.group_prompt(group,chunks[:2])
    with execution(f) as run:
        with run.stage('native'), pytest.raises(parent_writer.PendingResponse) as caught:
            parent_writer.request(f.root,prompt,20,execution=run,operations=operations[:2])
        jid = caught.value.job_id
        with run.stage('native'):
            before = run.path.read_bytes()
            with pytest.raises(parent_writer.PendingResponse) as repeated:
                parent_writer.request(f.root,prompt,20,execution=run,operations=operations[:2])
            assert repeated.value.job_id == jid
            assert run.path.read_bytes() == before
            altered = {'subset': operations[:1], 'superset': operations[:3],
                       'disjoint': operations[2:4], 'reverse': list(reversed(operations[:2]))}.get(change)
            kwargs = {'operations':altered} if altered is not None else {
                'operation':operations[0 if change == 'scalar_subset' else 2]}
            with pytest.raises(ExecutionConflict):
                parent_writer.request(f.root,prompt,20,execution=run,**kwargs)
            assert run.path.read_bytes() == before
            assert len(run.snapshot()['operations']) == 2
        assert len(list((f.root/'data/writer-queue').glob('*.job.json'))) == 1


def test_admission_before_queue_publication_recovers_identical_group(tmp_path,monkeypatch):
    f=fixture(tmp_path)
    real=parent_writer.atomic_json
    def crash(path,value):
        if path.name.endswith('.job.json'):
            raise KeyboardInterrupt('admitted before queue publication')
        real(path,value)
    with execution(f) as run:
        monkeypatch.setattr(parent_writer,'atomic_json',crash)
        with pytest.raises(KeyboardInterrupt):
            batches.read_papers([f.record],f.config,invoke(f),execution=run)
        assert len(run.snapshot()['operations']) == 4
        bound={op['queue_job_id'] for op in run.snapshot()['operations'].values()}
        frozen=manifests(f)[0].read_bytes()
        monkeypatch.setattr(parent_writer,'atomic_json',real)
        jid=pending(f,run)
        assert bound == {jid}
        assert manifests(f)[0].read_bytes() == frozen
        answer(f,jid)
        batches.read_papers([f.record],f.config,invoke(f),execution=run)
        assert f.record.reading['complete']
        assert len(run.snapshot()['operations']) == 4


def test_after_ledger_write_io_failure_keeps_original_manifest_and_no_free_slots(tmp_path,monkeypatch):
    f=fixture(tmp_path)
    real=parent_writer.atomic_json
    def fail(path,value):
        if path.name.endswith('.job.json'):
            raise OSError('simulated queue persistence failure')
        real(path,value)
    with execution(f) as run:
        monkeypatch.setattr(parent_writer,'atomic_json',fail)
        with pytest.raises(StateCorrupt):
            batches.read_papers([f.record],f.config,invoke(f),execution=run)
        assert len(run.snapshot()['operations']) == 4
        assert len(manifests(f)) == 1
    monkeypatch.setattr(parent_writer,'atomic_json',real)
    with execution(f) as run:
        consume(f,run)
        assert len(run.snapshot()['operations']) == 4
        assert f.record.reading['complete']


def test_repair_deadline_retains_valid_rows_without_more_slots(tmp_path,monkeypatch):
    f=fixture(tmp_path)
    with execution(f) as run:
        jid=pending(f,run)
        bad=response(job(f,jid)['prompt'])
        bad['results'][1]['text_sha256']='wrong'
        answer(f,jid,bad)
        original=batches._project
        def slow_projection(folder,notes):
            original(folder,notes)
            if len(notes)==3:
                f.clock.now+=100
        monkeypatch.setattr(batches,'_project',slow_projection)
        batches.read_papers([f.record],f.config,invoke(f),execution=run)
        assert len(f.record.reading['notes'])==3
        assert f.record.reading['failures'][0]['chunk_id']=='c0002'
        assert run.remaining('native')==0
        assert len(run.snapshot()['operations'])==4
        assert len(list((f.root/'data/writer-queue').glob('*.job.json')))==1


def test_transport_source_and_serialized_prompt_bounds(tmp_path):
    f=fixture(tmp_path)
    chunks=f.record.paper_document['chunks']
    group=batches._group({'document_sha256':'source','phase':'native'},chunks)
    invalid=deepcopy(group);invalid['members'][0]['text_sha256']='wrong'
    with pytest.raises(ValueError):
        batches.group_prompt(invalid,chunks)
    chunks=[{**chunks[0],'text':'x'*10001}]
    with pytest.raises(ValueError):
        batches.group_prompt(batches._group({},chunks),chunks)
    chunks=[{**chunks[0],'text':'a','section':'x'*70000}]
    with pytest.raises(ValueError):
        batches.group_prompt(batches._group({},chunks),chunks)


@pytest.mark.parametrize('retained', ['queue_only','initial_receipt','repair_receipt'])
def test_retired_generation_rejects_old_answer_and_saved_receipt(tmp_path, retained):
    f=fixture(tmp_path)
    with execution(f) as run:
        jid=pending(f,run)
        if retained == 'repair_receipt':
            answer(f,jid,{})
            retired=pending(f,run)
            answer(f,retired)
            batches.read_papers([f.record],f.config,invoke(f),execution=run)
        else:
            retired=jid
            answer(f,jid)
            if retained == 'initial_receipt':
                batches.read_papers([f.record],f.config,invoke(f),execution=run)
        queue=f.root/'data/writer-queue'
        atomic_json(queue/(retired+'.active.json'),{'base_job_id':retired,'job_id':'f'*64})
        with run.stage('native'):
            before=run.path.read_bytes()
            with pytest.raises(StateCorrupt,match='retired'):
                batches._read_record(f.record,f.config,invoke(f),run,'native')
            assert run.path.read_bytes()==before
        assert (queue/(retired+'.answer.json')).exists()
        assert len(list(queue.glob('*.job.json'))) == (2 if retained == 'repair_receipt' else 1)


def test_corrupt_active_pointer_blocks_answer_projection(tmp_path):
    f=fixture(tmp_path)
    with execution(f) as run:
        jid=pending(f,run);answer(f,jid)
        atomic_json(f.root/'data/writer-queue'/(jid+'.active.json'),{'base_job_id':'wrong','job_id':jid})
        with pytest.raises(StateCorrupt):
            batches.read_papers([f.record],f.config,invoke(f),execution=run)
        assert not (cache(f)/'c0001.json').exists()


@pytest.mark.parametrize('corruption', ['owner','job','invalid_generation','after_expiry','malformed'])
def test_replay_rejects_mismatched_retained_claim_provenance(tmp_path,corruption):
    from datetime import datetime,timedelta
    f=fixture(tmp_path)
    with execution(f) as run:
        jid=pending(f,run)
        claim=parent_writer.claim(f.root,jid,'offline-reader')
        imported=parent_writer.import_response(f.root,jid,response(job(f,jid)['prompt']),
                                               'offline-reader',claim_token=claim['token'])
        path=f.root/'data/writer-queue'/(jid+'.claim.json')
        if corruption=='owner':claim['worker_id']='different-reader'
        if corruption=='job':claim['job_id']='e'*64
        if corruption=='invalid_generation':claim['generation']=True
        if corruption=='after_expiry':
            claim['expires_at']=(datetime.fromisoformat(imported['received_at'])-timedelta(seconds=1)).isoformat()
        atomic_json(path,claim)
        if corruption=='malformed':path.write_text('{invalid')
        with pytest.raises(StateCorrupt):
            batches.read_papers([f.record],f.config,invoke(f),execution=run)
        assert not (cache(f)/'c0001.json').exists()
        assert len(run.snapshot()['operations'])==4


def test_imported_answer_survives_later_claim_and_job_expiry(tmp_path):
    from datetime import datetime,timedelta,timezone
    f=fixture(tmp_path)
    with execution(f) as run:
        jid=pending(f,run)
        claim=parent_writer.claim(f.root,jid,'offline-reader')
        imported=parent_writer.import_response(f.root,jid,response(job(f,jid)['prompt']),
                                               'offline-reader',claim_token=claim['token'])
        queue=f.root/'data/writer-queue'
        past=datetime.now(timezone.utc)-timedelta(hours=2)
        imported['received_at']=past.isoformat()
        claim['expires_at']=(past+timedelta(minutes=1)).isoformat()
        old_job=job(f,jid);old_job['expires_at']=(past+timedelta(minutes=2)).isoformat()
        atomic_json(queue/(jid+'.answer.json'),imported)
        atomic_json(queue/(jid+'.claim.json'),claim)
        atomic_json(queue/(jid+'.job.json'),old_job)
        with run.stage('native'):f.clock.now+=100
        batches.read_papers([f.record],f.config,lambda *a,**k:pytest.fail('no inference'),execution=run)
        assert f.record.reading['complete']
        assert len(run.snapshot()['operations'])==4


def test_queue_answer_read_serializes_with_response_import(tmp_path,monkeypatch):
    f=fixture(tmp_path)
    with execution(f) as run:
        jid=pending(f,run)
        prompt=job(f,jid)['prompt']
        operations=[(f.record.key,'native','chunk:'+c['id'],0) for c in f.record.paper_document['chunks']]
        entered=threading.Event();release=threading.Event();import_started=threading.Event();import_done=threading.Event()
        real=batches.read_json
        def gated_read(path):
            if path.name==jid+'.answer.json':
                entered.set()
                assert release.wait(timeout=5)
            return real(path)
        def importing():
            import_started.set()
            result=answer(f,jid)
            import_done.set()
            return result
        monkeypatch.setattr(batches,'read_json',gated_read)
        with run.stage('native'), ThreadPoolExecutor(max_workers=2) as pool:
            reading_future=pool.submit(batches._queue_answer,f.config,run,prompt,operations)
            assert entered.wait(timeout=5)
            import_future=pool.submit(importing)
            assert import_started.wait(timeout=5)
            assert not import_done.wait(timeout=.1)
            release.set()
            assert reading_future.result(timeout=5)==(False,None)
            import_future.result(timeout=5)
        monkeypatch.setattr(batches,'read_json',real)
        batches.read_papers([f.record],f.config,invoke(f),execution=run)
        assert f.record.reading['complete']
        assert len(run.snapshot()['operations'])==4


def test_missing_job_with_retained_answer_is_not_admission_recovery(tmp_path):
    f=fixture(tmp_path)
    with execution(f) as run:
        jid=pending(f,run);answer(f,jid)
        queue=f.root/'data/writer-queue'
        (queue/(jid+'.job.json')).unlink()
        with pytest.raises(StateCorrupt,match='lost its immutable queue job'):
            batches.read_papers([f.record],f.config,invoke(f),execution=run)
        assert not (queue/(jid+'.job.json')).exists()
        assert not (cache(f)/'c0001.json').exists()
        assert len(run.snapshot()['operations'])==4
