from copy import deepcopy
import hashlib
import json
import pytest
from daily_agent import editorial, parent_writer, reading
from daily_agent.workflow_state import read_json
from test_fidelity_first import fixture, ledger, StageClock, Transport, EvidenceReady


def test_fidelity_first_and_batched_reader_use_real_queue_resume_and_all_evidence(tmp_path,monkeypatch):
    cfg,original=fixture(tmp_path);clock=StageClock();responder=Transport(clock);jobs=[]
    def ready(*a,**k):raise EvidenceReady()
    monkeypatch.setattr(editorial,'_draft_with_llm',ready)
    for _ in range(30):
        record=deepcopy(original)
        try:
            with ledger(cfg,original,clock) as execution:
                editorial._draft_report_items_uncached(cfg,[record],execution=execution)
        except parent_writer.PendingResponse as pending:
            job=read_json(cfg.root/'data/writer-queue'/f'{pending.job_id}.job.json')
            jobs.append(job)
            payload=json.loads(job['prompt'].split('输入：\n',1)[1])
            if 'chunks' in payload:
                response={'contract':payload['contract'],'results':[
                    {'chunk_id':c['id'],'text_sha256':hashlib.sha256(c['text'].encode()).hexdigest(),
                     'note':{'chunk_id':c['id'],'summary':'完整原文块的限定条件证据。','quotes':[c['text'][:100]],
                             'conditions':'fixed baseline','evidence_kind':'simulation'}} for c in payload['chunks']]}
            else:
                response=responder(cfg.root,job['prompt'],10)
            worker='reviewer' if job['stage']=='review' else 'reader'
            claim=parent_writer.claim(cfg.root,pending.job_id,worker)
            parent_writer.import_response(cfg.root,pending.job_id,response,worker,claim_token=claim['token'])
        except EvidenceReady:break
    else:pytest.fail('bounded evidence did not complete')
    assert len(jobs)==6  # 2 transcription + 2 independent review + 2 grouped reads
    assert len({j['job_id'] for j in jobs})==6
    grouped=[j for j in jobs if '原文块批次' in j['prompt']]
    assert len(grouped)==2
    assert all(json.loads(j['prompt'].split('输入：\n',1)[1])['contract']['phase']=='repaired' for j in grouped)
    assert record.reading['complete'] and len(record.reading['notes'])==7
    assert record.reading['visual']['strict_fidelity']
    assert reading.audit_reading([record.to_dict()])['quality_passed_count']==0  # writer/science still required
    with ledger(cfg,original,clock) as execution:
        assert execution.snapshot()['pools']['native']['charged']==0
        with pytest.raises(EvidenceReady):
            editorial._draft_report_items_uncached(cfg,[deepcopy(original)],execution=execution)
    assert len(list((cfg.root/'data/writer-queue').glob('*.job.json')))==6
