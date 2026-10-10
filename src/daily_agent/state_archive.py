"""Verified private-state snapshot/restore; never overwrite existing state.

The parent must persist the produced archive privately and retain its checksum.
This utility does not upload, grant access, or imply that a backup exists remotely.
"""
from contextlib import ExitStack
from datetime import datetime,timezone
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import zipfile
from daily_agent.cloud_workflow import _config
from daily_agent.parent_writer import queue_lock
from daily_agent.workflow_state import exclusive_lock,read_json,StateCorrupt

MAX_BYTES=2*1024**3

def digest(content):return hashlib.sha256(content).hexdigest()

def _files(root):
    paths=[]
    for path in sorted(root.rglob('*')):
        if path.is_symlink():raise ValueError('State snapshot refuses symlinks')
        if not path.is_file() or path.suffix=='.lock':continue
        if path.parent == root and path.name in {'STATE-MANIFEST.json', '.daily-agent-recovery.json'}:continue
        name=path.name.lower()
        if name in {'auth.json','secrets.toml','credentials.json','.env'} or name.startswith('.env.') or name.endswith(('.pem','.key')):
            raise ValueError('Credential-like file present; keep credentials outside state archives')
        paths.append(path)
    return paths

def snapshot(root,output):
    config=_config(root);root=config.root;output=Path(output).resolve()
    if output.is_relative_to(root):raise ValueError('Archive must be outside the state root')
    output.parent.mkdir(parents=True,exist_ok=True)
    _files(root)  # Reject credential-like names before any JSON inspection.
    with ExitStack() as stack:
        stack.enter_context(exclusive_lock(config.state_dir/'cloud-dispatch.lock'))
        stack.enter_context(exclusive_lock(config.state_dir/'cloud-generation.lock'))
        stack.enter_context(exclusive_lock(config.state_dir/'pipeline.lock'))
        stack.enter_context(queue_lock(root/'data/writer-queue/queue.lock'))
        generation=read_json(config.state_dir/'cloud-generation.json')
        if generation and generation.get('running'):raise StateCorrupt('Running generation cannot be snapshotted safely')
        for journal in sorted((config.state_dir/'cloud-delivery').glob('*.json')):
            stack.enter_context(exclusive_lock(journal.with_suffix('.lock')))
        # Revisions and batch execution have independent mutation leases. Take
        # them nonblocking; never wait while holding a conflicting lock order.
        held = {config.state_dir/'cloud-dispatch.lock', config.state_dir/'cloud-generation.lock',
                config.state_dir/'pipeline.lock', root/'data/writer-queue/queue.lock'}
        held.update(p.with_suffix('.lock') for p in (config.state_dir/'cloud-delivery').glob('*.json'))
        for lock in sorted(root.rglob('*.lock')):
            if lock not in held:
                stack.enter_context(exclusive_lock(lock))
        from daily_agent.durable_recovery import retired_evidence, active_state_record
        retired = retired_evidence(root)
        for path in sorted(root.rglob('*.json')):
            if str(path) in retired:continue
            # The write-ahead intent is precisely what this snapshot must save.
            if path.name == '.daily-agent-boundary.json':continue
            value=read_json(path)
            if active_state_record(path,value):
                raise StateCorrupt('Active revision/controller cannot be snapshotted; settle or reconcile ownership first')
        files=_files(root);sizes=sum(path.stat().st_size for path in files)
        if sizes>MAX_BYTES:raise ValueError('State archive exceeds supported 2 GiB bound')
        manifest={'schema_version':1,'profile':'cloud-public-chatgpt-v1','created_at':datetime.now(timezone.utc).isoformat(),'source_root':str(root),'files':{}}
        temporary=output.with_name(output.name+'.temporary')
        try:
            with zipfile.ZipFile(temporary,'w',zipfile.ZIP_DEFLATED) as archive:
                for path in files:
                    relative=str(path.relative_to(root));content=path.read_bytes()
                    manifest['files'][relative]={'size':len(content),'sha256':digest(content)}
                    archive.writestr(relative,content)
                archive.writestr('STATE-MANIFEST.json',json.dumps(manifest,ensure_ascii=False,indent=2))
            if [str(p.relative_to(root)) for p in _files(root)]!=list(manifest['files']):
                raise StateCorrupt('State changed while taking snapshot')
            for relative,metadata in manifest['files'].items():
                if digest((root/relative).read_bytes())!=metadata['sha256']:
                    raise StateCorrupt('State changed while taking snapshot')
            os.replace(temporary,output)
        finally:
            temporary.unlink(missing_ok=True)
    return {'archive':str(output),'size':output.stat().st_size,'sha256':digest(output.read_bytes()),'file_count':len(manifest['files'])}

def verify(archive):
    with zipfile.ZipFile(archive) as source:
        infos=source.infolist();names=[v.filename for v in infos]
        if len(names)!=len(set(names)) or sum(v.file_size for v in infos)>MAX_BYTES:raise StateCorrupt('Invalid/oversize state archive')
        try:manifest=__import__('daily_agent.durable_recovery',fromlist=['decode']).decode(source.read('STATE-MANIFEST.json'))
        except (KeyError,ValueError) as exc:raise StateCorrupt('Missing state manifest') from exc
        if manifest.get('schema_version')!=1 or manifest.get('profile')!='cloud-public-chatgpt-v1' or not isinstance(manifest.get('files'),dict):raise StateCorrupt('Invalid state manifest')
        if set(names)!=set(manifest['files'])|{'STATE-MANIFEST.json'}:raise StateCorrupt('Unexpected archive members')
        member_names=set(manifest['files'])
        for name,metadata in manifest['files'].items():
            path=Path(name)
            if path.is_absolute() or '..' in path.parts or '\\' in name or not name or name.endswith('/') or name!=path.as_posix():
                raise StateCorrupt('Unsafe archive path')
            if any(parent.as_posix() in member_names for parent in path.parents if parent.as_posix()!='.'):
                raise StateCorrupt('Archive file/directory collision')
            info=source.getinfo(name)
            if (info.external_attr>>16)&0o170000==0o120000:raise StateCorrupt('Symlink archive member')
            content=source.read(name)
            if type(metadata.get('size')) is not int or len(content)!=metadata['size'] or digest(content)!=metadata.get('sha256'):
                raise StateCorrupt('State artifact identity mismatch')
        return manifest

def restore(archive,target,expected_sha256):
    archive=Path(archive).resolve();target=Path(target).resolve()
    if digest(archive.read_bytes())!=expected_sha256:raise StateCorrupt('Archive does not match trusted checksum')
    manifest=verify(archive)
    if target.exists():raise FileExistsError('Never overwrite local state; reconcile it before selecting a new restore destination')
    target.parent.mkdir(parents=True,exist_ok=True)
    staging=Path(tempfile.mkdtemp(prefix='.daily-agent-restore-',dir=target.parent))
    try:
        with zipfile.ZipFile(archive) as source:
            for relative in manifest['files']:
                path=staging/relative;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(source.read(relative))
        config=_config(staging)
        generation=read_json(config.state_dir/'cloud-generation.json')
        if generation and generation.get('running'):raise StateCorrupt('Archive retains active-process evidence; operator reconciliation required')
        from daily_agent.durable_recovery import _write_json, FENCE
        _write_json(staging/FENCE, {'schema_version':1, 'status':'blocked',
            'reason':'local_archive_restore', 'receipts':{'local_archive_sha256':expected_sha256},
            'original_state_root':manifest.get('source_root'),
            'requires':['verify_library_source_and_state', 'verify_previous_executor_stopped',
                        'reconcile_claim_owners', 'inspect_unresolved_delivery',
                        'verify_site_archive_owner', 'revalidate_absolute_paths']})
        os.rename(staging,target)
    finally:
        if staging.exists():shutil.rmtree(staging)
    return {'root':str(target),'files':len(manifest['files']),'restored':True,'mutation_blocked':True,'warning':'Accepted/uncertain deliveries retained; verify remote state before any send. Restore evidence caches at original root or revalidate any absolute source paths.', 'original_root':manifest.get('source_root')}

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('action',choices=['snapshot','verify','restore']);p.add_argument('--root');p.add_argument('--archive',required=True);p.add_argument('--sha256');a=p.parse_args()
    value=snapshot(a.root,a.archive) if a.action=='snapshot' else restore(a.archive,a.root,a.sha256) if a.action=='restore' else verify(a.archive)
    print(json.dumps(value,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
