import test from 'node:test';
import assert from 'node:assert/strict';
import { handleZoteroRequest } from '../worker/index.js';
import { MockD1 } from './mock-d1.mjs';
import { canonicalJSON, sha256Text, registrationBinding } from '../src/import-domain.mjs';
import { hashBytes } from '../src/hash-bytes.mjs';
import { privateBlobKey } from '../src/catalog-registration.mjs';
const origin='https://daily.example.test', owner='synthetic-owner', user='123456', fakeKey='SYNTHETICTESTONLY12345678';
const pdf=new TextEncoder().encode('%PDF-1.7\nSYNTHETIC APPLICATION TEST ONLY\n%%EOF\n');
const clone=value=>structuredClone(value);
const response=(value,status=200,headers={})=>new Response(status===204?null:JSON.stringify(value),{status,headers:{'Content-Type':'application/json',...headers}});
async function setup({withPdf=false,providerManaged=false}={}) {
 const db=new MockD1(), hashes=await hashBytes(pdf), blobs=new Map(), calls=[];
 const env={DB:db,BUCKET:{get:async key=>{const bytes=blobs.get(key);return bytes?{size:bytes.length,arrayBuffer:async()=>bytes.buffer.slice(bytes.byteOffset,bytes.byteOffset+bytes.byteLength)}:null;}},SITES_GATEWAY_IDENTITY_TRUSTED:'platform-managed',ZOTERO_OWNER_ID:owner,ZOTERO_SITE_ORIGIN:origin};
 const papers=[0,1].map(i=>({paper_id:`synthetic-${i}`,title:`Synthetic integration paper ${i}`,source_url:`https://publisher.example.test/${i}`,doi:`10.9999/synthetic.${i}`,arxiv:'',authors:['Synthetic Author'],year:'2000',venue:'Synthetic Journal',item_type:'journalArticle',publication:'published_verified',publication_evidence_sha256:'c'.repeat(64),topics:['agents'],pdf:withPdf?{blob_id:hashes.sha256,sha256:hashes.sha256,size:pdf.length}:null,review_sha256:'d'.repeat(64),qualification_receipt_sha256:'e'.repeat(64),source_pdf_sha256:withPdf?hashes.sha256:'f'.repeat(64)}));
 const body=canonicalJSON({schema:'daily-agent-zotero-catalog-v1',report_sha256:'b'.repeat(64),papers});
 const row={owner_id:owner,report_id:'synthetic-report',catalog_id:'synthetic-catalog',catalog_version:1,content_sha256:await sha256Text(body),enabled:1,created_at:'2000-01-02T00:00:00.000Z',catalog_blob_json:body,report_sha256:'b'.repeat(64),report_day:'2000-01-02',personal_scope:`personal:${user}`,source_catalog_sha256:'a'.repeat(64)};
 row.registration_sha256=await sha256Text(canonicalJSON(registrationBinding(row)));
 const keys=Object.keys(row);db.sqlite.prepare(`INSERT INTO zotero_report_catalogs (${keys}) VALUES (${keys.map(()=>'?')})`).run(...keys.map(key=>row[key]));
 for(const p of papers)db.sqlite.prepare('INSERT INTO zotero_catalog_papers(owner_id,report_id,catalog_id,paper_id,display_title,metadata_json) VALUES(?,?,?,?,?,?)').run(owner,row.report_id,row.catalog_id,p.paper_id,p.title,canonicalJSON(p));
 blobs.set(await privateBlobKey(owner,hashes.sha256),pdf);
 const server=new FakeServer(calls,hashes);
 const request={action:'save_selected',report_id:row.report_id,catalog_id:row.catalog_id,catalog_version:1,paper_ids:['synthetic-0'],pdf_intent:withPdf?'open_access_if_available':'metadata_only'};
 async function call(path,{method='GET',body,headers={},asOwner=owner}={}) {
  const all={'oai-authenticated-user-id':asOwner,...(method==='POST'?{Origin:origin,'Content-Type':'application/json','X-Zotero-Action':'1'}:{}),...headers};
  for(const [k,v] of Object.entries(all))if(v===null)delete all[k];
  const result=await handleZoteroRequest(new Request(origin+path,{method,headers:all,body:body===undefined?undefined:JSON.stringify(body)}),env,{fetchImpl:server.fetch.bind(server)});
  const text=await result.text();return {status:result.status,text,data:result.headers.get('Content-Type').includes('application/json')?JSON.parse(text):null};
 }
 async function stage() {
  const now=Date.now();Object.assign(env,{ZOTERO_VERIFIED_PERSONAL_LIBRARY_ID:user,ZOTERO_ENCRYPTION_KEY_ID:'synthetic-encryption-v1',ZOTERO_CREDENTIAL_ENCRYPTION_KEY_B64:Buffer.alloc(32,7).toString('base64'),ZOTERO_SETUP_GRANT_JSON:JSON.stringify({grant_id:'synthetic-grant',owner_id:owner,library_id:user,purpose:'zotero_credential_staging',issued_at:new Date(now-1000).toISOString(),expires_at:new Date(now+60000).toISOString(),security_action_approved:true,secure_user_entry_approved:true})});
  const result=await call('/api/zotero/connect',{method:'POST',body:{action:'stage_credentials',api_key:fakeKey}});assert.equal(result.status,201,result.text);return result;
 }
 async function connect() {if(providerManaged){Object.assign(env,{ZOTERO_CREDENTIAL_STORAGE:'provider-managed-d1',ZOTERO_CREDENTIAL_ENTRY_ENABLED:'approved',ZOTERO_EXECUTION_ENABLED:'approved'});const result=await call('/api/zotero/connect',{method:'POST',body:{action:'connect_personal_library',library_id:user,api_key:fakeKey}});assert.equal(result.status,201,result.text);return;}await stage();env.ZOTERO_EXECUTION_ENABLED='approved';const result=await call('/api/zotero/verify-connection',{method:'POST',body:{action:'verify_connection'}});assert.equal(result.status,200,result.text);assert.equal(result.data.ready,true);}
 async function create(body=request){return call('/api/zotero/executions',{method:'POST',body});}
 async function finish(jobId) {let r;for(let i=0;i<20;i++){r=await call(`/api/zotero/executions/${jobId}/advance`,{method:'POST',body:{action:'continue_import'}});assert.equal(r.status,200,r.text);if(!['queued','running'].includes(r.data.state))return r;}throw Error('did not settle');}
 return {db,env,blobs,calls,papers,hashes,server,request,call,stage,connect,create,finish};
}
class FakeServer {
 constructor(calls,hashes){this.calls=calls;this.hashes=hashes;this.items=new Map();this.collections=new Map();this.version=1;this.uploads=new Map();this.files=new Map();this.hook=null;}
 put(map,data){const version=++this.version,obj={key:data.key,version,library:{type:'user',id:Number(user)},data:{...clone(data),version}};map.set(data.key,obj);return clone(obj);}
 async fetch(raw,init){const url=new URL(raw);this.calls.push({url:raw,method:init.method,body:typeof init.body==='string'?init.body:null});if(this.hook){const r=await this.hook(url,init,this);if(r)return r;}
  if(url.hostname==='zoterofilestorage.s3.us-east-1.amazonaws.com') {assert.equal(init.headers['Zotero-API-Key'],undefined);const key=url.pathname.slice(1);if(init.method==='POST'){this.files.set(key,new Uint8Array(await init.body.arrayBuffer()));return new Response(null,{status:201});}const bytes=this.files.get(key);return new Response(bytes,{headers:{ETag:`"${this.hashes.md5}"`,'Content-Length':String(bytes.length)}});}
  assert.equal(url.origin,'https://api.zotero.org');assert.equal(init.headers['Zotero-API-Key'],fakeKey);
  if(url.pathname==='/keys/current')return response({userID:Number(user),access:{user:{library:true,write:true,files:true,notes:false}}});
  const match=url.pathname.match(/^\/users\/123456\/(items|collections)(?:\/([^/]+))?(\/file)?$/);assert.ok(match,raw);const [,kind,key,file]=match,map=this[kind],head={'Last-Modified-Version':String(this.version)};
  if(file){if(init.method==='GET')return new Response(null,{status:302,headers:{Location:`https://zoterofilestorage.s3.us-east-1.amazonaws.com/${key}`}});const params=new URLSearchParams(init.body);if(params.has('upload')){const meta=this.uploads.get(key);this.put(map,{...map.get(key).data,md5:meta.md5,mtime:Number(meta.mtime)});return new Response(null,{status:204});}this.uploads.set(key,Object.fromEntries(params));return response({url:`https://zoterofilestorage.s3.us-east-1.amazonaws.com/${key}`,contentType:'multipart/form-data; boundary=synthetic',prefix:'',suffix:'',uploadKey:'SYNTHETIC'});}
  if(init.method==='POST'){const [data]=JSON.parse(init.body);assert.equal(data.version,0);assert.ok(!map.has(data.key));const object=this.put(map,data);return response({successful:{0:object},unchanged:{},failed:{}},200,{'Last-Modified-Version':String(this.version)});}
  if(init.method==='PATCH'){const old=map.get(key);if(String(old.version)!==init.headers['If-Unmodified-Since-Version'])return response({},412);this.put(map,{...old.data,...JSON.parse(init.body)});return response(null,204,{'Last-Modified-Version':String(this.version)});}
  if(key)return map.has(key)?response(map.get(key),200,{'Last-Modified-Version':String(map.get(key).version)}):response({},404);
  let values=[...map.values()];
  if(kind==='items') {
    assert.equal(url.searchParams.get('itemType'),'-note || annotation');
    values=values.filter(item=>!['note','annotation'].includes(item.data.itemType));
    if(url.searchParams.has('itemKey')) {
      const found=values.filter(item=>item.key===url.searchParams.get('itemKey'));
      return response(found,200,{...head,'Total-Results':String(found.length)});
    }
  }
  if(url.searchParams.get('format')==='versions')return response(Object.fromEntries(values.map(o=>[o.key,o.version])),200,head);
  const start=Number(url.searchParams.get('start')),limit=Number(url.searchParams.get('limit'));return response(values.slice(start,start+limit),200,{...head,'Total-Results':String(values.length)});
 }
}

test('default manager/catalog/history are usable read-only; all execution and verify actions stay network-free',async()=>{
 const f=await setup();for(const path of ['/zotero','/api/zotero/connection','/api/zotero/catalogs','/api/zotero/executions'])assert.equal((await f.call(path)).status,200);
 const catalog=await f.call('/api/zotero/reports/synthetic-report/catalogs/synthetic-catalog');assert.equal(catalog.data.catalog_verified,true);assert.equal(catalog.data.papers[0].venue,'Synthetic Journal');
 assert.equal((await f.create()).status,409);assert.equal((await f.call('/api/zotero/verify-connection',{method:'POST',body:{action:'verify_connection'}})).status,409);assert.equal(f.calls.length,0);assert.equal(f.db.rows('zotero_execution_jobs').length,0);
});
test('secure synthetic staging does not connect; explicit gated verification alone makes ready and never returns key',async()=>{
 const f=await setup();await f.stage();assert.equal(f.calls.length,0);let status=await f.call('/api/zotero/connection');assert.equal(status.data.ready,false);assert.equal(status.data.verification_enabled,false);assert.equal(status.text.includes(fakeKey),false);assert.equal(JSON.stringify(f.db.rows('zotero_encrypted_credentials')).includes(fakeKey),false);
 f.env.ZOTERO_EXECUTION_ENABLED='approved';const r=await f.call('/api/zotero/verify-connection',{method:'POST',body:{action:'verify_connection'}});assert.equal(r.data.ready,true);assert.equal(f.calls.length,1);assert.equal(r.text.includes(fakeKey),false);
});
test('real route to real domain/HTTP adapter imports only explicit selection and repeat Save is idempotent',async()=>{
 const f=await setup();await f.connect();const before=f.calls.length,job=await f.create();assert.equal(job.status,201,job.text);assert.equal(f.calls.length,before);assert.equal(f.db.rows('zotero_execution_items').length,1);
 const result=await f.finish(job.data.job_id);assert.equal(result.data.state,'complete',result.text);assert.equal(f.server.items.size,1);const item=[...f.server.items.values()][0];assert.equal(item.data.DOI,f.papers[0].doi);assert.equal(item.data.title,f.papers[0].title);assert.equal(result.data.items[0].pdf_verified,false);
 assert.equal((await f.create()).data.job_id,job.data.job_id);const count=f.calls.length;assert.equal((await f.finish(job.data.job_id)).data.state,'complete');assert.equal(f.calls.length,count);assert.equal(f.db.rows('zotero_import_intents').length,0);
 assert.equal(JSON.stringify(f.db.rows('zotero_execution_items')).includes(fakeKey),false);
});
test('complete PDF route loads only private owner blob and proves actual bytes after registration',async()=>{
 const f=await setup({withPdf:true});await f.connect();const job=await f.create(),result=await f.finish(job.data.job_id);assert.equal(result.data.state,'complete',result.text);assert.equal(result.data.items[0].pdf_verified,true);assert.equal(f.server.items.size,2);assert.equal(f.server.files.size,1);assert.deepEqual([...f.server.files.values()][0],pdf);
 assert.ok(f.calls.every(c=>!c.url.includes('publisher.example.test')));
});
test('missing private PDF keeps metadata_saved and honest pdf_pending with no external source fetch',async()=>{
 const f=await setup({withPdf:true});await f.connect();f.blobs.clear();const job=await f.create(),result=await f.finish(job.data.job_id);assert.equal(result.data.state,'retry_wait',result.text);assert.equal(result.data.items[0].state,'pdf_pending');assert.equal(result.data.items[0].metadata_saved,true);assert.equal(result.data.items[0].pdf_verified,false);assert.equal(f.server.files.size,0);
});
test('all new endpoints require owner identity and mutation origin before any network or body persistence',async()=>{
 const f=await setup();await f.connect();f.calls.length=0;for(const path of ['/api/zotero/catalogs','/api/zotero/executions','/api/zotero/verify-connection']){assert.equal((await f.call(path,{asOwner:null})).status,401);assert.equal((await f.call(path,{asOwner:'other-owner'})).status,403);}
 assert.equal((await f.create({...f.request,url:'http://169.254.169.254/'})).status,400);assert.equal((await f.call('/api/zotero/executions',{method:'POST',body:f.request,headers:{Origin:'https://attacker.example'}})).status,403);assert.equal(f.calls.length,0);assert.equal(f.db.rows('zotero_execution_jobs').length,0);
});
test('off gate and changed ciphertext binding block continuation of a queued execution',async()=>{
 const f=await setup();await f.connect();const job=await f.create();f.env.ZOTERO_EXECUTION_ENABLED='off';const before=f.calls.length;assert.equal((await f.call(`/api/zotero/executions/${job.data.job_id}`)).data.state,'connection_required');assert.equal((await f.call(`/api/zotero/executions/${job.data.job_id}/advance`,{method:'POST',body:{action:'continue_import'}})).status,409);assert.equal(f.calls.length,before);f.env.ZOTERO_EXECUTION_ENABLED='approved';f.db.sqlite.prepare("UPDATE zotero_encrypted_credentials SET key_id='changed'").run();assert.equal((await f.create()).status,409);assert.equal(f.calls.length,before);
});
test('tampered ciphertext fails closed without any verification request or plaintext error',async()=>{
 const f=await setup();await f.stage();f.env.ZOTERO_EXECUTION_ENABLED='approved';f.db.sqlite.prepare("UPDATE zotero_encrypted_credentials SET ciphertext_b64='AAAA'").run();const r=await f.call('/api/zotero/verify-connection',{method:'POST',body:{action:'verify_connection'}});assert.equal(r.status,409);assert.equal(r.data.error,'credential_unavailable');assert.equal(f.calls.length,0);
});
test('explicit key rejection revokes prior active connection and returns only sanitized code',async()=>{
 const f=await setup();await f.connect();f.server.hook=()=>response({secret:fakeKey},403);const r=await f.call('/api/zotero/verify-connection',{method:'POST',body:{action:'verify_connection'}});assert.equal(r.status,403);assert.equal(r.text.includes(fakeKey),false);assert.equal((await f.call('/api/zotero/connection')).data.ready,false);
});
test('standalone Verify persists successful-response Backoff and respects it across repeated explicit clicks',async()=>{
 const f=await setup();await f.stage();f.env.ZOTERO_EXECUTION_ENABLED='approved';f.server.hook=(url)=>url.pathname==='/keys/current'?response({userID:Number(user),access:{user:{library:true,write:true,files:true,notes:false}}},200,{Backoff:'120'}):null;
 const verified=await f.call('/api/zotero/verify-connection',{method:'POST',body:{action:'verify_connection'}});assert.equal(verified.status,200,verified.text);assert.ok(f.db.rows('zotero_execution_leases')[0].not_before>Date.now());const before=f.calls.length;
 const again=await f.call('/api/zotero/verify-connection',{method:'POST',body:{action:'verify_connection'}});assert.equal(again.status,409);assert.equal(again.data.error,'backoff_required');assert.ok(again.data.retry_after_ms>0);assert.equal(f.calls.length,before);
 const job=await f.create();assert.equal(job.data.state,'retry_wait');assert.equal((await f.call(`/api/zotero/executions/${job.data.job_id}/advance`,{method:'POST',body:{action:'continue_import'}})).data.state,'retry_wait');assert.equal(f.calls.length,before);
});
test('standalone Verify 429 persists Retry-After without losing safe error details or issuing retries',async()=>{
 const f=await setup();await f.stage();f.env.ZOTERO_EXECUTION_ENABLED='approved';f.server.hook=()=>response({secret:fakeKey},429,{'Retry-After':'60'});
 const r=await f.call('/api/zotero/verify-connection',{method:'POST',body:{action:'verify_connection'}});assert.equal(r.status,409);assert.equal(r.data.error,'rate_limited');assert.equal(r.data.retry_after_ms,60000);assert.equal(r.text.includes(fakeKey),false);assert.equal(f.calls.length,1);assert.ok(f.db.rows('zotero_execution_leases')[0].not_before>Date.now());
});
test('API denial during advance revokes connection before any further request or write',async()=>{
 const f=await setup();await f.connect();const job=await f.create();f.calls.length=0;f.server.hook=()=>response({secret:fakeKey},403);
 const r=await f.call(`/api/zotero/executions/${job.data.job_id}/advance`,{method:'POST',body:{action:'continue_import'}});assert.equal(r.status,409,r.text);assert.equal((await f.call('/api/zotero/connection')).data.ready,false);assert.equal(f.calls.length,1);assert.equal(f.server.items.size,0);assert.equal(r.text.includes(fakeKey),false);
});
test('API denial of a pending uncertain operation preserves recovery journal while revoking connection',async()=>{
 const f=await setup();await f.connect();const job=await f.create();f.server.hook=(url,init)=>{if(init.method==='POST'&&url.pathname==='/users/123456/items')throw Error('synthetic ambiguous request');return null;};
 const first=await f.call(`/api/zotero/executions/${job.data.job_id}/advance`,{method:'POST',body:{action:'continue_import'}});assert.equal(first.data.items[0].uncertain,true,first.text);const pending=f.db.rows('zotero_execution_items')[0].pending_operation;
 f.db.sqlite.prepare('UPDATE zotero_execution_items SET retry_at=0').run();f.db.sqlite.prepare('UPDATE zotero_execution_leases SET not_before=0').run();f.server.hook=()=>response({},401);const before=f.calls.length;
 await f.call(`/api/zotero/executions/${job.data.job_id}/advance`,{method:'POST',body:{action:'continue_import'}});const item=f.db.rows('zotero_execution_items')[0];assert.equal(item.pending_operation,pending);assert.equal(item.uncertain,1);assert.equal((await f.call('/api/zotero/connection')).data.ready,false);assert.equal(f.calls.length,before+1);
});
test('HTTP200 permission downgrade during advance disables stale ready status without writes',async()=>{
 const f=await setup();await f.connect();const job=await f.create();f.calls.length=0;f.server.hook=()=>response({userID:Number(user),access:{user:{library:true,write:false,files:true,notes:false}}});
 const result=await f.call(`/api/zotero/executions/${job.data.job_id}/advance`,{method:'POST',body:{action:'continue_import'}});assert.equal(result.status,409);assert.equal((await f.call('/api/zotero/connection')).data.ready,false);assert.equal(f.calls.length,1);assert.equal(f.server.items.size,0);
});
test('registration page/routes are owner-only and default-disabled without reading upload bodies or calling Zotero',async()=>{
 const f=await setup();assert.equal((await f.call('/zotero/register')).status,200);const state=await f.call('/api/zotero/catalog-registration');assert.equal(state.data.enabled,false);
 const blocked=await f.call('/api/zotero/catalog-registration/finalize',{method:'POST',body:{action:'register_catalog',owner_id:owner,approved:true}});assert.equal(blocked.status,409);assert.equal(f.calls.length,0);
 for(const path of ['/zotero/register','/api/zotero/catalog-registration','/api/zotero/catalog-registration/finalize','/api/zotero/catalog-registration/pdfs/'+'a'.repeat(64)]) {
  assert.equal((await f.call(path,{asOwner:null})).status,401);assert.equal((await f.call(path,{asOwner:'other-owner'})).status,403);
 }
 assert.equal(f.db.rows('zotero_execution_jobs').length,0);
});
test('notes-disabled key completes PDF import while standalone notes are excluded and incidental attachment notes never persist',async()=>{
 const f=await setup({withPdf:true});const marker='SYNTHETIC_PRIVATE_NOTE_MUST_NOT_PERSIST';
 const note=f.server.put(f.server.items,{key:'NOTE2345',itemType:'note',note:marker});
 const annotation=f.server.put(f.server.items,{key:'ANNO2345',itemType:'annotation',annotationText:marker});
 const attachment=f.server.put(f.server.items,{key:'ATCH2345',itemType:'attachment',parentItem:'OTHR2345',linkMode:'imported_file',contentType:'application/pdf',filename:'other.pdf',note:marker});
 await f.connect();const job=await f.create(),result=await f.finish(job.data.job_id);assert.equal(result.data.state,'complete',result.text);assert.equal(result.data.items[0].pdf_verified,true);
 assert.deepEqual(f.server.items.get(note.key),note);assert.deepEqual(f.server.items.get(annotation.key),annotation);assert.deepEqual(f.server.items.get(attachment.key),attachment);
 for(const table of ['zotero_active_connections','zotero_execution_jobs','zotero_execution_items','zotero_execution_collections'])assert.equal(JSON.stringify(f.db.rows(table)).includes(marker),false);
 assert.equal(JSON.stringify(f.calls).includes(marker),false);assert.ok(f.calls.filter(c=>new URL(c.url).pathname==='/users/123456/items'&&c.method==='GET').every(c=>new URL(c.url).searchParams.get('itemType')==='-note || annotation'));
});

// Provider-managed credentials drive the same unchanged durable runner and transport.
test('provider mode persists only explicitly selected metadata/PDF and repeat Save remains deduplicated',async()=>{
 const f=await setup({withPdf:true,providerManaged:true});await f.connect();assert.equal(f.db.rows('zotero_execution_jobs').length,0);
 const job=await f.create(),result=await f.finish(job.data.job_id);assert.equal(result.data.state,'complete',result.text);assert.equal(result.data.items.length,1);assert.equal(result.data.items[0].pdf_verified,true);assert.equal(f.server.items.size,2);
 assert.equal((await f.create()).data.job_id,job.data.job_id);const before=f.calls.length;await f.finish(job.data.job_id);assert.equal(f.calls.length,before);
 for(const table of ['zotero_active_connections','zotero_execution_jobs','zotero_execution_items','zotero_execution_collections'])assert.equal(JSON.stringify(f.db.rows(table)).includes(fakeKey),false);
});
test('provider disconnect/reconnect leaves old uncertain job journal visible and blocks stale continuation without replay',async()=>{
 const f=await setup({providerManaged:true});await f.connect();const job=await f.create();
 f.server.hook=(url,init)=>{if(init.method==='POST'&&url.pathname==='/users/123456/items')throw Error('synthetic uncertain outcome');return null;};
 const first=await f.call(`/api/zotero/executions/${job.data.job_id}/advance`,{method:'POST',body:{action:'continue_import'}});assert.equal(first.data.items[0].uncertain,true,first.text);
 const journal=f.db.rows('zotero_execution_items')[0];const version=f.db.rows('zotero_active_connections')[0].connection_version;
 await f.call('/api/zotero/disconnect',{method:'POST',body:{action:'disconnect'}});f.server.hook=null;f.db.sqlite.prepare('UPDATE zotero_execution_leases SET not_before=0').run();await f.connect();
 assert.equal(f.db.rows('zotero_active_connections')[0].connection_version,version+1);const before=f.calls.length;
 const view=await f.call(`/api/zotero/executions/${job.data.job_id}`);assert.equal(view.data.state,'connection_required');assert.equal(view.data.items[0].uncertain,true);
 assert.equal((await f.call(`/api/zotero/executions/${job.data.job_id}/advance`,{method:'POST',body:{action:'continue_import'}})).status,409);
 const after=f.db.rows('zotero_execution_items')[0];assert.equal(after.pending_operation,journal.pending_operation);assert.equal(after.reserved_key,journal.reserved_key);assert.equal(after.uncertain,1);assert.equal(f.calls.length,before);assert.equal(f.server.items.size,0);
});
