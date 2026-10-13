// Entirely synthetic in-memory catalogs, credentials and asset bytes. No network.
import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { MockD1 } from './mock-d1.mjs';
import worker, {handleZoteroRequest} from '../worker/index.js';
import {denyPrivateBundleRequest} from '../src/private-bundle-paths.mjs';
const origin='https://daily.example.test',owner='synthetic-bundle-owner',library='123456',base='/api/zotero/private-bundles';
const utf8=s=>new TextEncoder().encode(s),sha=b=>createHash('sha256').update(b).digest('hex');
import {fixture} from './private-bundles-fixture.mjs';
test('GET and connected page are read-only; execution flag cannot bypass incomplete catalog set',async()=>{
  const f=fixture();assert.equal((await f.call()).data.ready,false);const connection=(await f.call('/api/zotero/connection')).data;
  assert.equal(connection.ready,true);assert.equal(connection.live_ready,false);assert.equal(connection.catalog_ready,false);
  assert.deepEqual((await f.call('/api/zotero/catalogs')).data.catalogs,[]);assert.equal((await f.call('/zotero/register')).status,200);
  assert.equal(f.calls.length,0);assert.equal(f.upstream.length,0);assert.equal(f.db.calls.some(c=>c.operation==='run'),false);
});
test('two disabled catalog registrations preserve union; explicit finish activates together without Zotero',async()=>{
  const f=fixture();assert.equal((await f.stage(f.manifest.batches[0])).status,200);assert.equal(f.db.rows('zotero_report_catalogs')[0].enabled,0);
  assert.equal((await f.action(base+'/finish')).status,409);assert.deepEqual((await f.call('/api/zotero/catalogs')).data.catalogs,[]);
  assert.equal((await f.stage(f.manifest.batches[1])).status,200);assert.deepEqual(f.db.rows('zotero_report_catalogs').map(r=>r.enabled),[0,0]);
  const finish=await f.action(base+'/finish');assert.equal(finish.status,200,finish.text);assert.equal(finish.data.paper_count,2);
  assert.deepEqual(f.db.rows('zotero_catalog_papers').map(p=>p.paper_id).sort(),['paper-1','paper-2']);
  assert.equal((await f.call('/api/zotero/catalogs')).data.catalogs.length,2);assert.equal((await f.call('/api/zotero/connection')).data.live_ready,true);
  assert.equal(f.upstream.length,0);assert.equal(f.db.rows('zotero_execution_jobs').length,0);
  assert.ok(f.db.calls.every(c=>!/^SELECT api_key/i.test(c.sql)));
});
test('concurrent clicks and resumed preparation are idempotent and do not replace enabled catalogs',async()=>{
  const f=fixture();const r=await Promise.all([f.all(),f.all()]);assert.ok(r.every(x=>x.status===200));const puts=f.calls.filter(c=>c[0]==='put').length;
  assert.equal((await f.all()).status,200);assert.equal(f.calls.filter(c=>c[0]==='put').length,puts);assert.equal(f.db.rows('zotero_catalog_papers').length,2);assert.equal(f.upstream.length,0);
});
test('owner, CSRF, same-origin, JSON, action and exact body enforced before asset access',async()=>{
  for(const options of [{identity:'foreign'},{headers:{Origin:'https://evil.test'}},{headers:{'X-Zotero-Action':''}},{headers:{'Sec-Fetch-Site':'cross-site'}},{headers:{'Content-Type':'text/plain'}},{body:{action:'prepare_private_catalogs',url:'https://evil.test'}},{body:{action:'auto_import'}}]){
    const f=fixture(),b=f.manifest.batches[0],r=await f.call(base+'/'+b.id+'/pdfs/'+b.config.pdfs[0].sha256,{body:{action:'prepare_private_catalogs'},...options});assert.ok([403,409,415].includes(r.status),r.text);assert.equal(f.calls.length,0);}
});
test('unknown paths/hashes and client pins cannot cause any arbitrary asset fetch',async()=>{
  const f=fixture();for(const p of ['/unknown/register','/batch-1/pdfs/'+'f'.repeat(64),'/batch-1/register?url=https://evil.test','/batch-1/../register'])assert.ok((await f.action(base+p)).status>=400);
  assert.equal(f.calls.length,0);
});
test('raw upload validates size, signature and pinned hash, never accessing static assets',async()=>{
  for(const variant of ['short','long','hash','signature']){const f=fixture(),b=f.manifest.batches[0],p=b.config.pdfs[0];
    const original=f.assets.get('/__zotero_private_bundles/'+b.id+'/pdfs/'+p.sha256);let bytes=new Uint8Array(original);
    if(variant==='short')bytes=bytes.subarray(1);else if(variant==='long')bytes=new Uint8Array(bytes.length+1);else bytes[variant==='hash'?10:0]^=1;
    const response=await f.call(base+'/'+b.id+'/pdfs/'+p.sha256,{body:bytes,headers:{'Content-Type':'application/pdf'}});
    assert.ok([409,413].includes(response.status));assert.equal(f.objects.size,0);assert.equal(f.calls.length,0);}
  const f=fixture();delete f.env.ASSETS;assert.equal((await f.all()).status,200);assert.ok(f.calls.every(c=>c[0]!=='asset'));
});
test('missing staged PDF, altered source and manifest union/pin mismatch fail closed',async()=>{
  const f=fixture();assert.equal((await f.action(base+'/batch-1/register')).status,409);assert.equal(f.db.rows('zotero_report_catalogs').length,0);
  const path='/__zotero_private_bundles/batch-1/report.html';f.assets.set(path,utf8('changed'));assert.equal((await f.stage(f.manifest.batches[0])).status,409);assert.equal(f.db.rows('zotero_report_catalogs').length,0);
  for(const mutate of [m=>m.paper_ids.push('phantom'),m=>m.batches[1].paper_ids=['paper-1'],m=>m.batches[0].config.enable_after_registration=true,m=>m.batches[0].sources.report.sha256='f'.repeat(64),m=>m.owner_id='foreign']){const q=fixture();mutate(q.manifest);assert.equal((await q.call()).status,409);assert.equal(q.calls.length,0);}
});
test('revoked/version-changed/wrong-library connection cannot prepare, finish or enable execution',async()=>{
  for(const change of ["state='revoked'","connection_version=2","library_id='999999'"]){const f=fixture();for(const b of f.manifest.batches)await f.stage(b);f.db.sqlite.exec('UPDATE zotero_active_connections SET '+change);
    assert.equal((await f.action(base+'/finish')).status,409);assert.deepEqual(f.db.rows('zotero_report_catalogs').map(r=>r.enabled),[0,0]);assert.equal((await f.call('/api/zotero/connection')).data.live_ready,false);assert.equal(f.upstream.length,0);}
});
test('legacy registered rows never become listed or executable in bundled mode',async()=>{
  const f=fixture();await f.all();f.db.seedCatalog({owner,report:'legacy-report',catalog:'legacy-catalog'});
  assert.equal((await f.call('/api/zotero/catalogs')).data.catalogs.length,2);assert.equal((await f.call('/api/zotero/reports/legacy-report/catalogs/legacy-catalog')).status,404);
  const r=await f.call('/api/zotero/executions',{body:{action:'save_to_zotero',report_id:'legacy-report',catalog_id:'legacy-catalog',catalog_version:1,paper_ids:['paper-a'],pdf_intent:'metadata_only'}});assert.equal(r.data.error,'catalog_not_allowed');assert.equal(f.db.rows('zotero_execution_jobs').length,0);
  assert.equal((await f.call('/api/zotero/catalog-registration')).status,404);
});
test('private or ambiguous paths are denied before ALL static fallbacks for every method and query',async()=>{
  const paths=['/__zotero_private_bundles','/__zotero_private_bundles/','/__ZOTERO_PRIVATE_BUNDLES/batch-1/report.html','//__zotero_private_bundles/batch-1/report.html','/%5f_zotero_private_bundles/batch-1/report.html','/%255f_zotero_private_bundles/batch-1/report.html','/%2f__zotero_private_bundles/batch-1/report.html','/%ZZ','/foo/%252e%252e/__zotero_private_bundles/a','/__zotero_private_bundles%5cfile'];
  for(const path of paths)for(const method of ['GET','HEAD','POST']){let called=false;const request=new Request(origin+path+'?x=%E4%B8%AD',{method,headers:{Range:'bytes=0-9'}});assert.equal(denyPrivateBundleRequest(request),true,path);const response=await worker.fetch(request,{ASSETS:{fetch(){called=true;throw Error('Private leak');}}});assert.equal(response.status,404,path);assert.equal(called,false);}
  assert.equal(denyPrivateBundleRequest(new Request(origin+'/archive/?q=%E4%B8%AD')),false);
});

test('required bundled mode with absent compiled manifest blocks legacy admission and every execution entry',async()=>{
  const f=fixture();f.env.ZOTERO_PRIVATE_BUNDLES_REQUIRED='approved';const empty={bundleManifest:null};
  assert.equal((await f.call('/api/zotero/connection',empty)).data.live_ready,false);
  assert.deepEqual((await f.call('/api/zotero/catalogs',empty)).data.catalogs,[]);
  assert.equal((await f.call('/api/zotero/catalog-registration',empty)).status,404);
  assert.equal((await f.call('/api/zotero/executions',{...empty,body:{action:'save_to_zotero'}})).status,409);
  assert.equal((await f.call('/api/zotero/executions/ze_'+'a'.repeat(64)+'/advance',{...empty,body:{action:'continue_import'}})).status,409);
  assert.equal(f.upstream.length,0);assert.equal(f.db.rows('zotero_execution_jobs').length,0);assert.equal(f.objects.size,0);
});
test('missing or corrupted staged PDF after registration prevents atomic activation',async()=>{
  for(const corrupt of [false,true]){const f=fixture();for(const b of f.manifest.batches)await f.stage(b);const key=[...f.objects.keys()][0];
    if(corrupt)f.objects.set(key,utf8('%PDF-corrupt'));else f.objects.delete(key);
    assert.equal((await f.action(base+'/finish')).status,409);assert.deepEqual(f.db.rows('zotero_report_catalogs').map(r=>r.enabled),[0,0]);assert.equal(f.upstream.length,0);}
});
test('duplicate JSON action cannot be interpreted as a fresh owner preparation instruction',async()=>{
  const f=fixture();const r=await f.call(base+'/batch-1/register',{body:'{"action":"ignored","action":"prepare_private_catalogs"}'});assert.equal(r.status,409);assert.equal(f.calls.length,0);
});
test('only explicit selected Save creates a job; later incomplete bundle blocks advance before Zotero',async()=>{
  const f=fixture();await f.all();assert.equal(f.db.rows('zotero_execution_jobs').length,0);
  const r=await f.call('/api/zotero/executions',{body:{action:'save_selected',report_id:'report-1',catalog_id:'catalog-1',catalog_version:1,paper_ids:['paper-1'],pdf_intent:'metadata_only'}});
  assert.equal(r.status,201,r.text);assert.equal(f.db.rows('zotero_execution_items').length,1);assert.equal(f.upstream.length,0);
  f.db.sqlite.exec("UPDATE zotero_report_catalogs SET enabled=0 WHERE catalog_id='catalog-2'");
  assert.equal((await f.call('/api/zotero/executions/'+r.data.job_id+'/advance',{body:{action:'continue_import'}})).status,409);assert.equal(f.upstream.length,0);
});
test('compiled containment blocks preparation and live execution even with complete enabled rows and approved execution flag',async()=>{
  const f=fixture();await f.all();const rows=JSON.stringify(f.db.rows('zotero_report_catalogs')),credentials=JSON.stringify(f.db.rows('zotero_provider_credentials'));f.calls.length=0;f.db.calls.length=0;
  const blocked={bundleContainment:true};const status=await f.call(base,blocked);assert.equal(status.data.state,'temporarily_unavailable');assert.equal(status.data.enabled,false);
  for(const path of [base+'/finish',base+'/batch-1/register',base+'/batch-1/pdfs/'+f.manifest.batches[0].config.pdfs[0].sha256])assert.equal((await f.call(path,{...blocked,body:{action:'prepare_private_catalogs'},headers:{'Content-Type':path.includes('/pdfs/')?'application/pdf':'application/json'}})).data.error,'private_bundle_preparation_temporarily_unavailable');
  const connection=(await f.call('/api/zotero/connection',blocked)).data;assert.equal(connection.ready,true);assert.equal(connection.live_ready,false);assert.equal(connection.live_gate_enabled,false);assert.equal(connection.catalog_preparation_unavailable,true);
  assert.deepEqual((await f.call('/api/zotero/catalogs',blocked)).data.catalogs,[]);assert.equal((await f.call('/api/zotero/executions',{...blocked,body:{action:'save_selected',report_id:'report-1',catalog_id:'catalog-1',catalog_version:1,paper_ids:['paper-1'],pdf_intent:'metadata_only'}})).status,409);
  assert.equal((await f.call('/api/zotero/executions/ze_'+'a'.repeat(64)+'/advance',{...blocked,body:{action:'continue_import'}})).status,409);
  assert.equal((await f.call('/api/zotero/catalog-registration',blocked)).status,404);
  const page=await f.call('/zotero/register',blocked);assert.match(page.text,/目录准备暂时不可用/);assert.match(page.text,/无需重新输入密钥/);
  assert.equal(f.calls.length,0);assert.equal(f.upstream.length,0);assert.equal(f.db.calls.some(c=>c.operation==='run'),false);
  assert.equal(JSON.stringify(f.db.rows('zotero_report_catalogs')),rows);assert.equal(JSON.stringify(f.db.rows('zotero_provider_credentials')),credentials);
});

test('owner upload descriptor is read-only, exact and independent of all client supplied paths',async()=>{
  const f=fixture();const r=await f.call();assert.equal(r.status,200);const descriptor=r.data.upload;
  const header=JSON.parse(descriptor.header_json);assert.equal(header.schema,'daily-agent-owner-bundle-v1');assert.equal(header.members.length,8);
  let offset=0;for(const m of header.members){assert.equal(m.offset,offset);offset+=m.size;assert.ok(['report','html','catalog','pdf'].includes(m.kind));}
  assert.equal(descriptor.total_size,12+new TextEncoder().encode(descriptor.header_json).length+offset);
  assert.equal(f.calls.length,0);assert.equal(f.upstream.length,0);
  for(const secret of [owner,'personal:'+library,'SYNTHETICBUNDLEKEY12345'])assert.equal(descriptor.header_json.includes(secret),false);
});
test('a duplicate upload still rejects wrong bytes before reading or trusting an existing object',async()=>{
  const f=fixture(),b=f.manifest.batches[0],p=b.config.pdfs[0],path=base+'/'+b.id+'/pdfs/'+p.sha256;
  assert.equal((await f.action(path)).status,200);f.calls.length=0;
  const wrong=new Uint8Array(f.assets.get('/__zotero_private_bundles/'+b.id+'/pdfs/'+p.sha256));wrong[10]^=1;
  assert.equal((await f.call(path,{body:wrong,headers:{'Content-Type':'application/pdf'}})).status,409);assert.equal(f.calls.length,0);
  assert.equal((await f.action(path)).data.created,false);
});
test('registration source envelopes reject duplicate, extra, malformed base64 and size mismatches before storage',async()=>{
  for(const variant of ['duplicate','extra','base64','empty','large','wrong-size']){
    const f=fixture(),b=f.manifest.batches[0];const body={action:'register_catalog',...Object.fromEntries(['report','html','catalog'].map(k=>[k+'_b64',Buffer.from(f.assets.get('/__zotero_private_bundles/'+b.id+'/'+b.config.filenames[k])).toString('base64')]))};
    let encoded;
    if(variant==='duplicate')encoded=JSON.stringify(body).replace('{','{"action":"ignored",');
    if(variant==='extra')body.path='/arbitrary/private/path';
    if(variant==='base64')body.report_b64='!!!!';
    if(variant==='empty')body.report_b64='';
    if(variant==='large')body.report_b64='A'.repeat(Math.ceil(1024*1024/3)*4+4);
    if(variant==='wrong-size')body.html_b64=Buffer.from('different source').toString('base64');
    const result=await f.call(base+'/'+b.id+'/register',{body:encoded||body});assert.equal(result.status,409,variant+':'+result.text);
    assert.equal(f.calls.length,0);assert.equal(f.db.rows('zotero_report_catalogs').length,0);
  }
});
test('PDF media type, content length and revoked connection checks are independent of browser validation',async()=>{
  const f=fixture(),b=f.manifest.batches[0],p=b.config.pdfs[0],path=base+'/'+b.id+'/pdfs/'+p.sha256,bytes=f.assets.get('/__zotero_private_bundles/'+b.id+'/pdfs/'+p.sha256);
  assert.equal((await f.call(path,{body:bytes})).status,415);
  for(const length of ['0','-1','01',String(p.size+1),'16777217'])assert.equal((await f.call(path,{body:bytes,headers:{'Content-Type':'application/pdf','Content-Length':length}})).status,413);
  f.db.sqlite.exec("UPDATE zotero_active_connections SET state='revoked'");assert.equal((await f.call(path,{body:bytes,headers:{'Content-Type':'application/pdf'}})).status,409);
  assert.equal(f.calls.length,0);assert.equal(f.objects.size,0);assert.equal(f.upstream.length,0);
});

test('private compiled manifest without the explicit supported upload mode remains contained',async()=>{
  try{
    globalThis.__ZOTERO_PRIVATE_BUNDLE_MANIFEST__=fixture().manifest;
    const missing=await import('../src/private-bundles.mjs?synthetic-missing-upload-mode');
    assert.equal(missing.deploymentBundleContainment(),true);
    globalThis.__ZOTERO_PRIVATE_BUNDLE_UPLOAD_MODE__='unsupported-mode';assert.equal(missing.deploymentBundleContainment(),true);
    globalThis.__ZOTERO_PRIVATE_BUNDLE_UPLOAD_MODE__='owner-upload-v1';assert.equal(missing.deploymentBundleContainment(),false);
  }finally{delete globalThis.__ZOTERO_PRIVATE_BUNDLE_MANIFEST__;delete globalThis.__ZOTERO_PRIVATE_BUNDLE_UPLOAD_MODE__;}
});
