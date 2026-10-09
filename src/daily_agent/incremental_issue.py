"""Stable, opt-in planner for new cloud parent-queue issues.

The caller owns publication locks and pipeline.lock. Source changes invalidate a
plan in place; they never select a new budget namespace. No legacy issue is
inferred to be fresh from a cache miss.
"""
from copy import deepcopy
from dataclasses import asdict
import hashlib
import os
import time
from pathlib import Path

from daily_agent.batch_execution import ExecutionConflict, digest
from daily_agent.models import MaterialRecord
from daily_agent.workflow_state import StateCorrupt, atomic_json, read_json

PROTOCOL = 'incremental-cloud-v1'


def issue_dir(config, day):
    return config.state_dir / 'incremental-issues' / str(day)


def source_version():
    return hashlib.sha256(b''.join(p.read_bytes() for p in sorted(Path(__file__).parent.rglob('*.py')))).hexdigest()


def _contract(config, day):
    selection = config.sources.get('selection', {})
    batches, size = selection.get('max_review_batches'), selection.get('editorial_batch_size')
    if type(batches) is not int or not 1 <= batches <= 6 or type(size) is not int or not 1 <= size <= 8:
        raise ExecutionConflict('Incremental issue requires bounded original batch policy (at most 6 x 8)')
    return {'protocol': PROTOCOL, 'date': str(day), 'source_version': source_version(),
            'sources': deepcopy(config.sources), 'domains': [asdict(d) for d in config.domains],
            'quota': deepcopy(config.quota), 'max_batches': batches, 'batch_size': size}


def _read(path):
    envelope = read_json(path)
    if envelope is None:
        return None
    if (not isinstance(envelope, dict) or set(envelope) != {'payload', 'sha256'}
            or digest(envelope['payload']) != envelope['sha256']):
        raise StateCorrupt('Incremental planner integrity mismatch')
    return envelope['payload']


def _write(path, payload):
    atomic_json(path, {'payload': payload, 'sha256': digest(payload)})


def publication_owned(config, day):
    """Read-only fail-closed gate; existence itself reserves this issue.

    The calling generator holds both publication locks while checking and
    generating. Malformed records cannot be mistaken for an empty issue.
    """
    ready = config.state_dir / 'ready-reports' / f'{day}.json'
    manifest = config.state_dir / 'cloud-delivery' / f'{day}.json'
    for path in (manifest, ready, ready.with_suffix('.outbox.json'), ready.with_suffix('.delivered.json')):
        if read_json(path) is not None:
            return True
    return False


def assert_generation_allowed(config, day):
    if publication_owned(config, day):
        raise ExecutionConflict('Sealed or delivery-owned issue cannot be regenerated')


def can_enroll(config, day):
    if (not config.delivery.get('cloud', {}).get('profile')
            or config.delivery['cloud'].get('pilot')
            or config.sources.get('llm_writer', {}).get('provider') != 'parent_queue'):
        return False
    if publication_owned(config, day):
        return False
    # All known date-scoped prior activity makes this a legacy issue. Never
    # migrate an unknown in-progress issue after a source-hash cache miss.
    return not has_prior_activity(config, day)


def has_prior_activity(config, day):
    """Prior work cannot regain an issue budget merely because its ledger vanished."""
    paths = [config.root / 'data/cloud-checkpoints' / str(day),
             config.root / 'data/editorial' / str(day),
             config.reports_dir / f'daily-agent-{day}.md',
             config.reports_dir / f'daily-agent-{day}.html',
             config.selected_dir / f'selected-{day}.json',
             config.logs_dir / f'run-{day}.log']
    return any(p.exists() for p in paths)


def enroll(config, day):
    """Called only once immediately after first issue-budget creation."""
    folder = issue_dir(config, day)
    if folder.exists():
        raise ExecutionConflict('Existing issue planner cannot be initialized again')
    contract = _contract(config, day)
    folder.mkdir(parents=True)
    # A missing plan after this marker fails closed. Never recreate a budget.
    _write(folder / 'initialized.json', {'contract_sha256': digest(contract)})
    _write(folder / 'plan.json', {'contract': contract, 'discovery': None, 'batches': []})
    return IssuePlan(config, day)


class IssuePlan:
    def __init__(self, config, day):
        self.config, self.day = config, day
        self.folder = issue_dir(config, day)
        self.path = self.folder / 'plan.json'
        self.state = _read(self.path)
        marker = _read(self.folder / 'initialized.json')
        contract = _contract(config, day)
        if self.state is None or marker is None:
            raise StateCorrupt('Incomplete initialized planner; refusing a fresh execution budget')
        if (not isinstance(self.state, dict) or set(self.state) != {'contract', 'discovery', 'batches'}
                or self.state['contract'] != contract or marker != {'contract_sha256': digest(contract)}):
            raise ExecutionConflict('Issue source/config/protocol changed; original budget retained')
        batches = self.state['batches']
        if not isinstance(batches, list) or len(batches) > contract['max_batches']:
            raise StateCorrupt('Invalid original batch plan')
        keys = set()
        for index, batch in enumerate(batches):
            if (not isinstance(batch, dict) or set(batch) != {'id', 'selected', 'enriched'}
                    or batch['id'] != f'{day}:batch:{index}' or not isinstance(batch['selected'], list)
                    or not 1 <= len(batch['selected']) <= contract['batch_size']):
                raise StateCorrupt('Invalid frozen candidate batch')
            selected = [MaterialRecord.from_dict(r) for r in batch['selected']]
            batch_keys = [r.key for r in selected]
            if len(set(batch_keys)) != len(batch_keys) or keys.intersection(batch_keys):
                raise StateCorrupt('Duplicate original candidate')
            keys.update(batch_keys)
            if batch['enriched'] is not None:
                enriched = [MaterialRecord.from_dict(r) for r in batch['enriched']]
                if [r.key for r in enriched] != batch_keys:
                    raise StateCorrupt('Enrichment changed original candidate order')

    def _save(self):
        _write(self.path, self.state)

    def discovery(self):
        return deepcopy(self.state['discovery'])

    def freeze_discovery(self, value):
        if self.state['discovery'] is not None and self.state['discovery'] != value:
            raise ExecutionConflict('Discovery snapshot is immutable')
        self.state['discovery'] = deepcopy(value)
        self._save()

    def batch(self, index):
        batches = self.state['batches']
        if index < len(batches):
            return deepcopy(batches[index])
        if index != len(batches) or index >= self.state['contract']['max_batches']:
            raise ExecutionConflict('Batch index outside original finite plan')
        return None

    def freeze_batch(self, index, records):
        if self.batch(index) is not None:
            raise ExecutionConflict('Original candidate batch already exists')
        if not 1 <= len(records) <= self.state['contract']['batch_size']:
            raise ExecutionConflict('Candidate batch exceeds original limit')
        previous = {r['key'] for b in self.state['batches'] for r in b['selected']}
        keys = [r.key for r in records]
        if len(set(keys)) != len(keys) or previous.intersection(keys):
            raise ExecutionConflict('Candidate already admitted in original issue')
        self.state['batches'].append({'id': f'{self.day}:batch:{index}',
                                     'selected': [r.to_dict() for r in records], 'enriched': None})
        self._save()

    def freeze_enriched(self, index, records):
        batch = self.state['batches'][index]
        values = [r.to_dict() for r in records]
        if [r['key'] for r in values] != [r['key'] for r in batch['selected']]:
            raise ExecutionConflict('Enrichment changed candidate identity/order')
        if batch['enriched'] is not None and batch['enriched'] != values:
            raise ExecutionConflict('Enriched batch is immutable')
        batch['enriched'] = values
        self._save()


def load_plan(config, day, *, require_budget=False):
    folder = issue_dir(config, day)
    budget = read_json(config.state_dir / 'cloud-generation-budgets' / f'{day}.json')
    if require_budget:
        from daily_agent.cloud_workflow import _validate_budget
        _validate_budget(budget, day)
    protocol = budget.get('execution_protocol') if isinstance(budget, dict) else None
    if protocol is not None and protocol != PROTOCOL:
        raise ExecutionConflict('Unknown frozen issue execution protocol')
    if protocol == PROTOCOL and not folder.exists():
        raise StateCorrupt('Enrolled issue planner missing; refusing legacy budget reset')
    if require_budget and folder.exists() and protocol != PROTOCOL:
        raise StateCorrupt('Incremental planner has no matching authoritative issue budget')
    return IssuePlan(config, day) if folder.exists() else None


def assert_active_generation(config, day, *, expected_attempt=None, expected_namespace=None):
    """Only the explicitly bound supervised worker may spend an issue attempt.

    Never adopt authority from the current journal: a previous executor may
    reuse the same numeric PID/PGID. on_start follows Popen, so allow its bounded
    local publication handshake while checking the supplied fence on each poll.
    """
    from datetime import datetime
    from daily_agent.cloud_workflow import _validate_budget, _clock
    from daily_agent.workflow_runtime import process_namespace
    if (not isinstance(expected_attempt, str) or not expected_attempt
            or not isinstance(expected_namespace, str) or not expected_namespace):
        raise ExecutionConflict('Cloud generation requires an explicit supervisor attempt and namespace')
    if process_namespace() != expected_namespace:
        raise ExecutionConflict('Cloud worker execution namespace mismatch')
    until = time.monotonic() + 1.0
    while True:
        budget = read_json(config.state_dir / 'cloud-generation-budgets' / f'{day}.json')
        _validate_budget(budget, day)
        now = _clock()
        if (budget.get('active_attempt_id') != expected_attempt
                or budget.get('active_namespace') != expected_namespace
                or not budget.get('active_started_at')
                or now >= datetime.fromisoformat(budget['deadline'])
                or budget['runtime_seconds'] >= budget['max_runtime_seconds']
                or (now - datetime.fromisoformat(budget['active_started_at'])).total_seconds()
                   >= budget.get('active_timeout_seconds', 0)):
            raise ExecutionConflict('Cloud generation requires the supervisor active attempt')
        state = read_json(config.state_dir / 'cloud-generation.json')
        if (not isinstance(state, dict) or state.get('running') is not True
                or state.get('date') != str(day) or state.get('attempt_id') != expected_attempt
                or state.get('namespace') != expected_namespace):
            raise ExecutionConflict('Cloud generation is not bound to its active supervisor')
        identity = state.get('child_identity')
        if identity is not None:
            if (not isinstance(identity, dict) or identity.get('pid') != os.getpid()
                    or identity.get('pgid') != os.getpgid(0)):
                raise ExecutionConflict('Only the recorded cloud worker may execute this attempt')
            return
        if time.monotonic() >= until:
            raise ExecutionConflict('Cloud worker launch identity not published')
        time.sleep(.01)
