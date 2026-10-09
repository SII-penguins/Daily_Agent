"""Explicit production revisions within the original production root.

No implicit enrollment, budget reset, transport or deployment. Authorization and
remote tool authenticity belong to the caller. Legacy date receipts stay intact.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace, asdict
from datetime import date, datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import time
import uuid

from daily_agent.workflow_state import StateCorrupt, atomic_bytes, atomic_json, exclusive_lock, read_json

EDITION = 'production_revision'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False, separators=(',', ':')).encode()).hexdigest()


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _now():
    return datetime.now(timezone.utc)


def _hash(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def _stamp(value):
    try:
        result = datetime.fromisoformat(value)
        if result.tzinfo is None or result.utcoffset() is None: raise ValueError()
        return result
    except (ValueError, TypeError) as exc:
        raise StateCorrupt('Revision requires an absolute timezone-aware timestamp') from exc


def _config(root):
    from daily_agent.cloud_workflow import _config as original
    config = original(root)
    if config.delivery['cloud'].get('pilot'):
        raise ValueError('A production revision cannot use a pilot profile')
    return config


def issue_dir(root, day, revision_id):
    if not _hash(revision_id): raise ValueError('Invalid production revision ID')
    day = date.fromisoformat(str(day))
    config = _config(root)
    path = config.state_dir / 'production-revisions' / str(day) / revision_id
    if any(p.is_symlink() for p in [path, *path.parents] if p.is_relative_to(config.root)):
        raise StateCorrupt('Revision namespace cannot contain symlinks')
    return path


def _immutable(path, value):
    old = read_json(path)
    if old is not None and old != value:
        raise StateCorrupt('Immutable revision artifact changed')
    if old is None: atomic_json(path, value)


def _parent(root, day, identity):
    from daily_agent.cloud_workflow import read_handoff as legacy
    parent = legacy(root, day)
    if (parent['identity'] != identity or parent['kind'] != 'report'
            or parent['state'] != 'confirmed' or not parent['publication_reconciled']):
        raise StateCorrupt('Revision parent must be the exact confirmed, reconciled production report')
    return parent


def authorize_revision(root, day, *, parent_identity, authorization, policy,
                       revision_number=1, carry_keys=None):
    """Create one explicit authorized revision. Caller owns user-evidence provenance.

    A repeated authorization cannot silently create another edition. A missing
    mutable budget after initialization is corruption, never a fresh allowance.
    """
    config = _config(root)
    if type(revision_number) is not int or revision_number < 1: raise ValueError('Invalid revision number')
    if (not isinstance(authorization, dict) or set(authorization) != {'source', 'text', 'authorized_at'}
            or any(not isinstance(authorization[k], str) or not authorization[k].strip() for k in authorization)):
        raise ValueError('Explicit user authorization source, text and time required')
    _stamp(authorization['authorized_at'])
    parent = _parent(root, day, parent_identity)
    if parent.get('site_binding') is None: raise ValueError('Revision requires an existing verified Site destination')
    expected = {'deadline', 'max_runtime_seconds', 'max_failures', 'max_resumes', 'per_launch_seconds'}
    if not isinstance(policy, dict) or set(policy) != expected: raise ValueError('Complete frozen revision policy required')
    from daily_agent.cloud_workflow import _validate_budget
    started = _now().isoformat()
    seed = {'schema_version':1, 'date':str(day), 'issue_started_at':started,
            'runtime_seconds':0., 'failures':0, 'resumes':0,
            **{k:policy[k] for k in expected if k != 'per_launch_seconds'}}
    _validate_budget(seed, day)
    if (type(policy['per_launch_seconds']) not in (int, float)
            or not math.isfinite(policy['per_launch_seconds']) or not 0 < policy['per_launch_seconds'] <= 18000):
        raise ValueError('Revision launch runtime must be bounded')
    rows = parent['approval']
    chosen = set(carry_keys if carry_keys is not None else [r['key'] for r in rows])
    if not chosen <= {r['key'] for r in rows}: raise ValueError('Carry-forward is limited to the exact parent approval')
    from daily_agent.paper_document import version_identity
    from daily_agent.scheduling import _approved_items
    carry = [{'key':item.key, 'version':version_identity(item.material), 'approval_sha256':digest(row), 'row':row}
             for row, item in zip(rows, _approved_items(deepcopy(rows))) if item.key in chosen]
    request = {'edition_type':EDITION, 'date':str(day), 'revision_number':revision_number,
               'parent_identity':parent_identity, 'authorization':deepcopy(authorization),
               'policy':deepcopy(policy), 'carry_forward':carry,
               'conversation':parent['conversation'],
               'site_project_id':parent['site_binding']['content']['project_id']}
    # Stable across retries and independent of initialization wall-clock.
    revision_id = digest(request)
    folder = issue_dir(root, day, revision_id)
    with exclusive_lock(config.state_dir/'cloud-dispatch.lock'):
        siblings = folder.parent
        for p in sorted(siblings.glob('*/contract.json')):
            prior = read_contract(root, day, p.parent.name)
            if prior['revision_number'] == revision_number and prior['revision_id'] != revision_id:
                raise ValueError('Revision number already authorized with a different contract')
        if folder.exists():
            value = read_contract(root, day, revision_id)
            _read_budget(root, day, revision_id)
            return value
        if _stamp(policy['deadline']) <= _now():
            raise ValueError('A new revision deadline must be future')
        from daily_agent.storage import load_material_library, select_library_candidates
        from daily_agent.incremental_issue import source_version
        selection = config.sources.get('selection', {})
        batch_size, max_batches = selection.get('editorial_batch_size', 8), selection.get('max_review_batches', 6)
        if type(batch_size) is not int or not 1 <= batch_size <= 8 or type(max_batches) is not int or not 1 <= max_batches <= 6:
            raise ValueError('Revision needs a bounded original batch plan (at most 6 x 8)')
        from daily_agent.scoring.topics import balanced_quantum_order
        from daily_agent.editorial import annotate_topic_relevance, passes_topic_gate
        remaining = select_library_candidates(config, load_material_library(config), day)
        candidates, deferred = [], []
        for record in remaining:
            reason = budget_deferral(config, record)
            if reason is not None:
                deferred.append(reason)
                continue
            annotate_topic_relevance(record, config)
            if passes_topic_gate(record, config): candidates.append(record)
        candidates = balanced_quantum_order(candidates, config)
        # Match ordinary topic ordering without README/network enrichment during
        # authorization. Preserve room for the configured repository target.
        limit = batch_size*max_batches
        repos = [r for r in candidates if r.item_type == 'repo'][:min(limit,config.quota.get('github_target',2))]
        papers = [r for r in candidates if r.item_type == 'paper'][:limit-len(repos)]
        kept = {r.key for r in papers+repos}
        ordered = [r for r in candidates if r.key in kept]
        ordered.extend(r for r in candidates if r.key not in kept)
        candidates = ordered[:limit]
        contract = {**request, 'revision_id':revision_id, 'created_at':started,
                    'generation_settings':{'sources':deepcopy(config.sources), 'quota':deepcopy(config.quota),
                                           'domains':[asdict(d) for d in config.domains]},
                    'source_version':source_version(), 'batch_size':batch_size, 'max_batches':max_batches,
                    'candidates':[r.to_dict() for r in candidates[:batch_size*max_batches]],
                    'budget_deferred':deferred,
                    'parent_approval_sha256':parent['approval_sha256']}
        folder.mkdir(parents=True)
        atomic_json(folder/'contract.json', {'payload':contract, 'sha256':digest(contract)})
        atomic_json(folder/'budget.json', {**seed, 'revision_id':revision_id, 'contract_sha256':digest(contract)})
        _immutable(folder/'initialized.json', {'contract_sha256':digest(contract)})
        return read_contract(root, day, revision_id)


def read_contract(root, day, revision_id):
    folder = issue_dir(root, day, revision_id)
    envelope = read_json(folder/'contract.json')
    if (not isinstance(envelope, dict) or set(envelope) != {'payload', 'sha256'}
            or digest(envelope['payload']) != envelope['sha256']):
        raise StateCorrupt('Invalid revision contract')
    c = envelope['payload']
    required = ('edition_type','date','revision_number','parent_identity','authorization','policy',
                'carry_forward','conversation','site_project_id')
    if (c.get('date') != str(day) or c.get('revision_id') != revision_id
            or c.get('edition_type') != EDITION
            or digest(c['migration_identity'] if c.get('migration_identity') is not None else {k:c[k] for k in required}) != revision_id):
        raise StateCorrupt('Revision contract identity mismatch')
    if c.get('migration_identity') is not None:
        snapshot=_read_migration_snapshot(issue_dir(root,day,c['replacement_of']))
        if c!=_replacement_contract(snapshot): raise StateCorrupt('Replacement differs from immutable migration authority')
        if read_json(folder/'migration-complete.json')!={'snapshot_sha256':digest(snapshot),'revision_id':revision_id}:
            raise StateCorrupt('Incomplete replacement cannot receive a budget')
    parent = _parent(root, day, c['parent_identity'])
    if (parent['approval_sha256'] != c['parent_approval_sha256'] or c['conversation'] != parent['conversation']
            or parent['site_binding']['content']['project_id'] != c['site_project_id']):
        raise StateCorrupt('Revision parent/destination changed')
    from daily_agent.paper_document import version_identity
    from daily_agent.scheduling import _approved_items
    parent_rows = {r['key']:r for r in parent['approval']}
    keys = set()
    for carried in c['carry_forward']:
        row = carried['row']
        if (row != parent_rows.get(carried['key']) or digest(row) != carried['approval_sha256']
                or version_identity(_approved_items([deepcopy(row)])[0].material) != carried['version']
                or carried['key'] in keys):
            raise StateCorrupt('Carry-forward does not match exact parent evidence/version')
        keys.add(carried['key'])
    if read_json(folder/'initialized.json') != {'contract_sha256':digest(c)}:
        raise StateCorrupt('Incomplete revision initialization; budget cannot be recreated')
    return c


def _read_budget(root, day, revision_id):
    c = read_contract(root, day, revision_id)
    b = read_json(issue_dir(root,day,revision_id)/'budget.json')
    from daily_agent.cloud_workflow import _validate_budget
    _validate_budget(b, day)
    if (b.get('revision_id') != revision_id or b.get('contract_sha256') != digest(c)
            or any(b.get(k) != v for k,v in c['policy'].items() if k != 'per_launch_seconds')):
        raise StateCorrupt('Revision budget contract changed')
    if c.get('migration_identity') is not None:
        snapshot=_read_migration_snapshot(issue_dir(root,day,c['replacement_of']))
        initial=_replacement_budget(snapshot,c)
        if (any(type(b.get(field)) not in (int,float) or b[field]<initial[field]
                for field in ('runtime_seconds','failures','resumes','forfeited_seconds'))
                or b.get('migration_snapshot_sha256')!=initial['migration_snapshot_sha256']
                or b.get('migration_accounting')!=initial['migration_accounting']):
            raise StateCorrupt('Replacement cannot reset inherited runtime or attempts')
    return b


def reserve_generation(root, day, revision_id, timeout=900):
    """Reserve a launch under the original issue lock; no subprocess is started."""
    folder = issue_dir(root,day,revision_id)
    with exclusive_lock(folder/'generation.lock'):
        if (folder/'handoff.json').exists() or (folder/'ready.json').exists():
            raise ValueError('Sealed revision cannot regenerate')
        _assert_not_retired(folder)
        c = read_contract(root,day,revision_id); b = _read_budget(root,day,revision_id)
        if b.get('active_attempt_id'): raise StateCorrupt('Unreconciled revision generation; inspect retained child before recovery')
        if type(timeout) not in (int,float) or not math.isfinite(timeout) or timeout <= 0: raise ValueError('Invalid timeout')
        remaining = min((_stamp(b['deadline'])-_now()).total_seconds(), b['max_runtime_seconds']-b['runtime_seconds'])
        if remaining <= 0 or b['failures'] >= b['max_failures'] or b['resumes'] >= b['max_resumes']:
            raise ValueError('Revision deadline or budget exhausted')
        permitted = min(timeout, c['policy']['per_launch_seconds'], remaining)
        from daily_agent.workflow_runtime import process_namespace
        namespace=process_namespace()
        b.update(resumes=b['resumes']+1, active_namespace=namespace, active_attempt_id=uuid.uuid4().hex,
                 active_started_at=_now().isoformat(), active_timeout_seconds=permitted)
        atomic_json(folder/'budget.json', b)
        atomic_json(folder/'generation.json',{'revision_id':revision_id,'attempt_id':b['active_attempt_id'],
            'namespace':namespace,'running':True,'child_identity':None,'started_at':b['active_started_at']})
        return {'attempt_id':b['active_attempt_id'], 'namespace':namespace, 'timeout_seconds':permitted, 'deadline':b['deadline']}


def _assert_not_retired(folder):
    if (folder/'migration-snapshot.json').exists() or (folder/'retired.json').exists():
        raise StateCorrupt('Revision permanently migration-owned; no further attempt or mutation allowed')


def _owned_attempt(root,day,revision_id,attempt_id,namespace,*,allow_settled=False):
    from daily_agent.workflow_runtime import process_namespace
    folder=issue_dir(root,day,revision_id);_assert_not_retired(folder)
    if not _hash(namespace) or process_namespace()!=namespace or not attempt_id:
        raise StateCorrupt('Revision callback namespace/attempt mismatch')
    b=_read_budget(root,day,revision_id);state=read_json(folder/'generation.json')
    active=b.get('active_attempt_id')==attempt_id and b.get('active_namespace')==namespace
    settled=(allow_settled and not b.get('active_attempt_id') and b.get('last_attempt_id')==attempt_id
             and b.get('last_namespace')==namespace)
    if (not (active or settled) or not isinstance(state,dict) or state.get('revision_id')!=revision_id
            or state.get('attempt_id')!=attempt_id or state.get('namespace')!=namespace):
        raise StateCorrupt('Stale revision callback rejected by attempt CAS')
    return b,state


def _generation_cas(root,day,revision_id,attempt_id,namespace,updates):
    folder=issue_dir(root,day,revision_id)
    with exclusive_lock(folder/'generation.lock'):
        b,state=_owned_attempt(root,day,revision_id,attempt_id,namespace)
        if state.get('running') is not True: raise StateCorrupt('Revision callback targets a finished attempt')
        if 'child_identity' in updates and state.get('child_identity') is not None:
            prior=state['child_identity'];new=updates['child_identity']
            if (prior.get('pid'),prior.get('pgid'))!=(new.get('pid'),new.get('pgid')):
                raise StateCorrupt('Revision started callback changed child identity')
        state.update(updates);atomic_json(folder/'generation.json',state)
        return state


def settle_generation(root, day, revision_id, attempt_id, returncode, elapsed_seconds, *, expected_namespace=None):
    folder = issue_dir(root,day,revision_id)
    with exclusive_lock(folder/'generation.lock'):
        return _settle_generation_unlocked(root,day,revision_id,attempt_id,returncode,elapsed_seconds,
                                           expected_namespace=expected_namespace)


def _settle_generation_unlocked(root,day,revision_id,attempt_id,returncode,elapsed_seconds,*,expected_namespace):
    folder=issue_dir(root,day,revision_id)
    b,state=_owned_attempt(root,day,revision_id,attempt_id,expected_namespace,allow_settled=True)
    if b.get('last_attempt_id') == attempt_id and not b.get('active_attempt_id'):
        if b.get('last_returncode')!=returncode: raise StateCorrupt('Conflicting revision settlement')
    else:
        if type(elapsed_seconds) not in (int,float) or not math.isfinite(elapsed_seconds) or elapsed_seconds < 0:
            raise ValueError('Measured runtime required')
        measured = max(elapsed_seconds, (_now()-_stamp(b['active_started_at'])).total_seconds(), 0.)
        b.update(runtime_seconds=b['runtime_seconds']+measured, failures=b['failures']+int(returncode not in (0,75)),
                 last_attempt_id=attempt_id, last_returncode=returncode,last_namespace=expected_namespace)
        for k in ('active_attempt_id','active_started_at','active_timeout_seconds','active_namespace'): b.pop(k,None)
        atomic_json(folder/'budget.json',b)
    state.update(running=False,finished_at=_now().isoformat(),returncode=returncode)
    atomic_json(folder/'generation.json',state)
    return b



def scoped_config(root, day, revision_id):
    """Shared canonical pool/cache; only report/editorial/state projections scoped."""
    config = _config(root); c = read_contract(root,day,revision_id)
    _assert_not_retired(issue_dir(root,day,revision_id))
    from daily_agent.incremental_issue import source_version
    if (c['generation_settings'] != {'sources':config.sources, 'quota':config.quota,
                                     'domains':[asdict(d) for d in config.domains]}
            or c['source_version'] != source_version()):
        raise StateCorrupt('Revision source/generation settings changed; original budgets retained')
    relative = issue_dir(root,day,revision_id).relative_to(config.root).as_posix()
    delivery = deepcopy(config.delivery)
    delivery['cloud']['production_revision_id'] = revision_id
    delivery['report'].update(output_dir=relative+'/reports', selected_dir=relative+'/selected',
                              logs_dir=relative+'/logs', state_dir=relative+'/state')
    return replace(config, delivery=delivery)


def carry_forward_rows(root, day, revision_id):
    return [deepcopy(r['row']) for r in read_contract(root,day,revision_id)['carry_forward']]


def iter_handoffs(root):
    from daily_agent.config import load_config
    config = load_config(root)
    paths = sorted((config.state_dir/'production-revisions').glob('*/*/handoff.json'))
    if not paths:
        return
    _config(root)  # Any actual revision in a pilot/unconfigured root is corrupt.
    for p in paths:
        yield read_handoff(root,date.fromisoformat(p.parent.parent.name),p.parent.name)


def reservation_keys(root, *, exclude_revision_id=None):
    from daily_agent.scoring.dedup import _identity_keys
    from daily_agent.scheduling import _approved_items
    keys = set()
    for m in iter_handoffs(root):
        if m['revision_id'] == exclude_revision_id: continue
        if m['state'] in {'sending','uncertain','accepted'} or (m['state']=='confirmed' and not m['publication_reconciled']):
            if not m.get('attempt_id') or (m['state'] in {'accepted','confirmed'} and not m.get('message_id')):
                raise StateCorrupt('Unresolved revision lacks an attempt/message identity')
            for item in _approved_items(deepcopy(m['approval'])):
                keys.update(_identity_keys(item.material.to_digest_item())); keys.add(item.key)
    return keys


def eligible_candidates(root, day, revision_id):
    from daily_agent.storage import load_material_library, select_library_candidates
    config = _config(root)
    # Parent carry-forward is an exact approval exception, never an altered library.
    read_contract(root,day,revision_id)
    return select_library_candidates(config,load_material_library(config),day)


def _check_rows(root, day, revision_id, rows):
    from daily_agent.cloud_workflow import _qualifying_rows, read_handoff as legacy
    from daily_agent.scheduling import _approved_items
    from daily_agent.storage import load_material_library, select_library_candidates
    from daily_agent.scoring.dedup import _identity_keys
    from daily_agent.paper_document import version_identity
    config = _config(root); c = read_contract(root,day,revision_id)
    items = _approved_items(deepcopy(rows))
    if len(items) > max(config.quota.get('max_items',10),len(c['carry_forward'])):
        raise ValueError('Revision approval exceeds its frozen total quota')
    for kind,field,default in [('paper','paper_target',8),('repo','github_target',2)]:
        carry_count=sum(r['row']['item_type']==kind for r in c['carry_forward'])
        if sum(i.item_type==kind for i in items)>max(config.quota.get(field,default),carry_count):
            raise ValueError('Revision approval exceeds its frozen item quota')
    qualified, excluded = _qualifying_rows(deepcopy(rows))
    if not rows or excluded or len(qualified) != len(rows): raise ValueError('Revision requires fully qualified evidence')
    carried = {r['key']:r for r in c['carry_forward']}
    # Selection must exclude the current revision's own reservation, but retain
    # every other edition, including same-day unresolved original receipts.
    check_config = replace(config, delivery=deepcopy(config.delivery))
    check_config.delivery['cloud']['reservation_exclude_revision_id'] = revision_id
    library = load_material_library(config)
    from daily_agent.models import MaterialRecord
    from daily_agent.material_pool import same_source_version
    frozen = {r['key']:MaterialRecord.from_dict(r) for r in c['candidates']}
    for item in items:
        if item.key not in carried:
            stored = library.get(item.key)
            if (stored is None or item.key not in frozen
                    or not same_source_version(item.material, stored)
                    or not same_source_version(item.material, frozen[item.key])):
                raise ValueError('Revision item is outside the frozen/current source version')
    eligible = {r.key for r in select_library_candidates(check_config,library,day)}
    reserved = reservation_keys(root,exclude_revision_id=revision_id)
    for p in sorted((config.state_dir/'cloud-delivery').glob('*.json')):
        m = legacy(root,date.fromisoformat(p.stem))
        if m['state'] in {'sending','uncertain','accepted'} or (m['state']=='confirmed' and not m['publication_reconciled']):
            for item in _approved_items(deepcopy(m['approval'])):
                reserved.update(_identity_keys(item.material.to_digest_item())); reserved.add(item.key)
    seen = set()
    for row,item in zip(rows,items):
        aliases = {item.key} | set(_identity_keys(item.material.to_digest_item()))
        if aliases & (seen | reserved): raise ValueError('Revision item is duplicate or reserved by another issue')
        seen.update(aliases)
        if item.key in carried:
            allowed = carried[item.key]
            if digest(row) != allowed['approval_sha256'] or version_identity(item.material) != allowed['version']:
                raise ValueError('Carry-forward changed parent approval/version')
        elif item.key not in eligible:
            raise ValueError('Revision item is stale against publication history')
        if item.item_type == 'paper':
            from daily_agent.deferred_review_cache import _assets
            if _assets(config,item.material,save=False) is None:
                raise ValueError('Revision evidence assets failed integrity verification')
    return items


def seal_ready(root, day, revision_id, rows):
    """Seal a trusted worker's completed approval, independently rechecked here."""
    _assert_not_retired(issue_dir(root,day,revision_id))
    folder=issue_dir(root,day,revision_id)
    with exclusive_lock(folder/'handoff.lock'), exclusive_lock(_config(root).state_dir/'pipeline.lock'):
        if (folder/'handoff.json').exists(): raise ValueError('Delivery-owned revision cannot reseal')
        return _seal_ready_unlocked(root,day,revision_id,rows)


def _seal_ready_unlocked(root,day,revision_id,rows):
    scoped_config(root,day,revision_id)  # New seals bind frozen source/settings.
    _check_rows(root,day,revision_id,rows)
    c=read_contract(root,day,revision_id)
    payload={'schema_version':1,'date':str(day),'revision_id':revision_id,
             'contract_sha256':digest(c),'approval':deepcopy(rows),'approval_sha256':digest(rows)}
    _immutable(issue_dir(root,day,revision_id)/'ready.json',{'payload':payload,'sha256':digest(payload)})
    return payload


def _ready(root,day,revision_id):
    folder=issue_dir(root,day,revision_id); c=read_contract(root,day,revision_id)
    e=read_json(folder/'ready.json')
    if (not isinstance(e,dict) or set(e)!={'payload','sha256'} or digest(e['payload'])!=e['sha256']):
        raise StateCorrupt('Missing or invalid revision ready seal')
    p=e['payload']
    if (p.get('date')!=str(day) or p.get('revision_id')!=revision_id or p.get('contract_sha256')!=digest(c)
            or digest(p.get('approval'))!=p.get('approval_sha256')):
        raise StateCorrupt('Revision ready identity mismatch')
    return p


def _identity(m):
    from daily_agent.cloud_workflow import _html_identity
    return digest({'content':_html_identity(m), **{k:m.get(k) for k in
        ('edition_type','revision_id','revision_number','parent_identity','contract_sha256')}})


def prepare_handoff(root,day,revision_id):
    _assert_not_retired(issue_dir(root,day,revision_id))
    from daily_agent.cloud_workflow import PROFILE
    from daily_agent.rendering.editorial import render_editorial_html
    folder=issue_dir(root,day,revision_id)
    with exclusive_lock(folder/'handoff.lock'):
        if (folder/'handoff.json').exists(): return read_handoff(root,day,revision_id)
        scoped_config(root,day,revision_id)
        c=read_contract(root,day,revision_id); ready=_ready(root,day,revision_id)
        rows=ready['approval']; items=_check_rows(root,day,revision_id,rows)
        papers=sum(i.item_type=='paper' for i in items)
        body=f'Daily Agent 日报 · {day} · 修订版 r{c["revision_number"]}\n{papers} 篇已核验论文 · {len(items)-papers} 个开源项目\n公共来源版（非全源）；仅收录通过证据审核的内容\n'
        approval_sha=_sha(json.dumps(rows,sort_keys=True,ensure_ascii=False).encode())
        content=render_editorial_html(items,day,kind='report',coverage=body,excluded_count=0,
                    approval_sha256=approval_sha,body_sha256=_sha(body.encode()),identity=digest(ready)).encode()
        relative=folder.relative_to(_config(root).root).as_posix()+'/artifacts/'
        artifacts=[]
        for data,suffix,media in ((body.encode(),'txt','text/plain'),(content,'html','text/html')):
            path=relative+_sha(data)+'.'+suffix
            if (_config(root).root/path).exists() and (_config(root).root/path).read_bytes()!=data: raise StateCorrupt('Revision artifact changed')
            atomic_bytes(_config(root).root/path,data)
            artifacts.append({'path':path,'sha256':_sha(data),'size':len(data),'media_type':media})
        m={'schema_version':3,'profile':PROFILE,'date':str(day),'kind':'report','channel':'chatgpt',
           'conversation':c['conversation'],'edition_type':EDITION,'revision_id':revision_id,
           'revision_number':c['revision_number'],'parent_identity':c['parent_identity'],'contract_sha256':digest(c),
           'state':'prepared','body_sha256':artifacts[0]['sha256'],'approval_sha256':approval_sha,
           'report':artifacts[0]['path'],'html_report':artifacts[1]['path'],'artifacts':artifacts,
           'approval':rows,'excluded':[],'created_at':_now().isoformat(),'source_sha256':digest(ready),
           'delivery_format':'html','library_binding':None,'publication_reconciled':False}
        m['identity']=_identity(m); atomic_json(folder/'handoff.json',m)
        return read_handoff(root,day,revision_id)


def read_handoff(root,day,revision_id):
    from daily_agent.cloud_workflow import _safe_relative, PROFILE
    folder=issue_dir(root,day,revision_id); c=read_contract(root,day,revision_id)
    m=read_json(folder/'handoff.json')
    if (not isinstance(m,dict) or m.get('schema_version')!=3 or m.get('profile')!=PROFILE
            or m.get('edition_type')!=EDITION or m.get('revision_id')!=revision_id or m.get('date')!=str(day)
            or m.get('identity')!=_identity(m) or m.get('contract_sha256')!=digest(c)
            or m.get('parent_identity')!=c['parent_identity'] or m.get('revision_number')!=c['revision_number']
            or m.get('conversation')!=c['conversation'] or m.get('kind')!='report' or m.get('channel')!='chatgpt'
            or m.get('state') not in {'prepared','sending','uncertain','accepted','confirmed'}
            or type(m.get('publication_reconciled')) is not bool
            or m['publication_reconciled'] and m['state']!='confirmed'):
        raise StateCorrupt('Invalid production revision handoff')
    ready=_ready(root,day,revision_id)
    if (m.get('source_sha256')!=digest(ready) or m.get('approval')!=ready['approval']
            or m.get('approval_sha256')!=_sha(json.dumps(m['approval'],sort_keys=True,ensure_ascii=False).encode())):
        raise StateCorrupt('Revision approval/source seal mismatch')
    if not isinstance(m.get('artifacts'),list) or len(m['artifacts'])!=2: raise StateCorrupt('Invalid revision artifacts')
    for i,a in enumerate(m['artifacts']):
        expected=m['report'] if i==0 else m['html_report']
        data=_safe_relative(_config(root).root,a['path']).read_bytes()
        if (a['path']!=expected or a.get('media_type')!=('text/plain' if i==0 else 'text/html')
                or a.get('sha256')!=_sha(data) or type(a.get('size')) is not int or a['size']!=len(data)):
            raise StateCorrupt('Revision artifact integrity mismatch')
    body=_safe_relative(_config(root).root,m['report']).read_bytes()
    if _sha(body)!=m['body_sha256'] or m.get('library_binding') is not None: raise StateCorrupt('Invalid revision caption/transport')
    if m.get('site_binding') is not None:
        from daily_agent.cloud_site_delivery import validate_site_binding
        body=validate_site_binding(_config(root).root,m).encode()
        if m['site_binding']['content']['project_id']!=c['site_project_id']: raise StateCorrupt('Revision Site project changed')
    elif m['state']!='prepared' or 'dispatch_body_sha256' in m:
        raise StateCorrupt('Revision delivery has no verified Site binding')
    return {**m,'body':body.decode(),'library_file_ids':[]}


def _stored(m):
    return {k:v for k,v in m.items() if k not in ('body','library_file_ids')}


def bind_site(root,day,revision_id,*,archive_dir,deployment_file,site_version_file,site_repo):
    _assert_not_retired(issue_dir(root,day,revision_id))
    from daily_agent.cloud_site_delivery import (_deployment,_version,_source_proof,_content_binding,_caption,validate_site_binding)
    from daily_agent.cloud_workflow import _binding_identity
    from daily_agent.report_archive import verify_archive
    config=_config(root); folder=issue_dir(root,day,revision_id)
    with exclusive_lock(config.state_dir/'cloud-dispatch.lock'),exclusive_lock(folder/'handoff.lock'):
        m=_stored(read_handoff(root,day,revision_id))
        if m['state']!='prepared': raise ValueError('Revision Site binding requires a prepared handoff')
        archive=verify_archive(archive_dir); archive_bytes=(Path(archive_dir)/'manifest.json').read_bytes()
        db=Path(deployment_file).read_bytes(); dep=_deployment(json.loads(db))
        vb=Path(site_version_file).read_bytes(); ver=_version(json.loads(vb),dep)
        if dep['project_id']!=read_contract(root,day,revision_id)['site_project_id']: raise ValueError('Revision must use the authorized Site project')
        proof=_source_proof(site_repo,ver,dep,archive_bytes,archive)
        content=_content_binding(m,dep,archive); content['source_commit_sha']=ver['source']['commit_sha']
        caption=_caption(m,content).encode()
        payloads=[('deployment',db),('site_version',vb),('archive_manifest',archive_bytes),
                  ('source_proof',json.dumps(proof,sort_keys=True).encode()),('dispatch_caption',caption)]
        evidence={}
        for name,data in payloads:
            relative=(folder.relative_to(config.root)/'artifacts'/f'site-{name}-{_sha(data)}.evidence').as_posix()
            evidence[name]={'path':relative,'sha256':_sha(data),'size':len(data)}
        binding={'content':content,'evidence':evidence}
        if m.get('site_binding') is not None and m['site_binding']!=binding: raise ValueError('Revision Site binding is immutable')
        for name,data in payloads: atomic_bytes(config.root/evidence[name]['path'],data)
        m.update(site_binding=binding,delivery_identity=_binding_identity(m,binding),dispatch_body_sha256=_sha(caption))
        validate_site_binding(config.root,m); atomic_json(folder/'handoff.json',m)
    return read_handoff(root,day,revision_id)


def record_transition(root,day,revision_id,event,*,attempt_id=None,message_id=None,conversation=None,body=None):
    _assert_not_retired(issue_dir(root,day,revision_id))
    config=_config(root); folder=issue_dir(root,day,revision_id)
    with exclusive_lock(config.state_dir/'cloud-dispatch.lock'),exclusive_lock(folder/'handoff.lock'):
        m=_stored(read_handoff(root,day,revision_id)); state=m['state']
        if event=='begin':
            if state!='prepared' or not m.get('site_binding'): raise ValueError('Revision resend/unbound send blocked')
            with exclusive_lock(config.state_dir/'pipeline.lock'):
                _check_rows(root,day,revision_id,m['approval'])
            m.update(state='sending',attempt_id=uuid.uuid4().hex,attempted_at=_now().isoformat())
        else:
            if not attempt_id or attempt_id!=m.get('attempt_id'): raise ValueError('Revision attempt identity mismatch')
            if event=='uncertain':
                if state!='sending': raise ValueError('Only sending can become uncertain')
                m['state']='uncertain'
            elif event=='accepted':
                if state=='accepted' and message_id==m.get('message_id'): return read_handoff(root,day,revision_id)
                if state not in {'sending','uncertain'} or not message_id: raise ValueError('Tool message acceptance required')
                m.update(state='accepted',message_id=message_id,accepted_at=_now().isoformat())
            elif event=='confirmed':
                if state!='accepted' or message_id!=m.get('message_id'): raise ValueError('Matching accepted message required')
                if conversation!=m['conversation'] or body is None or _sha(body.encode())!=m['dispatch_body_sha256']:
                    raise ValueError('Revision readback destination/body mismatch')
                m.update(state='confirmed',confirmed_at=_now().isoformat(),verified_site_delivery=m['delivery_identity'])
            else: raise ValueError('Unknown revision transition')
        atomic_json(folder/'handoff.json',m)
    if m['state']=='confirmed': reconcile_publication(root,day,revision_id)
    return read_handoff(root,day,revision_id)


def reconcile_publication(root,day,revision_id):
    from daily_agent.scheduling import _approved_items
    from daily_agent.storage import mark_materials_published, write_revision_publication
    config=_config(root); folder=issue_dir(root,day,revision_id)
    with exclusive_lock(folder/'handoff.lock'):
        m=_stored(read_handoff(root,day,revision_id))
        if m['state']!='confirmed': raise ValueError('Revision remote readback required before publication')
        if m['publication_reconciled']: return
        with exclusive_lock(config.state_dir/'pipeline.lock'):
            items=_approved_items(deepcopy(m['approval']))
            # The durable receipt retains reservations through any interrupted projection.
            write_revision_publication(config,m)
            carried={r['key'] for r in read_contract(root,day,revision_id)['carry_forward']}
            # Re-sending parent rows is not authority to roll its shared evidence
            # back to the old source version. Its publication was reconciled.
            mark_materials_published(config,[i.material for i in items if i.key not in carried],day)
        m.update(publication_reconciled=True,reconciled_at=_now().isoformat())
        atomic_json(folder/'handoff.json',m)



def budget_deferral(config, record):
    """Issue-only feasibility, not a scientific rejection or quota deferral.

    Reviewed cache hits use their full independent/asset validation. A repaired
    effective document is judged by its own chunks, never its native backup.
    """
    if record.item_type != 'paper': return None
    document=record.paper_document or {}
    chunks=document.get('chunks')
    limit=config.sources.get('reading',{}).get('max_chunks_per_paper',80)
    if not isinstance(chunks,list) or not chunks or type(limit) is not int or len(chunks)<=limit:
        return None
    from daily_agent.deferred_review_cache import _load
    if _load(config,record) is not None: return None
    from daily_agent.paper_document import version_identity
    return {'key':record.key,'version':version_identity(record),'status':'budget_deferred',
            'reason':'parsed_document_exceeds_frozen_chunk_budget','chunk_count':len(chunks),
            'max_chunks_per_paper':limit,'scope':'this_revision_only'}


def assert_active_generation(root,day,revision_id,*,expected_attempt=None,expected_namespace=None):
    """Only the exact supervised child can spend a reserved launch window."""
    import os
    folder=issue_dir(root,day,revision_id)
    b,_=_owned_attempt(root,day,revision_id,expected_attempt,expected_namespace)
    if (not b.get('active_attempt_id') or not b.get('active_started_at')
            or _now()>=_stamp(b['deadline']) or b['runtime_seconds']>=b['max_runtime_seconds']
            or (_now()-_stamp(b['active_started_at'])).total_seconds()>=b.get('active_timeout_seconds',0)):
        raise StateCorrupt('Revision worker requires an unexpired supervisor attempt')
    until=time.monotonic()+1.
    while True:
        state=read_json(folder/'generation.json')
        if (not isinstance(state,dict) or state.get('running') is not True
                or state.get('revision_id')!=revision_id or state.get('attempt_id')!=expected_attempt
                or state.get('namespace')!=expected_namespace):
            raise StateCorrupt('Revision worker is not bound to the active supervisor')
        identity=state.get('child_identity')
        if identity is not None:
            if (not isinstance(identity,dict) or identity.get('pid')!=os.getpid()
                    or identity.get('pgid')!=os.getpgid(0)):
                raise StateCorrupt('Only the recorded revision child may execute')
            return
        if time.monotonic()>=until: raise StateCorrupt('Revision launch identity not published')
        time.sleep(.01)


def generate(root,day,revision_id,*,expected_attempt=None,expected_namespace=None):
    """Explicit bounded pool-only revision worker; never invoke legacy day pipeline.

    The canonical library and content-validated reading/PDF caches are shared.
    Original candidate/batch identities and enriched inputs survive parent-queue
    resumes. No fresh discovery, default enrollment, send, or publication occurs.
    """
    import os
    from types import SimpleNamespace
    from daily_agent import pipeline
    from daily_agent.models import MaterialRecord
    from daily_agent.scheduling import _approved_items
    from daily_agent.storage import load_material_library, write_material_library
    from daily_agent.editorial import approve_publication
    from daily_agent.cloud_cache import prepare_batch
    from daily_agent.paper_document import version_identity
    from daily_agent.material_pool import same_source_version
    folder=issue_dir(root,day,revision_id); canonical=_config(root)
    before=os.environ.get('DAILY_AGENT_DISABLE_EXTERNAL_SECRETS')
    os.environ['DAILY_AGENT_DISABLE_EXTERNAL_SECRETS']='1'
    try:
        with exclusive_lock(folder/'handoff.lock'),exclusive_lock(canonical.state_dir/'pipeline.lock'):
            if (folder/'handoff.json').exists() or (folder/'ready.json').exists():
                raise ValueError('Sealed revision cannot regenerate')
            c=read_contract(root,day,revision_id); config=scoped_config(root,day,revision_id)
            assert_active_generation(root,day,revision_id,expected_attempt=expected_attempt,expected_namespace=expected_namespace)
            library=load_material_library(canonical)
            carried=_approved_items(carry_forward_rows(root,day,revision_id))
            records,drafts,reviews=[],[],[]
            approved=list(carried); attempted=set()
            plan=SimpleNamespace(folder=folder/'incremental')
            for index in range(c['max_batches']):
                if all(sum(i.item_type==kind for i in approved)>=config.quota.get(field,default)
                       for kind,field,default in [('paper','paper_target',8),('repo','github_target',2)]): break
                path=folder/'batches'/f'{index}.json'
                frozen=read_json(path)
                if frozen is not None:
                    if (set(frozen)!={'payload','sha256'} or digest(frozen['payload'])!=frozen['sha256']
                            or frozen['payload'].get('id')!=f'{c.get("logical_issue_id",revision_id)}:batch:{index}'):
                        raise StateCorrupt('Revision frozen batch identity mismatch')
                    batch=[MaterialRecord.from_dict(r) for r in frozen['payload']['selected']]
                else:
                    eligible={r.key:r for r in eligible_candidates(root,day,revision_id)}
                    pending=[]
                    for row in c['candidates']:
                        candidate=MaterialRecord.from_dict(deepcopy(row)); current=eligible.get(candidate.key)
                        if candidate.key in attempted or current is None: continue
                        if not same_source_version(candidate,current): continue
                        pending.append(candidate)
                    pending=pipeline.build_shortlist(config,{r.key:r for r in pending},day)
                    batch=pipeline._bounded_editorial_batch(config,pending,approved)
                    if not batch: break
                    payload={'id':f'{c.get("logical_issue_id",revision_id)}:batch:{index}','selected':[r.to_dict() for r in batch]}
                    _immutable(path,{'payload':payload,'sha256':digest(payload)})
                attempted.update(r.key for r in batch)
                ep=path.with_name(f'{index}.enriched.json'); enrichment=read_json(ep)
                if enrichment is not None:
                    if (set(enrichment)!={'payload','sha256'} or digest(enrichment['payload'])!=enrichment['sha256']):
                        raise StateCorrupt('Revision enriched input integrity mismatch')
                    enriched=[MaterialRecord.from_dict(r) for r in enrichment['payload']]
                    if [r.key for r in enriched]!=[r.key for r in batch]: raise StateCorrupt('Revision enrichment changed candidate order')
                    batch=enriched
                else:
                    def enrich(values):
                        values=pipeline.enrich_open_access_links(values,config)
                        values=pipeline.enrich_unpaywall_links(values,config)
                        values=pipeline.enrich_paper_texts(values,config)
                        from daily_agent.author_context import enrich_author_contexts
                        context=enrich_author_contexts([r.to_digest_item() for r in values],config)
                        for r,item in zip(values,context):
                            if item.raw.get('research_context'): r.raw['research_context']=item.raw['research_context']
                        return pipeline.enrich_citation_contexts(values,config)
                    batch=prepare_batch(config,day,batch,enrich)
                    if not batch: break
                    payload=[r.to_dict() for r in batch]
                    _immutable(ep,{'payload':payload,'sha256':digest(payload)})
                current_eligible={r.key:r for r in eligible_candidates(root,day,revision_id)}
                eligible_keys={r.key for r in batch if r.key in current_eligible
                               and same_source_version(r,current_eligible[r.key])}
                projection_keys=set(eligible_keys)
                # Previously unparsed candidates may reveal an unfinishable
                # topology only after bounded enrichment. Defer before any
                # author/reading/model admission; retain PDF and pool evidence.
                for record in batch:
                    dp=folder/'budget-deferrals'/(digest(record.key)+'.json')
                    old=read_json(dp)
                    if old is not None:
                        if (not isinstance(old,dict) or set(old)!={'payload','sha256'}
                                or not isinstance(old['payload'],dict) or digest(old['payload'])!=old['sha256']):
                            raise StateCorrupt('Revision budget deferral integrity mismatch')
                        reason=old['payload']
                        if (reason.get('key')!=record.key or reason.get('version')!=version_identity(record)
                                or reason.get('status')!='budget_deferred' or reason.get('scope')!='this_revision_only'
                                or reason.get('reason')!='parsed_document_exceeds_frozen_chunk_budget'
                                or reason.get('max_chunks_per_paper')!=config.sources['reading']['max_chunks_per_paper']
                                or reason.get('chunk_count')!=len(record.paper_document.get('chunks',[]))
                                or reason['chunk_count']<=reason['max_chunks_per_paper']):
                            raise StateCorrupt('Revision budget deferral identity mismatch')
                    else:
                        reason=budget_deferral(config,record)
                    if reason is not None:
                        _immutable(dp,{'payload':reason,'sha256':digest(reason)})
                        eligible_keys.discard(record.key)
                library.update({r.key:r for r in batch if r.key in projection_keys}); write_material_library(config,library)
                output,ds,rs=pipeline._process_incremental_batch(config,day,batch,plan,
                    f'{c.get("logical_issue_id",revision_id)}:batch:{index}',library,records,drafts,reviews,use_llm=True,
                    eligible_keys=eligible_keys)
                records.extend(output);drafts.extend(ds);reviews.extend(rs)
                additions=approve_publication(config,records,drafts,reviews)
                # Carry-forward rows are immutable. New approval takes only the
                # remaining ordinary quota and cannot replace an original row.
                approved=list(carried)
                for item in additions:
                    field,default=('paper_target',8) if item.item_type=='paper' else ('github_target',2)
                    if (len(approved)<config.quota.get('max_items',10)
                            and sum(a.item_type==item.item_type for a in approved)<config.quota.get(field,default)): approved.append(item)
                snapshot={'rows':[i.to_dict() for i in approved], 'completed_batches':index+1,
                          'revision_id':revision_id,'contract_sha256':digest(c)}
                atomic_json(folder/'progress.json',{'payload':snapshot,'sha256':digest(snapshot)})
            # This worker does not change the exact parent rows to add links or
            # presentation metadata. HTML rendering remains the handoff's job.
            return _seal_ready_unlocked(root,day,revision_id,[i.to_dict() for i in approved])
    finally:
        if before is None: os.environ.pop('DAILY_AGENT_DISABLE_EXTERNAL_SECRETS',None)
        else: os.environ['DAILY_AGENT_DISABLE_EXTERNAL_SECRETS']=before



def freeze_completed(root,day,revision_id):
    """Stop admission and seal only committed, revalidated work without models.

    Caller must first settle/verify-stop an active child. Unfinished paper work
    is excluded; a batch-wide pending response cannot hide committed siblings.
    """
    _assert_not_retired(issue_dir(root,day,revision_id))
    from daily_agent.batch_execution import Execution, candidate, existing_review_validator, digest as batch_digest
    from daily_agent.incremental_issue import PROTOCOL
    from daily_agent.models import MaterialRecord, EditorialDraft, EditorialReview
    from daily_agent.editorial import approve_publication
    from daily_agent.scheduling import _approved_items
    canonical=_config(root);folder=issue_dir(root,day,revision_id)
    with exclusive_lock(folder/'handoff.lock'),exclusive_lock(canonical.state_dir/'pipeline.lock'):
        if (folder/'handoff.json').exists(): return _ready(root,day,revision_id)
        if (folder/'ready.json').exists(): return _ready(root,day,revision_id)
        if _read_budget(root,day,revision_id).get('active_attempt_id'):
            raise ValueError('Verify-stop and settle active revision generation before freezing')
        c=read_contract(root,day,revision_id);config=scoped_config(root,day,revision_id)
        carried=_approved_items(carry_forward_rows(root,day,revision_id))
        records,drafts,reviews=[],[],[]
        for index in range(c['max_batches']):
            path=folder/'batches'/f'{index}.enriched.json';e=read_json(path)
            if e is None: continue
            if set(e)!={'payload','sha256'} or digest(e['payload'])!=e['sha256']:
                raise StateCorrupt('Revision frozen enrichment is corrupt')
            batch=[MaterialRecord.from_dict(row) for row in e['payload']]
            batch_id=f'{c.get("logical_issue_id",revision_id)}:batch:{index}'
            ledger=folder/'incremental'/'batch-execution'/batch_digest(batch_id)
            if not ledger.exists(): continue  # Never create a budget just to inspect.
            with Execution(folder/'incremental',batch_id,[candidate(r) for r in batch],config,
                           protocol=PROTOCOL,issue_state='generating') as execution:
                validator=existing_review_validator(config)
                for original in batch:
                    complete=execution.completed(original.key,validator=validator)
                    if complete is None: continue
                    records.append(MaterialRecord.from_dict(complete['record']))
                    drafts.append(EditorialDraft.from_dict(complete['draft']))
                    reviews.append(EditorialReview.from_dict(complete['review']))
        from daily_agent.material_pool import same_source_version
        current={r.key:r for r in eligible_candidates(root,day,revision_id)}
        eligible={r.key for r in records if r.key in current and same_source_version(r,current[r.key])}
        additions=approve_publication(config,[r for r in records if r.key in eligible],drafts,reviews)
        approved=list(carried)
        for item in additions:
            field,default=('paper_target',8) if item.item_type=='paper' else ('github_target',2)
            if (len(approved)<config.quota.get('max_items',10)
                            and sum(a.item_type==item.item_type for a in approved)<config.quota.get(field,default)): approved.append(item)
        return _seal_ready_unlocked(root,day,revision_id,[i.to_dict() for i in approved])


def run_generation(root,day,revision_id,timeout=900):
    """Parent-supervised launch, every callback bound to immutable token+namespace."""
    import sys
    from daily_agent.workflow_runtime import run_process,CleanupPending
    folder=issue_dir(root,day,revision_id)
    attempt=reserve_generation(root,day,revision_id,timeout)
    token,namespace=attempt['attempt_id'],attempt['namespace']
    launched=False
    def started(identity):
        nonlocal launched
        launched=True
        _generation_cas(root,day,revision_id,token,namespace,{'child_identity':identity})
    def tick():
        _generation_cas(root,day,revision_id,token,namespace,{'heartbeat_at':_now().isoformat()})
    start=time.monotonic()
    try:
        rc=run_process([sys.executable,'-m','daily_agent.production_revision','worker',
            '--root',str(_config(root).root),'--date',str(day),'--revision-id',revision_id,
            '--expected-attempt',token,'--expected-namespace',namespace],
            _config(root).root,attempt['timeout_seconds'],on_start=started,on_tick=tick,
            log_prefix=folder/'logs'/'generation')
    except CleanupPending:
        raise  # Never infer no child or refund an uncertain cleanup boundary.
    except BaseException:
        if not launched:
            settle_generation(root,day,revision_id,token,1,time.monotonic()-start,expected_namespace=namespace)
        raise
    settle_generation(root,day,revision_id,token,rc,time.monotonic()-start,expected_namespace=namespace)
    return rc


def recover_generation(root,day,revision_id):
    """Same-namespace verified cleanup; cross-namespace absence proves nothing."""
    from daily_agent.workflow_runtime import stop_verified_orphan,process_namespace
    folder=issue_dir(root,day,revision_id)
    with exclusive_lock(folder/'generation.lock'):
        _assert_not_retired(folder)
        budget=_read_budget(root,day,revision_id)
        if not budget.get('active_attempt_id'): return budget
        namespace=process_namespace()
        b,state=_owned_attempt(root,day,revision_id,budget['active_attempt_id'],namespace)
        if not state.get('running') or not state.get('child_identity'):
            raise StateCorrupt('Revision orphan lacks verifiable cleanup identity; operator review required')
        stop_verified_orphan(state['child_identity'],expected_root=_config(root).root)
        return _settle_generation_unlocked(root,day,revision_id,b['active_attempt_id'],124,
            max(0,(_now()-_stamp(b['active_started_at'])).total_seconds()),expected_namespace=namespace)


def _migration_locks(root,day,revision_id):
    folder=issue_dir(root,day,revision_id);config=_config(root)
    return {'dispatch':config.state_dir/'cloud-dispatch.lock', 'generation':folder/'generation.lock',
            'handoff':folder/'handoff.lock', 'pipeline':config.state_dir/'pipeline.lock',
            'queue':config.root/'data/writer-queue/queue.lock'}


def _lock_evidence(paths):
    import stat
    result={}
    for name,path in paths.items():
        info=path.lstat()
        if not stat.S_ISREG(info.st_mode): raise StateCorrupt('Migration locks must be retained regular files')
        result[name]={'path':str(path),'device':info.st_dev,'inode':info.st_ino}
    return result


def _snapshot_files(folder,*,queue=False):
    import base64,stat
    files={}
    if not folder.exists(): return files
    for path in sorted(folder.rglob('*')):
        info=path.lstat()
        if stat.S_ISDIR(info.st_mode): continue
        if not stat.S_ISREG(info.st_mode): raise StateCorrupt('Migration refuses symlinks/nonregular evidence')
        relative=path.relative_to(folder).as_posix()
        if relative.endswith('.lock'): continue
        if relative in {'migration-snapshot.json','retired.json','migration-complete.json'}: continue
        if queue and not relative.endswith('.json'): continue
        data=path.read_bytes()
        files[relative]={'sha256':_sha(data),'size':len(data),'base64':base64.b64encode(data).decode()}
    return files


def _snapshot_decode(value):
    import base64
    if not isinstance(value,dict) or set(value)!={'sha256','size','base64'}: raise StateCorrupt('Invalid migration file evidence')
    try: data=base64.b64decode(value['base64'],validate=True)
    except (ValueError,TypeError) as exc: raise StateCorrupt('Invalid migration bytes') from exc
    if len(data)!=value['size'] or _sha(data)!=value['sha256']: raise StateCorrupt('Migration bytes/hash mismatch')
    return data


def _evidence_json(evidence,name):
    try: return json.loads(_snapshot_decode(evidence['files'][name]))
    except (KeyError,ValueError,TypeError) as exc: raise StateCorrupt('Missing/invalid migration JSON evidence: '+name) from exc


def migration_evidence(root,day,revision_id):
    """Read-only preflight. The transaction recollects under all five locks."""
    folder=issue_dir(root,day,revision_id);c=read_contract(root,day,revision_id)
    b=_read_budget(root,day,revision_id)
    paths=_migration_locks(root,day,revision_id)
    library=_config(root).root/'data/materials/library.json'
    evidence={'schema_version':1,'root':str(_config(root).root),'date':str(day),'revision_id':revision_id,
              'locks':_lock_evidence(paths),'files':_snapshot_files(folder),
              'queue_files':_snapshot_files(_config(root).root/'data/writer-queue',queue=True),
              'library_sha256':_sha(library.read_bytes()) if library.exists() else None}
    return {'sha256':digest(evidence),'payload':evidence}


def _validate_migration_execution(root,day,old_id,evidence):
    """Read every nonempty execution namespace; unknown/missing state cannot reset."""
    from daily_agent.batch_execution import Execution,candidate,_read as ledger_read,digest as batch_digest,existing_review_validator
    from daily_agent.incremental_issue import PROTOCOL
    from daily_agent.models import MaterialRecord
    from daily_agent.parent_writer import validate_job,_contract as job_contract,digest as queue_digest
    c=_evidence_json(evidence,'contract.json')['payload'];folder=issue_dir(root,day,old_id)
    config=_config(root)
    if c['generation_settings']!={'sources':config.sources,'quota':config.quota,'domains':[asdict(d) for d in config.domains]}:
        raise StateCorrupt('Migration cannot change original generation settings')
    logical=c.get('logical_issue_id',old_id)
    allowed={r['key'] for r in c['candidates']};seen=set();known_ledgers=set()
    for p in sorted((folder/'batches').glob('*.json')):
        if p.name.endswith('.enriched.json'): continue
        if not p.stem.isdecimal() or int(p.stem)>=c['max_batches']: raise StateCorrupt('Unknown frozen batch')
        index=int(p.stem);value=read_json(p)
        if (not isinstance(value,dict) or set(value)!={'payload','sha256'} or digest(value['payload'])!=value['sha256']):
            raise StateCorrupt('Frozen batch corrupt')
        row=value['payload'];batch_id=f'{logical}:batch:{index}'
        keys=[r['key'] for r in row['selected']]
        if (row.get('id')!=batch_id or not keys or len(keys)>c['batch_size'] or len(set(keys))!=len(keys)
                or not set(keys)<=allowed or set(keys)&seen): raise StateCorrupt('Migration candidate/batch identity changed')
        seen.update(keys)
        ep=p.with_name(f'{index}.enriched.json');ledger=folder/'incremental/batch-execution'/batch_digest(batch_id)
        if not ep.exists():
            if ledger.exists(): raise StateCorrupt('Execution exists without frozen enriched input')
            continue
        enriched=read_json(ep)
        if (not isinstance(enriched,dict) or set(enriched)!={'payload','sha256'} or digest(enriched['payload'])!=enriched['sha256']
                or [r['key'] for r in enriched['payload']]!=keys): raise StateCorrupt('Frozen enrichment corrupt')
        if not ledger.exists(): continue
        known_ledgers.add(ledger.name)
        batch=[MaterialRecord.from_dict(r) for r in enriched['payload']]
        ex=Execution(folder/'incremental',batch_id,[candidate(r) for r in batch],config,protocol=PROTOCOL)
        state=ledger_read(ledger/'journal.json');marker=ledger_read(ledger/'initialized.json')
        if state is None or marker is None: raise StateCorrupt('Unknown nonempty execution cannot receive a fresh budget')
        ex._validate(state)
        if state['batch_id']!=batch_id or state['contract']!=ex.contract or marker!={'batch_id':batch_id,'contract_sha256':batch_digest(ex.contract)}:
            raise StateCorrupt('Migration execution contract mismatch')
        ex._state=state
        validator=existing_review_validator(config)
        for key,sha in state['completed'].items():
            if validator(ex._completion(key,sha)) is not True: raise StateCorrupt('Migration completion no longer validates')
        for op in state['operations'].values():
            job_id=op.get('queue_job_id')
            if job_id is None: continue
            ref=evidence['queue_files'].get(job_id+'.job.json')
            if ref is None: raise StateCorrupt('Migration lost a bound queue job')
            job=json.loads(_snapshot_decode(ref));validate_job(config.root,job)
            if queue_digest(job_contract(job))!=job_id or batch_digest(job_contract(job))!=op['input_sha256']:
                raise StateCorrupt('Migration bound job contract changed')
    tree=folder/'incremental/batch-execution'
    if tree.exists() and {p.name for p in tree.iterdir()}!=known_ledgers:
        raise StateCorrupt('Unknown nonempty execution namespace; migration refused')
    for p in (folder/'batches').glob('*.enriched.json'):
        if not p.with_name(p.name.replace('.enriched','')).exists(): raise StateCorrupt('Orphan enriched batch')


def _read_migration_snapshot(folder):
    value=read_json(folder/'migration-snapshot.json')
    if (not isinstance(value,dict) or set(value)!={'payload','sha256'} or digest(value['payload'])!=value['sha256']):
        raise StateCorrupt('Invalid immutable migration snapshot')
    snap=value['payload']
    if digest(snap['evidence'])!=snap['expected_evidence_sha']: raise StateCorrupt('Migration evidence seal mismatch')
    if digest(snap['identity'])!=snap['replacement_id']: raise StateCorrupt('Migration replacement identity mismatch')
    for files in (snap['evidence']['files'],snap['evidence']['queue_files']):
        for name,record in files.items():
            path=Path(name)
            if path.is_absolute() or '..' in path.parts or path.as_posix()!=name: raise StateCorrupt('Unsafe migration path')
            _snapshot_decode(record)
    return snap


def _replacement_contract(snap):
    old=_evidence_json(snap['evidence'],'contract.json')['payload']
    c=deepcopy(old)
    c.update(revision_id=snap['replacement_id'],source_version=snap['target_source_version'],
             logical_issue_id=old.get('logical_issue_id',old['revision_id']),
             migration_identity=snap['identity'],migration_snapshot_sha256=digest(snap),
             replacement_of=old['revision_id'])
    return c


def _replacement_budget(snap,c):
    b=deepcopy(_evidence_json(snap['evidence'],'budget.json'))
    old_attempt=b.pop('active_attempt_id')
    for k in ('active_started_at','active_timeout_seconds','active_namespace'):b.pop(k,None)
    b.update(revision_id=c['revision_id'],contract_sha256=digest(c),
             runtime_seconds=b['runtime_seconds']+snap['forfeited_seconds'],failures=b['failures']+1,
             last_attempt_id=old_attempt,last_returncode=124,
             forfeited_seconds=b.get('forfeited_seconds',0.)+snap['forfeited_seconds'],
             migration_snapshot_sha256=digest(snap),
             migration_accounting={'kind':'conservative_unobserved_exposure','measured':False,
                 'active_attempt_id':old_attempt,'seconds':snap['forfeited_seconds'],'fenced_at':snap['fenced_at']})
    return b


def retire_and_replace(root,day,old_id,*,expected_evidence_sha,authorization,expected_source_version,shared_lock_proof):
    """Explicit transaction fence, never proof the unreachable old process died.

    The caller supplies root-approved evidence and a genuine cross-executor
    shared-lock probe. Old mutable callbacks remain isolated forever. Queue
    bytes are evidence only: never restore shared jobs/answers/claims.
    """
    from daily_agent.parent_writer import queue_lock
    from daily_agent.incremental_issue import source_version
    from daily_agent.workflow_runtime import process_namespace
    from contextlib import ExitStack
    folder=issue_dir(root,day,old_id);paths=_migration_locks(root,day,old_id)
    if (not _hash(expected_evidence_sha) or not _hash(expected_source_version)
            or not isinstance(authorization,dict) or set(authorization)!={'source','text','authorized_at'}
            or any(not isinstance(v,str) or not v.strip() for v in authorization.values())):
        raise ValueError('Explicit migration authorization and expected evidence/source identities required')
    _stamp(authorization['authorized_at'])
    if (not isinstance(shared_lock_proof,dict) or set(shared_lock_proof)!={'source','shared_locking_verified','locks'}
            or not isinstance(shared_lock_proof['source'],str) or not shared_lock_proof['source'].strip()
            or shared_lock_proof['shared_locking_verified'] is not True):
        raise ValueError('Verified shared-lock probe evidence required')
    # Never replace lock files. Checking inode evidence before and inside the
    # transaction binds admission to the actually probed shared filesystem.
    if _lock_evidence(paths)!=shared_lock_proof['locks']: raise StateCorrupt('Shared lock proof does not match retained inodes')
    with ExitStack() as locks:
        for name in ('dispatch','generation','handoff','pipeline'):
            locks.enter_context(exclusive_lock(paths[name]))
        locks.enter_context(queue_lock(paths['queue'],timeout=0,strict_io=True))
        if _lock_evidence(paths)!=shared_lock_proof['locks']: raise StateCorrupt('Migration lock identity changed')
        if (folder/'migration-snapshot.json').exists():
            snap=_read_migration_snapshot(folder)
            if (snap['expected_evidence_sha']!=expected_evidence_sha or snap['authorization']!=authorization
                    or snap['target_source_version']!=expected_source_version or snap['shared_lock_proof']!=shared_lock_proof):
                raise StateCorrupt('Existing immutable migration has different approved inputs')
        else:
            if (folder/'retired.json').exists(): raise StateCorrupt('Retirement without migration snapshot')
            evidence=migration_evidence(root,day,old_id)
            if evidence['sha256']!=expected_evidence_sha: raise StateCorrupt('Migration evidence changed; new progress requires renewed review')
            if source_version()!=expected_source_version: raise StateCorrupt('Migration target source changed')
            old=read_contract(root,day,old_id);b=_read_budget(root,day,old_id);state=read_json(folder/'generation.json')
            if ((folder/'ready.json').exists() or (folder/'handoff.json').exists()
                    or not b.get('active_attempt_id') or not b.get('active_started_at')
                    or not isinstance(state,dict) or state.get('running') is not True
                    or state.get('revision_id')!=old_id or state.get('attempt_id')!=b['active_attempt_id']):
                raise StateCorrupt('Only exact unsealed unresolved revision attempts may migrate')
            reserved=b.get('active_timeout_seconds')
            if type(reserved) not in (int,float) or not math.isfinite(reserved) or reserved<=0:
                raise StateCorrupt('Unknown outstanding revision allowance')
            exposure=(_now()-_stamp(b['active_started_at'])).total_seconds()
            if exposure<reserved: raise ValueError('Original active launch window has not expired')
            if _now()>=_stamp(b['deadline']): raise ValueError('Original revision deadline already expired')
            _validate_migration_execution(root,day,old_id,evidence['payload'])
            fenced_at=_now().isoformat()
            identity={'kind':'production_revision_retirement_v1','old_revision_id':old_id,
                      'expected_evidence_sha':expected_evidence_sha,'authorization':deepcopy(authorization),
                      'target_source_version':expected_source_version}
            snap={'schema_version':1,'evidence':evidence['payload'],'expected_evidence_sha':expected_evidence_sha,
                  'identity':identity,'replacement_id':digest(identity),'authorization':deepcopy(authorization),
                  'target_source_version':expected_source_version,'shared_lock_proof':deepcopy(shared_lock_proof),
                  'fenced_at':fenced_at,'fence_namespace':process_namespace(),
                  'forfeited_seconds':max(reserved,(_stamp(fenced_at)-_stamp(b['active_started_at'])).total_seconds()),
                  'process_death_claimed':False}
            # Existence itself permanently fences new-code reserve/settle, even
            # if the next retirement-marker write crashes.
            _immutable(folder/'migration-snapshot.json',{'payload':snap,'sha256':digest(snap)})
        retirement={'schema_version':1,'old_revision_id':old_id,'replacement_id':snap['replacement_id'],
                    'snapshot_sha256':digest(snap),'permanent':True,'process_death_claimed':False}
        _immutable(folder/'retired.json',retirement)
        target=issue_dir(root,day,snap['replacement_id']);c=_replacement_contract(snap)
        completed=read_json(target/'migration-complete.json')
        if completed is not None:
            if completed!={'snapshot_sha256':digest(snap),'revision_id':c['revision_id']}:
                raise StateCorrupt('Conflicting completed migration')
            return read_contract(root,day,c['revision_id'])
        target.mkdir(parents=True,exist_ok=True)
        # Copies preserve operation identifiers and spent/reserved/circuit state.
        # Mutable old budget/state and shared queue are NEVER replayed.
        for name,record in snap['evidence']['files'].items():
            if name in {'contract.json','budget.json','generation.json','initialized.json'} or name.startswith('logs/'):
                continue
            path=target/name;data=_snapshot_decode(record)
            if path.exists() and path.read_bytes()!=data: raise StateCorrupt('Partial migration target bytes differ')
            if not path.exists(): atomic_bytes(path,data)
        _immutable(target/'contract.json',{'payload':c,'sha256':digest(c)})
        _immutable(target/'budget.json',_replacement_budget(snap,c))
        _immutable(target/'generation.json',{'revision_id':c['revision_id'],'running':False,
                   'migrated_from':old_id,'snapshot_sha256':digest(snap),'process_death_claimed':False})
        _immutable(target/'initialized.json',{'contract_sha256':digest(c)})
        _immutable(target/'migration-complete.json',{'snapshot_sha256':digest(snap),'revision_id':c['revision_id']})
        return read_contract(root,day,c['revision_id'])


def main(argv=None):
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['worker','generate','freeze','read','prepare','reconcile'])
    parser.add_argument('--root',required=True);parser.add_argument('--date',required=True)
    parser.add_argument('--revision-id',required=True);parser.add_argument('--timeout',type=float,default=900)
    parser.add_argument('--expected-attempt');parser.add_argument('--expected-namespace')
    a=parser.parse_args(argv);day=date.fromisoformat(a.date)
    if a.action=='worker':
        from daily_agent.parent_writer import PendingResponse
        try: result=generate(a.root,day,a.revision_id,expected_attempt=a.expected_attempt,expected_namespace=a.expected_namespace)
        except PendingResponse as exc:
            print(json.dumps({'state':'expired_parent_writer' if getattr(exc,'expired',False) else 'awaiting_parent_writer',
                              'job_id':exc.job_id,'issue_date':str(day),'revision_id':a.revision_id}))
            return 75
        print(json.dumps({'generated':True,'revision_id':a.revision_id,'approval_count':len(result['approval'])}))
        return 0
    if a.action=='generate': return run_generation(a.root,day,a.revision_id,a.timeout)
    if a.action=='freeze':
        result=freeze_completed(a.root,day,a.revision_id)
    elif a.action=='prepare':result=prepare_handoff(a.root,day,a.revision_id)
    elif a.action=='reconcile':
        reconcile_publication(a.root,day,a.revision_id);result=read_handoff(a.root,day,a.revision_id)
    else:result=read_handoff(a.root,day,a.revision_id)
    from daily_agent.cloud_workflow import _cli_result
    print(json.dumps(_cli_result(result),ensure_ascii=False,indent=2));return 0


if __name__=='__main__':
    raise SystemExit(main())
