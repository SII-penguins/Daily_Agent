"""Public fixtures contain invented content with intact positional contracts."""
from pathlib import Path
import gzip
import hashlib
import json
import re

from daily_agent.models import MaterialRecord
from daily_agent.paper_document import valid_document, version_identity


FIXTURES = Path(__file__).parent / 'fixtures'


def load(name):
    data = (FIXTURES / name).read_bytes()
    return json.loads(gzip.decompress(data) if name.endswith('.gz') else data)


def test_public_fixture_provenance_is_explicitly_synthetic():
    for path in FIXTURES.glob('*.json*'):
        value = load(path.name)
        assert 'synthetic' in value['description'].lower()
        encoded = json.dumps(value, ensure_ascii=False)
        assert not re.search(r'/Users/|/workspace/|/home/|libfile_|codex://|chatgpt\.site|chatgpt\.com/(?:c|codex)/', encoded)
        assert not re.search(r'"(?:journal_sha256|scientific_job|rewrite_job|source_audit|source_file_sha256)"', encoded)


def test_synthetic_reading_spans_match_the_public_manifest_exactly():
    documents = load('reading_actual_documents_20261009.json.gz')['documents']
    manifests = load('reading_pack_audit_20261009.json')['documents']
    assert len(documents) == len(manifests) == 3
    for value, manifest in zip(documents, manifests):
        record = MaterialRecord.from_dict(value['record'])
        document = value['document']
        assert record.key.startswith('fixture:')
        assert document['native_document'] == record.paper_document
        assert valid_document(record.paper_document, version_identity(record))
        assert valid_document(document, version_identity(record))
        assert manifest['material_key'] == record.key
        assert manifest['expected_groups'] == value['expected_groups']
        assert len(manifest['chunks']) == len(document['chunks'])
        for chunk, span in zip(document['chunks'], manifest['chunks']):
            assert span == {'id': chunk['id'], 'page': chunk['page'],
                            'offset': chunk['offset'], 'chars': len(chunk['text']),
                            'text_sha256': hashlib.sha256(chunk['text'].encode()).hexdigest()}
