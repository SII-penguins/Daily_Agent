import httpx
import pytest
from daily_agent.delivery import feishu as f


def test_image_created_empty_then_uploaded_and_replaced(tmp_path,monkeypatch):
    image=tmp_path/'figure.png';image.write_bytes(b'png')
    calls=[]
    def post(url,**kw):
        calls.append(url)
        if url.endswith('/children'):
            assert kw['json']['children']==[{'block_type':27,'image':{}}]
            return httpx.Response(200,json={'data':{'children':[{'block_id':'img1'}]}},request=httpx.Request('POST',url))
        assert kw['data']=={'file_name':'figure.png','parent_type':'docx_image','parent_node':'img1','size':'3'}
        return httpx.Response(200,json={'data':{'file_token':'uploaded'}},request=httpx.Request('POST',url))
    def patch(url,**kw):
        assert url.endswith('/blocks/img1') and kw['json']=={'replace_image':{'token':'uploaded'}}
        calls.append(url)
        return httpx.Response(200,json={'code':0},request=httpx.Request('PATCH',url))
    monkeypatch.setattr(f.httpx,'post',post);monkeypatch.setattr(f.httpx,'patch',patch)
    blocks=f._markdown_to_blocks('![figure](figure.png)',tmp_path,'token')
    assert f._create_blocks_in_batches('token','doc','doc',blocks,30)==['img1']
    assert len(calls)==3


def test_failed_image_rolls_back_partial_batches(tmp_path,monkeypatch):
    monkeypatch.setattr(f,'_create_blocks',lambda *a:['img1'])
    def fail(*a):raise RuntimeError('upload failed')
    monkeypatch.setattr(f,'_upload_image',fail)
    removed=[];monkeypatch.setattr(f,'_delete_managed_blocks',lambda *a,**kw:removed.append(a[3]))
    with pytest.raises(RuntimeError):f._create_blocks_in_batches('token','doc','doc',[{'block_type':27,'image':{},'_image_path':str(tmp_path/'x.png')}],30)
    assert removed==[{'block_count':1,'start_index':0}]


def test_bold_is_real_rich_text():
    payload=f._text_payload('**结果/发现：**正文')
    assert payload['elements'][0]['text_run']['text_element_style']['bold']
    assert payload['elements'][0]['text_run']['content']=='结果/发现：'


def test_nonzero_api_code_is_failure():
    with pytest.raises(RuntimeError):f._raise_for_feishu_error(httpx.Response(200,json={'code':123,'msg':'failed'},request=httpx.Request('POST','https://example.org')))


def test_image_references_cannot_escape_report_directory(tmp_path):
    reports = tmp_path / 'reports'
    reports.mkdir()
    private = tmp_path / 'private.png'
    private.write_bytes(b'private')
    (reports / 'linked.png').symlink_to(private)
    for reference in ('../private.png', '%2e%2e/private.png', str(private), 'linked.png'):
        blocks = f._markdown_to_blocks(f'![figure]({reference})', reports, 'token')
        assert all('_image_path' not in block for block in blocks)


def test_image_content_invalidates_delivery_hash(tmp_path):
    image = tmp_path / 'figure.png'
    markdown = '![figure](figure.png)'
    missing = f._render_content_hash(markdown, f._markdown_to_blocks(markdown, tmp_path, 'token'))
    image.write_bytes(b'original')
    blocks = f._markdown_to_blocks(markdown, tmp_path, 'token')
    original = f._render_content_hash(markdown, blocks)
    assert original != missing
    assert f._render_content_hash(markdown, blocks) == original
    image.write_bytes(b'updated')
    assert f._render_content_hash(markdown, blocks) != original
