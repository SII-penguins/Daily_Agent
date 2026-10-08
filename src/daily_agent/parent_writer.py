"""Explicit resumable parent-assisted model transport, never an implicit fallback.

Jobs freeze their prompts and image hashes. Imported JSON is only a model
response, not an approval: the caller still applies all existing evidence checks.
"""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import threading
import time
import uuid
import math
from datetime import date, datetime, timezone, timedelta
import hashlib
import json
from pathlib import Path
from daily_agent.workflow_state import atomic_json, read_json, exclusive_lock, StateCorrupt, WorkflowBusy

class PendingResponse(BaseException):
    def __init__(self, job_id):
        self.job_id = job_id
        super().__init__(f'Parent-assisted writer response required: {job_id}')

class ExpiredResponse(PendingResponse):
    expired = True

def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()

def _folder(root): return Path(root).resolve() / 'data' / 'writer-queue'

def _contract(job):
    value={key:job[key] for key in ('schema_version','transport','prompt','images','stage')}
    if job.get('retry'):value['retry']=job['retry']
    return value

def _base_id(job):
    return digest({key:job[key] for key in ('schema_version','transport','prompt','images','stage')})

def _active_id(folder,base):
    pointer=read_json(folder/f'{base}.active.json')
    if pointer is None:return base
    if not isinstance(pointer,dict):raise StateCorrupt('Invalid active writer generation')
    candidate=pointer.get('job_id')
    if pointer.get('base_job_id')!=base or not isinstance(candidate,str) or len(candidate)!=64 or any(c not in '0123456789abcdef' for c in candidate):
        raise StateCorrupt('Invalid active writer generation')
    return candidate

def _assert_active(folder,job):
    if _active_id(folder,_base_id(job))!=job['job_id']:
        raise ValueError('Writer generation retired; late response/claim rejected')

_QUEUE_THREADS = threading.Lock()

@contextmanager
def queue_lock(path):
    # Upstream reads pages/chunks concurrently. Serialize in-process requests,
    # then bound cross-process contention; never misreport a brief lock race as
    # an unread source chunk.
    if not _QUEUE_THREADS.acquire(timeout=10): raise TimeoutError('Writer queue busy')
    try:
        deadline=time.monotonic()+10
        while True:
            manager=exclusive_lock(path)
            try:
                manager.__enter__()
                break
            except WorkflowBusy:
                if time.monotonic()>=deadline: raise TimeoutError('Writer queue busy')
                time.sleep(.02)
        try: yield
        finally: manager.__exit__(None,None,None)
    finally: _QUEUE_THREADS.release()

def _stage(prompt):
    if any(marker in prompt[:240] for marker in ('独立', '核验员', '校验员', '审核', '审查', '核对', 'reviewer')):
        return 'review'
    if prompt.startswith('阅读论文的一个原文块'): return 'reading'
    return 'draft'

def request(root, prompt, timeout, image_path=None, *, stage=None):
    if not isinstance(prompt,str) or len(prompt)>500000: raise ValueError('Invalid/beyond-budget parent prompt')
    root=Path(root).resolve(); folder=_folder(root); folder.mkdir(parents=True,exist_ok=True)
    paths=image_path if isinstance(image_path,list) else [image_path] if image_path else []
    images=[]
    for value in paths:
        path=Path(value).resolve(); path.relative_to(root)
        content=path.read_bytes()
        images.append({'path':str(path.relative_to(root)), 'sha256':hashlib.sha256(content).hexdigest()})
    contract={'schema_version':1, 'transport':'parent_assisted', 'prompt':prompt,
              'images':images, 'stage':stage or _stage(prompt)}
    base_id=digest(contract)
    with queue_lock(folder/'queue.lock'):
        job_id=_active_id(folder,base_id);path=folder/f'{job_id}.job.json'
        job=read_json(path)
        if job is None:
            if job_id!=base_id:raise StateCorrupt('Active writer generation is missing')
            if len(pending(root)) >= 200: raise RuntimeError('Parent writer pending-job budget exhausted')
            now=datetime.now(timezone.utc)
            job={**contract,'job_id':job_id,'input_sha256':job_id,'created_at':now.isoformat(),
                 'expires_at':(now+timedelta(hours=6)).isoformat(),'call_timeout_seconds':timeout}
            atomic_json(path,job)
        validate_job(root, job)
        if digest({key:job[key] for key in contract}) != base_id: raise StateCorrupt('Queued prompt changed')
        answer=read_json(folder/f'{job_id}.answer.json')
        if answer is None:
            if datetime.now(timezone.utc)>=datetime.fromisoformat(job['expires_at']): raise ExpiredResponse(job_id)
            raise PendingResponse(job_id)
        if answer.get('job_id')!=job_id or answer.get('input_sha256')!=job_id or digest(answer.get('response'))!=answer.get('response_sha256'):
            raise StateCorrupt('Parent writer response identity mismatch')
        return answer['response']

def import_response(root, job_id, response, worker_id, *, model='native-assistant', claim_token=None):
    if not worker_id or not model: raise ValueError('Worker/model provenance required')
    folder=_folder(root)
    if len(job_id)!=64 or any(c not in '0123456789abcdef' for c in job_id): raise ValueError('Invalid job ID')
    with queue_lock(folder/'queue.lock'):
        job=read_json(folder/f'{job_id}.job.json')
        if not job: raise ValueError('Unknown pending job')
        validate_job(root, job)
        _assert_active(folder,job)
        lease=read_json(folder/f'{job_id}.claim.json')
        if job.get('retry') and lease is None:raise ValueError('Retried writer job requires a fresh claim')
        if lease and (lease.get('worker_id')!=worker_id or lease.get('token')!=claim_token or datetime.now(timezone.utc)>=datetime.fromisoformat(lease['expires_at'])):
            raise ValueError('Writer claim is missing, expired or owned by another worker')
        contract=_contract(job)
        if digest(contract)!=job_id: raise StateCorrupt('Queued job identity mismatch')
        if datetime.now(timezone.utc)>datetime.fromisoformat(job['expires_at']): raise RuntimeError('Queued job expired')
        # Separate worker identities are mandatory for independently reviewed evidence.
        for candidate in folder.glob('*.answer.json'):
            previous=read_json(candidate)
            previous_job=read_json(folder/f"{previous['job_id']}.job.json")
            if (previous_job['stage']=='review') != (job['stage']=='review') and previous['worker_id']==worker_id:
                raise ValueError('Independent review must use a different worker from readers/writers')
        value={'job_id':job_id,'input_sha256':job_id,'response':response,'response_sha256':digest(response),
               'worker_id':worker_id,'model':model,'received_at':datetime.now(timezone.utc).isoformat()}
        path=folder/f'{job_id}.answer.json'
        previous=read_json(path)
        if previous:
            if previous['response_sha256']!=value['response_sha256'] or previous['worker_id']!=worker_id:
                raise ValueError('An imported answer is immutable')
            return previous
        atomic_json(path,value)
        return value


def claim(root, job_id, worker_id, lease_seconds=900):
    if not worker_id or not math.isfinite(lease_seconds) or not 30<=lease_seconds<=3600:
        raise ValueError('Bounded worker claim required')
    if len(job_id)!=64 or any(c not in '0123456789abcdef' for c in job_id):raise ValueError('Invalid job ID')
    folder=_folder(root)
    with queue_lock(folder/'queue.lock'):
        job=read_json(folder/f'{job_id}.job.json');validate_job(root,job)
        _assert_active(folder,job)
        if (folder/f'{job_id}.answer.json').exists():raise ValueError('Job already answered')
        now=datetime.now(timezone.utc);limit=datetime.fromisoformat(job['expires_at'])
        if now>=limit:raise ValueError('Job expired')
        path=folder/f'{job_id}.claim.json';previous=read_json(path)
        active=previous and now<datetime.fromisoformat(previous['expires_at'])
        if active and previous['worker_id']!=worker_id:raise WorkflowBusy('Another worker owns this job')
        token=previous['token'] if active else uuid.uuid4().hex
        value={'job_id':job_id,'worker_id':worker_id,'token':token,
               'expires_at':min(limit,now+timedelta(seconds=lease_seconds)).isoformat(),
               'generation':previous.get('generation',0)+(not active) if previous else int(job.get('retry',{}).get('claim_generation',1))}
        atomic_json(path,value);return value


def validate_job(root, job, *, check_images=True):
    root=Path(root).resolve()
    if not isinstance(job,dict):raise StateCorrupt('Missing writer job')
    contract=_contract(job)
    if digest(contract)!=job.get('job_id') or job.get('input_sha256')!=job['job_id']:
        raise StateCorrupt('Queued input identity mismatch')
    if not check_images:return
    for image in job['images']:
        path=Path(image['path'])
        if path.is_absolute() or '..' in path.parts: raise StateCorrupt('Image must be root-relative')
        path=(root/path).resolve(); path.relative_to(root)
        if hashlib.sha256(path.read_bytes()).hexdigest()!=image['sha256']:
            raise StateCorrupt('Queued image changed')

def pending(root, *, include_expired=False):
    folder=_folder(root); jobs=[]
    for path in sorted(folder.glob('*.job.json')):
        if path.with_name(path.name.replace('.job.json','.answer.json')).exists(): continue
        job=read_json(path)
        if _active_id(folder,_base_id(job))!=job['job_id']:continue
        expired=datetime.now(timezone.utc)>=datetime.fromisoformat(job['expires_at'])
        if expired and not include_expired:continue
        validate_job(root,job,check_images=not expired)
        jobs.append({**job,'expired':expired,'claim':read_json(path.with_name(path.name.replace('.job.json','.claim.json')))})
    return jobs


def retry_expired(root,job_id,issue_date,reason):
    """Explicit new generation; preserve old evidence and frozen issue budgets."""
    from daily_agent.cloud_workflow import _config,_validate_budget
    if not isinstance(reason,str) or not reason.strip() or len(reason)>500:raise ValueError('Bounded retry reason required')
    if len(job_id)!=64 or any(c not in '0123456789abcdef' for c in job_id):raise ValueError('Invalid job ID')
    day=date.fromisoformat(str(issue_date));config=_config(root);folder=_folder(root)
    with exclusive_lock(config.state_dir/'cloud-generation.lock'), queue_lock(folder/'queue.lock'):
        budget_path=config.state_dir/'cloud-generation-budgets'/f'{day}.json'
        budget=read_json(budget_path);_validate_budget(budget,day)
        now=datetime.now(timezone.utc);deadline=datetime.fromisoformat(budget['deadline'])
        if (budget.get('active_started_at') or now>=deadline or budget['runtime_seconds']>=budget['max_runtime_seconds']
                or budget['failures']>=budget['max_failures'] or budget['resumes']>=budget['max_resumes']):
            raise ValueError('Issue budget/deadline does not permit writer retry')
        job=read_json(folder/f'{job_id}.job.json');validate_job(root,job);_assert_active(folder,job)
        if (folder/f'{job_id}.answer.json').exists():raise ValueError('Answered jobs are immutable and reusable, not retryable')
        if now<datetime.fromisoformat(job['expires_at']):raise ValueError('Only expired unanswered jobs can be retried')
        if len(pending(root))>=200:raise ValueError('Pending-job budget exhausted')
        base=_base_id(job)
        records=budget.get('writer_job_retries',[])
        if not isinstance(records,list) or any(not isinstance(row,dict) or not isinstance(row.get('base_job_id'),str) for row in records):
            raise StateCorrupt('Invalid writer retry accounting')
        if len(records)>=budget['max_resumes'] or sum(row['base_job_id']==base for row in records)>=min(3,budget['max_failures']):
            raise ValueError('Writer retry budget exhausted')
        prior_claim=read_json(folder/f'{job_id}.claim.json')
        prior_generation=prior_claim.get('generation') if prior_claim else job.get('retry',{}).get('claim_generation',1)
        if type(prior_generation) is not int or prior_generation<1:raise StateCorrupt('Invalid prior claim generation')
        retry={'base_job_id':base,'previous_job_id':job_id,'sequence':int(job.get('retry',{}).get('sequence',0))+1,
               'claim_generation':prior_generation+1,
               'issue_date':str(day),'attempt_nonce':uuid.uuid4().hex,'reason':reason.strip()}
        contract={key:job[key] for key in ('schema_version','transport','prompt','images','stage')};contract['retry']=retry
        new_id=digest(contract)
        new_job={**contract,'job_id':new_id,'input_sha256':new_id,'created_at':now.isoformat(),
                 'expires_at':min(deadline,now+timedelta(hours=6)).isoformat(),'call_timeout_seconds':job['call_timeout_seconds']}
        # Account before publishing the pointer. A crash may conservatively spend
        # a retry, never silently reset or exceed the budget.
        budget['writer_job_retries']=[*records,{'base_job_id':base,'previous_job_id':job_id,'job_id':new_id,'at':now.isoformat()}]
        atomic_json(budget_path,budget)
        atomic_json(folder/f'{new_id}.job.json',new_job)
        atomic_json(folder/f'{base}.active.json',{'base_job_id':base,'job_id':new_id,'sequence':retry['sequence'],'issue_date':str(day)})
        return new_job


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['pending','import','claim','retry-expired'])
    parser.add_argument('--root',required=True)
    parser.add_argument('--job-id'); parser.add_argument('--response-file'); parser.add_argument('--worker-id'); parser.add_argument('--claim-token'); parser.add_argument('--lease-seconds',type=float,default=900)
    parser.add_argument('--include-expired',action='store_true');parser.add_argument('--date');parser.add_argument('--reason')
    args=parser.parse_args()
    result=retry_expired(args.root,args.job_id,args.date,args.reason) if args.action=='retry-expired' else pending(args.root,include_expired=args.include_expired) if args.action=='pending' else claim(args.root,args.job_id,args.worker_id,args.lease_seconds) if args.action=='claim' else import_response(args.root,args.job_id,json.loads(Path(args.response_file).read_text()),args.worker_id,claim_token=args.claim_token)
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__': main()
