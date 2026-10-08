"""Content-based diversity within the bidirectional quantum/AI research stream.

Classification is a selection aid, not publication or scientific-claim evidence.
Discovery query tags are deliberately not used as proof of a direction.
"""
from __future__ import annotations

import re
from collections import defaultdict, deque

AI_SUBTOPICS = {
    'control_calibration': ('control', 'calibrat', 'pulse', 'feedback'),
    'error_correction': ('error correction', 'error mitigation', 'decoder', 'surface code', 'qec', 'fault toleran'),
    'experiments': ('experiment', 'tomograph', 'processor', 'hardware', 'qubit'),
    'materials_states': ('material', 'quantum state', 'many body', 'many-body', 'phase', 'hamiltonian learning'),
    'compilation': ('compil', 'synthes', 'routing', 'transpil', 'gate decomposition'),
}
QUANTUM_FOR_AI = ('quantum machine learning', 'quantum neural', 'quantum kernel', 'quantum generative',
    'quantum learning', 'quantum classifier', 'quantum classification', 'quantum reinforcement learning',
    'quantum reservoir', 'quantum transformer', 'quantum diffusion', 'quantum boltzmann',
    'quantum born machine', 'quantum support vector', 'quantum convolution', 'barren plateau',
    'quantum generalization', 'quantum trainability', 'quantum circuit learning', 'qnn')


def qas_qnas_subtopic(record):
    """User's organizational umbrella, not an assertion of academic equivalence."""
    text = ' '.join(str(getattr(record, field, '') or '') for field in ('title', 'abstract', 'repo_description')).casefold()
    for name, pattern in (
        ('quantum_neural_architecture_search', r'quantum neural architecture|\bqnas\b'),
        ('quantum_architecture_search', r'quantum architecture search|\bqas\b'),
        ('compilation_synthesis', r'quantum compil|quantum circuit synthes|quantum transp|qubit rout|unitary synthes|hardware.aware.*compil'),
        ('circuit_generation_design', r'quantum circuit generat|quantum circuit design')):
        if re.search(pattern, text):
            return name
    return None


def quantum_topic(record):
    text = ' '.join(str(getattr(record, field, '') or '') for field in ('title', 'abstract', 'repo_description')).casefold()
    text = re.sub(r'\s+', ' ', text.replace('-', ' '))
    if any(term in text for term in QUANTUM_FOR_AI):
        return 'quantum_for_ai', 'learning_models_theory_benchmarks'
    if getattr(record, 'quota_group', None) != 'quantum' and not re.search(r'quantum|qubit|\bqec\b', text):
        return None, None
    for topic, terms in AI_SUBTOPICS.items():
        if any(term in text for term in terms):
            return 'ai_for_quantum', topic
    return 'ai_for_quantum', 'learning_dynamics'


def balanced_quantum_order(records, config):
    """Reorder quantum slots round-robin; preserve nonquantum score positions.

    Quality/evidence gates run separately. Empty directions do not prevent filling
    the issue, and each AI-for-quantum subtopic gets a turn before repeats.
    """
    if not config.sources.get('selection', {}).get('balance_quantum_directions', False):
        return list(records)
    queues = {'ai_for_quantum': defaultdict(deque), 'quantum_for_ai': defaultdict(deque)}
    quantum_slots = []
    for index, record in enumerate(records):
        direction, topic = quantum_topic(record)
        if getattr(record, 'quota_group', None) == 'quantum' and direction:
            queues[direction][topic].append(record)
            quantum_slots.append(index)
            record.raw['quantum_direction'] = direction
            record.raw['quantum_subtopic'] = topic
    active = {key: deque(value) for key, value in queues.items()}
    ordered = []
    while any(active.values()):
        for direction in queues:
            if not active[direction]:
                continue
            topic = active[direction].popleft()
            ordered.append(queues[direction][topic].popleft())
            if queues[direction][topic]:
                active[direction].append(topic)
    result = list(records)
    for index, record in zip(quantum_slots, ordered):
        result[index] = record
    # Keep a meaningful 2–3/8-paper QAS/QNAS presence when supported candidates
    # exist. This is a soft ordering preference, never a minimum evidence waiver.
    target = max(0, int(config.sources.get('selection', {}).get('qas_qnas_soft_target', 2)))
    umbrella = []
    for record in result:
        subtopic = qas_qnas_subtopic(record)
        if subtopic:
            record.raw['personal_topic_umbrella'] = {'name': 'QAS/QNAS', 'subtopic': subtopic,
                                                   'basis': 'user_organizational_grouping'}
        if record.item_type == 'paper' and subtopic:
            umbrella.append(record)
    head = umbrella[:target]
    head_keys = {record.key if hasattr(record, 'key') else record.canonical_key() for record in head}
    return head + [record for record in result if (record.key if hasattr(record, 'key') else record.canonical_key()) not in head_keys]
