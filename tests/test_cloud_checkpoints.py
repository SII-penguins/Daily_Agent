from datetime import date
from pathlib import Path
import pytest
from daily_agent.cloud_workflow import init_profile,_config
from daily_agent.cloud_cache import fetch_source,prepare_batch
from daily_agent.models import DigestItem,MaterialRecord,RunStatus,SourceStatus
from daily_agent.workflow_state import StateCorrupt

@pytest.fixture
def config(tmp_path):
    init_profile(Path(__file__).resolve().parents[1],tmp_path/'cloud','codex')
    return _config(tmp_path/'cloud')

def test_source_resume_reuses_frozen_results_and_error_visibility(config):
    calls=[]
    def original(name,callback,status):
        calls.append(name);status.sources.append(SourceStatus(name=name,ok=False,item_count=1,error='partial 403'))
        status.errors.append('partial 403')
        return [DigestItem(id='x',source='github',item_type='repo',title='x',url='https://github.com/a/x')]
    for _ in range(10):
        state=RunStatus();items=fetch_source(config,date(2026,10,8),'GitHub',lambda:None,state,original)
        assert len(items)==1 and state.errors==['partial 403'] and not state.sources[0].ok
    assert len(calls)==1

def test_batch_resume_skips_expensive_enrichment_and_rejects_tamper(config):
    record=MaterialRecord(key='github:a/x',source='github',item_type='repo',title='x',url='https://github.com/a/x')
    calls=[]
    def enrich(batch):calls.append(1);return batch
    assert prepare_batch(config,date(2026,10,8),[record],enrich)[0].key==record.key
    assert prepare_batch(config,date(2026,10,8),[record],enrich)[0].key==record.key
    assert len(calls)==1
    next((config.root/'data/cloud-checkpoints').rglob('*.json')).write_text('{"payload":[],"sha256":"bad"}')
    with pytest.raises(StateCorrupt):prepare_batch(config,date(2026,10,8),[record],enrich)

def test_incomplete_batch_retries_after_cooldown_preserving_successes(config):
    from daily_agent.cloud_cache import _read,_write
    from datetime import datetime,timezone,timedelta
    paper=MaterialRecord(key='pmlr:p',source='pmlr',item_type='paper',title='p',url='https://proceedings.mlr.press/v1/p.html')
    repo=MaterialRecord(key='github:a/x',source='github',item_type='repo',title='x',url='https://github.com/a/x',readme_excerpt='verified readme')
    calls=[]
    def enrich(batch):
        calls.append([record.key for record in batch])
        if len(calls)>1:
            for record in batch:
                record.paper_document={'document_kind':'full_text'};record.paper_text_status={'available':True}
        return batch
    prepare_batch(config,date(2026,10,8),[paper,repo],enrich)
    prepare_batch(config,date(2026,10,8),[paper,repo],enrich)
    assert len(calls)==1
    path=next((config.root/'data/cloud-checkpoints').rglob('*.json'));payload=_read(path)
    payload['enriched_at']=(datetime.now(timezone.utc)-timedelta(seconds=301)).isoformat();_write(path,payload)
    result=prepare_batch(config,date(2026,10,8),[paper,repo],enrich)
    assert calls[-1]==[paper.key] and result[0].paper_document['document_kind']=='full_text'
    assert result[1].readme_excerpt=='verified readme'

def test_full_domain_config_invalidates_source_checkpoint(config):
    from daily_agent.cloud_cache import _file
    before=_file(config,date(2026,10,8),'sources','arxiv')
    config.domains[0].arxiv_categories.append('fixture.changed')
    assert _file(config,date(2026,10,8),'sources','arxiv')!=before
