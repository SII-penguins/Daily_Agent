"""Offline, append-only website export of verified delivery snapshots.

No publication, network I/O, paper reading, editorial revision or delivery acknowledgement.
Only public report content and non-identifying delivery audit labels leave the state root.
"""
from __future__ import annotations

import argparse
import base64
from datetime import date
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

from daily_agent.rendering.editorial import CSS, link, text
from daily_agent.workflow_state import atomic_bytes, exclusive_lock


def sha(data):
    return hashlib.sha256(data).hexdigest()


def strict_date(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise ValueError('Report date must be YYYY-MM-DD')
    return date.fromisoformat(value)


def safe_path(root, name):
    path = Path(name)
    if not isinstance(name, str) or not name or path.is_absolute() or '..' in path.parts or '\\' in name or path.as_posix() != name:
        raise ValueError('Unsafe archive path')
    target = root / path
    if not target.resolve().is_relative_to(root.resolve()) or any(p.is_symlink() for p in [target, *target.parents] if p.is_relative_to(root)):
        raise ValueError('Archive symlinks are forbidden')
    return target


class DocumentLinks(HTMLParser):
    def __init__(self):
        super().__init__(); self.links=[]; self.ids=set(); self.styles=[]; self.in_style=False
    def handle_starttag(self, tag, attrs):
        attrs=dict(attrs)
        if tag in {'script','iframe','object','embed','form','input','button','base','link','video','audio','source','svg','math'}:
            raise ValueError('Active or remote document content forbidden')
        if any(k.startswith('on') or k in {'srcset','action','formaction','background'} for k in attrs):
            raise ValueError('Active document attribute forbidden')
        if tag == 'meta' and attrs.get('http-equiv','').lower() == 'refresh':
            raise ValueError('Redirect forbidden')
        if attrs.get('style'): self.styles.append(attrs['style'])
        if tag == 'style': self.in_style=True
        if 'id' in attrs:
            if attrs['id'] in self.ids: raise ValueError('Duplicate document anchor')
            self.ids.add(attrs['id'])
        for field in ('href','src'):
            if field in attrs: self.links.append((field, attrs[field]))
    def handle_endtag(self, tag):
        if tag == 'style': self.in_style=False
    def handle_data(self, data):
        if self.in_style: self.styles.append(data)


def inspect_html(content):
    parser=DocumentLinks(); parser.feed(content)
    if any(re.search(r'url\s*\(|@import|expression\s*\(', s, re.I) for s in parser.styles):
        raise ValueError('Remote CSS resource forbidden')
    if re.search(r'(?<![A-Za-z0-9:/])(?:file://|/(?:workspace|home|Users|tmp|root|mnt|private)/)', content):
        raise ValueError('Local filesystem reference forbidden')
    for field, url in parser.links:
        parts=urlsplit(url)
        if parts.scheme in {'http','https'}:
            if field != 'href' or '<a ' not in link('source',url): raise ValueError('Unsafe external resource')
        elif parts.scheme == 'data':
            if field != 'src' or not re.fullmatch(r'data:image/png;base64,[A-Za-z0-9+/=]+',url): raise ValueError('Only embedded PNG assets are allowed')
        elif parts.scheme or url.startswith('//') or '\\' in url or any(ord(c)<32 for c in url):
            raise ValueError('Unsafe document link')
    return parser


def _shell(title, body):
    return ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="referrer" content="no-referrer"><meta http-equiv="Content-Security-Policy" '
        'content="default-src \'none\'; img-src \'self\' data:; style-src \'unsafe-inline\'; base-uri \'none\'; form-action \'none\'">'
        '<title>'+text(title)+'</title><style>'+CSS+ARCHIVE_CSS+'</style></head><body><div class="page">'+body+'</div></body></html>')


def audit_label(manifest):
    if manifest['state'] == 'prepared':
        return '已生成 · 网站预览；聊天投递尚未受理'
    if manifest['state'] == 'confirmed':
        return '投递已确认'
    if manifest.get('readback_observation'):
        return '发送已受理；消息及文件已观察，附件身份尚未完成回读核验'
    return '发送已受理；尚未完成投递回读核验'


REVISION_FIELDS=('edition_type','revision_id','revision_number','parent_identity','contract_sha256')


def _revision_metadata(manifest):
    """Validate revision provenance without changing legacy manifest handling."""
    if manifest.get('schema_version')!=3 and not any(field in manifest for field in REVISION_FIELDS):
        return {}
    if (manifest.get('schema_version')!=3 or manifest.get('kind')!='report'
            or manifest.get('edition_type')!='production_revision'
            or type(manifest.get('revision_number')) is not int or manifest['revision_number']<=0):
        raise ValueError('Invalid production revision metadata')
    for field in ('identity','revision_id','parent_identity','contract_sha256'):
        if not isinstance(manifest.get(field),str) or not re.fullmatch('[0-9a-f]{64}',manifest[field]):
            raise ValueError('Invalid production revision '+field)
    return {field:manifest[field] for field in REVISION_FIELDS}


def archive_entry_key(manifest):
    """Return a stable same-depth archive key for original, pilot or revision."""
    day=strict_date(manifest['date']).isoformat()
    revision=_revision_metadata(manifest)
    if revision:
        return f"reports/{day}-r{revision['revision_number']}-{manifest['identity'][:12]}"
    if manifest['kind']=='pilot':
        return 'examples/'+day+'-'+manifest['identity'][:12]
    return 'reports/'+day


def collect_entries(roots, include_current=None):
    """Read only cryptographically verified, accepted/confirmed source snapshots."""
    from daily_agent.cloud_workflow import read_handoff, _safe_relative
    from daily_agent.production_revision import iter_handoffs
    results=[]
    current=None
    roots=list(roots)
    if include_current is not None:
        if (not isinstance(include_current,dict)
                or set(include_current) not in ({'root','date','identity'},{'root','date','identity','revision_id'})):
            raise ValueError('Current candidate requires root, date and sealed identity')
        strict_date(include_current['date'])
        if not re.fullmatch('[0-9a-f]{64}',str(include_current['identity'])):
            raise ValueError('Invalid candidate identity')
        if 'revision_id' in include_current and not re.fullmatch('[0-9a-f]{64}',str(include_current['revision_id'])):
            raise ValueError('Invalid candidate revision identity')
        current={**include_current,'root':str(Path(include_current['root']).resolve())}
        roots.append(current['root'])
    seen_roots=set(); matched_current=False
    for raw_root in roots:
        root=Path(raw_root).resolve()
        if str(root) in seen_roots: continue
        seen_roots.add(str(root))
        def snapshots():
            for journal in sorted((root/'data/state/cloud-delivery').glob('*.json')):
                day=strict_date(journal.stem)
                yield {**read_handoff(root,day),'date':day.isoformat()}
            yield from iter_handoffs(root)
        for manifest in snapshots():
            day=strict_date(manifest['date'])
            revision=_revision_metadata(manifest)
            selected=bool(current and current['root']==str(root) and current['date']==day.isoformat()
                and current.get('revision_id')==revision.get('revision_id'))
            if selected:
                if manifest['identity']!=current['identity'] or manifest['state'] not in {'prepared','accepted','confirmed'}:
                    raise ValueError('Current candidate is not the exact prepared or received handoff')
                matched_current=True
            if not selected and manifest['state'] not in {'accepted','confirmed'}: continue
            category='examples' if manifest['kind']=='pilot' else 'reports'
            # Examples never count as a formal daily edition, even when sent.
            key=archive_entry_key(manifest)
            content=(_safe_relative(root,manifest['html_report']).read_bytes() if manifest.get('html_report') else None)
            source_sha=sha(content) if content else manifest['body_sha256']
            record={'key':key, 'date':day.isoformat(), 'category':category,
                'kind':manifest['kind'], 'format':'html' if content else 'text', 'created_at':manifest.get('created_at',''), 'source_sha256':source_sha, 'identity':manifest['identity'],
                'approval_sha256':manifest['approval_sha256'], 'audit':audit_label(manifest),
                'page':key+'/index.html', **revision}
            results.append((record,content,manifest['body']))
    if current and not matched_current: raise ValueError('Current candidate not found')
    return results


def _put(root, relative, data, files):
    target=safe_path(root,relative)
    if target.exists() and target.read_bytes()!=data:
        raise ValueError('Immutable archive artifact changed: '+relative)
    if not target.exists():
        target.parent.mkdir(parents=True,exist_ok=True); atomic_bytes(target,data)
    files[relative]={'sha256':sha(data),'size':len(data)}


def _entry_page(record, content, body, output, files):
    edition='验收样例，非正式日报' if record['category']=='examples' else '每日快照'
    if record.get('edition_type')=='production_revision': edition+=' · 修订版 r'+str(record['revision_number'])
    navigation='<nav class="archive-bar" aria-label="日报归档"><a href="../../index.html">← 按日期浏览全部日报</a><span>'+text(record['date'])+' · '+edition+'</span></nav>'
    audit='<aside class="archive-audit"><p>'+text(record['audit'])+'</p><p>此页保留首次归档时的内容；最新投递审计状态见日期目录。</p></aside>'
    if content is not None:
        source=content.decode('utf-8'); inspect_html(source)
        source_name=record['key']+'/original.html'
        _put(output,source_name,content,files)
        navigation += '<p class="archive-download"><a href="original.html" download>下载当时的独立 HTML 原件</a></p>'
        def externalize(match):
            data=base64.b64decode(match.group(1),validate=True)
            if not data.startswith(b'\x89PNG\r\n\x1a\n') or len(data)>2_000_000: raise ValueError('Invalid or oversized image')
            name='assets/'+sha(data)+'.png'; _put(output,name,data,files)
            return 'src="../../'+name+'"'
        source=re.sub(r'src="data:image/png;base64,([A-Za-z0-9+/=]+)"',externalize,source)
        source=source.replace("img-src data:","img-src 'self' data:")
        source=source.replace('</style>',ARCHIVE_CSS+'</style>',1)
        if '<body>' not in source: raise ValueError('Expected complete HTML document')
        source=source.replace('<body>','<body><div class="page">'+navigation+audit+'</div>',1)
    else:
        source=_shell('Daily Agent · '+record['date'],navigation+audit+'<main><h1>'+text(record['date'])+'</h1><pre class="archive-text">'+text(body)+'</pre></main>')
    _put(output,record['page'],source.encode(),files)


def _entry_order(entry):
    return (entry['date'],entry.get('revision_number',0),entry.get('created_at',''),entry['key'])


def _index(entries, base_url=None):
    sections=[]
    revised_days={e['date'] for e in entries if e.get('edition_type')=='production_revision'}
    def label(entry,category):
        if entry.get('edition_type')=='production_revision': return '打开修订版 r'+str(entry['revision_number'])
        if category=='reports' and entry['date'] in revised_days: return '日报状态' if entry['kind']=='status' else '打开原版日报'
        return '日报状态' if entry['kind']=='status' else '打开日报' if category=='reports' else ('HTML 验收样例' if entry.get('format')=='html' else '文本验收样例' if entry.get('format')=='text' else '打开验收样例')
    for category,title in [('reports','每日归档'),('examples','验收样例 · 不计入正式日报')]:
        chosen=sorted((e for e in entries if e['category']==category),key=_entry_order,reverse=True)
        rows=''.join('<li><a href="'+text(e['page'])+'"><time datetime="'+e['date']+'">'+e['date']+'</time><span>'+label(e,category)+' →</span></a><small>'+text(e['audit'])+'</small></li>' for e in chosen)
        sections.append('<section class="archive-section"><h2>'+title+'</h2>'+('<ul class="archive-list">'+rows+'</ul>' if chosen else ('<p class="empty-state">尚无正式日报。实际日报生成并受理发送后，才会出现对应日期；不会补造历史日期。</p>' if category=='reports' else '<p class="empty-state">尚无已归档的验收样例。</p>'))+'</section>')
    reports=sorted((e for e in entries if e['category']=='reports'),key=_entry_order,reverse=True)
    latest_edition=(' · 修订版 r'+str(reports[0]['revision_number'])) if reports and reports[0].get('edition_type')=='production_revision' else ''
    latest='<a class="latest-report" href="'+text(reports[0]['page'])+'">直接阅读最新日报 · '+reports[0]['date']+latest_edition+' →</a>' if reports else ''
    if not reports:
        examples=sorted((e for e in entries if e['category']=='examples'),key=lambda e:(e['date'],e.get('created_at',''),e['key']),reverse=True)
        if examples:
            latest='<a class="latest-report" href="'+text(examples[0]['page'])+'">阅读最新验收样例 · '+examples[0]['date']+' →</a>'
    address='<p class="archive-address">固定目录：'+link(base_url,base_url)+'</p>' if base_url else ''
    return _shell('Daily Agent · 日报归档','<header class="masthead"><div class="brand">DA<span>DAILY AGENT<small>RESEARCH ARCHIVE</small></span></div></header><main><section class="compact-hero"><h1>按日期阅读日报</h1><p>原始内容留档 · 图表与来源随期保存</p></section>'+latest+address+''.join(sections)+'</main><footer class="page-footer">日期为原始日报日期。归档不等于确认投递或重新审核论文。</footer>')


def verify_archive(output):
    root=Path(output).resolve()
    manifest=json.loads((root/'manifest.json').read_text())
    if manifest.get('schema_version')!=1: raise ValueError('Unknown archive schema')
    expected=set(manifest['files'])|{'manifest.json','.export.lock'}
    actual={p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file()}
    if actual-expected: raise ValueError('Unexpected archive files')
    documents={}
    for name,metadata in manifest['files'].items():
        path=safe_path(root,name); data=path.read_bytes()
        if sha(data)!=metadata['sha256'] or len(data)!=metadata['size']: raise ValueError('Archive hash/size mismatch: '+name)
        if name.endswith('.html'): documents[name]=inspect_html(data.decode())
    for name,document in documents.items():
        for _,url in document.links:
            parts=urlsplit(url)
            if parts.scheme or url.startswith('//'): continue
            target=(safe_path(root,name).parent/unquote(parts.path)).resolve() if parts.path else safe_path(root,name)
            if not target.is_relative_to(root) or not target.is_file(): raise ValueError('Broken or escaping archive link: '+url)
            if parts.fragment:
                key=target.relative_to(root).as_posix()
                if key not in documents or unquote(parts.fragment) not in documents[key].ids: raise ValueError('Broken archive anchor: '+url)
    for entry in manifest['entries']:
        strict_date(entry['date'])
        if any(field in entry for field in REVISION_FIELDS):
            revision={**entry,'schema_version':3}
            key=archive_entry_key(revision)
            if entry.get('category')!='reports' or entry.get('key')!=key or entry.get('page')!=key+'/index.html':
                raise ValueError('Invalid production revision archive entry')
        if entry['page'] not in manifest['files']: raise ValueError('Missing date page')
    return manifest


def export_archive(roots, output, *, base_url=None, include_current=None):
    """Append snapshots, retain every existing entry, refresh the date index.

    base_url is an already verified private site address supplied by the caller;
    it is only a visible link, never proof of publication or audience access.
    """
    if base_url and ('<a ' not in link('archive',base_url) or urlsplit(base_url).scheme!='https'):
        raise ValueError('A verified HTTPS archive base URL is required')
    output=Path(output).resolve(); output.mkdir(parents=True,exist_ok=True)
    with exclusive_lock(output/'.export.lock'):
        existing=verify_archive(output) if (output/'manifest.json').exists() else {'entries':[],'files':{}}
        files=dict(existing['files']); entries={e['key']:dict(e) for e in existing['entries']}
        candidates=collect_entries(roots,include_current) if include_current is not None else collect_entries(roots)
        for record,content,body in candidates:
            strict_date(record['date'])
            previous=entries.get(record['key'])
            if previous:
                for field in ('source_sha256','identity','approval_sha256','date','category','kind','page'):
                    if previous[field]!=record[field]: raise ValueError('Existing dated report is immutable: '+record['key'])
                for field in REVISION_FIELDS:
                    if previous.get(field)!=record.get(field): raise ValueError('Existing revision provenance is immutable: '+record['key'])
                previous['audit']=record['audit']
                for field in ('format','created_at'):
                    if field in record: previous[field]=record[field]
            else:
                _entry_page(record,content,body,output,files); entries[record['key']]=record
        ordered=sorted(entries.values(),key=lambda e:e['key'])
        index=_index(ordered,base_url).encode(); atomic_bytes(output/'index.html',index)
        files['index.html']={'sha256':sha(index),'size':len(index)}
        manifest={'schema_version':1,'entries':ordered,'files':files}
        atomic_bytes(output/'manifest.json',json.dumps(manifest,ensure_ascii=False,sort_keys=True,indent=2).encode())
        return verify_archive(output)


ARCHIVE_CSS='''
.archive-bar{display:flex;justify-content:space-between;gap:16px;flex-wrap:wrap;padding:18px 0;font-size:12px;border-bottom:1px solid var(--line)}.archive-download,.archive-audit{font-size:11px;color:var(--muted);margin:8px 0}.archive-audit p+p{margin-top:2px}.archive-section{margin:30px 0}.archive-section h2{font-size:20px;margin-bottom:16px}.archive-list{list-style:none;margin:0;padding:0}.archive-list li{padding:18px 0;border-bottom:1px solid var(--line)}.archive-list a{display:flex;gap:20px;justify-content:space-between;text-decoration:none;min-height:32px}.archive-list time{font-size:21px;font-weight:600}.archive-list small{display:block;color:var(--muted);font-size:12px}.latest-report{display:block;padding:16px 20px;margin-top:24px;background:var(--soft);border:1px solid var(--line);font-weight:600}.archive-address{font-size:12px;overflow-wrap:anywhere}.archive-text{font-family:inherit;white-space:pre-wrap;overflow-wrap:anywhere;font-size:14px}.archive-section .empty-state{font-size:14px}.archive-list a span{font-size:13px}.archive-bar,.archive-audit{overflow-wrap:anywhere}
@media(max-width:480px){.archive-list a{gap:12px}.archive-list time{font-size:19px}.archive-bar{gap:8px}.archive-section{margin:24px 0}}
'''


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',action='append',default=[]); parser.add_argument('--output',required=True)
    parser.add_argument('--base-url'); parser.add_argument('--verify',action='store_true')
    parser.add_argument('--current-root'); parser.add_argument('--current-date'); parser.add_argument('--current-identity')
    parser.add_argument('--current-revision-id')
    args=parser.parse_args(argv)
    current_values=(args.current_root,args.current_date,args.current_identity)
    if any(current_values) and not all(current_values): parser.error('All three --current-* options are required')
    if args.current_revision_id and not all(current_values): parser.error('--current-revision-id requires all three --current-* options')
    current=dict(zip(('root','date','identity'),current_values)) if all(current_values) else None
    if current is not None and args.current_revision_id: current['revision_id']=args.current_revision_id
    result=verify_archive(args.output) if args.verify else export_archive(args.root,args.output,base_url=args.base_url,include_current=current)
    print(json.dumps({'entries':len(result['entries']),'files':len(result['files']),'output':str(Path(args.output).resolve())}))

if __name__=='__main__': main()
