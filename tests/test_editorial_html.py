from datetime import date
from html import escape
from html.parser import HTMLParser
import pytest
from daily_agent.models import ApprovedItem, MaterialRecord
from daily_agent.rendering.editorial import render_editorial_html, link


def item(kind='paper', title='Test <script>alert(1)</script>'):
    material = MaterialRecord(key=kind, source='PMLR', item_type=kind, title=title, url='https://example.org/paper', pdf_url='https://example.org/paper.pdf')
    return ApprovedItem(key=kind,item_type=kind,title=title,source='PMLR',url=material.url,material=material,final_fields={
        'problem':'完整问题','method':'完整方法','key_result':'有条件的结果','limitations':'仿真限制',
        'maturity_signal':'只审阅 README，未安装运行','what_it_is':'开源库','core_capabilities':'代码能力',
        'method_steps':['方法步骤一','方法步骤二']})


class Tags(HTMLParser):
    def __init__(self):
        super().__init__(); self.tags=[];self.ids=[];self.links=[]
    def handle_starttag(self, tag, attrs):
        attrs=dict(attrs);self.tags.append(tag)
        if 'id' in attrs:self.ids.append(attrs['id'])
        if 'href' in attrs:self.links.append(attrs['href'])


def test_complete_safe_portable_deterministic_html():
    args=([item(),item('repo')],date(2026,10,8))
    kwargs=dict(kind='pilot',coverage='原始正文 <danger>\n状态',approval_sha256='a'*64,body_sha256='b'*64,identity='c'*64)
    html=render_editorial_html(*args,**kwargs)
    assert html==render_editorial_html(*args,**kwargs)
    assert '<!doctype html>' in html and 'lang="zh-CN"' in html
    assert '<script>' not in html and '&lt;script&gt;' in html
    assert '迁移验收样例 · 非今日新闻' in html
    assert '只审阅 README，未安装运行' in html
    assert '有条件的结果' in html and '结果性质与适用条件尚未核验' in html
    assert '方法步骤一；方法步骤二' in html
    assert '原始正文 &lt;danger&gt;' in html
    parser=Tags();parser.feed(html)
    assert not {'script','form','iframe','img','button','input'} & set(parser.tags)
    assert len(parser.ids)==len(set(parser.ids))
    assert all(href[1:] in parser.ids for href in parser.links if href.startswith('#'))
    assert all(href.startswith(('#','https://')) for href in parser.links)
    assert 'prefers-color-scheme:dark' in html and 'prefers-reduced-motion' in html


@pytest.mark.parametrize('url',['javascript:alert(1)','data:text/html,unsafe','file:///tmp/private','notes/private.html','http://127.0.0.1:8000','http://localhost','https://user:pass@example.org','https://example.org/\nfoo','http://[invalid','http://127.0.0.2','http://127.1','http://2130706433','http://0.0.0.0','http://localhost.','http://0x7f000001','http://10.0.0.1','http://0x7f.0.0.1','http://127%2e0%2e0%2e1','http://localhost%2e'])
def test_reject_unsafe_or_nonportable_links(url):
    assert '<a ' not in link('source',url)


def test_escaped_link_attributes_and_coverage():
    assert '&quot;' in link('quoted','https://example.org/?a="x"')
    html=render_editorial_html([],date(2026,10,8),kind='status',coverage='失败：<script>')
    assert '本期状态 · 暂无合格内容' in html
    assert '不补入未经核验的内容' in html
    assert '失败：&lt;script&gt;' in html
    assert '0 篇论文' in html


def test_redacts_local_paths_but_preserves_official_urls():
    from daily_agent.rendering.editorial import text
    value = text('cached /workspace/shared/private.json and file:///home/user/paper.pdf source https://example.org/home/user/paper.pdf')
    assert '/workspace/' not in value and 'file://' not in value
    assert 'https://example.org/home/user/paper.pdf' in value
    assert '[本地路径已省略]' in value


def test_explicit_preprint_and_unverified_lab_labels():
    paper=item(); paper.material.source='arxiv'
    html=render_editorial_html([paper],date(2026,10,8))
    assert '预印本 · 不代表同行评审通过' in html
    assert '不根据邮箱、机构名或作者顺序推断' in html


def test_author_lab_contract_sources_and_escaping():
    paper=item()
    paper.material.raw['research_context']={'authors':[{'name':'Author <b>', 'roles':['corresponding_author'],
        'institutions':[{'name':'Institute','status':'verified_primary','source_url':'https://example.org/institution'}],
        'evidence':[{'source_url':'https://example.org/paper.pdf','excerpt':'Explicit affiliation'}]}],
        'labs':[{'name':'Lab','url':'https://example.org/lab','status':'verified_official','relationship':'paper_listed_by_group'}],
        'research_lines':[{'text':'Historical research','scope':'historical_background','source_url':'https://example.org/lab'}]}
    html=render_editorial_html([paper],date(2026,10,8))
    assert 'Author &lt;b&gt;' in html and '通讯作者' in html and '原始来源已核验' in html
    assert '不据此推断每位作者的隶属关系' in html and '历史研究背景' in html
    assert 'https://example.org/lab' in html


def test_shared_author_excerpts_deduplicate_without_losing_affiliation_scope():
    paper = item()
    shared = {'source_url':'https://example.org/official','excerpt':'A shared correspondence declaration.'}
    paper.material.raw['research_context'] = {'authors':[
        {'name':'Alice','roles':['first_author'],'institutions':[{'name':'Institute A','status':'verified_primary','source_url':'https://example.org/a'}], 'evidence':[shared]},
        {'name':'Bob','roles':['corresponding_author'],'institutions':[{'name':'Institute B','status':'source_metadata_unverified','source_url':'https://example.org/b'}], 'evidence':[shared]}]}
    html = render_editorial_html([paper],date(2026,10,8))
    assert html.count(shared['excerpt']) == 1
    assert html.count('作者来源 1') == 3  # two scoped references, one shared excerpt
    alice = html[html.index('<strong>Alice'):html.index('</p>',html.index('<strong>Alice'))]
    bob = html[html.index('<strong>Bob'):html.index('</p>',html.index('<strong>Bob'))]
    assert 'Institute A' in alice and 'Institute B' not in alice
    assert 'Institute B' in bob and 'Institute A' not in bob
    assert '原始来源已核验' in alice and '关系未独立核验' in bob


def test_responsive_contents_are_native_and_mobile_starts_collapsed():
    html = render_editorial_html([item(), item('repo')], date(2026, 10, 8))
    assert 'id="issue-contents"' in html
    assert '<div class="desktop-contents">' in html
    assert '<details class="contents-disclosure">' in html
    assert '<details class="contents-disclosure" open' not in html
    assert '本期导航 · 2 条' in html
    assert html.count('href="#issue-contents"') == 2
    assert '.desktop-contents{display:none}.contents-disclosure{display:block}' in html
    assert 'position:sticky;top:24px' in html
    assert 'max-height:calc(100vh - 48px);overflow:auto' in html
    assert 'min-height:44px' in html
    assert '.scientific-prose p,.reading-block p,.lead{font-size:16px' in html
    parser = Tags(); parser.feed(html)
    assert len(parser.ids) == len(set(parser.ids))
    assert all(href[1:] in parser.ids for href in parser.links if href.startswith('#'))


def test_figure_jump_exists_only_for_visible_verified_figures(monkeypatch):
    import daily_agent.paper_visual_assets as visuals
    monkeypatch.setattr(visuals, 'visual_assets_html', lambda _: '<section class="paper-visual-assets"><p>No asset</p></section>')
    html = render_editorial_html([item()], date(2026, 10, 8))
    assert 'href="#figures-1"' not in html
    monkeypatch.setattr(visuals, 'visual_assets_html', lambda _: '<section class="paper-visual-assets"><figure class="scientific-asset">Original</figure></section>')
    html = render_editorial_html([item()], date(2026, 10, 8))
    assert 'href="#figures-1"' in html
    assert 'id="figures-1"' in html
    assert html.index('href="#figures-1"') < html.index('id="figures-1"')


def test_ten_item_mobile_directory_is_collapsed_without_hiding_articles():
    items = [item(title=f'QA synthetic item {number}: ' + 'Long research title ' * 5) for number in range(1, 11)]
    html = render_editorial_html(items, date(2026, 10, 8), kind='pilot')
    assert '本期导航 · 10 条' in html
    assert '<details class="contents-disclosure">' in html
    assert html.count('<article class="story paper"') == 10
    parser = Tags(); parser.feed(html)
    assert len(parser.ids) == len(set(parser.ids))
    assert {f'item-{number}' for number in range(1, 11)} <= set(parser.ids)
    assert all(href[1:] in parser.ids for href in parser.links if href.startswith('#'))


def test_reader_typography_floor_and_essential_label_sizes():
    import re
    from daily_agent.rendering.editorial import CSS
    sizes = [int(size) for size in re.findall(r'font-size:(\d+)px', CSS)]
    sizes += [int(size) for size in re.findall(r'font:\d+ (\d+)px/', CSS)]
    assert sizes and min(sizes) >= 12  # Includes every responsive override.
    assert '.publication-status{font-size:14px' in CSS
    assert 'summary{cursor:pointer;font-size:14px' in CSS
    assert '.reading-block h4{font-size:14px' in CSS
    assert '.scientific-prose h4{font-size:14px' in CSS
    assert '.paper-visual-assets>h4{font-size:14px' in CSS
    assert '.contents-disclosure>summary>.contents-toggle{font-size:14px' in CSS
    assert '.source-links a,.figure-jump,.figure-source,.page-footer a{display:inline-flex;align-items:center;min-height:44px;font-size:14px}' in CSS
    assert '.scientific-asset figcaption{font-size:14px;line-height:1.85' in CSS
    assert '.scientific-prose p,.reading-block p,.lead{font-size:16px' in CSS
