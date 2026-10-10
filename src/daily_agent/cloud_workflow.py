"""Explicit cloud profile and durable parent-mediated ChatGPT delivery.

No messaging credentials or tools live here. The parent binds verified Library
HTML, sends the exact caption and attachment identity, records acceptance, then
verifies remote readback before committing publication. Legacy text stays valid.
Upstream full/Feishu configuration and workflows remain separate.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import time
import uuid
from datetime import date, datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import yaml
from daily_agent.config import load_config
from daily_agent.workflow_state import atomic_bytes, atomic_json, exclusive_lock, read_json, StateCorrupt, WorkflowBusy

PROFILE = 'cloud-public-chatgpt-v1'


def _hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _clock():
    return datetime.now(timezone.utc)

def _now():
    return _clock().isoformat()


def init_profile(source: Path, root: Path, writer: str) -> dict:
    from daily_agent.workflow_state import assert_mutation_allowed
    assert_mutation_allowed(Path(root))
    source, root = source.resolve(), root.resolve()
    if source == root or (root / 'config').exists():
        raise ValueError('Cloud profile must use a new separate root; existing configuration is never overwritten')
    root.mkdir(parents=True, exist_ok=True)
    values = {name: yaml.safe_load((source / 'config' / f'{name}.yaml').read_text()) or {}
              for name in ('sources', 'interests', 'delivery', 'feedback')}
    sources = values['sources']
    for key in ('google_scholar', 'core', 'ieee', 'unpaywall'):
        sources.setdefault(key, {})['enabled'] = False
    sources.setdefault('report_writing', {})['paper_visual_selection_provider'] = 'parent_queue'
    sources['llm_writer'].update(provider='parent_queue', command=writer, load_user_config=False, workdir=str(root))
    values['feedback'].setdefault('feishu_comments', {})['enabled'] = False
    delivery = values['delivery']
    delivery['delivery']['default'] = 'local'
    delivery['delivery']['feishu']['enabled'] = False
    delivery['delivery']['cc_connect']['enabled'] = False
    # Partial counts are allowed, never partial evidence. Quotas stay as targets.
    delivery['schedule']['recovery']['require_target_counts'] = False
    delivery['cloud'] = {'profile': PROFILE, 'channel': 'chatgpt', 'formal_papers_only': False,
                         'require_full_reading': True, 'partial_counts_allowed': True,
                         'source_profile_is_full': False, 'max_generation_seconds': 900, 'delivery_format':'html'}
    (root / 'config').mkdir()
    for name, value in values.items():
        atomic_bytes(root / 'config' / f'{name}.yaml', yaml.safe_dump(value, allow_unicode=True, sort_keys=False).encode())
    research_context = source / 'config' / 'research-context-evidence.json'
    if research_context.is_file():
        atomic_bytes(root / 'config' / research_context.name, research_context.read_bytes())
    manifest = {'profile': PROFILE, 'created_at': _now(), 'source_root': str(source),
                'unavailable_integrations': ['Google Scholar/SerpAPI', 'CORE', 'IEEE', 'Unpaywall', 'Feishu'],
                'source_profile_is_full': False, 'writer_verified': False,
                'warning': 'Network permissions and writer must pass live preflight before scheduling'}
    atomic_json(root / 'cloud-profile.json', manifest)
    return manifest


def _config(root):
    config = load_config(Path(root).resolve())
    if config.delivery.get('cloud', {}).get('profile') != PROFILE:
        raise ValueError('Explicit cloud profile required')
    for path in (config.reports_dir,config.selected_dir,config.state_dir,config.logs_dir):
        if not path.resolve().is_relative_to(config.root):
            raise ValueError('Cloud storage directories must stay inside the profile root')
    return config


def _path(config, day):
    return config.state_dir / 'cloud-delivery' / f'{day.isoformat()}.json'


def _safe_relative(root, value):
    relative = Path(value)
    if relative.is_absolute() or '..' in relative.parts:
        raise StateCorrupt('Artifact path must be root-relative')
    resolved = (root / relative).resolve()
    resolved.relative_to(root.resolve())
    return resolved


def _plain_text(markdown):
    # Chat messages cannot contain private local links or Markdown decorations.
    text = re.sub(r'!\[[^\]]*\]\([^)]*\)', '', markdown)
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)',
                  lambda m: f'{m[1]} ({m[2]})' if m[2].startswith(('https://', 'http://')) else m[1], text)
    text = re.sub(r'^#{1,6}\s*', '', text, flags=re.M)
    text = text.replace('**', '').replace('`', '')
    text = re.sub(r'飞书评论可写[^。]*。', '可以在这里告诉我哪些内容相关或不相关。', text)
    text = re.sub(r'https?://(?:127\.0\.0\.1|localhost)(?::\d+)?[^\s)]*', '', text)
    # Runtime errors can contain literal executor paths outside Markdown links.
    text = re.sub(r'(?i)file://[^\s<>\]）)]+', '[本地路径已省略]', text)
    text = re.sub(r'(?<![\w/])/(?:workspace|home|Users|tmp|private|root|mnt|var|opt)(?:/[^\s<>\]）)]+)+', '[本地路径已省略]', text)
    text = re.sub(r'(?i)(?<![\w])[a-z]:\\[^\s<>\]）)]+', '[本地路径已省略]', text)
    return text.strip() + '\n'


def _qualifying_rows(rows, *, config=None):
    from daily_agent.source_evidence_policy import audit_reading
    from daily_agent.scoring.publication import publication_evidence
    from daily_agent.models import DigestItem
    from daily_agent.scheduling import _approved_items
    items = _approved_items(rows) if rows else []
    accepted, excluded = [], []
    audits = {r['key']: r for r in audit_reading(rows)['papers']}
    for row, item in zip(rows, items):
        if item.item_type == 'repo':
            accepted.append(row)
            continue
        material = row['material']
        raw = dict(material.get('raw') or {})
        raw['evidence'] = material.get('evidence') or raw.get('evidence') or {}
        paper = DigestItem(id=item.key, source=material['source'], item_type='paper', title=item.title,
                           url=material['url'], doi=material.get('doi'), arxiv_id=material.get('arxiv_id'), raw=raw)
        evidence = publication_evidence(paper)
        reason = []
        # Preprints are eligible; publication evidence labels never imply peer review.
        # Full-reading/claim-support checks below remain mandatory.
        raw['publication_evidence'] = evidence
        if config is not None:
            from daily_agent.source_evidence_policy import record_policy, expected_record_policy
            if record_policy(material) != expected_record_policy(config, material):
                reason.append('source_evidence_policy_mismatch')
        if not audits[item.key]['quality_passed']:
            if audits[item.key].get('core_quality_passed') and audits[item.key].get('require_scientific_analysis'):
                reason.append('required_scientific_analysis_incomplete')
            else:
                reason.append('full_reading_or_claim_support_incomplete')
        if reason: excluded.append({'key':item.key, 'reasons':reason})
        else: accepted.append(row)
    return accepted, excluded


def reserved_delivery_identities(config, day):
    """Unresolved sends suppress reuse without claiming publication.

    The authoritative receipts are the reservation store. Never age out an
    ambiguous send, and never mix pilot roots with production history.
    """
    if config.delivery.get('cloud', {}).get('profile') != PROFILE or config.delivery['cloud'].get('pilot'):
        return set()
    from daily_agent.scheduling import _approved_items
    from daily_agent.scoring.dedup import _identity_keys
    reserved = set()
    for path in sorted((config.state_dir / 'cloud-delivery').glob('*.json')):
        try:
            other_day = date.fromisoformat(path.stem)
            manifest = read_handoff(config.root, other_day)
        except (KeyError, TypeError, ValueError, OSError) as exc:
            raise StateCorrupt('Invalid delivery ledger; reservations cannot be verified') from exc
        if other_day == day or manifest['kind'] in {'pilot', 'status'}:
            continue
        unresolved = manifest['state'] in {'sending', 'uncertain', 'accepted'} or (
            manifest['state'] == 'confirmed' and not manifest['publication_reconciled'])
        if unresolved and manifest['approval']:
            if not manifest.get('attempt_id') or (manifest['state'] in {'accepted','confirmed'} and not manifest.get('message_id')):
                raise StateCorrupt('Unresolved delivery lacks attempt/message identity')
            for item in _approved_items(copy.deepcopy(manifest['approval'])):
                reserved.update(_identity_keys(item.material.to_digest_item()))
                reserved.add(item.key)
    # Explicit revisions share the canonical publication history and reservations.
    # Only an exact current revision may exclude itself; never the whole day.
    from daily_agent.production_revision import reservation_keys
    reserved.update(reservation_keys(config.root,
        exclude_revision_id=config.delivery.get('cloud', {}).get('reservation_exclude_revision_id')))
    return reserved


def _check_reservations(config, day, rows, *, kind=None):
    if kind == 'pilot' or config.delivery.get('cloud', {}).get('pilot'):
        return
    from daily_agent.scheduling import _approved_items
    from daily_agent.scoring.dedup import _identity_keys
    reserved = reserved_delivery_identities(config, day)
    for item in _approved_items(copy.deepcopy(rows)) if rows else []:
        if ({item.key} | set(_identity_keys(item.material.to_digest_item()))) & reserved:
            raise ValueError('Approved item is reserved by an unresolved delivery; send blocked')


def _check_published_snapshot(config, day, rows, *, kind=None):
    """A prepared snapshot can become stale after another issue reconciles."""
    if kind == 'pilot' or config.delivery.get('cloud', {}).get('pilot') or not rows:
        return
    from daily_agent.storage import load_material_library, select_library_candidates
    from daily_agent.scheduling import _approved_items
    library = copy.deepcopy(load_material_library(config))
    items = _approved_items(copy.deepcopy(rows))
    for item in items:
        candidate = item.material
        stored = library.get(item.key)
        if stored:
            candidate.published_dates = copy.deepcopy(stored.published_dates)
            if stored.raw.get('published_paper_identity'):
                candidate.raw['published_paper_identity'] = stored.raw['published_paper_identity']
        library[item.key] = candidate
    eligible = {record.key for record in select_library_candidates(config, library, day)}
    if any(item.key not in eligible for item in items):
        raise ValueError('Prepared report is stale against publication/delivery history; send blocked')


def _failure_progress(config, day):
    """Read-only observed progress; failure is not proof that no work qualified."""
    try:
        snapshot = read_json(config.root / 'data/editorial' / str(day) / 'approval.json')
        if snapshot is None:
            return '完成和未封版数量尚未核实：尚无可重新核验的审批快照\n'
        if not isinstance(snapshot, list) or len({row['key'] for row in snapshot}) != len(snapshot):
            raise ValueError('Invalid approval snapshot')
        qualified, excluded = _qualifying_rows(snapshot, config=config)
        papers = sum(row['item_type'] == 'paper' for row in qualified)
        progress = f'当前审批快照已核验完成 {len(qualified)} 条（{papers} 篇论文、{len(qualified) - papers} 个开源项目）\n'
        if excluded:
            progress += f'另有 {len(excluded)} 条审批候选未通过当前交付门槛\n'
    except Exception as exc:
        return f'完成和未封版数量尚未核实：审批快照无法重新核验（{type(exc).__name__}）\n'
    from daily_agent.scheduling import _ready_path, validate_ready_report
    if not _ready_path(config, day).exists():
        return progress + f'其中尚未封版 {len(qualified)} 条；本期尚未生成可交付封版\n'
    try:
        ready = validate_ready_report(config, day)
        sealed, _ = _qualifying_rows(ready['approval'], config=config)
        identity = lambda row: _hash(json.dumps(row, sort_keys=True, ensure_ascii=False).encode())
        sealed_ids = {identity(row) for row in sealed}
        unsealed = sum(identity(row) not in sealed_ids for row in qualified)
        return progress + f'已核验封版 {len(sealed)} 条；当前完成快照中尚未封版 {unsealed} 条\n'
    except Exception as exc:
        return progress + f'封版数量尚未核实：现有封版未通过重新核验（{type(exc).__name__}）\n'


def prepare_handoff(root, day, conversation, *, failure=None, delivery_format=None):
    from daily_agent.workflow_state import assert_issue_allowed
    assert_issue_allowed(Path(root), day)
    """Seal independently validated report bytes; does not send or mark published."""
    config = _config(root)
    if not conversation or not conversation.strip(): raise ValueError('Verified conversation identifier required')
    path = _path(config, day)
    with exclusive_lock(path.with_suffix('.lock')):
        existing = read_json(path)
        if existing:
            return read_handoff(root, day)
        rows, excluded = [], []
        source_manifest = None
        if failure is None:
            from daily_agent.scheduling import validate_ready_report
            source_manifest = validate_ready_report(config, day)
            if source_manifest.get('schema_version') != 3:
                raise StateCorrupt('Cloud delivery requires a current v3 evidence seal; legacy import must be explicitly validated')
            rows, excluded = _qualifying_rows(source_manifest['approval'], config=config)
            _check_reservations(config, day, rows)
        if rows:
            from daily_agent.scheduling import _approved_items
            from daily_agent.models import RunStatus, SourceStatus, DeliveryStatus
            from daily_agent.rendering.markdown import render_daily_markdown
            log = read_json(config.logs_dir / f'run-{day.isoformat()}.log')
            raw_status = log.get('status', {}) if isinstance(log, dict) else {}
            status = RunStatus(generated_at=raw_status.get('generated_at') or '未记录',
                               sources=[SourceStatus(**value) for value in raw_status.get('sources', [])],
                               fallback=raw_status.get('fallback'), errors=raw_status.get('errors', []))
            if not raw_status: status.errors.append('本次快照没有来源运行日志，无法确认完整抓取范围')
            report = render_daily_markdown(_approved_items(rows), day, status)
            body = 'Daily Agent 日报 · 公共来源版（非全源）\n' + _plain_text(report)
            body += f'\n核验范围：{len(rows)} 条通过审核；{len(excluded)} 条因全文证据或结论支持不足未列入\n'
            kind = 'pilot' if config.delivery['cloud'].get('pilot') else 'report'
            if kind == 'pilot': body = '迁移验收样例（非今日新闻，不进入正式发布历史）\n' + body
        else:
            body = f'Daily Agent 日报状态 · {day.isoformat()}\n本期未完成可交付日报\n'
            if failure is not None:
                body += _failure_progress(config, day)
                blocker = str(failure).strip() or '运行未完成，具体阻塞未提供'
                body += f'阻塞：{blocker}\n'
            else:
                body += '本次封版快照中通过当前交付门槛的内容为 0 条\n'
            if excluded: body += f'{len(excluded)} 条候选未通过当前交付门槛\n'
            kind = 'status'
        body += '来源范围：未启用 Google Scholar、CORE、IEEE 和 Unpaywall；GitHub 与 Semantic Scholar 使用匿名接口，可能限流\n'
        body = _plain_text(body)
        delivery_format = delivery_format or config.delivery['cloud'].get('delivery_format', 'html')
        if delivery_format not in {'html', 'text'}:
            raise ValueError('Unsupported cloud delivery format')
        html = None
        if rows and delivery_format == 'html':
            from daily_agent.rendering.editorial import render_editorial_html
            html = render_editorial_html(_approved_items(rows), day, kind=kind, coverage=body,
                        excluded_count=len(excluded),
                        approval_sha256=_hash(json.dumps(rows, sort_keys=True, ensure_ascii=False).encode()),
                        body_sha256=_hash(body.encode()), identity=source_manifest['sha256']).encode('utf-8')
            papers = sum(row['item_type'] == 'paper' for row in rows)
            repos = len(rows) - papers
            body = (f'Daily Agent 日报 · {day.isoformat()}\n'
                    f'{papers} 篇已核验论文 · {repos} 个开源项目，完整阅读见 HTML 附件\n'
                    '公共来源版（非全源）；仅收录通过证据审核的内容\n')
            if kind == 'pilot':
                body = '界面验收样例（非今日新闻，不进入正式发布历史）\n' + body
        encoded = body.encode()
        sha = _hash(encoded)
        relative = f'data/cloud-artifacts/{day.isoformat()}/{sha}.txt'
        atomic_bytes(config.root / relative, encoded)
        approval_sha = _hash(json.dumps(rows, sort_keys=True, ensure_ascii=False).encode())
        identity = _hash(json.dumps([day.isoformat(), sha, approval_sha, 'chatgpt', conversation], ensure_ascii=False).encode())
        manifest = {'schema_version':1, 'profile':PROFILE, 'date':day.isoformat(), 'kind':kind,
                    'channel':'chatgpt', 'conversation':conversation, 'identity':identity,
                    'state':'prepared', 'body_sha256':sha, 'approval_sha256':approval_sha, 'report':relative,
                    'artifacts':[{'path':relative, 'sha256':sha, 'size':len(encoded), 'media_type':'text/plain'}],
                    'approval':rows, 'excluded':excluded, 'created_at':_now(),
                    'source_sha256':source_manifest['sha256'] if source_manifest else None,
                    'publication_reconciled':False}
        if html is not None:
            html_sha = _hash(html)
            html_relative = f'data/cloud-artifacts/{day.isoformat()}/{html_sha}.html'
            atomic_bytes(config.root / html_relative, html)
            manifest.update(schema_version=2, delivery_format='html', html_report=html_relative,
                            library_binding=None)
            manifest['artifacts'].append({'path':html_relative, 'sha256':html_sha,
                                          'size':len(html), 'media_type':'text/html'})
            manifest['identity'] = _html_identity(manifest)
        atomic_json(path, manifest)
        return read_handoff(root, day)


def _html_identity(manifest):
    # Seal all publication-affecting metadata, not only the caption.
    fields = ('schema_version', 'profile', 'date', 'kind', 'channel', 'conversation',
              'body_sha256', 'approval_sha256', 'report', 'artifacts', 'html_report',
              'delivery_format', 'excluded', 'source_sha256')
    return _hash(json.dumps({key:manifest.get(key) for key in fields},
                            sort_keys=True, ensure_ascii=False).encode())


def _binding_identity(manifest, binding):
    return _hash(json.dumps([manifest['identity'], binding], sort_keys=True, ensure_ascii=False).encode())


def _attachments_match(attachments, binding):
    return (isinstance(attachments, list) and len(attachments) == 1
            and isinstance(attachments[0], dict)
            and set(attachments[0]) == {'file_id', 'version'}
            and isinstance(attachments[0]['file_id'], str)
            and type(attachments[0]['version']) is int
            and attachments[0]['file_id'] == binding['file_id']
            and attachments[0]['version'] == binding['version'])


def _validate_partial_observation(value):
    if not isinstance(value, dict) or value.get('partial') is not True or set(value) != {'partial','attachments'}:
        raise ValueError('Actual partial readback and attachment references required')
    refs = value.get('attachments')
    if not isinstance(refs, list) or not 1 <= len(refs) <= 20:
        raise ValueError('Observed file attachment references required')
    seen = set()
    for ref in refs:
        if (not isinstance(ref, dict) or set(ref) != {'attachment_id','target','type'} or ref['type'] != 'file'
                or any(not isinstance(ref[key], str) or not ref[key].strip() or len(ref[key]) > 512 for key in ('attachment_id','target'))
                or ref['attachment_id'] in seen):
            raise ValueError('Invalid or duplicate observed attachment reference')
        seen.add(ref['attachment_id'])


def read_handoff(root, day):
    config = _config(root)
    manifest = read_json(_path(config, day))
    if not isinstance(manifest, dict) or manifest.get('schema_version') not in {1, 2} or manifest.get('date') != day.isoformat():
        raise StateCorrupt('Missing or invalid cloud handoff')
    html_mode = manifest['schema_version'] == 2
    body = _safe_relative(config.root, manifest['report']).read_bytes()
    if _hash(body) != manifest['body_sha256'] or manifest['channel'] != 'chatgpt':
        raise StateCorrupt('Cloud handoff integrity mismatch')
    artifacts = manifest.get('artifacts')
    if not isinstance(artifacts, list) or len(artifacts) != (2 if html_mode else 1):
        raise StateCorrupt('Invalid cloud artifact count')
    for index, artifact in enumerate(artifacts):
        expected_path = manifest['report'] if index == 0 else manifest.get('html_report')
        expected_media = 'text/plain' if index == 0 else 'text/html'
        if not isinstance(artifact, dict) or artifact.get('path') != expected_path or artifact.get('media_type') != expected_media:
            raise StateCorrupt('Invalid cloud artifact manifest')
        artifact_body = _safe_relative(config.root, artifact['path']).read_bytes()
        if (index == 0 and artifact.get('sha256') != manifest['body_sha256'] or
                _hash(artifact_body) != artifact.get('sha256') or type(artifact.get('size')) is not int or artifact['size'] != len(artifact_body)):
            raise StateCorrupt('Cloud artifact hash/size mismatch')
    if manifest.get('profile') != PROFILE or manifest.get('state') not in {'prepared','sending','uncertain','accepted','confirmed'}:
        raise StateCorrupt('Invalid cloud profile/state')
    if (manifest.get('kind') not in {'report','pilot','status'}
            or type(manifest.get('publication_reconciled')) is not bool
            or manifest['publication_reconciled'] and manifest['state'] != 'confirmed'):
        raise StateCorrupt('Invalid cloud publication receipt')
    approval_sha = _hash(json.dumps(manifest.get('approval'), sort_keys=True, ensure_ascii=False).encode())
    if approval_sha != manifest.get('approval_sha256'): raise StateCorrupt('Approval identity mismatch')
    expected = _html_identity(manifest) if html_mode else _hash(json.dumps([day.isoformat(), manifest['body_sha256'], approval_sha, 'chatgpt', manifest['conversation']], ensure_ascii=False).encode())
    if manifest['identity'] != expected: raise StateCorrupt('Cloud handoff destination/content mismatch')
    if 'dispatch_body_sha256' in manifest and manifest.get('site_binding') is None:
        raise StateCorrupt('Dispatch caption hash requires a verified Site binding')
    ids = []
    if html_mode:
        if manifest.get('delivery_format') != 'html': raise StateCorrupt('Invalid delivery format')
        binding = manifest.get('library_binding')
        if manifest.get('site_binding') is not None:
            from daily_agent.cloud_site_delivery import validate_site_binding
            body = validate_site_binding(config.root, manifest).encode()
        elif binding is not None:
            artifact = artifacts[1]
            if (not isinstance(binding, dict) or not isinstance(binding.get('file_id'), str) or not binding['file_id'].strip()
                    or type(binding.get('version')) is not int or binding['version'] < 0
                    or binding.get('sha256') != artifact['sha256'] or binding.get('size') != artifact['size']
                    or binding.get('media_type') != 'text/html'
                    or manifest.get('delivery_identity') != _binding_identity(manifest, binding)):
                raise StateCorrupt('Invalid Library binding')
            ids = [binding['file_id']]
            if manifest['state'] == 'confirmed' and not _attachments_match(manifest.get('verified_attachments'), binding):
                raise StateCorrupt('Confirmed HTML receipt lacks matching attachment evidence')
        elif manifest['state'] != 'prepared':
            raise StateCorrupt('HTML send has no verified Library binding')
    observed = manifest.get('readback_observation')
    if observed is not None:
        try:
            _validate_partial_observation({'partial':observed['partial'], 'attachments':observed['attachments']})
            if (not html_mode or manifest.get('site_binding') is not None or manifest['state'] not in {'accepted','confirmed'}
                    or observed.get('attachment_identity_verified') is not False
                    or any(observed.get(key) != manifest.get(key) for key in ('message_id','conversation','attempt_id','body_sha256'))
                    or manifest.get('readback_observation_sha256') != _binding_identity(manifest, observed)):
                raise ValueError('Observation receipt binding mismatch')
        except (KeyError, TypeError, ValueError) as exc:
            raise StateCorrupt('Invalid partial readback observation') from exc
    return {**manifest, 'body':body.decode(), **({'library_file_ids':ids} if html_mode else {})}


def bind_library(root, day, *, file_id, version, verified_file):
    from daily_agent.workflow_state import assert_issue_allowed
    assert_issue_allowed(Path(root), day)
    """Bind parent-verified Library export bytes before send; never upload here.

    The parent resolves file/version through Library, exports that exact version,
    and supplies its consumer-local file. Merely passing the original HTML path
    without a verified Library round trip does not satisfy this contract.
    """
    if not isinstance(file_id, str) or not file_id.strip() or type(version) is not int or version < 0:
        raise ValueError('Verified Library file ID and nonnegative integer version required')
    config = _config(root)
    path = _path(config, day)
    with exclusive_lock(path.with_suffix('.lock')):
        manifest = read_handoff(root, day)
        manifest.pop('body'); manifest.pop('library_file_ids', None)
        if manifest['schema_version'] != 2 or manifest['state'] != 'prepared' or manifest.get('site_binding') is not None:
            raise ValueError('Library binding requires a prepared HTML handoff')
        data = Path(verified_file).read_bytes()
        artifact = manifest['artifacts'][1]
        if _hash(data) != artifact['sha256'] or len(data) != artifact['size']:
            raise ValueError('Library export bytes do not match sealed HTML')
        binding = {'file_id':file_id, 'version':version, 'sha256':artifact['sha256'],
                   'size':artifact['size'], 'media_type':'text/html'}
        if manifest.get('library_binding') is not None and manifest['library_binding'] != binding:
            raise ValueError('Library binding is immutable; replacement requires a separate issue')
        manifest.update(library_binding=binding, delivery_identity=_binding_identity(manifest, binding))
        atomic_json(path, manifest)
    return read_handoff(root, day)


def record_transition(root, day, event, *, attempt_id=None, message_id=None, conversation=None, body=None, attachments=None, observation=None):
    from daily_agent.workflow_state import assert_issue_allowed
    assert_issue_allowed(Path(root), day, admission=(event == "begin"))
    from daily_agent.workflow_state import assert_mutation_allowed
    assert_mutation_allowed(Path(root))
    # Serialize dispatch across issue dates, not merely within one issue.
    config = _config(root)
    with exclusive_lock(config.state_dir / 'cloud-dispatch.lock'):
        return _record_transition(root, day, event, attempt_id=attempt_id, message_id=message_id,
                                  conversation=conversation, body=body, attachments=attachments, observation=observation)


def _record_transition(root, day, event, *, attempt_id=None, message_id=None, conversation=None, body=None, attachments=None, observation=None):
    config = _config(root)
    path = _path(config, day)
    with exclusive_lock(path.with_suffix('.lock')):
        manifest = read_handoff(root, day)
        manifest.pop('body'); manifest.pop('library_file_ids', None)
        state = manifest['state']
        if event == 'begin':
            if state != 'prepared': raise ValueError(f'Resend blocked in state {state}; reconcile remote outcome')
            if manifest['schema_version'] == 2 and not (manifest.get('library_binding') or manifest.get('site_binding')):
                raise ValueError('HTML attachment must be Library-verified before begin')
            if manifest['schema_version'] == 2 and config.delivery['cloud'].get('delivery_transport') == 'site' and not manifest.get('site_binding'):
                raise ValueError('Site transport requires a verified Site binding before begin')
            _check_reservations(config, day, manifest['approval'], kind=manifest['kind'])
            _check_published_snapshot(config, day, manifest['approval'], kind=manifest['kind'])
            manifest.update(state='sending', attempt_id=uuid.uuid4().hex, attempted_at=_now())
        else:
            if not attempt_id or manifest.get('attempt_id') != attempt_id: raise ValueError('Attempt identity mismatch')
            if event == 'uncertain':
                if state != 'sending': raise ValueError('Only sending attempts can become uncertain')
                manifest.update(state='uncertain')
            elif event == 'accepted':
                if state == 'accepted' and message_id == manifest.get('message_id'):
                    return read_handoff(root, day)
                if state not in {'sending', 'uncertain'} or not message_id: raise ValueError('Tool acceptance requires message identity')
                manifest.update(state='accepted', message_id=message_id, accepted_at=_now())
            elif event == 'observe-readback':
                if manifest['schema_version'] != 2 or manifest.get('site_binding') is not None or state != 'accepted' or message_id != manifest.get('message_id'):
                    raise ValueError('Partial readback must match an accepted HTML message')
                if conversation != manifest['conversation'] or body is None or _hash(body.encode()) != manifest.get('dispatch_body_sha256', manifest['body_sha256']):
                    raise ValueError('Readback destination/body mismatch')
                _validate_partial_observation(observation)
                recorded = {'message_id':message_id, 'conversation':conversation,
                            'attempt_id':attempt_id, 'body_sha256':manifest['body_sha256'],
                            'partial':True, 'attachments':copy.deepcopy(observation['attachments']),
                            'attachment_identity_verified':False}
                if manifest.get('readback_observation') is not None and manifest['readback_observation'] != recorded:
                    raise ValueError('Readback observation is immutable; conflicting evidence requires review')
                manifest['readback_observation'] = recorded
                manifest['readback_observation_sha256'] = _binding_identity(manifest, recorded)
            elif event == 'confirmed':
                if state != 'accepted' or message_id != manifest.get('message_id'):
                    raise ValueError('Readback must match accepted message identity')
                if conversation != manifest['conversation'] or body is None or _hash(body.encode()) != manifest.get('dispatch_body_sha256', manifest['body_sha256']):
                    raise ValueError('Readback destination/body mismatch')
                if manifest.get('site_binding') is not None:
                    manifest['verified_site_delivery'] = manifest['delivery_identity']
                elif manifest['schema_version'] == 2:
                    binding = manifest['library_binding']
                    expected = [{'file_id':binding['file_id'], 'version':binding['version']}]
                    if not _attachments_match(attachments, binding):
                        raise ValueError('Readback Library attachment identity/version mismatch')
                    manifest['verified_attachments'] = expected
                manifest.update(state='confirmed', confirmed_at=_now())
            else: raise ValueError('Unknown transition')
        atomic_json(path, manifest)
    if manifest['state'] == 'confirmed': reconcile_publication(root, day)
    return read_handoff(root, day)


def reconcile_publication(root, day):
    from daily_agent.workflow_state import assert_issue_allowed
    assert_issue_allowed(Path(root), day, admission=False)
    config = _config(root)
    path = _path(config, day)
    with exclusive_lock(path.with_suffix('.lock')):
        manifest = read_handoff(root, day); manifest.pop('body'); manifest.pop('library_file_ids', None)
        if manifest['state'] != 'confirmed': raise ValueError('Remote readback required before publication')
        if manifest['publication_reconciled']: return
        from daily_agent.scheduling import _approved_items
        from daily_agent.storage import mark_materials_published, write_published_index, write_selected
        if manifest['approval'] and manifest['kind'] != 'pilot':
            with exclusive_lock(config.state_dir / 'pipeline.lock'):
                items = _approved_items(copy.deepcopy(manifest['approval']))
                mark_materials_published(config, [item.material for item in items], day)
                write_published_index(config, items, day)
                write_selected(config, items, day)
        manifest.update(publication_reconciled=True, reconciled_at=_now())
        atomic_json(path, manifest)





def _resume_capacity(config):
    # Conservative one-job-per-resume allowance: native + repaired text, page
    # observation/transcription/review/one repair/review, and drafting checks.
    chunks=max(1,int(config.sources.get('reading',{}).get('max_chunks_per_paper',80)))
    pages=max(1,int(config.delivery.get('cloud',{}).get('budget_pages_per_paper',80)))
    papers=max(1,int(config.quota.get('paper_target',8)))
    repos=max(0,int(config.quota.get('github_target',2)))
    return min(10000,max(200,papers*(2*chunks+5*pages+12)+repos*4))


def _validate_budget(value, expected_day):
    if not isinstance(value,dict) or value.get('schema_version')!=1 or value.get('date')!=str(expected_day):
        raise StateCorrupt('Invalid cloud budget identity')
    for field in ('runtime_seconds','max_runtime_seconds'):
        number=value.get(field)
        if type(number) not in {int,float} or not math.isfinite(number) or number<0 or field=='max_runtime_seconds' and number<=0:
            raise StateCorrupt('Invalid cloud runtime budget')
    for field in ('failures','resumes','max_failures','max_resumes'):
        number=value.get(field)
        if type(number) is not int or number<0 or field.startswith('max_') and number<1:
            raise StateCorrupt('Invalid cloud attempt budget')
    for field in ('issue_started_at','deadline','active_started_at'):
        if field=='active_started_at' and field not in value: continue
        try:
            timestamp=datetime.fromisoformat(value[field])
            if timestamp.tzinfo is None or timestamp.utcoffset() is None: raise ValueError('naive time')
        except (KeyError,TypeError,ValueError) as exc:
            raise StateCorrupt('Invalid cloud issue timestamp') from exc
    if value['max_runtime_seconds']>86400 or value['max_failures']>20 or value['max_resumes']>10000:
        raise StateCorrupt('Cloud budget exceeds supported bounds')
    if (datetime.fromisoformat(value['deadline'])-datetime.fromisoformat(value['issue_started_at'])).total_seconds()>86400:
        raise StateCorrupt('Cloud issue window exceeds one day')
    if ('active_started_at' in value) != ('active_attempt_id' in value):
        raise StateCorrupt('Incomplete active cloud attempt')
    if 'active_attempt_id' in value and (not isinstance(value['active_attempt_id'],str) or not value['active_attempt_id']):
        raise StateCorrupt('Invalid active cloud attempt identity')
    # Historical idle budgets remain valid; only freshly fenced active attempts
    # may execute or be automatically recovered.
    if 'active_namespace' in value:
        if (not isinstance(value['active_namespace'], str) or not value['active_namespace']
                or not value.get('active_attempt_id')):
            raise StateCorrupt('Invalid active cloud execution namespace')
    if 'active_timeout_seconds' in value:
        limit = value['active_timeout_seconds']
        if type(limit) not in {int, float} or not math.isfinite(limit) or limit <= 0:
            raise StateCorrupt('Invalid active cloud attempt timeout')


def _local_day(root, requested=None):
    if requested: return date.fromisoformat(requested)
    config=load_config(root)
    return _clock().astimezone(ZoneInfo(config.delivery.get('report',{}).get('timezone','Asia/Shanghai'))).date()


def _read_generation_attempt(config, day, attempt, namespace, *, settled=False):
    """Compare both durable fences while the caller holds cloud-generation.lock.

    No nested flock: the ordinary supervisor owns this lock for its entire run.
    Budget settlement is persisted first; the matching settled fence allows the
    final journal write without ever adopting a newer attempt's metadata.
    """
    budget = read_json(config.state_dir / 'cloud-generation-budgets' / f'{day}.json')
    _validate_budget(budget, day)
    prefix = 'last' if settled else 'active'
    if (budget.get(f'{prefix}_attempt_id') != attempt
            or budget.get(f'{prefix}_namespace') != namespace
            or (settled and budget.get('active_attempt_id'))):
        raise StateCorrupt('Cloud supervisor attempt was superseded')
    state = read_json(config.state_dir / 'cloud-generation.json')
    if (not isinstance(state, dict) or state.get('date') != str(day)
            or state.get('running') is not True or state.get('attempt_id') != attempt
            or state.get('namespace') != namespace):
        raise StateCorrupt('Cloud supervisor journal was superseded')
    return budget, state


def run_generation(root, day, timeout, *, worker_module="daily_agent.cloud_workflow"):
    from daily_agent.workflow_state import assert_issue_allowed
    assert_issue_allowed(Path(root), day)
    from daily_agent.workflow_state import assert_mutation_allowed
    assert_mutation_allowed(Path(root))
    """Persist issue-wide deadline/runtime across resumptions and supervisor restarts."""
    from daily_agent.workflow_runtime import run_process, stop_verified_orphan, process_namespace, CleanupPending
    config = _config(root)
    if worker_module not in {'daily_agent.cloud_workflow','daily_agent.cloud_pilot'}:
        raise ValueError('Unsupported generation worker')
    if worker_module == 'daily_agent.cloud_pilot' and not config.delivery['cloud'].get('pilot'):
        raise ValueError('Pilot worker requires explicit calibration profile')
    path = config.state_dir / 'cloud-generation.json'
    budget_dir = config.state_dir / 'cloud-generation-budgets'
    budget_path = budget_dir / f'{day}.json'
    policy = config.delivery['cloud']
    recovery = config.delivery.get('schedule', {}).get('recovery', {})
    max_seconds = float(policy.get('max_issue_runtime_seconds', 25800))
    max_failures = int(policy.get('max_generation_failures', 3))
    max_resumes = int(policy.get('max_generation_resumes', _resume_capacity(config)))
    if not math.isfinite(max_seconds) or not 0 < max_seconds <= 86400 or not 1 <= max_failures <= 20 or not 1 <= max_resumes <= 10000:
        raise ValueError('Invalid persistent cloud issue budget')
    namespace = process_namespace()
    with exclusive_lock(path.with_suffix('.lock')):
        previous = read_json(path)
        if previous and previous.get('running'):
            if (not previous.get('namespace') or previous['namespace'] != namespace):
                raise StateCorrupt('Previous cloud generation namespace cannot be verified; operator review required')
            if not previous.get('child_identity'):
                raise StateCorrupt('Previous cloud generation has no cleanup identity; operator review required')
            old_path = budget_dir / f"{date.fromisoformat(previous['date'])}.json"
            old_budget = read_json(old_path)
            _validate_budget(old_budget, date.fromisoformat(previous['date']))
            if old_budget.get('active_started_at'):
                if (old_budget.get('active_attempt_id') != previous.get('attempt_id')
                        or old_budget.get('active_namespace') != namespace):
                    raise StateCorrupt('Retained cloud process budget identity mismatch')
            elif (old_budget.get('last_attempt_id') != previous.get('attempt_id')
                    or old_budget.get('last_namespace') != namespace):
                raise StateCorrupt('Retained cloud process has no matching completed budget')
            stop_verified_orphan(previous['child_identity'], expected_root=config.root)
            # Cleanup may block. Recheck ownership before charging runtime or
            # marking its journal stopped; never overwrite a replacement fence.
            old_day = date.fromisoformat(previous['date'])
            old_attempt = previous['attempt_id']
            old_budget, previous = _read_generation_attempt(config, old_day, old_attempt, namespace,
                settled=not bool(old_budget.get('active_started_at')))
            if old_budget.get('active_started_at'):
                elapsed = max(0, (_clock()-datetime.fromisoformat(old_budget['active_started_at'])).total_seconds())
                old_budget['runtime_seconds'] += elapsed
                old_budget['failures'] += 1
                old_budget['last_attempt_id'] = old_budget.pop('active_attempt_id')
                old_budget['last_namespace'] = old_budget.pop('active_namespace')
                old_budget.pop('active_timeout_seconds', None)
                old_budget['last_returncode'] = 124
                old_budget.pop('active_started_at', None)
                atomic_json(old_path, old_budget)
            _, previous = _read_generation_attempt(config, old_day, old_attempt, namespace, settled=True)
            previous.update(running=False, cleanup_confirmed_at=_now())
            atomic_json(path, previous)
        if previous and previous.get('date') == str(day) and previous.get('checkpoint'):
            from daily_agent.batch_dispatch import validate_response_payload
            observed = validate_response_payload(previous['checkpoint'], day)
            if observed is None:
                raise StateCorrupt('Invalid prior generation checkpoint; operator review required')
            if observed.get('auto_resume') is False:
                return 78  # No new child, resume counter, budget reservation or retry authority.
        budget = read_json(budget_path)
        from daily_agent.incremental_issue import issue_dir, can_enroll, enroll, assert_generation_allowed, load_plan, PROTOCOL, has_prior_activity
        # Do not launch or spend a new issue attempt after a publication seal.
        from daily_agent.scheduling import _ready_path
        with exclusive_lock(_path(config, day).with_suffix('.lock')), \
                exclusive_lock(_ready_path(config, day).with_suffix('.lock')):
            assert_generation_allowed(config, day)
        if budget is None and (issue_dir(config, day).exists() or has_prior_activity(config, day)):
            raise StateCorrupt('Prior issue budget missing; refusing reset')
        if budget is None:
            enroll_incremental = worker_module == 'daily_agent.cloud_workflow' and can_enroll(config, day)
            now = _clock()
            if policy.get('pilot'):
                deadline = now + timedelta(seconds=min(21600, max_seconds))
            else:
                hour, minute = map(int,config.delivery['schedule']['delivery_time'].split(':'))
                zone = ZoneInfo(config.delivery['report'].get('timezone', 'Asia/Shanghai'))
                deadline = datetime(day.year,day.month,day.day,hour,minute,tzinfo=zone).astimezone(timezone.utc)
                deadline -= timedelta(seconds=max(0,float(recovery.get('deadline_buffer_seconds',120))))
            budget = {'schema_version':1,'date':str(day),'issue_started_at':now.isoformat(),
                      'deadline':deadline.isoformat(),'runtime_seconds':0.,'failures':0,'resumes':0,
                      'max_runtime_seconds':max_seconds,'max_failures':max_failures,'max_resumes':max_resumes}
            if enroll_incremental:
                budget['execution_protocol'] = PROTOCOL
            atomic_json(budget_path,budget)
            if enroll_incremental:
                enroll(config, day)
        _validate_budget(budget, day)
        load_plan(config, day, require_budget=True)
        if budget.get('active_started_at'):
            raise StateCorrupt('Unreconciled cloud launch budget; operator review required')
        remaining_wall = (datetime.fromisoformat(budget['deadline'])-_clock()).total_seconds()
        remaining_runtime = budget['max_runtime_seconds']-budget['runtime_seconds']
        if remaining_wall <= 0 or remaining_runtime <= 0 or budget['failures']>=budget['max_failures'] or budget['resumes']>=budget['max_resumes']:
            budget.update(stopped_at=_now(),stop_reason='issue_deadline_or_budget_exhausted')
            atomic_json(budget_path,budget)
            return 124
        permitted = min(timeout, float(policy.get('max_generation_seconds',900)),remaining_wall,remaining_runtime)
        if not math.isfinite(permitted) or permitted <= 0:
            raise ValueError('Invalid cloud attempt timeout')
        started_at = _clock()
        started_monotonic = time.monotonic()
        attempt = uuid.uuid4().hex
        budget.update(resumes=budget['resumes']+1, active_started_at=started_at.isoformat(),
                      active_attempt_id=attempt, active_namespace=namespace, active_timeout_seconds=permitted)
        atomic_json(budget_path,budget)
        state = {'date':str(day),'running':True,'started_at':started_at.isoformat(),'child_identity':None,
                 'timeout_seconds':permitted,'issue_deadline':budget['deadline'],
                 'attempt_id':attempt,'namespace':namespace}
        launch_known = False
        def started(identity):
            nonlocal state, launch_known
            # The child exists before any fallible fence read/fsync. Retain
            # this fact even if the journal write or subsequent cleanup fails.
            launch_known = True
            state['child_identity'] = copy.deepcopy(identity)
            _, current = _read_generation_attempt(config, day, attempt, namespace)
            current['child_identity'] = identity
            atomic_json(path, current)
            state = current
        def tick():
            nonlocal state
            _, current = _read_generation_attempt(config, day, attempt, namespace)
            current['heartbeat_at'] = _now()
            atomic_json(path, current)
            state = current
        log_prefix = config.logs_dir / f'cloud-{day}'
        output_log = log_prefix.with_suffix('.out.log')
        output_offset = output_log.stat().st_size if output_log.exists() else 0
        # Persist the attempt before launch so the worker can bind itself to
        # this supervisor while on_start publishes its child process identity.
        atomic_json(path, state)
        command = [sys.executable, '-m', worker_module,
                   'generate' if worker_module == 'daily_agent.cloud_workflow' else 'worker',
                   '--root', str(config.root), '--date', str(day)]
        if worker_module == 'daily_agent.cloud_workflow':
            command += ['--expected-attempt', attempt, '--expected-namespace', namespace]
        try:
            rc = run_process(command,
                             config.root, permitted, on_start=started, on_tick=tick,
                             log_prefix=log_prefix)
        except CleanupPending:
            raise  # Unconfirmed cleanup can never settle a launch as stopped.
        except Exception:
            if launch_known:
                raise  # Retain the active budget for a possibly running child.
            rc = 1  # Popen/log-open failed before any launched-process callback.
        budget, state = _read_generation_attempt(config, day, attempt, namespace)
        from daily_agent.batch_dispatch import read_response_checkpoint
        checkpoint = read_response_checkpoint(output_log, output_offset, day) if rc == 75 else None
        if checkpoint is not None and checkpoint.get('auto_resume') is False:
            # Persist the stop intent before closing the budget. A crash between
            # settlement writes must not erase the reason another launch is barred.
            state['checkpoint'] = checkpoint
            atomic_json(path, state)
        budget['runtime_seconds'] += max(0,time.monotonic()-started_monotonic,(_clock()-started_at).total_seconds())
        budget['failures'] += int(rc not in {0,75})
        budget.pop('active_started_at',None)
        budget['last_attempt_id'] = budget.pop('active_attempt_id')
        budget['last_namespace'] = budget.pop('active_namespace')
        budget.pop('active_timeout_seconds', None)
        budget['last_returncode'] = rc
        atomic_json(budget_path,budget)
        _, state = _read_generation_attempt(config, day, attempt, namespace, settled=True)
        state.update(running=False,finished_at=_now(),returncode=rc)
        state.pop('checkpoint', None)
        if checkpoint is not None:
            state['checkpoint'] = checkpoint
        _read_generation_attempt(config, day, attempt, namespace, settled=True)
        atomic_json(path,state)
        return rc


def generate(root, day, *, expected_attempt=None, expected_namespace=None):
    from daily_agent.workflow_state import assert_issue_allowed
    assert_issue_allowed(Path(root), day)
    from daily_agent.workflow_state import assert_mutation_allowed
    assert_mutation_allowed(Path(root))
    previous = os.environ.get('DAILY_AGENT_DISABLE_EXTERNAL_SECRETS')
    os.environ['DAILY_AGENT_DISABLE_EXTERNAL_SECRETS'] = '1'
    try:
        config = _config(root)
        from daily_agent.pipeline import _run_pipeline_unlocked
        from daily_agent.incremental_issue import assert_generation_allowed, assert_active_generation, load_plan
        from daily_agent.scheduling import _ready_path
        # Publication owns the outer locks, matching existing reconciliation
        # (publication lock -> pipeline lock). Hold through the final seal.
        with exclusive_lock(_path(config, day).with_suffix('.lock')), \
                exclusive_lock(_ready_path(config, day).with_suffix('.lock')), \
                exclusive_lock(config.state_dir / 'pipeline.lock'):
            assert_generation_allowed(config, day)
            assert_active_generation(config, day, expected_attempt=expected_attempt,
                                     expected_namespace=expected_namespace)
            plan = load_plan(config, day, require_budget=True)
            return _run_pipeline_unlocked(config.root, day, dry_run=False, use_llm=True,
                                          delivery_mode='local', defer_delivery=True,
                                          _issue_plan=plan, _ready_lock_held=True)
    finally:
        if previous is None: os.environ.pop('DAILY_AGENT_DISABLE_EXTERNAL_SECRETS', None)
        else: os.environ['DAILY_AGENT_DISABLE_EXTERNAL_SECRETS'] = previous



def _cli_result(result, *, include_evidence=False):
    """Keep delivery output parseable without changing validated stored evidence."""
    if not isinstance(result,dict) or 'approval' not in result:
        return result
    value={key:item for key,item in result.items() if key!='approval'}
    value['approval_count']=len(result['approval'])
    if include_evidence:
        value['approval']=result['approval']
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['init', 'generate', 'prepare', 'read', 'begin', 'accepted', 'uncertain', 'confirmed', 'reconcile', 'bind-library', 'bind-site', 'observe-readback'])
    parser.add_argument('--root', required=True)
    parser.add_argument('--source')
    parser.add_argument('--writer', default='codex')
    parser.add_argument('--date')
    parser.add_argument('--conversation')
    parser.add_argument('--attempt-id')
    parser.add_argument('--expected-attempt', help='Immutable supervisor generation attempt')
    parser.add_argument('--expected-namespace', help='Immutable supervisor execution namespace')
    parser.add_argument('--message-id')
    parser.add_argument('--readback-file')
    parser.add_argument('--failure')
    parser.add_argument('--delivery-format', choices=['html', 'text'])
    parser.add_argument('--site-archive-dir')
    parser.add_argument('--site-deployment-file')
    parser.add_argument('--site-version-file')
    parser.add_argument('--site-repo')
    parser.add_argument('--library-file-id')
    parser.add_argument('--library-version', type=int)
    parser.add_argument('--verified-library-file')
    parser.add_argument('--readback-observation-file', help='Actual partial readback JSON with opaque file attachment references')
    parser.add_argument('--readback-attachments-file', help='JSON array of Library {file_id, version} observed on the exact message')
    parser.add_argument('--include-evidence', action='store_true', help='Include full approval evidence in JSON output; normally omitted to keep handoffs parseable')
    parser.add_argument('--timeout', type=float, default=900)
    args = parser.parse_args(argv)
    day = _local_day(args.root,args.date)
    if args.action == 'init':
        result = init_profile(Path(args.source), Path(args.root), args.writer)
    elif args.action == 'generate':
        from daily_agent.parent_writer import PendingResponse
        try: generate(args.root, day, expected_attempt=args.expected_attempt,
                      expected_namespace=args.expected_namespace)
        except PendingResponse as exc:
            from daily_agent.batch_dispatch import response_payload
            print(json.dumps({**response_payload(exc), 'issue_date':str(day)}))
            return 75
        result = {'generated':True}
    elif args.action == 'prepare':
        if not math.isfinite(args.timeout) or args.timeout <= 0 or args.timeout > 18000: raise ValueError('Invalid bounded generation timeout')
        failure = args.failure
        config = _config(args.root)
        previous = read_json(_path(config, day))
        if previous:
            print(json.dumps(_cli_result(read_handoff(args.root, day), include_evidence=args.include_evidence), ensure_ascii=False, indent=2))
            return 0
        if failure is None:
            try:
                rc = run_generation(args.root, day, args.timeout)
            except WorkflowBusy:
                print(json.dumps({'state':'deferred_busy','root':str(config.root)}))
                return 75
            if rc == 75:
                journal=read_json(config.state_dir/'cloud-generation.json') or {}
                from daily_agent.batch_dispatch import validate_response_payload
                checkpoint=validate_response_payload(journal.get('checkpoint'), day) if journal.get('date')==str(day) else None
                print(json.dumps({**({'state':'awaiting_parent_writer'} if not checkpoint else checkpoint),'root':str(config.root),'issue_date':str(day)}))
                return 75
            if rc == 78:
                from daily_agent.batch_dispatch import validate_response_payload
                journal = read_json(config.state_dir/'cloud-generation.json') or {}
                checkpoint = validate_response_payload(journal.get('checkpoint'), day)
                print(json.dumps({**(checkpoint or {}), 'state':'blocked_expired_parent_writer',
                                  'auto_resume':False, 'recovery_required':'expired_requires_authorized_recovery',
                                  'root':str(config.root), 'issue_date':str(day)}))
                return 78
            if rc:
                print(json.dumps({'state':'generation_failed','returncode':rc,'root':str(config.root)}))
                return rc
        try:
            result = prepare_handoff(args.root, day, args.conversation, failure=failure, delivery_format=args.delivery_format)
        except (FileNotFoundError, RuntimeError):
            print(json.dumps({'state':'no_verified_output','root':str(config.root)}))
            return 4
    elif args.action == 'bind-site':
        from daily_agent.cloud_site_delivery import bind_site
        result = bind_site(args.root, day, archive_dir=args.site_archive_dir,
                          deployment_file=args.site_deployment_file, site_version_file=args.site_version_file, site_repo=args.site_repo)
    elif args.action == 'bind-library':
        result = bind_library(args.root, day, file_id=args.library_file_id, version=args.library_version, verified_file=args.verified_library_file)
    elif args.action == 'read': result = read_handoff(args.root, day)
    elif args.action == 'reconcile':
        reconcile_publication(args.root, day); result = read_handoff(args.root, day)
    else:
        body = Path(args.readback_file).read_text() if args.readback_file else None
        result = record_transition(args.root, day, args.action, attempt_id=args.attempt_id,
                                   message_id=args.message_id, conversation=args.conversation, body=body,
                                   attachments=json.loads(Path(args.readback_attachments_file).read_text()) if args.readback_attachments_file else None,
                                   observation=json.loads(Path(args.readback_observation_file).read_text()) if args.readback_observation_file else None)
    print(json.dumps(_cli_result(result, include_evidence=args.include_evidence), ensure_ascii=False, indent=2))
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
