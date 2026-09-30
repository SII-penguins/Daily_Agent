import importlib.util
import json
from pathlib import Path
import httpx
import pytest
from test_trusted_reading import config, paper, document, responder, draft
from daily_agent.paper_document import attach_document
from daily_agent.reading import read_papers, repair_evidence
from daily_agent.connectors.openreview import fetch_openreview, OpenReviewAccessError


def test_repair_only_retrieves_read_source_and_discloses_budget(tmp_path):
    r = paper(); attach_document(r, document(r)); read_papers([r], config(tmp_path), responder)
    d = draft(r)
    omitted = r.reading['read_chunk_ids'].pop()
    value = repair_evidence(r, d, 1600)
    assert value['chunks'] and not value['complete']
    assert omitted not in [c['id'] for c in value['chunks']]
    assert sum(len(json.dumps(c, ensure_ascii=False)) for c in value['chunks']) <= 1600


def test_openreview_forbidden_is_not_successful_empty_result(tmp_path, monkeypatch):
    cfg = config(tmp_path)
    cfg.sources['openreview'] = {'enabled': True, 'venues': [{'venueid': 'ICLR.cc/2026/Conference'}]}
    monkeypatch.setattr(httpx.Client, 'get', lambda *a, **k: httpx.Response(403))
    with pytest.raises(OpenReviewAccessError, match='HTTP 403'):
        fetch_openreview(cfg)


def test_abstract_chunk_completion_cannot_pass_corpus_acceptance():
    spec = importlib.util.spec_from_file_location('audit', Path(__file__).resolve().parents[1]/'scripts/audit_evidence.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    row = {'key':'x', 'title':'x', 'item_type':'paper', 'paper_document': {'document_kind':'abstract_only'},
           'reading': {'complete':True, 'verification':{'status':'located','semantic_support':'model_checked'}}}
    result = module.audit([row])
    assert result['papers'][0]['extracted_chunks_read']
    assert result['full_text_read_count'] == 0 and not result['all_passed']
