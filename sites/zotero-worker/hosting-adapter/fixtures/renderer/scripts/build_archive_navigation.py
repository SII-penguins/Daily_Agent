"""Add an accessible reading layer without modifying immutable report editions.
Run after appending new entries to dist/manifest.json; original files are never rewritten.
"""
from pathlib import Path
import hashlib, html, json, re
ROOT=Path(__file__).resolve().parents[1]
DIST=ROOT/'dist'
MANIFEST=DIST/'manifest.json'
esc=html.escape
sha=lambda b:hashlib.sha256(b).hexdigest()
CSS='''
.archive-navigation{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:8px 20px;padding:12px 0;border-bottom:1px solid var(--line,#ccd3c8);font-size:14px;line-height:1.5}
.archive-navigation a{display:inline-flex;align-items:center;min-height:44px;padding:6px 2px;color:var(--accent,#14634c);text-decoration:none;font-weight:600}
.archive-navigation a:hover{text-decoration:underline}.archive-navigation a:focus-visible{outline:3px solid var(--focus,#925025);outline-offset:3px}
.archive-navigation .archive-history-link{border:1px solid var(--line,#ccd3c8);border-radius:4px;padding:8px 14px;background:var(--soft,#e9eee4)}
.archive-navigation .archive-date-links{display:flex;gap:16px;flex-wrap:wrap}.archive-navigation .archive-original-link{font-size:12px;font-weight:400}
@media(max-width:480px){.archive-navigation{gap:4px 10px}.archive-navigation .archive-date-links{width:100%;justify-content:space-between;gap:4px}.archive-navigation .archive-original-link{margin-left:auto}}
@media print{.archive-navigation{display:none}}
'''
STYLE='<!-- archive-navigation-style:v1 --><style>'+CSS+'</style><!-- /archive-navigation-style:v1 -->'
def edition_label(e):
 k=e['key']
 if 'corrected' in k:return '修订版'
 if 'supplement' in k:return '早前补充版'
 return '原始日报'
def main():
 m=json.loads(MANIFEST.read_text())
 reports=[e for e in m['entries'] if e['category']=='reports']
 examples=[e for e in m['entries'] if e['category']!='reports']
 dates=sorted({e['date'] for e in reports})
 groups={d:sorted([e for e in reports if e['date']==d],key=lambda e:e.get('created_at',''),reverse=True) for d in dates}
 reading={e['key']:str(Path(e['page']).with_name('reading.html')) for e in reports}
 preferred={d:reading[groups[d][0]['key']] for d in dates}
 generated={}
 for e in reports:
  path=e['page']; src=(DIST/path).read_text(); d=e['date']; i=dates.index(d)
  links=[]
  if i: links.append(f'<a href="/{preferred[dates[i-1]]}" rel="prev">← 上一期 · {dates[i-1]}</a>')
  if i+1<len(dates): links.append(f'<a href="/{preferred[dates[i+1]]}" rel="next">下一期 · {dates[i+1]} →</a>')
  nav='<!-- archive-navigation:v1 --><div class="page"><nav class="archive-navigation" aria-label="日报历史导航"><a class="archive-history-link" href="/archive/">往期日报 ↗</a><a class="archive-original-link" href="index.html">查看原始页面</a><div class="archive-date-links">'+''.join(links)+'</div></nav></div><!-- /archive-navigation:v1 -->'
  if '</head>' in src:
   assert src.count('</head>')==1 and len(re.findall(r'<body(?:\s[^>]*)?>',src))==1
   content=src.replace('</head>',STYLE+'</head>',1)
   content=re.sub(r'(<body(?:\s[^>]*)?>)',lambda x:x.group(1)+nav,content,count=1)
  else:
   assert '</style>' in src
   content=src.replace('</style>','</style>'+STYLE+nav,1)
  assert content.replace(STYLE,'',1).replace(nav,'',1)==src
  p=DIST/reading[e['key']];p.write_text(content)
  generated[str(p.relative_to(DIST))]={'sha256':sha(p.read_bytes()),'size':p.stat().st_size}
 # Retain the established editorial-v4 stylesheet byte-for-byte.
 old=(DIST/'index.html').read_text()
 head=old[:old.index('</head>')+7]
 head=re.sub(r'<!-- archive-index-style:v1 -->.*?<!-- /archive-index-style:v1 -->','',head,flags=re.S)
 head=re.sub(r'<title>.*?</title>','<title>往期日报 · Daily Agent</title>',head)
 from archive_page import render
 content,script,search_data=render(DIST,head,reports,examples,groups,reading,preferred,edition_label)
 for path,value in [('index.html',content),('archive/index.html',content),('assets/archive-browser.js',script)]:
  p=DIST/path;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(value)
  generated[path]={'sha256':sha(p.read_bytes()),'size':p.stat().st_size}
 m['files'].update(generated)
 m['navigation']={'schema':'daily-agent-archive-navigation-v2','archive_page':'archive/index.html','reading_pages':reading,'preferred_by_date':preferred,'immutable_reports_preserved':True,'search_title_count':sum(len(e['titles']) for d in search_data for e in d['editions']),'search_fields':['date','edition_label','article_title'],'browser_script':'assets/archive-browser.js'}
 MANIFEST.write_text(json.dumps(m,ensure_ascii=False,indent=2)+'\n')
 print(json.dumps({'reading_pages':len(reports),'dates':dates,'original_entries':len(m['entries']),'generated_files':generated},ensure_ascii=False,indent=2))
if __name__=='__main__':main()
