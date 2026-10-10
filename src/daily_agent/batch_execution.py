"""Opt-in, issue-scoped execution ledger with explicit caller-owned stage windows.

A single Execution owns the existing filesystem lease; worker threads share that
explicit object. Stage windows measure this process, NOT a queued parent worker.
Unknown reservations are forfeited on the next lease acquisition, never refunded.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import threading
import time
import uuid

from daily_agent.workflow_state import StateCorrupt, atomic_json, exclusive_lock, read_json

SCHEMA = 1
POOLS = ('native', 'repaired', 'visual', 'fidelity', 'primary_writer')
BLOCKED_ISSUE_STATES = {'prepared', 'sending', 'accepted', 'uncertain', 'confirmed'}


class ExecutionConflict(StateCorrupt):
    """Preserve the ledger; do not silently create a replacement budget."""


class BudgetExhausted(RuntimeError):
    pass


class WriterCircuitOpen(RuntimeError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False, separators=(',', ':')).encode()).hexdigest()


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _hash(value):
    return isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def _generation(job, generation):
    return ((job is None or (isinstance(job, str) and bool(job)))
            and (generation is None or (type(generation) is int and generation >= 0))
            and (job is not None or generation is not None))


def _seal(payload):
    return {'sha256': digest(payload), 'payload': payload}


def _read(path):
    value = read_json(path)
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) != {'sha256', 'payload'}
            or not _hash(value['sha256']) or digest(value['payload']) != value['sha256']):
        raise StateCorrupt(f'Execution integrity mismatch: {path}')
    return value['payload']


def candidate(record):
    """Freeze enriched input bytes, version, aliases, and actual topology."""
    from daily_agent.paper_document import version_identity
    value = record.to_dict()
    doc = record.paper_document
    return {'key': record.key, 'version': version_identity(record),
            'aliases': deepcopy(record.source_aliases), 'kind': record.item_type,
            'input_sha256': digest(value), 'input': value,
            'chunks': [c['id'] for c in doc.get('chunks', [])],
            'pages': [p['page'] for p in doc.get('pages', [])],
            'required_pages': [p['page'] for p in doc.get('pages', []) if p.get('visual_required')]}


def _contract(candidates, config, protocol):
    if not isinstance(protocol, str) or not protocol:
        raise ExecutionConflict('An explicit execution protocol is required')
    from daily_agent.source_evidence_policy import configured_policy
    configured_policy(config)  # Unknown modes cannot create a fresh ledger.
    reading = config.sources['reading']
    writer = config.sources['llm_writer']
    budgets = {'native': reading['run_budget_seconds'], 'repaired': reading['run_budget_seconds'],
               'visual': reading['visual_budget_seconds'], 'fidelity': reading['fidelity_budget_seconds'],
               'primary_writer': writer['run_budget_seconds']}
    if not all(_number(v) for v in budgets.values()):
        raise ExecutionConflict('Invalid configured budget')
    limit = reading['max_chunks_per_paper']
    if type(limit) is not int or limit < 0:
        raise ExecutionConflict('Invalid chunk limit')
    author = config.sources.get('author_context', {}) or {}
    if not isinstance(author, dict):
        raise ExecutionConflict('Invalid author settings')
    author_limit = author.get('max_research_papers_per_batch', 12)
    if type(author_limit) is not int:
        raise ExecutionConflict('Invalid author research limit')
    frozen = deepcopy(candidates)
    keys = []
    for c in frozen:
        if (not isinstance(c, dict) or not isinstance(c.get('key'), str) or not c['key']
                or c.get('kind') not in {'paper', 'repo'} or not c.get('version')
                or not isinstance(c.get('aliases'), (dict, list)) or not _hash(c.get('input_sha256'))
                or digest(c.get('input')) != c['input_sha256']):
            raise ExecutionConflict('Invalid frozen candidate')
        for name in ('chunks', 'pages', 'required_pages'):
            if not isinstance(c.get(name), list) or len(set(c[name])) != len(c[name]):
                raise ExecutionConflict('Invalid frozen topology')
        if not set(c['required_pages']) <= set(c['pages']):
            raise ExecutionConflict('Required page outside source')
        keys.append(c['key'])
    if len(set(keys)) != len(keys):
        raise ExecutionConflict('Duplicate candidate key')
    settings = {'reading': deepcopy(reading), 'llm_writer': deepcopy(writer),
                'author_context': deepcopy(author)}
    if configured_policy(config) == 'native_claim_evidence_v1':
        from daily_agent.source_evidence_policy import native_qualification_contract
        settings['native_qualification_contract'] = native_qualification_contract()
    return {'protocol': protocol, 'candidates': frozen, 'budgets': budgets, 'chunk_limit': limit,
            'settings': settings}


def _author_keys(contract):
    options = contract['settings'].get('author_context', {})
    limit = min(12, max(0, options.get('max_research_papers_per_batch', 12)))
    return [c['key'] for c in contract['candidates'] if c['kind'] == 'paper'][:limit]


def _slots(contract, repaired_bindings=None, author_candidates=None):
    """Finite slots; changing input/job/generation cannot manufacture a new slot."""
    slots = {}
    def add(key, phase, substep, count):
        for ordinal in range(count):
            identity = [key, phase, substep, ordinal]
            slots[digest(identity)] = identity
    for c in contract['candidates']:
        key = c['key']
        if c['kind'] == 'paper':
            native_policy = (contract['settings']['reading'].get('source_evidence_policy') == 'native_claim_evidence_v1'
                             and c['input'].get('paper_document', {}).get('source_type') == 'pdf')
            for phase in (('native',) if native_policy else ('native', 'repaired')):
                binding = (repaired_bindings or {}).get(key) if phase == 'repaired' else None
                chunks = binding['chunks'] if binding else c['chunks']
                for chunk in chunks[:contract['chunk_limit']]:
                    add(key, phase, 'chunk:' + str(chunk), 2)
            for page in ([] if native_policy else c['required_pages']):
                add(key, 'visual', 'page:' + str(page), 1)
            for page in ([] if native_policy else c['pages']):
                for role in ('transcribe', 'review'):
                    add(key, 'fidelity', role + ':' + str(page), 2)
            counts = {'draft': 1, 'rewrite': 1, 'semantic': 2, 'presentation': 1,
                      'scientific_writer': 1, 'scientific_review': 1,
                      'author_research': 1, 'author_review': 1}
            if native_policy:
                counts['native_visual_selection'] = 1
                # One material-local numeric-locator correction. The original
                # primary_writer pool/deadline and exact-input slots still bind.
                counts['scientific_writer'] = 2
                counts['semantic_overflow'] = 2
        else:
            # Repository primary work has its own material-bound slot.
            counts = {'draft': 1}
        for substep, count in counts.items():
            if (substep in {'author_research', 'author_review'}
                    and key not in (_author_keys(contract) if author_candidates is None else author_candidates)):
                continue
            add(key, 'primary_writer', substep, count)
    return slots


def _lineage(frozen, record, contract=None):
    """Only declared downstream output fields may differ from enriched input."""
    if not isinstance(record, dict) or not isinstance(frozen['input'], dict):
        return False
    original = frozen['input']
    mutable = {'reading', 'paper_document', 'paper_text_excerpt', 'paper_text_status',
               'detail', 'quality_status', 'raw'}
    if any(record.get(k) != value for k, value in original.items() if k not in mutable):
        return False
    if record.get('key') != frozen['key']:
        return False
    raw_before, raw_after = original.get('raw', {}), record.get('raw', {})
    if not isinstance(raw_before, dict) or not isinstance(raw_after, dict):
        return False
    author_fields = {'research_context', 'author_research_sources', 'author_research_evidence'}
    native_mode = ((contract or {}).get('settings', {}).get('reading', {}).get('source_evidence_policy')
                   == 'native_claim_evidence_v1'
                   and original.get('paper_document', {}).get('source_type') == 'pdf')
    if native_mode and raw_after.get('paper_visual_selection') != raw_before.get('paper_visual_selection'):
        from daily_agent.native_visual_evidence import verified_native_visual_evidence
        from daily_agent.models import MaterialRecord
        try:
            if not verified_native_visual_evidence(MaterialRecord.from_dict(record)):
                return False
        except (ValueError, TypeError, KeyError, OSError, AttributeError):
            return False
        author_fields = author_fields | {'paper_visual_selection'}
    if ({k: v for k, v in raw_before.items() if k not in author_fields}
            != {k: v for k, v in raw_after.items() if k not in author_fields}):
        return False
    before, after = original.get('paper_document', {}), record.get('paper_document', {})
    if after == before:
        return True
    # Existing visual_fidelity explicitly retains the original native document.
    return (isinstance(after, dict) and after.get('evidence_basis') == 'image_transcription_reviewed'
            and after.get('native_document') == before)


def _repaired_binding(frozen, document):
    """Derive finite topology from the actual reviewed document, not its labels."""
    native = frozen['input'].get('paper_document')
    if (isinstance(native, dict) and native.get('evidence_basis') == 'image_transcription_reviewed'
            and isinstance(native.get('native_document'), dict)):
        if document != native:
            raise ExecutionConflict('Frozen reviewed input cannot be replaced with another derivative')
        native = native['native_document']
    if (frozen['kind'] != 'paper' or not isinstance(native, dict)
            or not isinstance(document, dict)
            or document.get('evidence_basis') != 'image_transcription_reviewed'
            or document.get('native_document') != native):
        raise ExecutionConflict('Repaired document must preserve exact frozen native lineage')
    chunks = document.get('chunks')
    if (not isinstance(chunks, list) or not chunks
            or any(not isinstance(c, dict) or not isinstance(c.get('id'), str)
                   or not c['id'] for c in chunks)):
        raise ExecutionConflict('Invalid repaired chunk topology')
    ids = [c['id'] for c in chunks]
    if len(set(ids)) != len(ids):
        raise ExecutionConflict('Duplicate repaired chunk identity')
    return {'document': deepcopy(document), 'document_sha256': digest(document), 'chunks': ids}


def _validate_author_candidates(contract, keys):
    if keys is None:
        return
    papers = [c['key'] for c in contract['candidates'] if c['kind'] == 'paper']
    if (not isinstance(keys, list) or any(not isinstance(k, str) or k not in papers for k in keys)
            or len(set(keys)) != len(keys) or len(keys) > len(_author_keys(contract))
            or [papers.index(k) for k in keys] != sorted(papers.index(k) for k in keys)):
        raise ExecutionConflict('Author candidates must be an ordered finite original-paper subset')


class Execution:
    """Use with ``with Execution(issue_dir, original_batch_id, ...) as execution``.

    original_batch_id must be saved by the issue planner, not regenerated on
    resume. Configuration and source hashes never select a new ledger path.
    """
    def __init__(self, issue_dir, batch_id, candidates, config, *, protocol,
                 issue_state='new', clock=time.monotonic):
        if issue_state in BLOCKED_ISSUE_STATES:
            raise ExecutionConflict('Publication-owned issue cannot be rerun')
        if not isinstance(batch_id, str) or not batch_id:
            raise ExecutionConflict('Original batch identity required')
        self.folder = Path(issue_dir) / 'batch-execution' / digest(batch_id)
        self.path = self.folder / 'journal.json'
        self.contract = _contract(candidates, config, protocol)
        self.batch_id = batch_id
        self.clock = clock
        self._mutex = threading.RLock()
        self._windows = {}
        self._lease = None
        self._state = None
        self._poisoned = False

    def __enter__(self):
        from daily_agent.workflow_state import assert_mutation_allowed, assert_scope_admission
        assert_scope_admission(self.folder)
        assert_mutation_allowed(self.folder)
        with self._mutex:
            if self._lease is not None:
                raise ExecutionConflict('Execution already owns a lease')
            lease = exclusive_lock(self.folder / 'execution.lock')
            lease.__enter__()
            try:
                marker = _read(self.folder / 'initialized.json')
                expected_marker = {'batch_id': self.batch_id, 'contract_sha256': digest(self.contract)}
                if marker is not None and marker != expected_marker:
                    raise ExecutionConflict('Initialized batch identity conflict')
                state = _read(self.path)
                if state is None and marker is not None:
                    raise StateCorrupt('Initialized batch journal missing; refusing budget reset')
                if state is None:
                    state = {'schema': SCHEMA, 'batch_id': self.batch_id, 'contract': self.contract,
                             'pools': {p: {'limit': b, 'remaining': b, 'measured': 0, 'charged': 0,
                                          'forfeited': 0} for p, b in self.contract['budgets'].items()},
                             'reservations': {}, 'settlements': {}, 'operations': {},
                             'writer_circuit': None, 'completed': {}, 'repaired_bindings': {},
                             'author_candidates': None}
                self._validate(state)
                if state['batch_id'] != self.batch_id or state['contract'] != self.contract:
                    raise ExecutionConflict('Frozen batch/input/protocol/config conflict')
                self._state = state
                self._poisoned = False
                self._lease = lease
                for material, sha in state['completed'].items():
                    self._completion(material, sha)
                # Holding the nonblocking process lease proves previous owner is gone.
                for token in list(state['reservations']):
                    self._settle(token, None)
                self._save()
                if marker is None:
                    atomic_json(self.folder / 'initialized.json', _seal(expected_marker))
                return self
            except BaseException:
                self._state = self._lease = None
                lease.__exit__(None, None, None)
                raise

    def __exit__(self, *exc):
        with self._mutex:
            if self._windows:
                raise ExecutionConflict('Join all stage workers before closing Execution')
            lease, self._lease = self._lease, None
            self._state = None
            if lease is not None:
                lease.__exit__(*exc)

    def _validate(self, s):
        try:
            if set(s) - {'repaired_bindings', 'author_candidates'} != {'schema', 'batch_id', 'contract', 'pools', 'reservations', 'settlements',
                          'operations', 'writer_circuit', 'completed'} or type(s['schema']) is not int or s['schema'] != SCHEMA:
                raise ValueError('schema')
            settings = s['contract']['settings']
            if settings['reading'].get('source_evidence_policy') == 'native_claim_evidence_v1':
                if settings['reading'].get('require_scientific_analysis', True) is not True:
                    raise ValueError('native frozen reading qualification setting')
                from daily_agent.source_evidence_policy import valid_native_qualification_contract
                if not valid_native_qualification_contract(settings.get('native_qualification_contract')):
                    raise ValueError('native qualification contract')
            if set(s['pools']) != set(POOLS):
                raise ValueError('pools')
            for name in ('reservations', 'settlements', 'operations', 'completed'):
                if not isinstance(s[name], dict):
                    raise ValueError(name)
            for p, b in s['pools'].items():
                if (set(b) != {'limit', 'remaining', 'measured', 'charged', 'forfeited'}
                        or not all(_number(v) for v in b.values())
                        or b['limit'] != s['contract']['budgets'][p]):
                    raise ValueError('budget')
                held = sum(r['amount'] for r in s['reservations'].values() if r['pool'] == p)
                if not math.isclose(b['remaining'] + b['charged'] + b['forfeited'] + held,
                                    b['limit'], abs_tol=1e-8):
                    raise ValueError('budget conservation')
            for token, r in s['reservations'].items():
                if set(r) != {'pool', 'amount'} or r['pool'] not in POOLS or not _number(r['amount']):
                    raise ValueError('reservation')
                if token in s['settlements']:
                    raise ValueError('already settled')
            for event in s['settlements'].values():
                if (set(event) != {'pool', 'reserved', 'measured', 'charged', 'forfeited'}
                        or event['pool'] not in POOLS
                        or not all(_number(event[k]) for k in ('reserved', 'charged', 'forfeited'))
                        or (event['measured'] is not None and not _number(event['measured']))):
                    raise ValueError('settlement')
                expected_charge = 0 if event['measured'] is None else min(event['reserved'], event['measured'])
                expected_forfeit = event['reserved'] if event['measured'] is None else 0
                if event['charged'] != expected_charge or event['forfeited'] != expected_forfeit:
                    raise ValueError('settlement semantics')
            for pool, budget in s['pools'].items():
                events = [e for e in s['settlements'].values() if e['pool'] == pool]
                for field in ('charged', 'forfeited', 'measured'):
                    if not math.isclose(budget[field], sum(e[field] or 0 for e in events), abs_tol=1e-8):
                        raise ValueError('settlement totals')
            bindings = s.get('repaired_bindings', {})
            if not isinstance(bindings, dict):
                raise ValueError('repaired bindings')
            frozen_by_key = {c['key']: c for c in s['contract']['candidates']}
            for key, binding in bindings.items():
                if (key not in frozen_by_key or not isinstance(binding, dict)
                        or binding != _repaired_binding(frozen_by_key[key], binding.get('document'))):
                    raise ValueError('repaired binding')
            _validate_author_candidates(s['contract'], s.get('author_candidates'))
            allowed = _slots(s['contract'], bindings, s.get('author_candidates'))
            for slot, op in s['operations'].items():
                if (set(op) != {'identity', 'protocol', 'input_sha256', 'queue_job_id', 'retry_generation', 'queue_role'}
                        or slot not in allowed or op['identity'] != allowed[slot]
                        or op['protocol'] != s['contract']['protocol']
                        or not _hash(op['input_sha256'])
                        or not _generation(op['queue_job_id'], op['retry_generation'])):
                    raise ValueError('operation')
            circuit = s['writer_circuit']
            if circuit is not None and (not isinstance(circuit, dict)
                    or set(circuit) != {'kind', 'detail'}
                    or circuit.get('kind') not in {'backend', 'json', 'schema'}
                    or not isinstance(circuit['detail'], str)):
                raise ValueError('circuit')
            keys = {c['key'] for c in s['contract']['candidates']}
            if any(k not in keys or not _hash(v) for k, v in s['completed'].items()):
                raise ValueError('completion')
        except (ValueError, TypeError, KeyError, AttributeError, ExecutionConflict) as exc:
            raise StateCorrupt('Invalid execution journal; preserved without reset') from exc

    def _save(self):
        if self._poisoned:
            raise ExecutionConflict('Uncertain journal write; close and reopen before mutation')
        try:
            self._validate(self._state)
            atomic_json(self.path, _seal(self._state))
        except BaseException:
            self._poisoned = True
            raise

    def _require(self):
        if self._lease is None or self._poisoned:
            raise ExecutionConflict('Execution lease absent or write outcome uncertain; reopen required')

    def snapshot(self):
        with self._mutex:
            self._require()
            return deepcopy(self._state)

    def remaining(self, pool):
        """Actual unspent local time, including a currently reserved stage."""
        with self._mutex:
            self._require()
            if pool not in POOLS:
                raise ExecutionConflict('Unknown pool')
            window = self._windows.get(pool)
            if window is None:
                return self._state['pools'][pool]['remaining']
            now = self.clock()
            if not _number(now) or now < window['started']:
                raise ExecutionConflict('Invalid monotonic clock')
            return max(0, window['deadline'] - now)

    def author_allowed(self, material):
        """The original ordered batch, never a resumed subset, owns this cap."""
        with self._mutex:
            self._require()
            return material in (self._state.get('author_candidates') or [])

    def author_candidates(self):
        with self._mutex:
            self._require()
            return deepcopy(self._state.get('author_candidates'))

    def bind_author_candidates(self, keys):
        """Freeze the existing eligibility selection once for the entire batch."""
        with self._mutex:
            self._require()
            if keys is None:
                raise ExecutionConflict('Explicit author candidate list required')
            _validate_author_candidates(self.contract, keys)
            old = self._state.get('author_candidates')
            if old is not None:
                if old != keys:
                    raise ExecutionConflict('Original batch author candidates are already bound')
                return deepcopy(old)
            if any(op['identity'][1] == 'primary_writer'
                   and op['identity'][2] in {'author_research', 'author_review'}
                   for op in self._state['operations'].values()):
                raise ExecutionConflict('Cannot replace already admitted author topology')
            self._state['author_candidates'] = deepcopy(keys)
            self._save()
            return deepcopy(keys)

    def repaired_binding(self, material):
        with self._mutex:
            self._require()
            return deepcopy(self._state.get('repaired_bindings', {}).get(material))

    def bind_repaired(self, record):
        """Bind one reviewed derivative without changing initialized inputs/budgets.

        Call after existing fidelity validation and before repaired admissions.
        This freezes evidence; it does not itself grant editorial approval.
        """
        with self._mutex:
            self._require()
            value = record.to_dict() if hasattr(record, 'to_dict') else record
            if not isinstance(value, dict):
                raise ExecutionConflict('Invalid repaired record')
            frozen = next((c for c in self.contract['candidates'] if c['key'] == value.get('key')), None)
            if frozen is None or not _lineage(frozen, value, self.contract):
                raise ExecutionConflict('Repaired record changed frozen input lineage')
            binding = _repaired_binding(frozen, value.get('paper_document'))
            bindings = self._state.get('repaired_bindings', {})
            old = bindings.get(frozen['key'])
            if old is not None:
                if old != binding:
                    raise ExecutionConflict('Reviewed repaired document is already bound')
                return deepcopy(old)
            if any(op['identity'][:2] == [frozen['key'], 'repaired']
                   for op in self._state['operations'].values()):
                raise ExecutionConflict('Cannot replace already admitted repaired topology')
            completed = self._state['completed'].get(frozen['key'])
            if (completed is not None
                    and self._completion(frozen['key'], completed)['record'].get('paper_document') != binding['document']):
                raise ExecutionConflict('Repaired binding cannot replace completed evidence')
            self._state.setdefault('repaired_bindings', {})[frozen['key']] = binding
            self._save()
            return deepcopy(binding)

    def _settle(self, token, elapsed):
        self._require()
        if token in self._state['settlements']:
            return deepcopy(self._state['settlements'][token])
        r = self._state['reservations'][token]
        if elapsed is not None and not _number(elapsed):
            raise ExecutionConflict('Invalid monotonic elapsed time; reservation retained')
        b = self._state['pools'][r['pool']]
        charged = 0 if elapsed is None else min(elapsed, r['amount'])
        forfeited = r['amount'] if elapsed is None else 0
        b['remaining'] += r['amount'] - charged - forfeited
        b['charged'] += charged
        b['forfeited'] += forfeited
        b['measured'] += 0 if elapsed is None else elapsed
        event = {'pool': r['pool'], 'reserved': r['amount'], 'measured': elapsed,
                 'charged': charged, 'forfeited': forfeited}
        self._state['settlements'][token] = event
        del self._state['reservations'][token]
        self._save()
        return deepcopy(event)

    def settlement(self, token):
        """Idempotent read of the already-durable event; never issues a refund."""
        with self._mutex:
            self._require()
            return deepcopy(self._state['settlements'].get(token))

    @contextmanager
    def stage(self, pool):
        """Overlapping windows in one pool charge their union, including BaseException exits.

        Enclose only local stage execution. Exit before exit-75/parent waiting.
        The yielded reservation includes a local monotonic deadline; callers must
        enforce it on transports. This ledger cannot interrupt an external model.
        """
        with self._mutex:
            self._require()
            if pool not in POOLS:
                raise ExecutionConflict('Unknown pool')
            if pool == 'primary_writer' and self._state['writer_circuit'] is not None:
                raise WriterCircuitOpen('Original batch writer circuit is open')
            window = self._windows.get(pool)
            if window is None:
                amount = self._state['pools'][pool]['remaining']
                if amount <= 0:
                    raise BudgetExhausted(pool)
                token = uuid.uuid4().hex
                started = self.clock()
                if not _number(started):
                    raise ExecutionConflict('Invalid clock')
                self._state['pools'][pool]['remaining'] = 0
                self._state['reservations'][token] = {'pool': pool, 'amount': amount}
                self._save()  # Entire remainder is reserved before any operation runs.
                window = {'token': token, 'started': started, 'deadline': started + amount, 'users': 0}
                self._windows[pool] = window
            window['users'] += 1
            info = {k: window[k] for k in ('token', 'deadline')}
        try:
            yield info
        finally:
            with self._mutex:
                window['users'] -= 1
                if window['users'] == 0:
                    del self._windows[pool]
                    # Preserve the original write error and leave its reservation
                    # unresolved for conservative recovery on a new lease.
                    if not self._poisoned:
                        self._settle(window['token'], self.clock() - window['started'])

    def admit(self, material, phase, substep, ordinal, exact_input, *, queue_job_id=None,
              retry_generation=None, queue_role=None):
        """Preserve the original single-slot API and exact admission identity."""
        return self.admit_many([(material, phase, substep, ordinal)], exact_input,
            queue_job_id=queue_job_id, retry_generation=retry_generation, queue_role=queue_role)[0]

    def existing_operation(self, operation):
        """Read-only exact slot lookup; absence grants no operation admission."""
        with self._mutex:
            self._require()
            return deepcopy(self._state['operations'].get(digest(list(operation))))

    def admit_many(self, operations, exact_input, *, queue_job_id=None,
                   retry_generation=None, queue_role=None):
        """Atomically bind up to four original reading slots to one actual job.

        Every member retains its own frozen ordinal. Preflight all members before
        one write; a conflicting, duplicate or new member cannot extend a bound
        group. Single-slot callers retain the original stage behavior.
        """
        with self._mutex:
            self._require()
            if (not isinstance(operations, (list, tuple)) or not 1 <= len(operations) <= 4
                    or any(not isinstance(op, (list, tuple)) or len(op) != 4
                           or any(not isinstance(v, str) or not v for v in op[:3])
                           or type(op[3]) is not int for op in operations)):
                raise ExecutionConflict('One to four explicit finite operations required')
            identities = [list(op) for op in operations]
            material, phase = identities[0][:2]
            if any(op[:2] != [material, phase] for op in identities):
                raise ExecutionConflict('A grouped job must retain one material and phase')
            if len(identities) > 1 and phase not in {'native', 'repaired'}:
                raise ExecutionConflict('Only reading chunks support grouped transport')
            if phase not in self._windows:
                raise ExecutionConflict('Admission requires a reserved active stage')
            if phase == 'primary_writer' and self._state['writer_circuit'] is not None:
                raise WriterCircuitOpen('Original batch writer circuit is open')
            if not _generation(queue_job_id, retry_generation):
                raise ExecutionConflict('Stable queue job or explicit retry generation required')
            if len(identities) > 1 and not queue_job_id:
                raise ExecutionConflict('Grouped transport requires an actual queue job')
            if self.remaining(phase) <= 0:
                raise BudgetExhausted('Local stage deadline exhausted')
            available = _slots(self.contract, self._state.get('repaired_bindings'),
                               self._state.get('author_candidates'))
            if len(identities) > 1:
                if len({tuple(op[:3]) for op in identities}) != len(identities):
                    raise ExecutionConflict('A chunk cannot occupy multiple ordinals in one group')
                topology_order = {slot: index for index, slot in enumerate(available)}
                positions = [topology_order.get(digest(op), -1) for op in identities]
                if all(index >= 0 for index in positions) and positions != sorted(positions):
                    raise ExecutionConflict('Grouped operations must preserve frozen topology order')
            proposed, existing = {}, []
            for identity in identities:
                slot = digest(identity)
                if slot in proposed:
                    raise ExecutionConflict('Duplicate grouped operation')
                if slot not in available:
                    raise BudgetExhausted('Operation slot outside frozen topology')
                op = {'identity': identity, 'protocol': self.contract['protocol'],
                      'input_sha256': digest(exact_input), 'queue_job_id': queue_job_id,
                      'retry_generation': retry_generation, 'queue_role': queue_role}
                previous = self._state['operations'].get(slot)
                if previous is not None and previous != op:
                    raise ExecutionConflict('Existing operation input/job/protocol conflict')
                existing.append(previous is not None)
                proposed[slot] = op
            if queue_job_id is not None and phase in {'native', 'repaired'}:
                bound = {slot for slot, op in self._state['operations'].items()
                         if op['queue_job_id'] == queue_job_id and op['identity'][:2] == [material, phase]}
                if bound and bound != set(proposed):
                    raise ExecutionConflict('A bound group cannot acquire new members or lose existing members')
            if len(identities) > 1 and any(existing) and not all(existing):
                raise ExecutionConflict('A bound group cannot acquire new members')
            self._state['operations'].update(proposed)
            self._save()
            return tuple(proposed)

    def writer_failure(self, kind, detail=''):
        """Call only for an actual backend/JSON/schema failure, never PendingResponse."""
        if kind not in {'backend', 'json', 'schema'}:
            raise ExecutionConflict('Not a writer circuit failure')
        with self._mutex:
            self._require()
            if self._state['writer_circuit'] is None:
                self._state['writer_circuit'] = {'kind': kind, 'detail': str(detail)}
                self._save()

    def prepare_completion(self, material, *, record, draft, review, science, evidence, asset_hashes):
        """Persist all result bytes first. This alone never means completed/qualified."""
        with self._mutex:
            self._require()
            frozen = next((c for c in self.contract['candidates'] if c['key'] == material), None)
            if frozen is None or not _lineage(frozen, record, self.contract) or not isinstance(asset_hashes, dict) or not all(_hash(v) for v in asset_hashes.values()):
                raise ExecutionConflict('Unknown material or invalid asset manifest')
            binding = self._state.get('repaired_bindings', {}).get(material)
            if binding is not None and record.get('paper_document') != binding['document']:
                raise ExecutionConflict('Completion changed bound repaired evidence')
            payload = {'schema': SCHEMA, 'batch_id': self.batch_id, 'material': material,
                       'input_sha256': frozen['input_sha256'], 'protocol': self.contract['protocol'],
                       'record': record, 'draft': draft, 'review': review, 'science': science,
                       'evidence': evidence, 'asset_hashes': asset_hashes}
            sha = digest(payload)
            path = self.folder / 'envelopes' / (sha + '.json')
            old = _read(path)
            if old is not None and old != payload:
                raise StateCorrupt('Immutable completion collision')
            if old is None:
                atomic_json(path, _seal(payload))
            return sha

    def commit_completion(self, material, sha, *, validator):
        """Publish a journal reference only after existing review/asset validation.

        validator is a mandatory integration adapter to EXISTING independent
        review contracts, receiving a copy of the complete envelope. No status
        string (including PASS) is interpreted as acceptance by this module.
        """
        with self._mutex:
            self._require()
            payload = self._completion(material, sha)
            if validator(deepcopy(payload)) is not True:
                raise ExecutionConflict('Existing independent review contract rejected completion')
            previous = self._state['completed'].get(material)
            if previous is not None and previous != sha:
                raise ExecutionConflict('Completion is immutable')
            self._state['completed'][material] = sha
            self._save()

    def _completion(self, material, sha):
        if not _hash(sha):
            raise StateCorrupt('Invalid completion hash')
        payload = _read(self.folder / 'envelopes' / (sha + '.json'))
        frozen = next((c for c in self.contract['candidates'] if c['key'] == material), None)
        binding = self._state.get('repaired_bindings', {}).get(material)
        if (not isinstance(payload, dict) or digest(payload) != sha or frozen is None
                or set(payload) != {'schema', 'batch_id', 'material', 'input_sha256', 'protocol',
                                    'record', 'draft', 'review', 'science', 'evidence', 'asset_hashes'}
                or type(payload['schema']) is not int or payload['schema'] != SCHEMA or payload['batch_id'] != self.batch_id
                or payload['material'] != material or payload['input_sha256'] != frozen['input_sha256']
                or payload['protocol'] != self.contract['protocol']
                or not _lineage(frozen, payload['record'], self.contract)
                or (binding is not None and payload['record'].get('paper_document') != binding['document'])
                or not isinstance(payload['asset_hashes'], dict)
                or not all(_hash(v) for v in payload['asset_hashes'].values())):
            raise StateCorrupt('Completion envelope conflict or missing bytes')
        return payload

    def completed(self, material, *, validator):
        """Revalidate referenced results; orphan envelopes are never auto-promoted."""
        with self._mutex:
            self._require()
            sha = self._state['completed'].get(material)
            if sha is None:
                return None
            payload = self._completion(material, sha)
            if validator(deepcopy(payload)) is not True:
                raise StateCorrupt('Completion no longer satisfies existing review contract')
            return deepcopy(payload)


def existing_review_validator(config):
    """Read-only adapter for existing base qualification; no model or repairs.

    Native daily-selection contracts require independently validated scientific
    analysis. CORE-only work remains reusable in the separate draft cache.
    Historical strict contracts retain their existing acceptance behavior.
    """
    def validate(envelope):
        from daily_agent.deferred_review_cache import _assets, _supported
        from daily_agent.editorial import review_draft
        from daily_agent.models import EditorialDraft, EditorialReview, MaterialRecord
        try:
            record = MaterialRecord.from_dict(envelope['record'])
            draft = EditorialDraft.from_dict(envelope['draft'])
            review = EditorialReview.from_dict(envelope['review'])
            if (not (envelope['material'] == record.key == draft.key == review.key)
                    or record.item_type != draft.item_type or review.verdict != 'PASS'):
                return False
            if record.item_type == 'paper' and not _supported(record, draft, config=config):
                return False
            if record.item_type not in {'paper', 'repo'}:
                return False
            if review_draft(config, [draft], use_llm=False)[0].verdict != 'PASS':
                return False
            assets = _assets(config, record, save=False)
            return assets is not None and assets == envelope['asset_hashes']
        except (KeyError, ValueError, TypeError, AttributeError, IndexError, OSError):
            return False
    return validate
