"""Issue-scoped cloud checkpoints; never shared with the legacy profile."""
from datetime import datetime, timezone
from dataclasses import asdict
import hashlib
import json
from pathlib import Path

_CODE_VERSION = hashlib.sha256(b"".join(path.read_bytes() for path in sorted(Path(__file__).parent.rglob("*.py")))).hexdigest()
from daily_agent.models import DigestItem, MaterialRecord, SourceStatus
from daily_agent.workflow_state import atomic_json, read_json, StateCorrupt


def _digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()

def enabled(config):
    return bool(config.delivery.get('cloud',{}).get('profile'))

def _file(config,day,kind,identity):
    key=_digest([_CODE_VERSION,identity,config.sources,[asdict(domain) for domain in config.domains]])
    return config.root/'data/cloud-checkpoints'/str(day)/kind/f'{key}.json'

def _read(path):
    value=read_json(path)
    if value is None:return None
    if not isinstance(value,dict) or _digest(value.get('payload'))!=value.get('sha256'):
        raise StateCorrupt('Cloud checkpoint integrity mismatch')
    return value['payload']

def _write(path,payload):atomic_json(path,{'schema_version':1,'sha256':_digest(payload),'payload':payload})

def fetch_source(config,day,name,fetcher,status,original,**kwargs):
    if not enabled(config):return original(name,fetcher,status,**kwargs)
    path=_file(config,day,'sources',name)
    cached=_read(path)
    if cached:
        stamp=datetime.fromisoformat(cached['collected_at'])
        age=(datetime.now(timezone.utc)-stamp).total_seconds()
        if cached['status']['ok'] or 0<=age<300:
            status.sources.append(SourceStatus(**cached['status']))
            status.errors.extend(cached['errors'])
            return [DigestItem.from_dict(row) for row in cached['items']]
    errors=len(status.errors)
    items=original(name,fetcher,status,**kwargs)
    _write(path,{'collected_at':datetime.now(timezone.utc).isoformat(),'status':status.sources[-1].to_dict(),
                 'errors':status.errors[errors:],'items':[item.to_dict() for item in items]})
    return items

def _enrichment_ready(record):
    if record.item_type=='repo':return bool(record.readme_excerpt)
    return record.paper_document.get('document_kind')=='full_text' and bool(record.paper_text_status.get('available'))

def prepare_batch(config,day,batch,enrich):
    # Durable reviewed evidence is reusable across dates and selection changes.
    # Issue checkpoints still own incomplete/unreviewed enrichment and retries.
    from daily_agent.deferred_review_cache import reusable_enrichment
    ready = {record.key: record for record in batch if reusable_enrichment(config, record)}
    pending = [record for record in batch if record.key not in ready]
    if pending:
        ready.update({record.key: record for record in _prepare_batch(config,day,pending,enrich)})
    return [ready[record.key] for record in batch if record.key in ready]


def _prepare_batch(config,day,batch,enrich):
    if not enabled(config):return enrich(batch)
    from daily_agent.paper_document import version_identity
    identity=[(record.key,version_identity(record),_digest(record.to_digest_item().to_dict()),record.paper_document.get('content_hash')) for record in batch]
    path=_file(config,day,'batches',identity)
    payload=_read(path)
    if payload is not None:
        cached=[MaterialRecord.from_dict(row) for row in payload['items']]
        age=(datetime.now(timezone.utc)-datetime.fromisoformat(payload['enriched_at'])).total_seconds()
        incomplete=[record for record in cached if not _enrichment_ready(record)]
        if not incomplete or 0<=age<300:return cached
        # Retry only unavailable/partial enrichment, preserving successful bytes.
        replacements={record.key:record for record in enrich(incomplete)}
        result=[replacements.get(record.key,record) for record in cached]
    else:
        result=enrich(batch)
    _write(path,{'enriched_at':datetime.now(timezone.utc).isoformat(),'items':[record.to_dict() for record in result]})
    return result
