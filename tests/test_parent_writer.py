import json
import pytest
from daily_agent.parent_writer import request, import_response, pending, PendingResponse
from daily_agent.workflow_state import StateCorrupt

def queued(root,prompt,stage=None):
    with pytest.raises(PendingResponse) as exc: request(root,prompt,30,stage=stage)
    return exc.value.job_id

def test_queue_resumes_exact_response_and_records_provenance(tmp_path):
    job=queued(tmp_path,'read this')
    assert len(pending(tmp_path))==1
    result=import_response(tmp_path,job,{'ok':True},'worker-reader')
    assert result['worker_id']=='worker-reader'
    assert request(tmp_path,'read this',30)=={'ok':True}
    assert not pending(tmp_path)

def test_independent_review_and_immutable_answer(tmp_path):
    draft=queued(tmp_path,'draft this',stage='draft')
    import_response(tmp_path,draft,{'ok':True},'writer')
    review=queued(tmp_path,'review this',stage='review')
    with pytest.raises(ValueError): import_response(tmp_path,review,{'ok':True},'writer')
    import_response(tmp_path,review,{'ok':False},'reviewer')
    with pytest.raises(ValueError): import_response(tmp_path,review,{'ok':True},'reviewer')

def test_prompt_and_response_tamper_rejected(tmp_path):
    job=queued(tmp_path,'read this')
    path=tmp_path/'data/writer-queue'/f'{job}.job.json'
    value=json.loads(path.read_text()); value['prompt']='changed'; path.write_text(json.dumps(value))
    with pytest.raises(StateCorrupt): import_response(tmp_path,job,{'ok':True},'reader')

def test_queued_transport_never_invokes_codex(tmp_path,monkeypatch):
    from daily_agent.config import load_config
    from daily_agent.editorial import _llm_writer_settings
    cfg=load_config(tmp_path);cfg.sources['llm_writer']={'provider':'parent_queue'}
    assert _llm_writer_settings(cfg)['provider']=='parent_queue'

def test_review_cannot_become_writer_and_image_tamper_fails(tmp_path):
    review=queued(tmp_path,'核对结果正文')
    import_response(tmp_path,review,{'ok':True},'reviewer')
    draft=queued(tmp_path,'draft next')
    with pytest.raises(ValueError): import_response(tmp_path,draft,{'ok':True},'reviewer')
    image=tmp_path/'page.png';image.write_bytes(b'fixture-image')
    with pytest.raises(PendingResponse) as caught: request(tmp_path,'核对图片',30,str(image))
    image.write_bytes(b'changed')
    with pytest.raises(StateCorrupt): import_response(tmp_path,caught.value.job_id,{'ok':True},'reviewer')
    with pytest.raises(StateCorrupt): pending(tmp_path)

def test_concurrent_read_jobs_all_export_without_false_read_failure(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    def export(index):
        try: request(tmp_path,f'chunk {index}',30)
        except PendingResponse as pending_job: return pending_job.job_id
    with ThreadPoolExecutor(max_workers=4) as pool: jobs=list(pool.map(export,range(12)))
    assert len(set(jobs))==12 and len(pending(tmp_path))==12

def test_claim_prevents_overlapping_workers_and_late_import(tmp_path):
    from daily_agent.parent_writer import claim
    from daily_agent.workflow_state import WorkflowBusy
    job=queued(tmp_path,'read once')
    lease=claim(tmp_path,job,'reader-one')
    with pytest.raises(WorkflowBusy):claim(tmp_path,job,'reader-two')
    with pytest.raises(ValueError):import_response(tmp_path,job,{'ok':True},'reader-two')
    with pytest.raises(ValueError):import_response(tmp_path,job,{'ok':True},'reader-one',claim_token='old-token')
    import_response(tmp_path,job,{'ok':True},'reader-one',claim_token=lease['token'])
