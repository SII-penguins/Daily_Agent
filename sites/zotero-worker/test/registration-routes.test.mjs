import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { mkdtemp, mkdir, writeFile, readFile, stat, readdir, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawnSync } from 'node:child_process';
import { MockD1 } from './mock-d1.mjs';
import { handleRegistrationRoute, REGISTRATION_LIMITS } from '../src/registration-routes.mjs';
import { privateBlobKey, readRegisteredCatalog } from '../src/catalog-registration.mjs';
const utf8 = s => new TextEncoder().encode(s), sha = b => createHash('sha256').update(b).digest('hex');
const origin = 'https://daily.example.test', base = '/api/zotero/catalog-registration';
const binding = { owner_id: 'synthetic-owner', report_id: 'synthetic-report', catalog_id: 'synthetic-catalog', catalog_version: 1, personal_scope: 'personal:12345' };
function fixture() {
  const pdf = utf8('%PDF-1.4\nSYNTHETIC REGISTRATION TEST ONLY\n%%EOF\n'), html = utf8('<title>Synthetic test report</title>');
  const paper = { paper_id: 'synthetic-paper', title: 'Synthetic title', source_url: 'https://publisher.example.test/synthetic',
    doi: '10.9999/synthetic', arxiv: '', authors: ['Synthetic Author'], year: '2000', venue: 'Synthetic Journal',
    item_type: 'journalArticle', publication: 'published_verified', publication_evidence_sha256: 'f'.repeat(64),
    topics: ['other-reviewed'], pdf: { blob_id: sha(pdf), sha256: sha(pdf), size: pdf.length },
    review_sha256: 'a'.repeat(64), qualification_receipt_sha256: 'b'.repeat(64), source_pdf_sha256: sha(pdf) };
  const report = utf8(JSON.stringify({ schema: 'paper-first-corrected-edition-v1', edition_id: 'synthetic-edition', date: '2000-01-02',
    html_sha256: sha(html), html_bytes: html.length, qualified_papers: 1, paper_records: [{ key: paper.paper_id,
      ...Object.fromEntries(['title','source_url','review_sha256','qualification_receipt_sha256','source_pdf_sha256'].map(k => [k,paper[k]])) }] }));
  const catalog = utf8(JSON.stringify({ schema: 'daily-agent-zotero-catalog-v1', report_sha256: sha(report), papers: [paper] }));
  const cfg = { schema: 'daily-agent-zotero-registration-config-v1', approved: true, binding: { ...binding },
    trustedPins: { ...binding, report_sha256: sha(report), html_sha256: sha(html), catalog_sha256: sha(catalog),
      report_edition_id: 'synthetic-edition', allowed_source_hosts: ['publisher.example.test'] },
    pdfs: [{ sha256: sha(pdf), size: pdf.length, paper_id: paper.paper_id }],
    filenames: { report: 'corrected-manifest.json', html: 'report-reviewed.html', catalog: 'verified-catalog.json' }, enable_after_registration: true };
  const db = new MockD1(), objects = new Map(), calls = [];
  const bucket = { async get(key) { calls.push(['get',key]); const bytes = objects.get(key); return bytes ? {size:bytes.length,body:new Response(bytes).body} : null; },
    async put(key, bytes, options) { calls.push(['put',key,options]); assert.deepEqual(options.onlyIf,{etagDoesNotMatch:'*'});
      assert.equal(options.sha256,sha(bytes)); if (objects.has(key)) return null; objects.set(key,new Uint8Array(bytes)); return {key}; } };
  const env = { DB: db, BUCKET: bucket, SITES_GATEWAY_IDENTITY_TRUSTED: 'platform-managed', ZOTERO_OWNER_ID: binding.owner_id,
    ZOTERO_SITE_ORIGIN: origin, ZOTERO_VERIFIED_PERSONAL_LIBRARY_ID: '12345', ZOTERO_CATALOG_REGISTRATION_JSON: JSON.stringify(cfg) };
  const auth = { db, owner: binding.owner_id, origin };
  const fail = (status, code) => { const e = new Error(code); e.status = status; throw e; };
  const helpers = { fail, json: (data,status=200) => new Response(JSON.stringify(data),{status,headers:{'Content-Type':'application/json','Cache-Control':'private, no-store'}}),
    requireGet: req => { if(req.method!=='GET')fail(405,'method_not_allowed'); },
    requireAction: (req, target, contentType='application/json') => {
      if(req.method!=='POST')fail(405,'method_not_allowed');
      if(req.headers.get('Origin')!==target || (req.headers.has('Sec-Fetch-Site')&&req.headers.get('Sec-Fetch-Site')!=='same-origin'))fail(403,'origin_denied');
      if(req.headers.get('X-Zotero-Action')!=='1')fail(403,'action_header_required');
      if(req.headers.get('Content-Type')!==contentType)fail(415,'wrong_content_type');
    } };
  const payload = { action:'register_catalog',report_b64:Buffer.from(report).toString('base64'),html_b64:Buffer.from(html).toString('base64'),catalog_b64:Buffer.from(catalog).toString('base64') };
  const call = async (path=base,{method='GET',body,headers={},raw=false,request}={}) => {
    const h = {Origin:origin,'Sec-Fetch-Site':'same-origin','X-Zotero-Action':'1','Content-Type':raw?'application/pdf':'application/json',...headers};
    const req = request || new Request(origin+path,{method,headers:h,...(body===undefined?{}:{body:raw||typeof body==='string'?body:JSON.stringify(body)})});
    try { const response = await handleRegistrationRoute(req,env,auth,helpers);return {status:response.status,data:await response.json()}; }
    catch(e){return {status:e.status||500,data:{error:e.status?e.message:'internal_error'}};}
  };
  const stage = () => call(`${base}/pdfs/${sha(pdf)}`,{method:'POST',body:pdf,raw:true});
  const finalize = () => call(`${base}/finalize`,{method:'POST',body:payload});
  const setConfig = () => {env.ZOTERO_CATALOG_REGISTRATION_JSON=JSON.stringify(cfg);};
  return {pdf,html,report,catalog,paper,cfg,env,auth,db,objects,calls,call,stage,finalize,payload,setConfig};
}
const writes = f => f.calls.filter(c=>c[0]==='put').length + f.db.calls.filter(c=>c.operation==='run').length;

test('owner registration GET is read-only and excludes owner/library/configuration/credentials',async()=>{
  const f=fixture(), result=await f.call();assert.equal(result.status,200);assert.equal(result.data.state,'ready');assert.equal(result.data.pdfs[0].staged,false);
  assert.equal(writes(f),0);const text=JSON.stringify(result.data);for(const privateValue of ['synthetic-owner','personal:12345','trustedPins','allowed_source_hosts'])assert.equal(text.includes(privateValue),false);
  assert.equal(result.data.limits.config,5000);assert.equal(result.data.limits.totalPdf,24*1024*1024);
});
test('default gate-off routes read no private objects and cannot mutate',async()=>{
  const f=fixture();delete f.env.ZOTERO_CATALOG_REGISTRATION_JSON;
  assert.equal((await f.call()).data.enabled,false);assert.equal((await f.stage()).status,409);assert.equal((await f.finalize()).status,409);
  assert.equal(f.calls.length,0);assert.equal(f.db.calls.length,0);
});
test('exact pinned PDF plus source finalization produces one enabled immutable catalog',async()=>{
  const f=fixture();assert.equal((await f.stage()).data.staged,true);assert.equal(f.db.rows('zotero_report_catalogs').length,0);
  assert.equal((await f.call()).data.pdfs[0].staged,true);const done=await f.finalize();assert.equal(done.status,200,JSON.stringify(done));assert.equal(done.data.enabled,true);
  const row=await readRegisteredCatalog({db:f.db,ownerId:binding.owner_id,reportId:binding.report_id,catalogId:binding.catalog_id,personalScope:binding.personal_scope});
  assert.equal(row.papers.length,1);assert.equal(f.objects.size,1);assert.equal(f.db.rows('zotero_execution_jobs').length,0);
  const key=await privateBlobKey(binding.owner_id,sha(f.pdf));assert.deepEqual(f.objects.get(key),f.pdf);
  const puts=f.calls.filter(c=>c[0]==='put').length;assert.equal((await f.stage()).data.created,false);assert.equal((await f.finalize()).status,200);assert.equal(f.calls.filter(c=>c[0]==='put').length,puts);
});
test('explicit configuration can register disabled without silently enabling',async()=>{
  const f=fixture();f.cfg.enable_after_registration=false;f.setConfig();await f.stage();const done=await f.finalize();assert.equal(done.status,200);assert.equal(done.data.enabled,false);assert.equal(f.db.rows('zotero_report_catalogs')[0].enabled,0);
});
for(const [name,change] of [
  ['unapproved',f=>{f.cfg.approved=false;}],['foreign owner',f=>{f.cfg.binding.owner_id='someone-else';}],
  ['foreign personal library',f=>{f.cfg.binding.personal_scope=f.cfg.trustedPins.personal_scope='personal:777';}],
  ['pin binding mismatch',f=>{f.cfg.trustedPins.catalog_id='other';}],['nonpersonal library',f=>{f.cfg.binding.personal_scope='group:12345';}],
  ['extra configuration field',f=>{f.cfg.extra=true;}],['duplicate PDF',f=>{f.cfg.pdfs.push({...f.cfg.pdfs[0]});}],
  ['oversized aggregate PDF bundle',f=>{f.cfg.pdfs=[{...f.cfg.pdfs[0],size:16*1024*1024},{sha256:'d'.repeat(64),size:16*1024*1024,paper_id:'synthetic-two'}];}],
  ['unsafe filename',f=>{f.cfg.filenames.report='../report.json';}],['oversized config',f=>{f.cfg.trustedPins.allowed_source_hosts=Array(100).fill('verylongpublisher'.repeat(4)+'.test');}],
])test(`invalid ${name} gate fails before body/object access`,async()=>{
  const f=fixture();change(f);f.setConfig();assert.equal((await f.stage()).status,409);assert.equal(f.calls.length,0);assert.equal(f.db.calls.length,0);
});
test('wrong origin, missing action, cross-site and wrong media type cannot stage',async()=>{
  const f=fixture();for(const headers of [{Origin:'https://evil.invalid'},{'X-Zotero-Action':''},{'Sec-Fetch-Site':'cross-site'},{'Content-Type':'application/json'}]){
    const r=await f.call(`${base}/pdfs/${sha(f.pdf)}`,{method:'POST',body:f.pdf,raw:true,headers});assert.ok([403,415].includes(r.status));}
  assert.equal(writes(f),0);assert.equal(f.calls.length,0);
});
test('unknown hashes, wrong bytes, changed size and oversized streams write nothing',async()=>{
  const f=fixture();assert.equal((await f.call(`${base}/pdfs/${'c'.repeat(64)}`,{method:'POST',body:f.pdf,raw:true})).status,409);
  const changed=new Uint8Array(f.pdf);changed[10]^=1;assert.equal((await f.call(`${base}/pdfs/${sha(f.pdf)}`,{method:'POST',body:changed,raw:true})).status,409);
  for(const bytes of [f.pdf.subarray(1),new Uint8Array(f.pdf.length+1)])assert.equal((await f.call(`${base}/pdfs/${sha(f.pdf)}`,{method:'POST',body:bytes,raw:true})).status,413);
  assert.equal(writes(f),0);
});
test('finalize missing PDF or changed source bytes never publishes or enables a row',async()=>{
  const f=fixture();assert.equal((await f.finalize()).status,409);assert.equal(f.db.rows('zotero_report_catalogs').length,0);
  await f.stage();f.payload.html_b64=Buffer.from('changed source').toString('base64');assert.equal((await f.finalize()).status,409);assert.equal(f.db.rows('zotero_report_catalogs').length,0);
});
test('finalize rejects unexpected fields, duplicate fields, malformed base64 and overbudget bodies',async()=>{
  const f=fixture();await f.stage();
  for(const body of [{...f.payload,owner_id:'someone-else'},{...f.payload,html_b64:'%%%invalid'},JSON.stringify(f.payload).replace('"action":','"action":"other","action":')]){
    assert.equal((await f.call(`${base}/finalize`,{method:'POST',body})).status,409);}
  assert.equal((await f.call(`${base}/finalize`,{method:'POST',body:'{}',headers:{'Content-Length':String(REGISTRATION_LIMITS.finalBody+1)}})).status,413);
  assert.equal(f.db.rows('zotero_report_catalogs').length,0);
});
test('PDF manifest row mismatch prevents final DB registration despite exact staged bytes',async()=>{
  const f=fixture();f.cfg.pdfs[0].paper_id='wrong-synthetic-paper';f.setConfig();await f.stage();assert.equal((await f.finalize()).data.error,'pdf_registration_manifest_mismatch');assert.equal(f.db.rows('zotero_report_catalogs').length,0);
});
test('configuration revoked during raw body stream prevents object mutation',async()=>{
  const f=fixture();let pulled=false;const stream=new ReadableStream({pull(c){if(pulled){c.close();return;}pulled=true;c.enqueue(f.pdf);delete f.env.ZOTERO_CATALOG_REGISTRATION_JSON;}});
  const req=new Request(origin+`${base}/pdfs/${sha(f.pdf)}`,{method:'POST',headers:{Origin:origin,'X-Zotero-Action':'1','Content-Type':'application/pdf'},body:stream,duplex:'half'});
  assert.equal((await f.call('',{request:req})).status,409);assert.equal(writes(f),0);
});
test('configuration revoked after D1 registration leaves validated row disabled',async()=>{
  const f=fixture();await f.stage();const batch=f.db.batch.bind(f.db);f.db.batch=async statements=>{const r=await batch(statements);delete f.env.ZOTERO_CATALOG_REGISTRATION_JSON;return r;};
  assert.equal((await f.finalize()).status,409);assert.equal(f.db.rows('zotero_report_catalogs')[0].enabled,0);
});
test('existing corrupt PDF is never overwritten by staging',async()=>{
  const f=fixture(),key=await privateBlobKey(binding.owner_id,sha(f.pdf));f.objects.set(key,utf8('%PDF-corrupt'));
  assert.equal((await f.stage()).status,409);assert.equal(writes(f),0);assert.deepEqual(f.objects.get(key),utf8('%PDF-corrupt'));
});
test('concurrent finalization remains idempotent and produces no import jobs',async()=>{
  const f=fixture();await f.stage();const results=await Promise.all([f.finalize(),f.finalize()]);assert.ok(results.every(r=>r.status===200),JSON.stringify(results));
  assert.equal(f.db.rows('zotero_report_catalogs').length,1);assert.equal(f.db.rows('zotero_catalog_papers').length,1);assert.equal(f.db.rows('zotero_execution_jobs').length,0);
});
test('actual worker auth/media boundaries support stage to finalize to catalog listing',async()=>{
  const {handleZoteroRequest}=await import('../worker/index.js');const f=fixture();
  const actual=async(path,{body,raw=false,owner=binding.owner_id,headers={}}={})=>{
    const req=new Request(origin+path,{method:body===undefined?'GET':'POST',headers:{'oai-authenticated-user-id':owner,
      Origin:origin,'Sec-Fetch-Site':'same-origin','X-Zotero-Action':'1','Content-Type':raw?'application/pdf':'application/json',...headers},
      ...(body===undefined?{}:{body:raw?body:JSON.stringify(body)})});
    const r=await handleZoteroRequest(req,f.env);return {status:r.status,data:await r.json()};
  };
  const path=`${base}/pdfs/${sha(f.pdf)}`;
  assert.equal((await actual(path,{body:f.pdf,raw:true,owner:'foreign-owner'})).status,403);
  assert.equal((await actual(path,{body:f.pdf,raw:true,headers:{'Content-Type':'application/json'}})).status,415);
  assert.equal((await actual(path,{body:f.pdf,raw:true,headers:{Origin:'https://evil.invalid'}})).status,403);
  assert.equal((await actual(path,{body:f.pdf,raw:true})).status,200);
  assert.equal((await actual(`${base}/finalize`,{body:f.payload})).data.enabled,true);
  const catalogs=await actual('/api/zotero/catalogs');assert.equal(catalogs.status,200);assert.equal(catalogs.data.catalogs.length,1);
  assert.equal(catalogs.data.catalogs[0].catalog_id,binding.catalog_id);assert.equal(f.db.rows('zotero_execution_jobs').length,0);
});
test('raw upload timeout and declared truncation cannot stage bytes',async t=>{
  const f=fixture();
  assert.equal((await f.call(`${base}/pdfs/${sha(f.pdf)}`,{method:'POST',body:f.pdf.subarray(0,f.pdf.length-1),raw:true,headers:{'Content-Length':String(f.pdf.length)}})).status,413);
  t.mock.timers.enable({apis:['setTimeout']});let cancelled=false;
  const req=new Request(origin+`${base}/pdfs/${sha(f.pdf)}`,{method:'POST',headers:{Origin:origin,'X-Zotero-Action':'1','Content-Type':'application/pdf'},body:new ReadableStream({cancel(){cancelled=true;}}),duplex:'half'});
  const pending=f.call('',{request:req});await new Promise(resolve=>setImmediate(resolve));t.mock.timers.tick(10_001);
  const result=await pending;assert.equal(result.data.error,'registration_body_timeout');assert.equal(cancelled,true);assert.equal(writes(f),0);
});
test('gate revocation while reading staged bytes prevents any D1 registration',async()=>{
  const f=fixture();await f.stage();const get=f.env.BUCKET.get.bind(f.env.BUCKET);
  f.env.BUCKET.get=async key=>{const bytes=await get(key);delete f.env.ZOTERO_CATALOG_REGISTRATION_JSON;return bytes;};
  assert.equal((await f.finalize()).status,409);assert.equal(f.db.rows('zotero_report_catalogs').length,0);
});
test('concurrent PDF staging reports exactly one conditional creation',async()=>{
  const f=fixture();const results=await Promise.all([f.stage(),f.stage()]);assert.ok(results.every(r=>r.status===200));
  assert.equal(results.filter(r=>r.data.created).length,1);assert.equal(f.objects.size,1);
});
test('operator bundle CLI validates first and emits only explicit private exact-byte files',async t=>{
  const dir=await mkdtemp(join(tmpdir(),'zotero-registration-bundle-'));t.after(()=>rm(dir,{recursive:true,force:true}));const f=fixture();
  await mkdir(join(dir,'pdfs'));await writeFile(join(dir,'pdfs',sha(f.pdf)),f.pdf);
  for(const [name,bytes] of [['pins',utf8(JSON.stringify(f.cfg.trustedPins))],['report',f.report],['html',f.html],['catalog',f.catalog]])await writeFile(join(dir,name),bytes);
  const args=['scripts/prepare-registration-bundle.mjs','--pins',join(dir,'pins'),'--report',join(dir,'report'),'--html',join(dir,'html'),'--catalog',join(dir,'catalog'),'--pdf-dir',join(dir,'pdfs')];
  const run=extra=>spawnSync(process.execPath,[...args,...extra],{cwd:new URL('..',import.meta.url),encoding:'utf8'});
  const before=await readdir(dir),validation=run([]);assert.equal(validation.status,0,validation.stderr);assert.equal(JSON.parse(validation.stdout).private_bundle_written,false);assert.deepEqual(await readdir(dir),before);
  const output=join(dir,'operator-bundle'),result=run(['--output-dir',output,'--enable-after-registration']);assert.equal(result.status,0,result.stderr);
  const config=JSON.parse(await readFile(join(output,'registration-config.json'),'utf8'));
  assert.equal(config.enable_after_registration,true);assert.equal(config.approved,true);assert.ok((await stat(join(output,'registration-config.json'))).size<=5000);
  assert.equal((await stat(output)).mode&0o777,0o700);assert.equal((await stat(join(output,'registration-config.json'))).mode&0o777,0o600);
  assert.deepEqual(new Uint8Array(await readFile(join(output,'pdfs',sha(f.pdf)))),f.pdf);
  const manifest=JSON.parse(await readFile(join(output,'operator-manifest.json'),'utf8'));assert.equal(manifest.complete,true);assert.equal(manifest.files.length,5);
  for(const entry of manifest.files){const bytes=await readFile(join(output,entry.path));assert.equal(bytes.length,entry.size);assert.equal(sha(bytes),entry.sha256);}
  assert.equal(run(['--output-dir',output]).status,1);
  const forbidden=new URL('../test/forbidden-operator-bundle',import.meta.url).pathname;const rejected=run(['--output-dir',forbidden]);assert.equal(rejected.status,1);assert.match(rejected.stderr,/PUBLIC_OUTPUT_FORBIDDEN/);
  await writeFile(join(dir,'html'),utf8('changed'));const rejectedOutput=join(dir,'invalid-bundle');assert.equal(run(['--output-dir',rejectedOutput]).status,1);await assert.rejects(()=>stat(rejectedOutput),{code:'ENOENT'});
});
