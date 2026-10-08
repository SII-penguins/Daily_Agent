import base64
from datetime import date
import hashlib
import json
from pathlib import Path

import pytest

from daily_agent import report_archive as archive
from daily_agent.rendering.editorial import render_editorial_html


PNG=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aL1kAAAAASUVORK5CYII=')


def entry(day='2026-10-08', category='reports', image=False):
    html=render_editorial_html([],date.fromisoformat(day),kind='pilot' if category=='examples' else 'status')
    if image:
        html=html.replace('</main>','<figure><img src="data:image/png;base64,'+base64.b64encode(PNG).decode()+'" alt="Original table"></figure></main>')
    content=html.encode()
    key=category+'/'+day
    return ({'key':key,'date':day,'category':category,'kind':'pilot' if category=='examples' else 'status',
        'source_sha256':archive.sha(content),'identity':'a'*64,'approval_sha256':'b'*64,
        'audit':'发送已受理；尚未完成投递回读核验','page':key+'/index.html'},content,'body')


def export(monkeypatch,tmp_path,entries):
    monkeypatch.setattr(archive,'collect_entries',lambda roots:entries)
    return archive.export_archive([],tmp_path/'site')


def test_complete_archive_date_navigation_and_hashed_images(monkeypatch,tmp_path):
    report=entry(image=True)
    result=export(monkeypatch,tmp_path,[report,entry(category='examples')])
    root=tmp_path/'site'
    assert len(result['entries'])==2
    assert '直接阅读最新日报' in (root/'index.html').read_text()
    assert '验收样例 · 不计入正式日报' in (root/'index.html').read_text()
    assert (root/report[0]['key']/'original.html').read_bytes()==report[1]
    page=(root/report[0]['page']).read_text()
    assert 'src="../../assets/'+hashlib.sha256(PNG).hexdigest()+'.png"' in page
    assert 'src="data:' not in page
    assert (root/'assets'/f'{archive.sha(PNG)}.png').read_bytes()==PNG
    archive.verify_archive(root)


def test_example_does_not_create_formal_day(monkeypatch,tmp_path):
    export(monkeypatch,tmp_path,[entry(category='examples')])
    index=(tmp_path/'site/index.html').read_text()
    assert '尚无正式日报' in index
    assert '直接阅读最新日报' not in index
    assert not (tmp_path/'site/reports').exists()


def test_existing_dates_immutable_and_not_removed(monkeypatch,tmp_path):
    original=entry(); export(monkeypatch,tmp_path,[original])
    page=tmp_path/'site'/original[0]['page']; saved=page.read_bytes()
    export(monkeypatch,tmp_path,[])
    assert page.read_bytes()==saved
    updated=entry(); updated[0]['audit']='投递已确认'
    result=export(monkeypatch,tmp_path,[updated])
    assert result['entries'][0]['audit']=='投递已确认'
    assert page.read_bytes()==saved
    changed=entry(); changed[0]['identity']='c'*64
    with pytest.raises(ValueError,match='immutable'): export(monkeypatch,tmp_path,[changed])
    assert page.read_bytes()==saved


@pytest.mark.parametrize('value',['2026-2-03','2026-02-30','../2026-10-08','2026-10-08.html','20261008',''])
def test_invalid_dates(value):
    with pytest.raises(ValueError): archive.strict_date(value)


@pytest.mark.parametrize('fragment',[
    '<script>alert(1)</script>', '<img src="https://example.org/tracker.png">',
    '<img src="file:///tmp/secret">', '<a href="javascript:alert(1)">bad</a>',
    '<a href="http://127.0.0.1">bad</a>', '<p onclick="x()">bad</p>',
    '<style>p{background:url(https://example.org)}</style>', '<iframe src="index.html"></iframe>',
    '<meta http-equiv="refresh" content="0;url=https://example.org">',
])
def test_no_remote_assets_scripts_or_local_links(fragment):
    with pytest.raises(ValueError): archive.inspect_html(fragment)


@pytest.mark.parametrize('path',['/tmp/x','../x','a/../../x','a\\x','a//b'])
def test_escape_paths(tmp_path,path):
    with pytest.raises(ValueError): archive.safe_path(tmp_path,path)


def test_tampered_page_and_missing_asset_fail(monkeypatch,tmp_path):
    result=export(monkeypatch,tmp_path,[entry(image=True)])
    root=tmp_path/'site'; asset=next(n for n in result['files'] if n.endswith('.png'))
    (root/asset).unlink()
    with pytest.raises(FileNotFoundError): archive.verify_archive(root)
    (root/asset).write_bytes(PNG)
    (root/'index.html').write_text('tampered')
    with pytest.raises(ValueError,match='hash'): archive.verify_archive(root)


def test_broken_relative_link_fails_even_with_matching_hash(monkeypatch,tmp_path):
    result=export(monkeypatch,tmp_path,[entry()]); root=tmp_path/'site'
    content=(root/'index.html').read_text().replace('reports/2026-10-08/index.html','reports/2026-10-09/index.html').encode()
    (root/'index.html').write_bytes(content)
    result['files']['index.html']={'size':len(content),'sha256':archive.sha(content)}
    (root/'manifest.json').write_text(json.dumps(result))
    with pytest.raises(ValueError,match='Broken'): archive.verify_archive(root)


def test_index_escapes_audit_and_rejects_unsafe_base_url(monkeypatch,tmp_path):
    report=entry(); report[0]['audit']='<script>bad</script>'
    export(monkeypatch,tmp_path,[report])
    index=(tmp_path/'site/index.html').read_text()
    assert '&lt;script&gt;' in index and '<script>' not in index
    with pytest.raises(ValueError): archive.export_archive([],tmp_path/'site',base_url='https://localhost/private')


def test_explicit_current_requires_exact_sealed_identity(tmp_path,monkeypatch):
    from daily_agent import cloud_workflow
    root=tmp_path/'state'; folder=root/'data/state/cloud-delivery'; folder.mkdir(parents=True)
    (folder/'2026-10-08.json').write_text('{}')
    value={'state':'prepared','kind':'pilot','identity':'a'*64,'body_sha256':'b'*64,'approval_sha256':'c'*64,'body':'candidate preview'}
    monkeypatch.setattr(cloud_workflow,'read_handoff',lambda root,day:value)
    assert archive.collect_entries([root])==[]
    current={'root':str(root),'date':'2026-10-08','identity':'a'*64}
    entries=archive.collect_entries([root],current)
    assert len(entries)==1
    assert '聊天投递尚未受理' in entries[0][0]['audit']
    assert entries[0][0]['category']=='examples'
    current['identity']='d'*64
    with pytest.raises(ValueError,match='exact'): archive.collect_entries([root],current)


def test_symlink_asset_refused(tmp_path):
    root=tmp_path/'site'; root.mkdir(); (root/'asset.png').symlink_to(tmp_path/'outside')
    with pytest.raises(ValueError,match='symlinks'): archive.safe_path(root,'asset.png')
