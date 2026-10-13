from html.parser import HTMLParser
from pathlib import Path
import html,json,re
esc=html.escape
class Titles(HTMLParser):
 def __init__(self):super().__init__();self.anchor=None;self.in_heading=False;self.text=[];self.items=[];self.heading=None
 def handle_starttag(self,tag,attrs):
  a=dict(attrs)
  if tag=='article':self.anchor=a.get('id')
  if self.anchor and not self.in_heading and tag in ('h2','h3'):
   self.heading=tag;self.in_heading=True;self.text=[]
 def handle_data(self,data):
  if self.in_heading:self.text.append(data)
 def handle_endtag(self,tag):
  if self.in_heading and tag==self.heading:
   self.items.append({'title':' '.join(''.join(self.text).split()),'anchor':self.anchor});self.in_heading=False;self.anchor=None
  if tag=='article':self.anchor=None

def render(dist,head,reports,examples,groups,reading,preferred,label):
 dates=sorted(groups,reverse=True);data=[]
 for date in dates:
  editions=[]
  for e in groups[date]:
   parser=Titles();parser.feed((dist/e['page']).read_text())
   assert parser.items,e['page']
   editions.append({'label':label(e),'reading':'/'+reading[e['key']],'original':'/'+e['page'],'titles':parser.items})
  data.append({'date':date,'editions':editions})
 base=Path(__file__).parent/'archive';css=(base/'archive-browser.css').read_text();js=(base/'archive-browser.js').read_text()
 head=re.sub(r'<script src="/assets/archive-browser.js" defer></script>','',head)
 head=re.sub(r'<!-- archive-index-style:v[12] -->.*?<!-- /archive-index-style:v[12] -->','',head,flags=re.S)
 # Only the archive permits its own static script; report CSPs remain untouched.
 head=re.sub(r'(<meta http-equiv="Content-Security-Policy" content=")[^"]*(")',r"\1default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; script-src 'self'; base-uri 'none'; form-action 'none'\2",head)
 head=head.replace('</head>','<!-- archive-index-style:v2 --><style>'+css+'</style><!-- /archive-index-style:v2 --><script src="/assets/archive-browser.js" defer></script></head>')
 body='<body><a class="skip-link" href="#archive-days">跳到往期日报</a><div class="page"><header class="masthead"><a class="brand" href="/">DA<span>DAILY AGENT<small>RESEARCH ARCHIVE</small></span></a></header><main><section class="compact-hero"><h1>往期日报</h1><p class="archive-intro">按月份浏览，或搜索论文、项目标题与日期</p></section>'
 if dates:body+=f'<a class="latest-report" href="/{preferred[dates[0]]}">阅读最新日报 · {dates[0]} →</a>'
 body+='''<form id="archive-filters" class="archive-filters" hidden role="search" aria-label="查找往期日报"><label class="archive-search-field">标题关键词<input id="archive-query" type="search" name="q" placeholder="例如 Quantum、世界模型" maxlength="200"></label><label>年份<select id="archive-year" name="year"></select></label><label>月份<select id="archive-month" name="month"></select></label><label class="archive-date-field">精确日期<input id="archive-date" type="date" name="date"></label><div class="archive-filter-actions"><button type="submit">搜索</button><button id="archive-reset" type="button">重置筛选</button><span class="archive-filter-help">搜索范围：各版本的论文与项目标题；选择日期会清除年月筛选</span></div></form><div id="archive-days"><div id="archive-dynamic" hidden><p id="archive-status" class="archive-status" aria-live="polite" role="status" tabindex="-1"></p><div id="archive-results"></div><nav id="archive-pagination" class="archive-pagination" aria-label="归档分页"></nav></div><div id="archive-static"><p class="archive-status">按月份展开查看全部归档；启用 JavaScript 可使用日期与标题搜索</p>'''
 months=sorted({d[:7] for d in dates},reverse=True)
 for i,month in enumerate(months):
  body+=f'<details class="archive-month-group" {"open" if i==0 else ""}><summary><strong>{month[:4]} 年 {month[5:]} 月</strong><span>{sum(d.startswith(month) for d in dates)} 期</span></summary>'
  for day in [d for d in data if d['date'].startswith(month)]:
   date=day['date'];body+=f'<section class="archive-day" id="date-{date}"><div class="archive-date-heading"><h2><time datetime="{date}">{date}</time></h2><a href="/{preferred[date]}">打开日报 →</a></div><details class="archive-editions"><summary>本日版本与原始页面</summary>'
   for edition in day['editions']:
    body+=f'<div class="archive-edition"><a href="{edition["reading"]}">{esc(edition["label"])} · 阅读 →</a><a class="archive-original" href="{edition["original"]}">原始页面</a><ul class="archive-title-list">'
    for item in edition['titles']:body+=f'<li><a href="{edition["reading"]}#{item["anchor"]}">{esc(item["title"])}</a></li>'
    body+='</ul></div>'
   body+='</details></section>'
  body+='</details>'
 body+='</div></div><section class="archive-section"><details><summary>验收样例 · 不计入正式日报</summary><ul class="archive-list">'
 for e in sorted(examples,key=lambda e:e.get('created_at',''),reverse=True):body+=f'<li><a href="/{e["page"]}"><time datetime="{e["date"]}">{e["date"]}</time><span>{esc(e["format"].upper())} 验收样例 →</span></a><small>{esc(e["audit"])}</small></li>'
 body+='</ul></details></section></main><footer class="page-footer">仅搜索已归档内容；原始页面、图表与来源保持留档。</footer></div></body></html>'
 script='window.DAILY_ARCHIVE = '+json.dumps(data,ensure_ascii=False).replace('<','\\u003c')+';\n'+js
 return head+body,script,data
