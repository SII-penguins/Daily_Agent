"""Offline verification of parent-mediated private Sites delivery evidence.

No tool calls, network access, deployment or messaging is performed here. The
parent must supply genuine Sites deployment/version evidence. Exact committed
static source bytes are checked locally; no production HTTP read is claimed.
"""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit

from daily_agent.workflow_state import atomic_bytes, atomic_json, exclusive_lock, read_json, StateCorrupt


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _deployment(raw):
    if not isinstance(raw,dict) or raw.get('isError') is not False:
        raise ValueError('Successful Sites tool result required')
    value=raw.get('structuredContent')
    if (not isinstance(value,dict) or value.get('status')!='succeeded' or value.get('type')!='publish'
            or value.get('failure_message') is not None):
        raise ValueError('Actual succeeded Site publication required')
    for field,prefix in [('id','appgdep_'),('project_id','appgprj_')]:
        if not re.fullmatch(prefix+'[A-Za-z0-9]+',str(value.get(field,''))):
            raise ValueError('Invalid Site deployment identity')
    if not re.fullmatch(re.escape(value['project_id'])+r'~appgver_[A-Za-z0-9]+',str(value.get('version_id',''))):
        raise ValueError('Site project/version mismatch')
    url=value.get('url',''); parsed=urlsplit(url)
    if (parsed.scheme!='https' or not parsed.hostname or not parsed.hostname.endswith('.chatgpt.site')
            or parsed.username or parsed.password or parsed.port or parsed.query or parsed.fragment
            or parsed.path not in {'','/'} or any(ord(c)<33 for c in url)):
        raise ValueError('Verified private Site base URL required')
    try:
        stamp=datetime.fromisoformat(value['updated_at'].replace('Z','+00:00'))
        if stamp.tzinfo is None: raise ValueError('Naive deployment time')
    except (KeyError,TypeError,ValueError) as exc:
        raise ValueError('Invalid deployment timestamp') from exc
    return value


def _entry(archive, manifest):
    from daily_agent.report_archive import strict_date, archive_entry_key, _revision_metadata, REVISION_FIELDS
    if not isinstance(archive,dict) or archive.get('schema_version')!=1:
        raise ValueError('Invalid archive manifest')
    matches=[e for e in archive.get('entries',[]) if e.get('identity')==manifest['identity']]
    if len(matches)!=1: raise ValueError('Exact sealed handoff archive entry required')
    entry=matches[0]
    category='examples' if manifest['kind']=='pilot' else 'reports'
    key=archive_entry_key(manifest)
    revision=_revision_metadata(manifest)
    archived_revision=_revision_metadata({**entry,'schema_version':3}) if any(field in entry for field in REVISION_FIELDS) else {}
    if archived_revision!=revision:
        raise ValueError('Archive revision metadata does not match sealed provenance')
    strict_date(entry.get('date'))
    if (entry.get('key')!=key or entry.get('page')!=key+'/index.html'
            or entry.get('date')!=manifest['date'] or entry.get('category')!=category
            or entry.get('kind')!=manifest['kind'] or entry.get('format')!='html'
            or entry.get('source_sha256')!=manifest['artifacts'][1]['sha256']
            or entry.get('approval_sha256')!=manifest['approval_sha256']):
        raise ValueError('Archive entry does not match sealed content/provenance')
    original=archive.get('files',{}).get(key+'/original.html',{})
    if original!={'sha256':manifest['artifacts'][1]['sha256'],'size':manifest['artifacts'][1]['size']}:
        raise ValueError('Archived original HTML hash/size mismatch')
    page=archive.get('files',{}).get(entry['page'])
    if not isinstance(page,dict) or not re.fullmatch('[0-9a-f]{64}',str(page.get('sha256',''))) or type(page.get('size')) is not int or page['size']<=0:
        raise ValueError('Verified dated page required')
    return entry


def _content_binding(manifest, deployment, archive):
    entry=_entry(archive,manifest)
    base=deployment['url'].rstrip('/')
    return {'project_id':deployment['project_id'],'version_id':deployment['version_id'],
            'deployment_id':deployment['id'],'site_url':base,'entry_key':entry['key'],
            'page_url':base+'/'+entry['page'],'original_url':base+'/'+entry['key']+'/original.html',
            'html_sha256':manifest['artifacts'][1]['sha256'],
            'approval_sha256':manifest['approval_sha256'],'ready_source_sha256':manifest['source_sha256'],
            'handoff_identity':manifest['identity'],'page_sha256':archive['files'][entry['page']]['sha256']}


def _caption(manifest, content):
    papers=sum(row['item_type']=='paper' for row in manifest['approval'])
    repos=len(manifest['approval'])-papers
    prefix='界面验收样例（非今日新闻，不进入正式发布历史）\n' if manifest['kind']=='pilot' else ''
    edition=' · 修订版 r'+str(manifest['revision_number']) if manifest.get('edition_type')=='production_revision' else ''
    return (prefix+f"Daily Agent 日报 · {manifest['date']}{edition}\n{papers} 篇已核验论文 · {repos} 个开源项目\n"
            +f"阅读本期 HTML：{content['page_url']}\n按日期浏览归档：{content['site_url']}/\n"
            +'公共来源版（非全源）；仅收录通过证据审核的内容\n')


def _version(raw, deployment):
    if not isinstance(raw,dict) or raw.get('isError') is not False:
        raise ValueError('Actual Site version tool response required')
    v=raw.get('structuredContent')
    if (not isinstance(v,dict) or v.get('id')!=deployment['version_id']
            or v.get('project_id')!=deployment['project_id'] or v.get('deployment_id')!=deployment['id']
            or not re.fullmatch('[0-9a-f]{40}',str(v.get('source',{}).get('commit_sha','')))):
        raise ValueError('Site version/source/deployment mismatch')
    return v


def _source_proof(repo, version, deployment, archive_bytes, archive):
    commit=version['source']['commit_sha']
    def git(*args):
        return subprocess.run(['git','-C',str(Path(repo).resolve()),*args],check=True,capture_output=True,timeout=30).stdout
    # Do not inspect working-tree files: only the exact committed source version.
    hosting=json.loads(git('show',commit+':.openai/hosting.json'))
    if hosting!={'static':{'directory':'dist'},'project_id':deployment['project_id']}:
        raise ValueError('Site source must serve this project static dist directly')
    names=git('ls-tree','-r','-z','--name-only',commit,'--','dist').decode().split('\0')
    expected={'dist/'+name for name in archive['files']}|{'dist/manifest.json'}
    if set(filter(None,names))!=expected: raise ValueError('Committed Site file set differs from archive')
    files={}
    for name in sorted(expected):
        # Reject symlinks/submodules; Git object bytes must be regular files.
        mode=git('ls-tree',commit,'--',name).decode().split()[0]
        if mode not in {'100644','100755'}: raise ValueError('Nonregular Site source artifact')
        data=git('show',commit+':'+name)
        local=archive_bytes if name=='dist/manifest.json' else None
        metadata={'sha256':_sha(data),'size':len(data)}
        if local is not None:
            if data!=local: raise ValueError('Committed manifest differs from local archive')
        elif metadata!=archive['files'][name[5:]]:
            raise ValueError('Committed report bytes differ from archive')
        files[name[5:]]=metadata
    return {'commit_sha':commit,'hosting':hosting,'files':files}


def _validate_source_proof(proof,version,deployment,archive_bytes,archive):
    expected={**archive['files'],'manifest.json':{'sha256':_sha(archive_bytes),'size':len(archive_bytes)}}
    if (proof.get('commit_sha')!=version['source']['commit_sha']
            or proof.get('hosting')!={'static':{'directory':'dist'},'project_id':deployment['project_id']}
            or proof.get('files')!=expected):
        raise ValueError('Committed source provenance mismatch')


def validate_site_binding(root, manifest):
    from daily_agent.cloud_workflow import _safe_relative, _binding_identity
    binding=manifest.get('site_binding')
    if not isinstance(binding,dict) or manifest.get('library_binding') is not None:
        raise StateCorrupt('Invalid or mixed Site/Library binding')
    try:
        data={}
        for name in ('deployment','site_version','archive_manifest','source_proof','dispatch_caption'):
            evidence=binding['evidence'][name]
            value=_safe_relative(Path(root),evidence['path']).read_bytes()
            if _sha(value)!=evidence['sha256'] or type(evidence['size']) is not int or len(value)!=evidence['size']:
                raise ValueError('Site evidence bytes changed')
            data[name]=value
        deployment=_deployment(json.loads(data['deployment']))
        version=_version(json.loads(data['site_version']),deployment)
        archive=json.loads(data['archive_manifest'])
        _validate_source_proof(json.loads(data['source_proof']),version,deployment,data['archive_manifest'],archive)
        content=_content_binding(manifest,deployment,archive)
        content['source_commit_sha']=version['source']['commit_sha']
        if binding['content']!=content: raise ValueError('Site content/deployment identity mismatch')
        body=data['dispatch_caption'].decode()
        if body!=_caption(manifest,content) or manifest.get('dispatch_body_sha256')!=_sha(data['dispatch_caption']):
            raise ValueError('Site dispatch caption mismatch')
        if manifest.get('delivery_identity')!=_binding_identity(manifest,binding):
            raise ValueError('Site binding identity mismatch')
        if manifest['state']=='confirmed' and manifest.get('verified_site_delivery')!=manifest['delivery_identity']:
            raise ValueError('Missing Site caption delivery confirmation')
        return body
    except (KeyError,TypeError,ValueError,OSError) as exc:
        raise StateCorrupt('Invalid Site delivery evidence: '+str(exc)) from exc


def bind_site(root, day, *, archive_dir, deployment_file, site_version_file, site_repo, revision_id=None):
    """Bind terminal Site success to exact version and committed archive bytes.

    Parent owns authenticity of Sites tool responses and verified private access.
    This is deployed-source provenance, NOT a claimed production HTTP round trip.
    """
    if revision_id is not None:
        from daily_agent.production_revision import bind_site as bind_revision_site
        return bind_revision_site(root,day,revision_id=revision_id,archive_dir=archive_dir,
            deployment_file=deployment_file,site_version_file=site_version_file,site_repo=site_repo)
    from daily_agent.cloud_workflow import _config,_path,read_handoff,_binding_identity
    from daily_agent.report_archive import verify_archive
    config=_config(root); path=_path(config,day)
    with exclusive_lock(config.state_dir/'cloud-dispatch.lock'), exclusive_lock(path.with_suffix('.lock')):
        manifest=read_handoff(root,day);manifest.pop('body');manifest.pop('library_file_ids',None)
        if manifest['schema_version']!=2 or manifest['state']!='prepared' or manifest.get('library_binding') is not None:
            raise ValueError('Site binding requires an unbound prepared HTML handoff')
        archive=verify_archive(archive_dir)
        archive_bytes=(Path(archive_dir)/'manifest.json').read_bytes()
        deployment_bytes=Path(deployment_file).read_bytes(); deployment=_deployment(json.loads(deployment_bytes))
        version_bytes=Path(site_version_file).read_bytes(); version=_version(json.loads(version_bytes),deployment)
        proof=_source_proof(site_repo,version,deployment,archive_bytes,archive)
        content=_content_binding(manifest,deployment,archive); content['source_commit_sha']=version['source']['commit_sha']
        caption=_caption(manifest,content).encode()
        payloads=[('deployment',deployment_bytes),('site_version',version_bytes),('archive_manifest',archive_bytes),
                  ('source_proof',json.dumps(proof,sort_keys=True).encode()),('dispatch_caption',caption)]
        evidence={}
        for name,data in payloads:
            relative=f'data/cloud-artifacts/{day}/site-{name}-{_sha(data)}.evidence'
            evidence[name]={'path':relative,'sha256':_sha(data),'size':len(data)}
        binding={'content':content,'evidence':evidence}
        if manifest.get('site_binding') is not None and manifest['site_binding']!=binding:
            raise ValueError('Site binding is immutable; replacement requires a separate handoff')
        for name,data in payloads: atomic_bytes(config.root/evidence[name]['path'],data)
        manifest.update(site_binding=binding,delivery_identity=_binding_identity(manifest,binding),dispatch_body_sha256=_sha(caption))
        validate_site_binding(config.root,manifest)
        atomic_json(path,manifest)
    return read_handoff(root,day)
