"""Offline Sites proof fixtures, never fabricated production verification."""
from datetime import timedelta
import copy
import json
from pathlib import Path
import shutil
import subprocess
import pytest
from test_cloud_html_handoff import report, DAY, prepared, accepted
from daily_agent.cloud_workflow import (read_handoff,record_transition,bind_library,_config,_path,reserved_delivery_identities)
from daily_agent.cloud_site_delivery import bind_site
from daily_agent.report_archive import export_archive
from daily_agent.workflow_state import StateCorrupt

@pytest.fixture
def site(report,tmp_path):
    p=prepared(report);output=tmp_path/'export'
    export_archive([],output,include_current={'root':str(report),'date':str(DAY),'identity':p['identity']})
    repo=tmp_path/'site';(repo/'.openai').mkdir(parents=True)
    shutil.copytree(output,repo/'dist',ignore=shutil.ignore_patterns('.export.lock'))
    (repo/'.openai/hosting.json').write_text(json.dumps({'static':{'directory':'dist'},'project_id':'appgprj_fixture'}))
    def git(*a):return subprocess.run(['git','-C',str(repo),*a],capture_output=True,check=True).stdout.decode().strip()
    git('init');git('add','.');git('-c','user.name=Test','-c','user.email=test@example.org','commit','-m','Fixture')
    commit=git('rev-parse','HEAD')
    deployment={'isError':False,'structuredContent':{'id':'appgdep_fixture','project_id':'appgprj_fixture',
        'version_id':'appgprj_fixture~appgver_fixture','type':'publish','status':'succeeded',
        'failure_message':None,'url':'https://fixture.chatgpt.site','updated_at':'2026-10-08T10:00:00Z'}}
    version={'isError':False,'structuredContent':{'id':'appgprj_fixture~appgver_fixture','project_id':'appgprj_fixture',
        'deployment_id':'appgdep_fixture','source':{'commit_sha':commit}}}
    dep=tmp_path/'deployment.json';dep.write_text(json.dumps(deployment))
    ver=tmp_path/'version.json';ver.write_text(json.dumps(version))
    return {'root':report,'archive_dir':output,'deployment_file':dep,'site_version_file':ver,'site_repo':repo}


def bind(site):return bind_site(day=DAY,**site)


def test_site_source_proof_bound_then_exact_caption_confirms(site):
    before=read_handoff(site['root'],DAY)
    bound=bind(site)
    assert bound['identity']==before['identity'] and bound['body_sha256']==before['body_sha256']
    assert bound['body']!=before['body'] and bound['dispatch_body_sha256']!=before['body_sha256']
    assert 'https://fixture.chatgpt.site/reports/2026-10-08/index.html' in bound['body']
    assert bound['library_file_ids']==[] and not bound['publication_reconciled']
    sending=record_transition(site['root'],DAY,'begin')
    receipt=record_transition(site['root'],DAY,'accepted',attempt_id=sending['attempt_id'],message_id='site_message')
    with pytest.raises(ValueError,match='body'):
        record_transition(site['root'],DAY,'confirmed',attempt_id=receipt['attempt_id'],message_id='site_message',
                          conversation=receipt['conversation'],body=before['body'])
    confirmed=record_transition(site['root'],DAY,'confirmed',attempt_id=receipt['attempt_id'],message_id='site_message',
                          conversation=receipt['conversation'],body=receipt['body'])
    assert confirmed['state']=='confirmed' and confirmed['publication_reconciled']
    assert confirmed['verified_site_delivery']==bound['delivery_identity']


def test_bound_site_roundtrip_does_not_require_git_repo_after_restore(site,tmp_path):
    bound=bind(site)
    moved=tmp_path/'restored';shutil.copytree(site['root'],moved)
    shutil.rmtree(site['site_repo'])
    assert read_handoff(moved,DAY)['body']==bound['body']


@pytest.mark.parametrize('field,value',[('status','pending'),('type','preview'),('failure_message','failed'),
    ('version_id','appgprj_other~appgver_fixture'),('url','https://evil.example/'),('url','https://fixture.chatgpt.site/other')])
def test_failed_or_mismatched_deployment_rejected(site,field,value):
    path=site['deployment_file'];data=json.loads(path.read_text());data['structuredContent'][field]=value;path.write_text(json.dumps(data))
    with pytest.raises(ValueError):bind(site)
    assert read_handoff(site['root'],DAY)['state']=='prepared'
    assert 'site_binding' not in read_handoff(site['root'],DAY)


@pytest.mark.parametrize('field,value',[('id','appgprj_fixture~appgver_wrong'),('project_id','appgprj_other'),('deployment_id','appgdep_other')])
def test_version_alias_mismatch_rejected(site,field,value):
    path=site['site_version_file'];data=json.loads(path.read_text());data['structuredContent'][field]=value;path.write_text(json.dumps(data))
    with pytest.raises(ValueError):bind(site)


def test_committed_source_mismatch_rejected_not_worktree(site):
    path=site['archive_dir']/'manifest.json';data=json.loads(path.read_text());data['entries'][0]['audit']='different'
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError,match='Committed manifest'):bind(site)


def test_worktree_tamper_does_not_replace_committed_proof(site):
    (site['site_repo']/'dist/index.html').write_text('uncommitted tamper')
    assert bind(site)['site_binding']['content']['source_commit_sha']


@pytest.mark.parametrize('name',['deployment','site_version','archive_manifest','source_proof','dispatch_caption'])
def test_site_evidence_postbinding_mutation_blocks_send(site,name):
    value=bind(site);path=site['root']/value['site_binding']['evidence'][name]['path'];path.write_bytes(b'changed')
    with pytest.raises(StateCorrupt):record_transition(site['root'],DAY,'begin')


def test_native_accepted_receipt_cannot_switch_to_site(site,tmp_path):
    receipt=accepted(site['root'],tmp_path)
    before=_path(_config(site['root']),DAY).read_bytes()
    with pytest.raises(ValueError):bind(site)
    assert _path(_config(site['root']),DAY).read_bytes()==before
    assert receipt['state']=='accepted'


def test_site_cannot_mix_library_and_partial_observation(site,tmp_path):
    value=bind(site);export=tmp_path/'export.html';export.write_bytes((site['root']/value['html_report']).read_bytes())
    with pytest.raises(ValueError):bind_library(site['root'],DAY,file_id='native',version=0,verified_file=export)
    sending=record_transition(site['root'],DAY,'begin')
    receipt=record_transition(site['root'],DAY,'accepted',attempt_id=sending['attempt_id'],message_id='site_message')
    with pytest.raises(ValueError):record_transition(site['root'],DAY,'observe-readback',attempt_id=receipt['attempt_id'],message_id='site_message',conversation=receipt['conversation'],body=receipt['body'],observation={'partial':True,'attachments':[]})
    assert 'github:test/repo' in reserved_delivery_identities(_config(site['root']),DAY+timedelta(days=1))


def test_binding_retry_is_identical(site):
    one=bind(site);two=bind(site)
    assert one==two


def test_native_cannot_override_caption_hash(report):
    prepared(report);path=_path(_config(report),DAY);data=json.loads(path.read_text())
    data['dispatch_body_sha256']='a'*64;path.write_text(json.dumps(data))
    with pytest.raises(StateCorrupt):read_handoff(report,DAY)


def test_fresh_profile_preserves_context_registry_and_visual_provider(report):
    source=Path(__file__).resolve().parents[1]
    assert (report/'config/research-context-evidence.json').read_bytes()==(source/'config/research-context-evidence.json').read_bytes()
    assert _config(report).sources['report_writing']['paper_visual_selection_provider']=='parent_queue'


def test_site_config_cannot_accidentally_send_native_attachment(site,tmp_path):
    import yaml
    root=site['root'];p=prepared(root);file=tmp_path/'native.html';file.write_bytes((root/p['html_report']).read_bytes())
    bind_library(root,DAY,file_id='native',version=0,verified_file=file)
    path=root/'config/delivery.yaml';data=yaml.safe_load(path.read_text());data['cloud']['delivery_transport']='site';path.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError,match='Site transport'):record_transition(root,DAY,'begin')
