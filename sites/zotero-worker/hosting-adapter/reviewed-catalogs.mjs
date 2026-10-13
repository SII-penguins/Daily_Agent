/** Post-seal data projection only. Scientific source and archive bytes are inputs. */
import assert from 'node:assert/strict';
import {readFile,readdir,lstat} from 'node:fs/promises';
import {resolve} from 'node:path';
import {spawnSync} from 'node:child_process';
import {sha256Bytes} from '../src/hash-bytes.mjs';
import {REVIEWED_SCHEMA,MAX_REVIEWED_BYTES,MAX_ISSUE_BYTES,issueHash,issueJson,validateReviewedIssue,validateReviewedRegistry} from '../src/reviewed-issues.mjs';
export const CATALOG_PREFIX='zotero/reviewed-issues/';
export const catalogDataPath=path=>/^zotero\/reviewed-issues\/[A-Za-z0-9][A-Za-z0-9_-]{0,95}\.json$/.test(path);
export async function boundedFile(path,max=MAX_ISSUE_BYTES){const s=await lstat(path);if(!s.isFile()||s.isSymbolicLink()||s.size>max)throw new Error('Bounded regular source required');return readFile(path);}
/** HTMLParser, not a regex, validates unique structural article/section anchors.
 * Only generated control insertions/CSP removal are permitted in the new response.
 * Offsets are UTF8 byte positions, so Unicode scientific prose stays exact. */
export function inspectReadingHtml(bytes,mapping,papers) {
  const run=spawnSync('python3',['-c',String.raw`import sys,json
from html.parser import HTMLParser
x=json.load(sys.stdin);text=x['html'];lines=text.splitlines(keepends=True)
class P(HTMLParser):
 def __init__(self): super().__init__(convert_charrefs=True);self.ids={};self.csp=[];self.head=None;self.body=None;self.captures=[];self.done={}
 def pos(self):
  line,col=self.getpos();return len((''.join(lines[:line-1])+lines[line-1][:col]).encode())
 def handle_starttag(self,tag,attrs):
  a=dict(attrs);start=self.pos();end=start+len(self.get_starttag_text().encode())
  for capture in self.captures:
   if capture['tag']==tag: capture['depth']+=1
  if a.get('id') in {m['anchor'] for m in x['mapping']}: self.captures.append(dict(anchor=a['id'],tag=tag,depth=1,text=[],links=[]))
  for capture in self.captures:
   if tag=='a' and 'href' in a: capture['links'].append(a['href'])
  if len(a)!=len(attrs) or any(k.startswith('on') for k,v in attrs): raise ValueError('Active or duplicate attribute')
  if tag in ('script','form','input','button','select','textarea','iframe','object','embed','base','svg','math','template','noscript'): raise ValueError('Active original unsupported')
  for k,v in attrs:
   if k in ('srcdoc','action','formaction'): raise ValueError('Active original unsupported')
   if k in ('href','src','xlink:href') and ''.join((v or '').split()).lower().startswith(('javascript:','vbscript:','data:text/html')): raise ValueError('Active URL')
  if 'id' in a:
   if a['id'] in self.ids or a['id'].startswith('da-z-'): raise ValueError('Duplicate/reserved ID')
   self.ids[a['id']]={'tag':tag,'offset':end}
  if tag=='head':
   if self.head is not None: raise ValueError('Duplicate head')
   self.head=end
  if tag=='meta':
   if a.get('http-equiv','').lower()=='refresh': raise ValueError('Refresh forbidden')
   if a.get('http-equiv','').lower()=='content-security-policy': self.csp.append([start,end])
 def handle_data(self,data):
  for capture in self.captures: capture['text'].append(data)
 def handle_endtag(self,tag):
  for capture in list(self.captures):
   if capture['tag']==tag:
    capture['depth']-=1
    if capture['depth']==0: self.done[capture['anchor']]=capture;self.captures.remove(capture)
  if tag=='body':
   if self.body is not None: raise ValueError('Duplicate body')
   self.body=self.pos()
p=P();p.feed(text);p.close()
if p.head is None or p.body is None: raise ValueError('Complete sealed HTML required')
anchors=[]
for a in x['mapping']:
 found=p.ids.get(a['anchor'])
 if not found or found['tag'] not in ('article','section'): raise ValueError('Missing or nonstructural paper anchor')
 evidence=p.done.get(a['anchor']);paper=next((r for r in x['papers'] if r['paper_id']==a['paper_id']),None)
 if not evidence or not paper or ' '.join(paper['title'].split()).casefold() not in ' '.join(' '.join(evidence['text']).split()).casefold() or paper['source_url'] not in evidence['links']: raise ValueError('Paper anchor identity mismatch')
 anchors.append(dict(a,offset=found['offset']))
print(json.dumps(dict(anchors=anchors,csp_ranges=p.csp,head_offset=p.head,body_offset=p.body)))
`],{input:JSON.stringify({html:new TextDecoder('utf8',{fatal:true}).decode(bytes),mapping,papers}),encoding:'utf8',maxBuffer:20*1024*1024,timeout:10000});
  if(run.error||run.status!==0)throw new Error('Sealed HTML anchor validation failed');
  return JSON.parse(run.stdout);
}
// Descriptive legacy review detail has no approval semantics and is never projected.
function boundedLegacyDetail(value){
  const bytes=value=>new TextEncoder().encode(value).length;
  const scalar=value=>value===null || typeof value==='boolean' || typeof value==='number' && Number.isFinite(value) && Math.abs(value)<=Number.MAX_SAFE_INTEGER || typeof value==='string' && bytes(value)<=4096;
  if(value===null || typeof value==='string')return scalar(value);
  if(!value || typeof value!=='object' || Array.isArray(value) || ![Object.prototype,null].includes(Object.getPrototypeOf(value)))return false;
  const keys=Object.keys(value);
  return keys.length>=1 && keys.length<=16 && bytes(JSON.stringify(value))<=8192
    && keys.every(key=>/^[A-Za-z][A-Za-z0-9_ -]{0,63}$/.test(key) && !['constructor','prototype','__proto__'].includes(key) && scalar(value[key]));
}
export async function buildReviewedIssue({issue_id,entry,htmlBytes,batches,anchors,paper_ids,issueReportBytes,issueReportSha256,legacyMigration}) {
  if(!entry || entry.page!==entry.key+'/index.html' || entry.identity!==entry.source_sha256
      || !Array.isArray(anchors) || anchors.some(a=>Object.keys(a).sort().join(',')!=='anchor,paper_id'))throw new Error('Reviewed sealed archive entry required');
  let seal;
  if(legacyMigration!==undefined){
    if(legacyMigration?.approved!==true || Object.hasOwn(entry,'approval_sha256'))throw new Error('Explicit legacy migration without a fabricated approval required');
    const previous=legacyMigration.previousManifest?.entries?.filter(e=>e.page===entry.page);
    if(previous?.length!==1)throw new Error('Legacy migration requires an already-existing sealed entry');
    assert.deepEqual(previous[0],entry,'Legacy migration cannot substitute or alter an existing entry');
    const {compositionBytes,independentStaticReviewBytes}=legacyMigration;
    if(!(compositionBytes instanceof Uint8Array) || !(independentStaticReviewBytes instanceof Uint8Array)
        || compositionBytes.length>16*1024*1024 || independentStaticReviewBytes.length>16*1024*1024
        || await sha256Bytes(compositionBytes)!==entry.composition_manifest_sha256
        || await sha256Bytes(independentStaticReviewBytes)!==entry.independent_static_review_sha256)throw new Error('Legacy evidence bytes/hash mismatch');
    const composition=JSON.parse(new TextDecoder('utf8',{fatal:true}).decode(compositionBytes));
    const review=JSON.parse(new TextDecoder('utf8',{fatal:true}).decode(independentStaticReviewBytes));
    const fullReport=JSON.parse(new TextDecoder('utf8',{fatal:true}).decode(issueReportBytes));
    const count=fullReport.qualified_papers,component=composition.paper_component_seal;
    if(composition?.schema!=='daily-agent-oct13-host-composition-v1' || composition.schema!==entry.composition_schema
        || review?.schema!=='daily-agent-independent-static-composition-review-v1'
        || composition.independent_static_review_decision!=='PASS_STATIC_ENVELOPE' || review.verdict!=='PASS_STATIC_ENVELOPE'
        || composition.independent_static_review_sha256!==entry.independent_static_review_sha256
        || composition.paper_articles_preserved_byte_exact!==true || composition.issue_date!==entry.date
        || typeof composition.edition_id!=='string' || !composition.edition_id
        || composition.html?.sha256!==entry.source_sha256 || review.html_sha256!==entry.source_sha256
        || composition.html?.bytes!==htmlBytes.length || typeof composition.html?.path!=='string' || !composition.html.path
        || !/^[a-f0-9]{64}$/.test(component?.sha256) || review.paper_component_sha256!==component.sha256
        || typeof component.path!=='string' || !component.path || !Number.isFinite(Date.parse(component.sealed_at))
        || !Number.isSafeInteger(count) || count<1 || composition.counts?.papers!==count || component.qualified!==count
        || !Array.isArray(review.papers) || review.papers.length!==count
        || !Array.isArray(review.checks) || !review.checks.length || review.checks.length>1000
        || review.checks.some(c=>!c || c.pass!==true || typeof c.check!=='string' || !c.check || !boundedLegacyDetail(c.detail))
        || !Array.isArray(review.required_fixes) || review.required_fixes.length)throw new Error('Legacy composition/review evidence binding mismatch');
    // Static envelope evidence is never promoted to browser QA. browser_qa is
    // deliberately not projected into a browser acceptance capability.
    seal={legacy_seal:{kind:'legacy-sealed-migration-v1',entry_path:entry.page,html_sha256:entry.source_sha256,
      composition_schema:entry.composition_schema,composition_manifest_sha256:entry.composition_manifest_sha256,
      independent_static_review_sha256:entry.independent_static_review_sha256,full_adapter_sha256:issueReportSha256}};
  }else{
    if(!/^[a-f0-9]{64}$/.test(entry.approval_sha256))throw new Error('Normal daily approval_sha256 required; legacy admission is never implicit');
    seal={approval_sha256:entry.approval_sha256};
  }
  const owner_id=batches?.[0]?.binding?.owner_id,personal_scope=batches?.[0]?.binding?.personal_scope;
  const payload={schema:REVIEWED_SCHEMA,issue_id,day:entry.date,entry_path:entry.page,html_sha256:entry.source_sha256,
    html_bytes:htmlBytes.length,...seal,issue_report:new TextDecoder('utf8',{fatal:true}).decode(issueReportBytes),issue_report_sha256:issueReportSha256,owner_id,personal_scope,paper_ids,batches,
    ...inspectReadingHtml(htmlBytes,anchors,batches.flatMap(b=>JSON.parse(b.catalog).papers))};
  const issue={...payload,projection_sha256:await issueHash(payload)};
  await validateReviewedIssue(issue,htmlBytes);
  return issue;
}
export async function verifyIssueAgainstArchive(issue,manifest,readHtml) {
  const entries=manifest.entries?.filter(e=>e.page===issue.entry_path);
  if(entries?.length!==1)throw new Error('Projection archive entry missing/duplicated');
  const entry=entries[0];
  if(entry.source_sha256!==issue.html_sha256 || entry.identity!==issue.html_sha256 || entry.date!==issue.day
      || manifest.files?.[issue.entry_path]?.sha256!==issue.html_sha256 || manifest.files?.[issue.entry_path]?.size!==issue.html_bytes)throw new Error('Projection seal mismatch');
  if(issue.legacy_seal){
    if(Object.hasOwn(entry,'approval_sha256') || ['composition_schema','composition_manifest_sha256','independent_static_review_sha256'].some(k=>entry[k]!==issue.legacy_seal[k]))throw new Error('Legacy projection seal mismatch');
  }else if(entry.approval_sha256!==issue.approval_sha256)throw new Error('Projection seal mismatch');
  const html=await readHtml(issue.entry_path);
  await validateReviewedIssue(issue,html);
  assert.deepEqual(inspectReadingHtml(html,issue.anchors.map(({paper_id,anchor})=>({paper_id,anchor})),issue.batches.flatMap(b=>JSON.parse(b.catalog).papers)),
    {anchors:issue.anchors,csp_ranges:issue.csp_ranges,head_offset:issue.head_offset,body_offset:issue.body_offset},'Projection anchor/CSP mapping differs from sealed original');
}
export async function loadReviewedIssues(root) {
  const dir=resolve(root,CATALOG_PREFIX);let entries;
  try{const s=await lstat(dir);if(!s.isDirectory()||s.isSymbolicLink())throw new Error('Invalid reviewed catalog directory');entries=await readdir(dir,{withFileTypes:true});}
  catch(error){if(error.code==='ENOENT')return [];throw error;}
  const manifest=JSON.parse(await boundedFile(resolve(root,'public/manifest.json'),16*1024*1024));
  let total=0;const issues=[];
  for(const e of entries.sort((a,b)=>a.name.localeCompare(b.name,'en'))){
    if(!e.isFile()||!catalogDataPath(CATALOG_PREFIX+e.name))throw new Error('Unexpected reviewed catalog data path');
    const raw=await boundedFile(resolve(dir,e.name));if((total+=raw.length)>MAX_REVIEWED_BYTES)throw new Error('Accumulated reviewed catalogs exceed compiled budget');
    const issue=JSON.parse(raw);if(e.name!==issue.issue_id+'.json' || raw.toString()!==issueJson(issue)+'\n')throw new Error('Noncanonical reviewed catalog file');
    await verifyIssueAgainstArchive(issue,manifest,path=>boundedFile(resolve(root,'public',path),16*1024*1024));issues.push(issue);
  }
  return validateReviewedRegistry(issues);
}
/** Ordinary publication allows ONLY new canonical data files. Prior catalogs,
 * runtime, migrations and all other non-public files must remain byte-identical. */
export async function verifyCatalogAppend({prior,current,readCurrent,manifest,oldManifest,migrationAdmission=false}) {
  const old=new Map(prior.map(r=>[r.path,r])),now=new Map(current.map(r=>[r.path,r]));
  for(const row of prior)assert.deepEqual(now.get(row.path),row,'Archive-only publication changed framework/runtime/manifests/migrations or prior catalog');
  const issues=[];let total=0;
  for(const row of current){
    if(!old.has(row.path) && !catalogDataPath(row.path))throw new Error('Archive-only publication changed framework/runtime/manifests/migrations');
    if(!catalogDataPath(row.path))continue;
    const bytes=readCurrent(row.path);if(bytes.length>MAX_ISSUE_BYTES || (total+=bytes.length)>MAX_REVIEWED_BYTES)throw new Error('Compiled catalog budget exceeded');
    const issue=JSON.parse(bytes);if(row.path!==CATALOG_PREFIX+issue.issue_id+'.json' || bytes.toString()!==issueJson(issue)+'\n')throw new Error('Noncanonical projection path/bytes');
    if(!old.has(row.path)){
      if(issue.legacy_seal){
        if(!migrationAdmission)throw new Error('Legacy catalog admission is restricted to explicit initial migration');
        const previous=oldManifest.entries.filter(e=>e.page===issue.entry_path),present=manifest.entries.filter(e=>e.page===issue.entry_path);
        if(previous.length!==1 || present.length!==1)throw new Error('Legacy migration requires an already-existing sealed entry');
        assert.deepEqual(present[0],previous[0],'Legacy migration cannot substitute or alter an existing entry');
      }else if(!migrationAdmission && oldManifest.entries.some(e=>e.page===issue.entry_path))throw new Error('New catalog must belong to the newly sealed archive edition');
    }
    await verifyIssueAgainstArchive(issue,manifest,path=>readCurrent('public/'+path));issues.push(issue);
  }
  await validateReviewedRegistry(issues);
}
