/** Offline disposable-render composition. No filesystem, network or Site writes.
 * Authenticated baseline and explicit approved sealed entries/mappings are host
 * inputs. Historical renderer output is discarded and restored before proof.
 */
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { digest,inventory,validateMappings,remapReadingURL,planNavigationRemap,NAVIGATION_FILES } from './reading-navigation.mjs';
const fail=message=>{throw new Error(message);};
const canonical=value=>JSON.stringify(value,(_,v)=>v&&typeof v==='object'&&!Array.isArray(v)?Object.fromEntries(Object.keys(v).sort().map(k=>[k,v[k]])):v);
const same=(a,b,message)=>assert.equal(canonical(a),canonical(b),message);
const utf8=bytes=>new TextDecoder('utf-8',{fatal:true}).decode(bytes);
const metadata=bytes=>({sha256:digest(bytes),size:bytes.length});
const clone=value=>JSON.parse(JSON.stringify(value));
const isObject=value=>value!==null&&typeof value==='object'&&!Array.isArray(value);
const permitted=name=>NAVIGATION_FILES.includes(name)||/^(reports|examples)\/[A-Za-z0-9][A-Za-z0-9._-]*\/(index|original|reading)\.html$/.test(name)||/^assets\/[a-f0-9]{64}\.png$/.test(name);

// Strict duplicate-key detection and source spans, isolated from the reviewed
// URL-only helper so that helper's signed source remains unchanged.
function parse(text){let i=0;const nodes=[];const ws=()=>{while(/[\t\n\r ]/.test(text[i]??'!'))i++;};const str=()=>{const start=i;if(text[i++]!=='"')fail('Invalid JSON string');let escaped=false;while(i<text.length){const c=text[i++];if(c==='"'&&!escaped)return JSON.parse(text.slice(start,i));if(c==='\\'&&!escaped)escaped=true;else escaped=false;}fail('Unclosed JSON string');};function value(path){ws();const start=i;let v;if(text[i]==='"')v=str();else if(text[i]==='{'){i++;ws();v=Object.create(null);const seen=new Set();if(text[i]!=='}')while(true){ws();const k=str();if(seen.has(k))fail('Duplicate JSON key');seen.add(k);ws();if(text[i++]!==':')fail('Invalid JSON object');v[k]=value([...path,k]);ws();if(text[i]!==',')break;i++;}if(text[i++]!=='}')fail('Invalid JSON object');}else if(text[i]==='['){i++;ws();v=[];if(text[i]!==']')while(true){v.push(value([...path,v.length]));ws();if(text[i]!==',')break;i++;}if(text[i++]!==']')fail('Invalid JSON array');}else{const m=/^(?:true|false|null|-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?)/.exec(text.slice(i));if(!m)fail('Invalid JSON value');i+=m[0].length;v=JSON.parse(m[0]);}nodes.push({path,start,end:i,value:v});return v;}const result=value([]);ws();if(i!==text.length)fail('Trailing JSON content');return {text,value:result,nodes};}
function replace(text,patches){let result='',end=0;for(const p of patches.sort((a,b)=>a.start-b.start)){if(p.start<end)fail('Overlapping replacement');result+=text.slice(end,p.start)+p.value;end=p.end;}return result+text.slice(end);}
function rawNode(parsed,path){const node=parsed.nodes.find(n=>canonical(n.path)===canonical(path));if(!node)fail('Missing JSON node');return parsed.text.slice(node.start,node.end);}
function archiveManifest(files,{bind=true}={}){
 inventory(files);for(const name of files.keys())if(!permitted(name))fail('Unsupported public path: '+name);
 const parsed=parse(utf8(files.get('manifest.json'))),m=parsed.value;
 if(m.schema_version!==1||!isObject(m.files)||!Array.isArray(m.entries)||m.navigation?.schema!=='daily-agent-archive-navigation-v2')fail('Archive manifest with v2 navigation required');
 if(bind){same(Object.keys(m.files).sort(),[...files.keys()].filter(p=>p!=='manifest.json').sort(),'Manifest file inventory mismatch');for(const [path,meta]of Object.entries(m.files))same(meta,metadata(files.get(path)),'Manifest bytes mismatch: '+path);}
 const keys=new Set();for(const entry of m.entries){if(!isObject(entry)||typeof entry.key!=='string'||keys.has(entry.key)||entry.page!==entry.key+'/index.html')fail('Invalid or duplicate archive entry');keys.add(entry.key);}
 return parsed;
}
function dataDocument(bytes){const text=utf8(bytes),prefix='window.DAILY_ARCHIVE = ',i=text.indexOf('\n');if(i<0||!text.startsWith(prefix)||text[i-1]!==';')fail('Exact archive first-line data required');const parsed=parse(text.slice(prefix.length,i-1));if(!Array.isArray(parsed.value))fail('Archive data array required');const dates=new Set(),originals=new Set();for(const day of parsed.value){if(!isObject(day)||Object.keys(day).sort().join(',')!=='date,editions'||typeof day.date!=='string'||dates.has(day.date)||!Array.isArray(day.editions)||!day.editions.length)fail('Invalid archive day row');dates.add(day.date);for(const edition of day.editions){if(!isObject(edition)||Object.keys(edition).sort().join(',')!=='label,original,reading,titles'||typeof edition.original!=='string'||originals.has(edition.original)||typeof edition.reading!=='string'||typeof edition.label!=='string'||!Array.isArray(edition.titles))fail('Invalid or duplicate archive edition row');originals.add(edition.original);}}return {...parsed,prefix,tail:text.slice(i)};}
function mappedData(bytes,mappings){const doc=dataDocument(bytes),patches=[];for(const node of doc.nodes)if(node.path.length===4&&node.path[1]==='editions'&&node.path[3]==='reading'){const next=remapReadingURL(node.value,mappings);if(next!==node.value)patches.push({start:node.start,end:node.end,value:JSON.stringify(next)});}const text=replace(doc.text,patches);return {...parse(text),prefix:doc.prefix,tail:doc.tail};}
function sourceTitles(entries,files){const result=spawnSync('python3',['-c',`import json,sys
from html.parser import HTMLParser
class P(HTMLParser):
 def __init__(self):super().__init__();self.anchor=None;self.heading=None;self.text=[];self.items=[];self.ids=set()
 def handle_starttag(self,tag,attrs):
  a=dict(attrs)
  if tag=='article':
   self.anchor=a.get('id')
   if self.anchor:
    if self.anchor in self.ids:raise ValueError('Duplicate article anchor')
    self.ids.add(self.anchor)
  if self.anchor and self.heading is None and tag in ('h2','h3'):self.heading=tag;self.text=[]
 def handle_data(self,data):
  if self.heading:self.text.append(data)
 def handle_endtag(self,tag):
  if self.heading==tag:self.items.append({'title':' '.join(''.join(self.text).split()),'anchor':self.anchor});self.heading=None;self.anchor=None
  if tag=='article':self.anchor=None
out={}
for key,html in json.load(sys.stdin).items():
 p=P();p.feed(html);p.close()
 if not p.items or any(not x['title'] or not x['anchor'] for x in p.items):raise ValueError('Titles and anchors required')
 out[key]=p.items
print(json.dumps(out,ensure_ascii=False))
`],{input:JSON.stringify(Object.fromEntries(entries.map(e=>[e.key,utf8(files.get(e.page))]))),encoding:'utf8',maxBuffer:32*1024*1024,timeout:10000});if(result.error||result.status!==0)fail('New sealed original title/anchor extraction failed');return JSON.parse(result.stdout);}
const esc=text=>String(text).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#x27;');
const label=entry=>entry.key.includes('corrected')?'修订版':entry.key.includes('supplement')?'早前补充版':'原始日报';
const asHref=value=>value.startsWith('/')?value:'/'+value;
function expectedReading(source,entry,allEntries,baseline){
 const stylePattern=/<!-- archive-navigation-style:v1 -->[\s\S]*?<!-- \/archive-navigation-style:v1 -->/g;
 const styles=[...baseline].filter(([p])=>p.endsWith('/reading.html')).map(([,b])=>[...utf8(b).matchAll(stylePattern)]).filter(m=>m.length===1).map(m=>m[0][0]);
 if(!styles.length||new Set(styles).size!==1)fail('One retained reading-navigation style required');
 const reports=allEntries.filter(e=>e.category==='reports'),dates=[...new Set(reports.map(e=>e.date))].sort(),index=dates.indexOf(entry.date),preferred=date=>reports.filter(e=>e.date===date).sort((a,b)=>(b.created_at??'').localeCompare(a.created_at??''))[0].key+'/reading.html';
 const links=[];if(index>0)links.push(`<a href="/${preferred(dates[index-1])}" rel="prev">← 上一期 · ${dates[index-1]}</a>`);if(index+1<dates.length)links.push(`<a href="/${preferred(dates[index+1])}" rel="next">下一期 · ${dates[index+1]} →</a>`);
 const nav='<!-- archive-navigation:v1 --><div class="page"><nav class="archive-navigation" aria-label="日报历史导航"><a class="archive-history-link" href="/archive/">往期日报 ↗</a><a class="archive-original-link" href="index.html">查看原始页面</a><div class="archive-date-links">'+links.join('')+'</div></nav></div><!-- /archive-navigation:v1 -->';
 if(source.includes('</head>')){if(source.split('</head>').length!==2||[...source.matchAll(/<body(?:\s[^>]*)?>/g)].length!==1)fail('Unsupported sealed document structure');return source.replace('</head>',styles[0]+'</head>').replace(/(<body(?:\s[^>]*)?>)/,match=>match+nav);}
 if(!source.includes('</style>'))fail('Unsupported sealed document structure');return source.replace('</style>','</style>'+styles[0]+nav);
}
function renderMonths(data,preferred){const months=[...new Set(data.map(d=>d.date.slice(0,7)))].sort().reverse();return months.map((month,i)=>{const days=data.filter(d=>d.date.startsWith(month));let s=`<details class="archive-month-group" ${i===0?'open':''}><summary><strong>${month.slice(0,4)} 年 ${month.slice(5)} 月</strong><span>${days.length} 期</span></summary>`;for(const day of days){const date=day.date;s+=`<section class="archive-day" id="date-${date}"><div class="archive-date-heading"><h2><time datetime="${date}">${date}</time></h2><a href="${esc(asHref(preferred[date]))}">打开日报 →</a></div><details class="archive-editions"><summary>本日版本与原始页面</summary>`;for(const e of day.editions){s+=`<div class="archive-edition"><a href="${esc(e.reading)}">${esc(e.label)} · 阅读 →</a><a class="archive-original" href="${esc(e.original)}">原始页面</a><ul class="archive-title-list">`;for(const item of e.titles)s+=`<li><a href="${esc(e.reading)}#${esc(item.anchor)}">${esc(item.title)}</a></li>`;s+='</ul></div>';}s+='</details></section>';}return s+'</details>';}).join('');}
function expectedHTML(baseline,data,preferred){const start=baseline.indexOf('<details class="archive-month-group"'),end=baseline.indexOf('</div></div><section class="archive-section">',start);if(start<0||end<0)fail('Retained archive HTML template unsupported');let out=baseline.slice(0,start)+renderMonths(data,preferred)+baseline.slice(end);const latest=/<a class="latest-report" href="[^"]*">[^<]*<\/a>/g,matches=[...out.matchAll(latest)];if(matches.length!==1)fail('One retained latest-report link required');const date=data[0].date;return out.replace(latest,`<a class="latest-report" href="${esc(asHref(preferred[date]))}">阅读最新日报 · ${date} →</a>`);}
function restoreManifestFormatting(manifest,oldParsed){const text=JSON.stringify(manifest,null,2)+'\n',doc=parse(text),patches=[];for(const node of doc.nodes){if(node.path.length===2&&node.path[0]==='entries'&&node.path[1]<oldParsed.value.entries.length)patches.push({start:node.start,end:node.end,value:rawNode(oldParsed,node.path)});if(node.path.length===2&&node.path[0]==='files'&&Object.hasOwn(oldParsed.value.files,node.path[1])&&!NAVIGATION_FILES.includes(node.path[1]))patches.push({start:node.start,end:node.end,value:rawNode(oldParsed,node.path)});}return Buffer.from(replace(text,patches));}

/** Candidate may contain regenerated historical readings/manifest metadata;
 * those bytes are NEVER admitted. Only original baseline historical bytes and
 * raw entry/file-metadata records are carried into the composed output.
 */
export function composeNavigationAppend({baseline,candidate,approvedEntries,mappings}){
 mappings=validateMappings(mappings);const old=archiveManifest(baseline),rendered=archiveManifest(candidate,{bind:false}),oldKeys=new Set(old.value.entries.map(e=>e.key));
 if(!Array.isArray(approvedEntries)||!approvedEntries.length||approvedEntries.length>100)fail('Explicit bounded new sealed entries required');
 const approved=new Map();for(const e of approvedEntries){if(!isObject(e)||typeof e.key!=='string'||approved.has(e.key)||oldKeys.has(e.key)||e.category!=='reports'||!/^\d{4}-\d{2}-\d{2}$/.test(e.date??'')||e.page!==e.key+'/index.html'||!candidate.has(e.page)||!/^[a-f0-9]{64}$/.test(e.approval_sha256??'')||e.identity!==e.source_sha256||e.source_sha256!==digest(candidate.get(e.page))||!e.key.startsWith('reports/'+e.date+'-')||!e.key.endsWith('-'+e.identity.slice(0,12))||!/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(e.key.slice(('reports/'+e.date+'-').length,-13))||!Number.isFinite(Date.parse(e.created_at??'')))fail('Invalid new sealed report approval');approved.set(e.key,e);}
 same(rendered.value.entries.filter(e=>!oldKeys.has(e.key)).map(e=>e.key).sort(),[...approved.keys()].sort(),'Unapproved rendered entry set');for(const e of rendered.value.entries.filter(e=>approved.has(e.key)))same(e,approved.get(e.key),'Rendered new entry differs from approval');
 same(Object.keys(rendered.value).sort(),Object.keys(old.value).sort(),'Unapproved manifest top-level fields');for(const k of Object.keys(old.value))if(!['entries','files','navigation'].includes(k))same(rendered.value[k],old.value[k],'Manifest field changed');
 const allEntries=[...old.value.entries,...approvedEntries.map(clone)];
 const out=new Map([...baseline].map(([p,b])=>[p,Buffer.from(b)]));const newPaths=[];
 for(const [path,bytes]of candidate){if(baseline.has(path))continue;if(!permitted(path)||(!/^assets\/[a-f0-9]{64}\.png$/.test(path)&&![...approved.keys()].some(key=>path.startsWith(key+'/'))))fail('Unapproved new archive path');if(path.startsWith('assets/')&&path!==`assets/${digest(bytes)}.png`)fail('New asset content hash mismatch');same(rendered.value.files[path],metadata(bytes),'New file does not match rendered manifest');out.set(path,Buffer.from(bytes));newPaths.push(path);}
 for(const e of approvedEntries){if(!out.has(e.page)||!out.has(e.key+'/reading.html'))fail('New sealed original and reading page required');const reading=utf8(out.get(e.key+'/reading.html')),source=utf8(out.get(e.page));const style=/<!-- archive-navigation-style:v1 -->[\s\S]*?<!-- \/archive-navigation-style:v1 -->/g,nav=/<!-- archive-navigation:v1 -->[\s\S]*?<!-- \/archive-navigation:v1 -->/g;if([...reading.matchAll(style)].length!==1||[...reading.matchAll(nav)].length!==1||reading.replace(style,'').replace(nav,'')!==source)fail('New reading changes sealed scientific source');if(reading!==expectedReading(source,e,allEntries,baseline))fail('New reading navigation differs from exact retained renderer template');if(out.has(e.key+'/original.html')&&!out.get(e.key+'/original.html').equals(out.get(e.page)))fail('New original copy differs from sealed source');}
 // No candidate-only metadata may survive for a discarded/missing file.
 for(const path of Object.keys(rendered.value.files))if(!baseline.has(path)&&!out.has(path))fail('Rendered manifest contains unadmitted file metadata');
 const allKeys=new Set(allEntries.map(e=>e.key));for(const e of approvedEntries)if(!mappings.some(rule=>rule.from==='/'+e.key+'/reading.html'))fail('Every new report requires a validated projection mapping');for(const rule of mappings){const key=rule.from.slice(1,-'/reading.html'.length);if(!allKeys.has(key)||!out.has(rule.from.slice(1)))fail('Projection mapping lacks retained sealed edition');}
 const oldData=mappedData(baseline.get('assets/archive-browser.js'),mappings),candidateData=mappedData(candidate.get('assets/archive-browser.js'),mappings);if(oldData.tail!==candidateData.tail)fail('Archive JavaScript behavior changed');
 const titles=sourceTitles(approvedEntries,out),expectedEditions=new Map(),entryByOriginal=new Map(allEntries.map(e=>['/'+e.page,e]));for(const day of oldData.value)for(const edition of day.editions)expectedEditions.set(edition.original,{date:day.date,edition});for(const e of approvedEntries)expectedEditions.set('/'+e.page,{date:e.date,edition:{label:label(e),reading:remapReadingURL('/'+e.key+'/reading.html',mappings),original:'/'+e.page,titles:titles[e.key]}});
 const seen=new Set();for(const day of candidateData.value)for(const edition of day.editions){const expected=expectedEditions.get(edition.original);if(!expected||expected.date!==day.date)fail('Unapproved navigation row');same(edition,expected.edition,'Navigation row changed or unbound to sealed titles/anchors');seen.add(edition.original);}
 same([...seen].sort(),[...expectedEditions.keys()].sort(),'Missing retained or new navigation row');same(candidateData.value.map(d=>d.date),[...new Set([...expectedEditions.values()].map(e=>e.date))].sort().reverse(),'Archive dates must remain newest first');
 for(const day of candidateData.value){const expectedOrder=[...expectedEditions].filter(([,e])=>e.date===day.date).map(([original])=>original).sort((a,b)=>(entryByOriginal.get(b)?.created_at??'').localeCompare(entryByOriginal.get(a)?.created_at??''));same(day.editions.map(e=>e.original),expectedOrder,'Edition order changed');}
 // Preserve complete old day JSON bytes when its membership is unchanged;
 // preserve individual old edition JSON bytes for same-day additions.
 const rows=candidateData.value.map((day,i)=>{const oldIndex=oldData.value.findIndex(d=>d.date===day.date),oldDay=oldData.value[oldIndex];if(oldDay&&oldDay.editions.length===day.editions.length)return rawNode(oldData,[oldIndex]);let raw=rawNode(candidateData,[i]);if(oldDay){const parsed=parse(raw),patches=[];for(let j=0;j<day.editions.length;j++){const oi=oldDay.editions.findIndex(e=>e.original===day.editions[j].original);if(oi>=0){const node=parsed.nodes.find(n=>canonical(n.path)===canonical(['editions',j]));patches.push({start:node.start,end:node.end,value:rawNode(oldData,[oldIndex,'editions',oi])});}}raw=replace(raw,patches);}return raw;});
 out.set('assets/archive-browser.js',Buffer.from(oldData.prefix+'['+rows.join(', ')+'];'+oldData.tail));
 const navigation=clone(old.value.navigation);for(const e of approvedEntries)navigation.reading_pages[e.key]=e.key+'/reading.html';for(const rule of mappings){for(const group of ['reading_pages','preferred_by_date'])for(const [key,value]of Object.entries(navigation[group]))navigation[group][key]=remapReadingURL(value,mappings,{relativeSource:true});}
 for(const date of new Set(approvedEntries.map(e=>e.date))){const day=candidateData.value.find(d=>d.date===date);navigation.preferred_by_date[date]=day.editions[0].reading;}navigation.search_title_count=candidateData.value.reduce((sum,d)=>sum+d.editions.reduce((n,e)=>n+e.titles.length,0),0);
 const unremappedManifest={...clone(old.value),entries:allEntries,files:clone(old.value.files),navigation};for(const path of newPaths)unremappedManifest.files[path]=metadata(out.get(path));
 for(const path of ['index.html','archive/index.html'])out.set(path,Buffer.from(candidate.get(path)));
 for(const path of NAVIGATION_FILES.slice(0,3))unremappedManifest.files[path]=metadata(out.get(path));out.set('manifest.json',restoreManifestFormatting(unremappedManifest,old));
 // Existing URL-only helper maps only anchor hrefs, the data line and selected
 // manifest values. This keeps comments/text and all other attributes exact.
 const mapped=planNavigationRemap({files:out,mappings}).files;
 for(const path of ['index.html','archive/index.html']){const expected=expectedHTML(utf8(baseline.get(path)),candidateData.value,navigation.preferred_by_date);if(utf8(mapped.get(path))!==expected)fail('Rendered archive HTML contains unrelated changes or invalid projected links: '+path);}
 const finalManifest=archiveManifest(mapped);for(const [path,bytes]of baseline){if(NAVIGATION_FILES.includes(path))continue;if(!bytes.equals(mapped.get(path)))fail('Historical file changed');same(finalManifest.value.files[path],old.value.files[path],'Historical file metadata changed');if(rawNode(finalManifest,['files',path])!==rawNode(old,['files',path]))fail('Historical file metadata bytes changed');}
 for(let i=0;i<old.value.entries.length;i++)if(rawNode(finalManifest,['entries',i])!==rawNode(old,['entries',i]))fail('Historical entry bytes changed');
 return {files:mapped,proof:{schema:'daily-agent-sealed-navigation-append-v1',prior_files:baseline.size,output_files:mapped.size,new_entries:approvedEntries.map(e=>e.key),new_files:newPaths.sort(),historical_non_global_files_preserved:baseline.size-4,historical_entries_preserved:old.value.entries.length,historical_manifest_metadata_byte_preserved:true,historical_entry_records_byte_preserved:true,historical_dataset_rows_preserved_except_approved_mapping:true,javascript_tail_sha256:digest(oldData.tail),new_titles_bound_to_sealed_originals:true,global_html_exact_projection_verified:true,historical_reading_output_reused:true,mappings:clone(mappings),before_inventory_sha256:digest(JSON.stringify(inventory(baseline))),after_inventory_sha256:digest(JSON.stringify(inventory(mapped))),deployed:false}};
}

/** Independent re-derivation; rejects tampered output even when rehashed. */
export function verifyNavigationAppend({baseline,candidate,approvedEntries,mappings,after}){const expected=composeNavigationAppend({baseline,candidate,approvedEntries,mappings});same([...after.keys()].sort(),[...expected.files.keys()].sort(),'Composed file set changed');for(const [path,bytes]of expected.files)if(!Buffer.isBuffer(after.get(path))||!bytes.equals(after.get(path)))fail('Unapproved composed bytes: '+path);return expected.proof;}
