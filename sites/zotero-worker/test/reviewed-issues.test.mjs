import test from 'node:test';
import assert from 'node:assert/strict';
import {reviewedFixture,sha,utf8} from './reviewed-issue-fixture.mjs';
import {fixture as legacyFixture} from './private-bundles-fixture.mjs';
import {handleZoteroRequest} from '../worker/index.js';
import {prepareTrustedCatalog,prepareReviewedCatalog} from '../src/catalog-registration.mjs';
import {validateReviewedIssue,validateReviewedRegistry,issueHash,reviewedPdfUrl} from '../src/reviewed-issues.mjs';
import {fetchReviewedPdf} from '../src/reviewed-pdf.mjs';
import {renderReviewedPage} from '../src/reviewed-ui.mjs';
const clone=structuredClone;
async function context(options){
  const f=legacyFixture(),r=await reviewedFixture('2000-01-01',options);f.assets.set('/'+r.entry.page,r.html);
  let originalResponder=null;const priorAssetsFetch=f.env.ASSETS.fetch;
  f.env.ASSETS.fetch=async request=>{
    if(!new URL(request.url).pathname.startsWith('/reports/'))return priorAssetsFetch(request);
    f.calls.push(['original',request.url]);assert.equal(request.redirect,'manual');assert.equal(request.credentials,'omit');
    for(const name of ['Authorization','Cookie','oai-authenticated-user-id','X-Zotero-Action'])assert.equal(request.headers.get(name),null);
    if(originalResponder)return originalResponder(request);
    const bytes=f.assets.get(new URL(request.url).pathname);return new Response(bytes||'missing',{status:bytes?200:404});
  };
  let issues=[r.issue];const call=async(path,{body,identity=f.env.ZOTERO_OWNER_ID,origin=f.env.ZOTERO_SITE_ORIGIN,method=body?'POST':'GET',headers={}}={})=>{
    const response=await handleZoteroRequest(new Request(f.env.ZOTERO_SITE_ORIGIN+path,{method,headers:{'oai-authenticated-user-id':identity,Origin:origin,'Content-Type':'application/json','X-Zotero-Action':'1',...headers},...(body?{body:JSON.stringify(body)}:{})}),f.env,{reviewedIssues:issues,bundleManifest:f.manifest,fetchImpl:async()=>{throw new Error('NO_NETWORK');},pdfFetchImpl:async()=>{throw new Error('NO_PDF_NETWORK');}});
    return {status:response.status,headers:response.headers,text:await response.text()};
  };
  const base='/api/zotero/reviewed-issues/'+r.issue.issue_id;
  const body={action:'save_selected',projection_sha256:r.issue.projection_sha256,connection_version:1,paper_ids:['paper-1','paper-2'],pdf_intent:'metadata_only'};
  return {...f,...r,call,base,body,setIssues:v=>issues=v,setOriginalResponder:fn=>originalResponder=fn,changes:()=>f.db.sqlite.prepare('SELECT total_changes() AS n').get().n};
}
test('reviewed metadata admission is separate from strict all-PDF fallback',async()=>{
  const f=await reviewedFixture(),b=f.issue.batches[0],input={binding:b.binding,trustedPins:b.trustedPins,reportBytes:utf8(b.report),catalogBytes:utf8(b.catalog),htmlBytes:f.html};
  await assert.rejects(prepareTrustedCatalog(input),e=>e.code==='PDF_READER_REQUIRED');
  const ready=await prepareReviewedCatalog(input);assert.equal(ready.blobs.length,0);assert.equal(JSON.parse(ready.paperRows[0].metadata_json).pdf.sha256,JSON.parse(b.catalog).papers[0].pdf.sha256);
  await assert.rejects(prepareReviewedCatalog({...input,htmlBytes:utf8('changed')}));
});
test('two dates use one generic projection path, complete batch union and immutable hashes',async()=>{
  const a=await reviewedFixture(),b=await reviewedFixture('2000-01-02');await validateReviewedRegistry([a.issue,b.issue]);
  for(const edit of [i=>i.paper_ids.pop(),i=>i.anchors.pop(),i=>i.batches[0].catalog+=' ',i=>i.batches[0].binding.owner_id='other',i=>i.batches.push(i.batches[0])]){
    const i=clone(a.issue);edit(i);const {projection_sha256,...payload}=i;i.projection_sha256=await issueHash(payload);await assert.rejects(validateReviewedIssue(i,a.html));
  }
  await assert.rejects(validateReviewedRegistry([a.issue,a.issue]));
});
test('read, owner page and refresh cause no D1/R2 writes or upstream requests; legacy global gate stays closed',async()=>{
  const f=await context(),before=f.changes();
  const status=await f.call(f.base);assert.equal(status.status,200);assert.equal(JSON.parse(status.text).live_ready,true);
  const page=await f.call('/zotero/reports/'+f.issue.issue_id);assert.equal(page.status,200);assert.ok(page.text.includes('data-da-z-paper="paper-1"'));assert.equal(page.text.includes(' checked'),false);
  assert.equal(page.headers.get('X-Original-HTML-SHA256'),sha(f.html));assert.equal(f.changes(),before);assert.equal(f.objects.size,0);assert.equal(f.upstream.length,0);
  const legacy=await f.call('/api/zotero/catalogs');assert.equal(JSON.parse(legacy.text).live_ready,false);
});
test('one report Save spans both batches, creates only selected jobs, and repeats idempotently without PDFs',async()=>{
  const f=await context(),first=await f.call(f.base+'/save',{body:f.body});assert.equal(first.status,201,first.text);const jobs=JSON.parse(first.text).jobs;assert.equal(jobs.length,2);
  const repeated=await f.call(f.base+'/save',{body:f.body});assert.deepEqual(JSON.parse(repeated.text).jobs.map(j=>j.job_id),jobs.map(j=>j.job_id));assert.ok(JSON.parse(repeated.text).jobs.every(j=>!j.created));
  assert.equal(f.db.sqlite.prepare('SELECT COUNT(*) AS n FROM zotero_execution_items').get().n,2);assert.equal(f.objects.size,0);assert.equal(f.upstream.length,0);
});
test('unselected batch never registered and arbitrary metadata or URLs rejected before mutation',async()=>{
  const f=await context(),before=f.changes();
  for(const body of [{...f.body,paper_ids:['not-in-issue']},{...f.body,paper_ids:['paper-1','paper-1']},{...f.body,url:'https://internal/'},{...f.body,projection_sha256:'0'.repeat(64)}])assert.ok((await f.call(f.base+'/save',{body})).status>=400);
  assert.equal(f.changes(),before);assert.equal((await f.call(f.base+'/save',{body:{...f.body,paper_ids:['paper-2']}})).status,201);
  assert.equal(f.db.sqlite.prepare('SELECT COUNT(*) AS n FROM zotero_report_catalogs').get().n,1);
});
test('owner authentication, CSRF and unsupported old issue fail closed',async()=>{
  const f=await context(),before=f.changes();
  for(const identity of ['', 'another-owner'])assert.ok((await f.call(f.base,{identity})).status>=400);
  assert.equal((await f.call(f.base+'/save',{body:f.body,origin:'https://other.test'})).status,403);
  assert.equal((await f.call('/zotero/reports/not-registered')).status,404);assert.equal(f.changes(),before);
});
test('current connection version permits refreshed same-library reconnect but rejects stale intent and other library',async()=>{
  const f=await context();f.db.sqlite.prepare('UPDATE zotero_active_connections SET connection_version=2').run();
  assert.equal((await f.call(f.base+'/save',{body:f.body})).status,409);
  assert.equal(JSON.parse((await f.call(f.base)).text).connection_version,2);
  assert.equal((await f.call(f.base+'/save',{body:{...f.body,connection_version:2}})).status,201);
  f.db.sqlite.prepare("UPDATE zotero_active_connections SET library_id='555'").run();assert.equal(JSON.parse((await f.call(f.base)).text).live_ready,false);
});
test('HTML transformation retains original scientific bytes around exact insertions and does not mutate source',async()=>{
  const f=await reviewedFixture(),original=new Uint8Array(f.html),r=renderReviewedPage(f.html,f.issue),text=await r.text();assert.deepEqual(f.html,original);
  assert.ok(text.includes('已封存科学内容 1。'));assert.equal((text.match(/data-da-z-paper=/g)||[]).length,2);
  assert.ok(r.headers.get('Content-Security-Policy').includes("connect-src 'self'"));assert.ok(!r.headers.get('Content-Security-Policy').includes("script-src 'unsafe-inline'"));
  assert.ok(!text.includes('content="default-src'));assert.ok(text.includes('/zotero/reviewed-client.js'));
});
test('PDF network policy rejects arbitrary hosts, credentials, internal IPs and login paths',async()=>{
  for(const url of ['http://arxiv.org/pdf/2001.00001','https://127.0.0.1/pdf/2001.00001','https://arxiv.org@evil.test/pdf/2001.00001','https://arxiv.org:444/pdf/2001.00001','https://arxiv.org/login','https://evil.test/pdf/2001.00001','https://arxiv.org/pdf/2001.00001?token=x'])assert.equal(reviewedPdfUrl(url),false);
  assert.equal(reviewedPdfUrl('https://arxiv.org/pdf/2001.00001'),true);
});
test('PDF fetch strips credentials, validates each redirect, hash, signature, type and exact stream length',async()=>{
  const bytes=utf8('%PDF-1.4\nSYNTHETIC\n%%EOF\n'),base={url:'https://arxiv.org/pdf/2001.00001',sha256:sha(bytes),size:bytes.length};let calls=0;
  const good=async(url,options)=>{calls++;assert.equal(options.credentials,'omit');assert.equal(options.redirect,'manual');assert.deepEqual(options.headers,{Accept:'application/pdf'});return new Response(bytes,{headers:{'Content-Type':'application/pdf'}});};
  assert.deepEqual(await fetchReviewedPdf({...base,fetchImpl:good}),bytes);assert.equal(calls,1);
  calls=0;assert.equal(await fetchReviewedPdf({...base,fetchImpl:async()=>{calls++;return new Response(null,{status:302,headers:{Location:'https://127.0.0.1/'}});}}),null);assert.equal(calls,1);
  for(const response of [()=>new Response(bytes,{headers:{'Content-Type':'text/html'}}),()=>new Response(utf8('%PDF-WRONG'),{headers:{'Content-Type':'application/pdf'}}),()=>new Response(new Uint8Array(bytes.length+1),{headers:{'Content-Type':'application/pdf'}})])assert.equal(await fetchReviewedPdf({...base,fetchImpl:response}),null);
  assert.equal(await fetchReviewedPdf({...base,timeoutMs:5,fetchImpl:()=>new Promise(()=>{})}),null);
});

test('a dropped complete batch cannot redefine the independently sealed issue paper set',async()=>{
  const f=await reviewedFixture(),i=clone(f.issue);i.batches.pop();i.paper_ids.pop();i.anchors.pop();const {projection_sha256,...payload}=i;i.projection_sha256=await issueHash(payload);
  await assert.rejects(validateReviewedIssue(i,f.html));
});

test('per-paper capability distinguishes unsupported APS/Nature/PMLR sources from reviewed arXiv fetches',async()=>{
  for(const host of ['journals.aps.org','www.nature.com','proceedings.mlr.press']){
    const f=await context({publishers:[host,'arxiv.org']}),before=f.changes(),status=await f.call(f.base);assert.equal(status.status,200,status.text);
    const [unsupported,supported]=JSON.parse(status.text).papers;
    assert.equal(unsupported.pdf_reviewed,true);assert.equal(unsupported.pdf_fetch_supported,false);assert.equal(unsupported.pdf_status,'cache_only_on_save');assert.equal(unsupported.pdf_cache_status,'not_checked');
    assert.equal(supported.pdf_fetch_supported,true);assert.equal(supported.pdf_status,'cache_or_reviewed_fetch_on_save');
    assert.equal(f.changes(),before);assert.equal(f.upstream.length,0);assert.equal(f.objects.size,0);
    const save=await f.call(f.base+'/save',{body:{...f.body,paper_ids:['paper-1'],pdf_intent:'metadata_only'}});assert.equal(save.status,201,save.text);
  }
});

test('ASSETS 307 exact directory canonicalization follows once without credentials and preserves the sealed hash',async()=>{
  const f=await context(),original=f.env.ZOTERO_SITE_ORIGIN+'/'+f.entry.page,canonical=new URL('./',original),requests=[];
  f.setOriginalResponder(request=>{requests.push(request.url);return request.url===original?new Response(null,{status:307,headers:{Location:canonical.pathname}}):new Response(f.html);});
  const response=await f.call('/zotero/reports/'+f.issue.issue_id);assert.equal(response.status,200,response.text);assert.equal(response.headers.get('X-Original-HTML-SHA256'),sha(f.html));assert.deepEqual(requests,[original,canonical.href]);
});
test('ASSETS canonicalization rejects wrong origins/paths, extra URL parts, arbitrary relative forms, second hops and changed bytes',async()=>{
  const f=await context(),original=f.env.ZOTERO_SITE_ORIGIN+'/'+f.entry.page,canonical=new URL('./',original);
  const destinations=['https://other.example.test'+canonical.pathname,canonical.pathname+'different/',canonical.pathname+'?x=1',canonical.pathname+'#hash',canonical.href.replace('https://','https://user@'),canonical.href.replace('.test/','.test:8443/'),'./','//daily.example.test'+canonical.pathname];
  for(const location of destinations){let calls=0;f.setOriginalResponder(()=>{calls++;return new Response(null,{status:307,headers:{Location:location}});});assert.ok((await f.call(f.base)).status>=400);assert.equal(calls,1);}
  for(const second of [()=>new Response(null,{status:307,headers:{Location:canonical.pathname}}),()=>new Response(utf8('changed'))]){
    let calls=0;f.setOriginalResponder(()=>++calls===1?new Response(null,{status:307,headers:{Location:canonical.href}}):second());assert.ok((await f.call(f.base)).status>=400);assert.equal(calls,2);
  }
});
