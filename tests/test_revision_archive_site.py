"""Offline revision archive and Site evidence checks; no publication or sending."""
from datetime import date
import json

import pytest

from daily_agent import cloud_site_delivery as site
from daily_agent import cloud_workflow
from daily_agent import report_archive as archive
from daily_agent.rendering.editorial import render_editorial_html


DAY='2026-10-08'


def handoff(*, revision_number=None, identity='a'*64, state='accepted', created_at='2026-10-08T12:00:00Z'):
    content=render_editorial_html([],date.fromisoformat(DAY),kind='report').encode()
    value={'schema_version':2,'kind':'report','date':DAY,'identity':identity,'state':state,
        'created_at':created_at,'approval_sha256':'b'*64,'body':'offline fixture',
        'body_sha256':archive.sha(b'offline fixture'),'approval':[],
        'html_report':'data/fixture-'+identity[:12]+'.html',
        'artifacts':[{}, {'sha256':archive.sha(content),'size':len(content)}]}
    if revision_number is not None:
        value.update(schema_version=3,edition_type='production_revision',revision_id=identity,
            revision_number=revision_number,parent_identity='c'*64,contract_sha256='d'*64)
    return value,content


def snapshot(value,content):
    key=archive.archive_entry_key(value)
    record={'key':key,'date':value['date'],'category':key.split('/')[0],'kind':value['kind'],
        'format':'html','created_at':value['created_at'],'source_sha256':archive.sha(content),
        'identity':value['identity'],'approval_sha256':value['approval_sha256'],
        'audit':archive.audit_label(value),'page':key+'/index.html',
        **{field:value[field] for field in archive.REVISION_FIELDS if field in value}}
    return record,content,value['body']


def export(monkeypatch,tmp_path,snapshots):
    monkeypatch.setattr(archive,'collect_entries',lambda roots:snapshots)
    return archive.export_archive([],tmp_path/'site')


@pytest.mark.parametrize('kind,key',[
    ('report','reports/'+DAY),('status','reports/'+DAY),('pilot','examples/'+DAY+'-'+'a'*12),
])
def test_legacy_entry_keys_unchanged(kind,key):
    value,_=handoff();value['kind']=kind
    assert archive.archive_entry_key(value)==key


def test_revision_entry_key_preserves_navigation_depth():
    value,_=handoff(revision_number=2)
    assert archive.archive_entry_key(value)=='reports/'+DAY+'-r2-'+'a'*12
    assert len(archive.archive_entry_key(value).split('/'))==2


def test_empty_legacy_root_does_not_require_revision_configuration(tmp_path):
    assert archive.collect_entries([tmp_path])==[]


def test_legacy_pilot_root_remains_exportable_without_revisions(tmp_path,monkeypatch):
    root=tmp_path/'pilot';(root/'config').mkdir(parents=True)
    (root/'config/delivery.yaml').write_text('cloud:\n  profile: '+cloud_workflow.PROFILE+'\n  pilot: true\n')
    folder=root/'data/state/cloud-delivery';folder.mkdir(parents=True)
    (folder/(DAY+'.json')).write_text('{}')
    value,_=handoff();value['kind']='pilot';value.pop('html_report')
    monkeypatch.setattr(cloud_workflow,'read_handoff',lambda root,day:value)
    entries=archive.collect_entries([root])
    assert len(entries)==1 and entries[0][0]['category']=='examples'


@pytest.mark.parametrize('field,value',[
    ('schema_version',2),('kind','pilot'),('edition_type','original'),
    ('revision_number',0),('revision_number',-1),('revision_number',True),('revision_number','1'),
    ('revision_id','bad'),('identity','a'*63),('parent_identity','B'*64),
    ('contract_sha256','../outside'),('date','2026-02-30'),
])
def test_revision_key_rejects_invalid_metadata(field,value):
    manifest,_=handoff(revision_number=1);manifest[field]=value
    with pytest.raises(ValueError):archive.archive_entry_key(manifest)


@pytest.fixture
def source_handoffs(tmp_path,monkeypatch):
    from daily_agent import production_revision
    root=tmp_path/'source';folder=root/'data/state/cloud-delivery';folder.mkdir(parents=True)
    (folder/(DAY+'.json')).write_text('{}')
    original,html=handoff(identity='1'*64,state='prepared')
    first,_=handoff(revision_number=1,identity='2'*64)
    second,_=handoff(revision_number=2,identity='3'*64,state='prepared')
    for value in (original,first,second):
        path=root/value['html_report'];path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(html)
    monkeypatch.setattr(cloud_workflow,'read_handoff',lambda root,day:original)
    monkeypatch.setattr(production_revision,'iter_handoffs',lambda root:iter((first,second)))
    return root,original,first,second


def test_collection_includes_received_revisions_and_exact_current(source_handoffs):
    root,original,first,second=source_handoffs
    received=archive.collect_entries([root])
    assert [record['identity'] for record,_,_ in received]==[first['identity']]
    current={'root':str(root),'date':DAY,'identity':second['identity'],'revision_id':second['revision_id']}
    entries=archive.collect_entries([root,root],current)
    assert [record['identity'] for record,_,_ in entries]==[first['identity'],second['identity']]
    for record,_,_ in entries:
        assert record['category']=='reports' and record['parent_identity']=='c'*64
    assert '聊天投递尚未受理' in entries[-1][0]['audit']
    current.pop('revision_id');current['identity']=original['identity']
    entries=archive.collect_entries([],current)
    assert [record['identity'] for record,_,_ in entries]==[original['identity'],first['identity']]


def test_current_revision_requires_exact_revision_id_and_sealed_identity(source_handoffs):
    root,_,_,second=source_handoffs
    current={'root':str(root),'date':DAY,'identity':'f'*64,'revision_id':second['revision_id']}
    with pytest.raises(ValueError,match='exact'):archive.collect_entries([],current)
    current.update(identity=second['identity'],revision_id='e'*64)
    with pytest.raises(ValueError,match='not found'):archive.collect_entries([],current)
    current['revision_id']='../outside'
    with pytest.raises(ValueError,match='revision identity'):archive.collect_entries([],current)


def test_revision_export_keeps_original_and_prior_revision_bytes(monkeypatch,tmp_path):
    original=snapshot(*handoff(identity='1'*64,created_at='2026-10-08T23:00:00Z'))
    first=snapshot(*handoff(revision_number=1,identity='2'*64,created_at='2026-10-08T22:00:00Z'))
    second=snapshot(*handoff(revision_number=2,identity='3'*64,created_at='2026-10-08T01:00:00Z'))
    export(monkeypatch,tmp_path,[original])
    root=tmp_path/'site';old_page=(root/original[0]['page']).read_bytes()
    export(monkeypatch,tmp_path,[first])
    first_page=(root/first[0]['page']).read_bytes()
    first[0]['audit']='投递已确认'
    result=export(monkeypatch,tmp_path,[first,second])
    assert len(result['entries'])==3
    assert (root/original[0]['page']).read_bytes()==old_page
    assert (root/first[0]['page']).read_bytes()==first_page
    assert (root/first[0]['key']/'original.html').read_bytes()==first[1]
    assert 'href="../../index.html"' in (root/second[0]['page']).read_text()
    assert '修订版 r2' in (root/second[0]['page']).read_text()
    index=(root/'index.html').read_text()
    assert 'href="'+second[0]['page']+'">直接阅读最新日报 · '+DAY+' · 修订版 r2' in index
    assert index.index('打开修订版 r2')<index.index('打开修订版 r1')<index.index('打开原版日报')
    archive.verify_archive(root)


def test_newer_date_still_takes_precedence_over_revision_number(monkeypatch,tmp_path):
    first=snapshot(*handoff(revision_number=20))
    value,html=handoff(identity='2'*64);value['date']='2026-10-09'
    later=snapshot(value,html)
    export(monkeypatch,tmp_path,[first,later])
    assert 'href="'+later[0]['page']+'">直接阅读最新日报 · 2026-10-09' in (tmp_path/'site/index.html').read_text()


def test_existing_revision_metadata_is_immutable(monkeypatch,tmp_path):
    value=snapshot(*handoff(revision_number=1));export(monkeypatch,tmp_path,[value])
    value[0]['parent_identity']='e'*64
    with pytest.raises(ValueError,match='provenance is immutable'):export(monkeypatch,tmp_path,[value])


def test_verify_rejects_tampered_revision_path_metadata(monkeypatch,tmp_path):
    result=export(monkeypatch,tmp_path,[snapshot(*handoff(revision_number=1))])
    result['entries'][0]['revision_number']=2
    (tmp_path/'site/manifest.json').write_text(json.dumps(result))
    with pytest.raises(ValueError,match='revision archive entry'):archive.verify_archive(tmp_path/'site')


def test_site_entry_and_caption_bind_revision_metadata(monkeypatch,tmp_path):
    manifest,html=handoff(revision_number=2)
    result=export(monkeypatch,tmp_path,[snapshot(manifest,html)])
    record=site._entry(result,manifest)
    content={'page_url':'https://fixture.chatgpt.site/'+record['page'],'site_url':'https://fixture.chatgpt.site'}
    caption=site._caption(manifest,content)
    assert caption.startswith('Daily Agent 日报 · '+DAY+' · 修订版 r2\n')
    assert '界面验收样例' not in caption
    assert record['key']==archive.archive_entry_key(manifest)


@pytest.mark.parametrize('field,value',[
    ('edition_type','original'),('revision_id','e'*64),('revision_number',3),
    ('revision_number',True),('parent_identity','e'*64),('contract_sha256','e'*64),
])
def test_site_rejects_revision_archive_provenance_tampering(monkeypatch,tmp_path,field,value):
    manifest,html=handoff(revision_number=1)
    result=export(monkeypatch,tmp_path,[snapshot(manifest,html)])
    result['entries'][0][field]=value
    with pytest.raises(ValueError):site._entry(result,manifest)


def test_site_rejects_revision_under_original_key(monkeypatch,tmp_path):
    manifest,html=handoff(revision_number=1)
    result=export(monkeypatch,tmp_path,[snapshot(manifest,html)])
    result['entries'][0].update(key='reports/'+DAY,page='reports/'+DAY+'/index.html')
    with pytest.raises(ValueError,match='content/provenance'):site._entry(result,manifest)


def test_legacy_site_caption_bytes_remain_unchanged():
    manifest,_=handoff();content={'page_url':'https://fixture.chatgpt.site/reports/'+DAY+'/index.html','site_url':'https://fixture.chatgpt.site'}
    expected=('Daily Agent 日报 · '+DAY+'\n0 篇已核验论文 · 0 个开源项目\n'
        +'阅读本期 HTML：'+content['page_url']+'\n按日期浏览归档：https://fixture.chatgpt.site/\n'
        +'公共来源版（非全源）；仅收录通过证据审核的内容\n')
    assert site._caption(manifest,content)==expected
    manifest['kind']='pilot'
    assert site._caption(manifest,content)=='界面验收样例（非今日新闻，不进入正式发布历史）\n'+expected


def test_site_bind_routes_only_explicit_revision_to_revision_handler(tmp_path,monkeypatch):
    from daily_agent import production_revision
    calls=[]
    def bind(root,day,**kwargs):
        calls.append((root,day,kwargs));return {'offline':'revision route'}
    monkeypatch.setattr(production_revision,'bind_site',bind)
    paths={key:tmp_path/key for key in ('archive_dir','deployment_file','site_version_file','site_repo')}
    assert site.bind_site(tmp_path,date.fromisoformat(DAY),revision_id='a'*64,**paths)=={'offline':'revision route'}
    assert calls==[(tmp_path,date.fromisoformat(DAY),{'revision_id':'a'*64,**paths})]


def test_archive_cli_accepts_explicit_revision_candidate(tmp_path,monkeypatch,capsys):
    seen=[]
    def export(roots,output,**kwargs):
        seen.append(kwargs['include_current']);return {'entries':[],'files':{}}
    monkeypatch.setattr(archive,'export_archive',export)
    archive.main(['--output',str(tmp_path),'--current-root',str(tmp_path),'--current-date',DAY,
        '--current-identity','a'*64,'--current-revision-id','b'*64])
    assert seen==[{'root':str(tmp_path),'date':DAY,'identity':'a'*64,'revision_id':'b'*64}]
    assert json.loads(capsys.readouterr().out)['entries']==0
    with pytest.raises(SystemExit):archive.main(['--output',str(tmp_path),'--current-revision-id','b'*64])
