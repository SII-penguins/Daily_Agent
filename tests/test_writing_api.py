"""Acceptance transport safety: credentials, bounded calls and snapshot reuse."""
import importlib.util
from pathlib import Path
import json
import pytest
import httpx

spec = importlib.util.spec_from_file_location('writing_api', Path(__file__).resolve().parents[1]/'scripts/validate_writing_api.py')
writing = importlib.util.module_from_spec(spec)
spec.loader.exec_module(writing)


@pytest.mark.parametrize('base,expected', [
    ('https://example.org', 'https://example.org/v1/chat/completions'),
    ('https://example.org/v1/', 'https://example.org/v1/chat/completions'),
    ('https://example.org/v1/chat/completions', 'https://example.org/v1/chat/completions'),
])
def test_credentials_accept_complete_url_and_base(tmp_path, base, expected):
    path = tmp_path/'secret.md'
    path.write_text(f'baseurl: `{base}`\napikey: "test-secret"\n')
    assert writing.credentials(path) == (expected, 'test-secret')


def test_api_cache_reuses_exact_model_prompt_and_does_not_persist_key(tmp_path, monkeypatch):
    seen = []
    def post(url, **kwargs):
        seen.append(kwargs)
        return httpx.Response(200, json={'id':'test','choices':[{'finish_reason':'stop','message':{'content':'{"ok":true}'}}], 'usage':{'total_tokens':3}})
    monkeypatch.setattr(writing.httpx, 'post', post)
    api = writing.ChatAPI('https://example.org/v1/chat/completions', 'test-secret', 'model-a', tmp_path, max_calls=1)
    assert api('prompt') == {'ok':True}
    assert api('prompt') == {'ok':True}
    assert len(seen) == 1
    assert seen[0]['headers']['Authorization'] == 'Bearer test-secret'
    assert 'test-secret' not in next(tmp_path.glob('*.json')).read_text()
    with pytest.raises(RuntimeError, match='budget exhausted'):
        api('changed prompt')


def test_api_rejects_truncation_and_sanitizes_http_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(writing.httpx, 'post', lambda *a, **k: httpx.Response(401, text='test-secret'))
    api = writing.ChatAPI('https://example.org/v1/chat/completions', 'test-secret', 'model-a', tmp_path)
    with pytest.raises(RuntimeError, match='API HTTP 401') as caught:
        api('prompt')
    assert 'test-secret' not in str(caught.value)
    monkeypatch.setattr(writing.httpx, 'post', lambda *a, **k: httpx.Response(200, json={'choices':[{'finish_reason':'length','message':{'content':'{"ok":true}'}}]}))
    with pytest.raises(ValueError, match='finish normally'):
        api('prompt')
    assert not list(tmp_path.glob('*.json'))


def test_final_assessment_cannot_pass_with_major_issues_or_missing_schema():
    assert not writing.assessment_passed({'passed':True})
    assert not writing.assessment_passed({'passed':True, 'issues':[{'severity':'major','reason':'Changed baseline'}]})
    assert not writing.assessment_passed({'passed':'true', 'issues':[]})
    assert writing.assessment_passed({'passed':True, 'issues':[]})


def test_stream_assembles_content_and_still_requires_finish(tmp_path, monkeypatch):
    from contextlib import contextmanager
    class Response:
        status_code = 200
        def iter_lines(self):
            yield ': heartbeat'
            yield 'data: '+json.dumps({'id':'stream-id','choices':[{'index':0,'delta':{'content':'{"ok":'}}]})
            yield 'data: '+json.dumps({'choices':[{'index':0,'delta':{'content':'true}'},'finish_reason':'stop'}]})
            yield 'data: [DONE]'
    @contextmanager
    def stream(*a, **kwargs):
        assert kwargs['json']['stream'] is True
        yield Response()
    monkeypatch.setattr(writing.httpx, 'stream', stream)
    api = writing.ChatAPI('https://example.org/v1/chat/completions','test-secret','model',tmp_path,stream=True)
    assert api('prompt') == {'ok': True}
    assert 'test-secret' not in next(tmp_path.glob('*.json')).read_text()


def test_image_input_hash_invalidates_cache_without_persisting_image(tmp_path, monkeypatch):
    seen = []
    def post(url, **kwargs):
        seen.append(kwargs['json']['messages'][0]['content'])
        return httpx.Response(200, json={'choices':[{'finish_reason':'stop','message':{'content':'{"ok":true}'}}]})
    monkeypatch.setattr(writing.httpx, 'post', post)
    picture = tmp_path/'page.png'; picture.write_bytes(b'image-one')
    api = writing.ChatAPI('https://example.org', 'test-secret', 'model', tmp_path/'cache', max_calls=2)
    api('check', image_path=picture); api('check', image_path=picture)
    picture.write_bytes(b'image-two'); api('check', image_path=picture)
    assert len(seen) == 2 and seen[0][1]['image_url']['detail'] == 'high'
    assert all('base64,' not in p.read_text() for p in (tmp_path/'cache').glob('*.json'))


def test_multiple_detail_images_are_sent_and_each_affects_cache(tmp_path, monkeypatch):
    seen = []
    def post(url, **kwargs):
        seen.append(kwargs['json']['messages'][0]['content'])
        return httpx.Response(200, json={'choices':[{'finish_reason':'stop','message':{'content':'{"ok":true}'}}]})
    monkeypatch.setattr(writing.httpx, 'post', post)
    page = tmp_path/'page.png'; page.write_bytes(b'page')
    detail = tmp_path/'detail.png'; detail.write_bytes(b'detail')
    api = writing.ChatAPI('https://example.org', 'test-secret', 'model', tmp_path/'cache', max_calls=2)
    api('check', image_path=[page, detail]); api('check', image_path=[page, detail])
    detail.write_bytes(b'changed detail'); api('check', image_path=[page, detail])
    assert len(seen) == 2 and len(seen[0]) == 3
    assert all('base64,' not in p.read_text() for p in (tmp_path/'cache').glob('*.json'))
