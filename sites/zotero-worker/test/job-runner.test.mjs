import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {MockD1} from './mock-d1.mjs';
import {hashBytes} from '../src/hash-bytes.mjs';
import {createExecutionJob,advanceExecutionJob,readExecutionJob} from '../src/job-runner.mjs';
import {canonicalJSON,sha256Text,registrationBinding,normalizeDOI,normalizeArxiv,parsePaper,
  matchPaper,metadataFor,loadVerifiedCatalog} from '../src/import-domain.mjs';

const clone=value=>structuredClone(value);
const error=(code,options={})=>Object.assign(new Error(code),{code,...options});
const pdfBytes=new TextEncoder().encode('%PDF-1.7\nSYNTHETIC REVIEWED SOURCE\n%%EOF\n');
const KEY1='ABCD2345',KEY2='ABCD2346',NOTE='NOTE2345',USER_COLLECTION='USER2345';
async function setup({withPdf=false,paperCount=2}={}) {
  const db=new MockD1();
  if(!db.sqlite.prepare("SELECT name FROM sqlite_master WHERE type='table' AND name='zotero_execution_jobs'").get())db.sqlite.exec(readFileSync(new URL('../migrations/0002_zotero_execution.sql',import.meta.url),'utf8'));
  const hashes=await hashBytes(pdfBytes),reportHash='b'.repeat(64);
  const papers=Array.from({length:paperCount},(_,i)=>({paper_id:`paper-${i+1}`,title:`Synthetic title ${i+1}`,
    source_url:`https://example.org/paper-${i+1}`,doi:`10.1234/synthetic-${i+1}`,arxiv:'',authors:['Test Author'],year:'2026',venue:'Test Venue',
    item_type:'journalArticle',publication:'published_verified',publication_evidence_sha256:'c'.repeat(64),topics:['agents'],
    pdf:withPdf?{blob_id:hashes.sha256,sha256:hashes.sha256,size:pdfBytes.length}:null,
    review_sha256:'d'.repeat(64),qualification_receipt_sha256:'e'.repeat(64),source_pdf_sha256:withPdf?hashes.sha256:'f'.repeat(64)}));
  const body=canonicalJSON({schema:'daily-agent-zotero-catalog-v1',report_sha256:reportHash,papers});
  const row={owner_id:'owner-1',report_id:'report-1',catalog_id:'catalog-1',catalog_version:1,
    content_sha256:await sha256Text(body),enabled:1,created_at:'2026-10-10T00:00:00.000Z',catalog_blob_json:body,
    report_sha256:reportHash,report_day:'2026-10-10',personal_scope:'personal:1234',source_catalog_sha256:'a'.repeat(64)};
  row.registration_sha256=await sha256Text(canonicalJSON(registrationBinding(row)));
  const keys=Object.keys(row);db.sqlite.prepare(`INSERT INTO zotero_report_catalogs (${keys.join(',')}) VALUES (${keys.map(()=>'?').join(',')})`).run(...keys.map(k=>row[k]));
  for(const p of papers)db.sqlite.prepare('INSERT INTO zotero_catalog_papers (owner_id,report_id,catalog_id,paper_id,display_title,metadata_json) VALUES (?,?,?,?,?,?)').run(row.owner_id,row.report_id,row.catalog_id,p.paper_id,p.title,canonicalJSON(p));
  const connection={owner_id:'owner-1',library_type:'user',library_id:'1234',connection_version:1,state:'active'};
  db.sqlite.prepare(`INSERT INTO zotero_active_connections(owner_id,library_type,library_id,connection_version,state,verified_at,permissions_json) VALUES (?,?,?,?,?,?,?)`).run(...Object.values(connection),'2026-10-10T00:00:00.000Z',JSON.stringify({library:true,write:true,notes:false,files:true}));
  let time=Date.parse('2026-10-10T12:00:00Z');
  const transport=new MemoryTransport(),blobs={readBlob:async input=>{assert.equal(input.ownerId,'owner-1');assert.equal(input.sha256,hashes.sha256);return new Uint8Array(pdfBytes);}};
  const base={db,ownerId:'owner-1',connection,liveGate:true,now:()=>time,transport,blobs,hashBytes};
  const request={action:'save_selected',report_id:row.report_id,catalog_id:row.catalog_id,catalog_version:1,paper_ids:['paper-1'],pdf_intent:withPdf?'open_access_if_available':'metadata_only'};
  return {db,papers,row,connection,transport,base,request,hashes,tick:ms=>{time+=ms;},create:extra=>createExecutionJob({...base,request,...extra})};
}
class MemoryTransport {
  constructor(){this.items=new Map();this.collections=new Map();this.files=new Map();this.calls=[];this.hooks={};this.version=1;}
  async call(name,input,fn){this.calls.push({name,input:clone(input)});if(this.hooks[name])await this.hooks[name](input,this);return fn();}
  object(key,data){const version=++this.version;return {key,version,data:{...clone(data),key,version}};}
  put(key,data){const item=this.object(key,data);this.items.set(key,item);return clone(item);}
  async verifyCurrentKey(input){assert.equal(input.requireNotes,false,'Runner must not request notes access');return this.call('verifyCurrentKey',input,()=>({userID:'1234',library:true,write:true,notes:false,files:true}));}
  async inventory(){return this.call('inventory',null,()=>({libraryVersion:this.version,complete:true,includesTrashed:true,excludesNotes:true,items:clone([...this.items.values()].filter(item=>!['note','annotation'].includes(item.data.itemType))),collections:clone([...this.collections.values()])}));}
  async getItem(key){return this.call('getItem',key,()=>{const item=this.items.get(key);if(item&&['note','annotation'].includes(item.data.itemType))throw error('AUTHORIZATION_DENIED');return clone(item||null);});}
  async getCollection(key){return this.call('getCollection',key,()=>clone(this.collections.get(key)||null));}
  async createItem({key,data}){return this.call('createItem',{key,data},()=>{if(this.items.has(key))throw error('CREATE_ALREADY_EXISTS');return this.put(key,data);});}
  async createCollection({key,name,parentCollection=false}){return this.call('createCollection',{key,name,parentCollection},()=>{if(this.collections.has(key))throw error('CREATE_ALREADY_EXISTS');const obj=this.object(key,{name,parentCollection});this.collections.set(key,obj);return clone(obj);});}
  async patchItemMembership(input){return this.call('patchItemMembership',input,()=>{const current=this.items.get(input.itemKey);if(current.version!==input.expectedVersion)throw error('WRITE_CONFLICT');return this.put(input.itemKey,{...current.data,collections:input.collections,tags:input.tags});});}
  async createStoredAttachment(input){return this.call('createStoredAttachment',input,()=>{if(this.items.has(input.key))throw error('CREATE_ALREADY_EXISTS');return this.put(input.key,{itemType:'attachment',parentItem:input.parentItem,linkMode:'imported_file',contentType:'application/pdf',filename:input.filename,title:input.title,tags:[],md5:null,mtime:null});});}
  async authorizePdfUpload(input){return this.call('authorizePdfUpload',input,async()=>{const hashes=await hashBytes(input.bytes);return {ticket:{input:clone(input),hashes},itemKey:input.itemKey,exists:false,size:input.bytes.length,...hashes};});}
  async transferPdfUpload(ticket){return this.call('transferPdfUpload',ticket,()=>({transferred:true}));}
  async registerPdfUpload(ticket){return this.call('registerPdfUpload',ticket,()=>{const item=this.items.get(ticket.input.itemKey);this.put(item.key,{...item.data,md5:ticket.hashes.md5,mtime:ticket.input.mtime});this.files.set(item.key,new Uint8Array(ticket.input.bytes));return {registered:true};});}
  async verifyStoredPdf(proof){return this.call('verifyStoredPdf',proof,async()=>{const item=this.items.get(proof.itemKey),bytes=this.files.get(proof.itemKey);if(!bytes)throw error('NOT_FOUND');const hashes=await hashBytes(bytes);if(item.data.md5!==proof.md5||hashes.md5!==proof.md5||hashes.sha256!==proof.sha256||bytes.length!==proof.size)throw error('STORED_BYTES_MISMATCH');return {...proof,version:item.version,storedBytesVerified:true};});}
  count(name){return this.calls.filter(c=>c.name===name).length;}
}
async function run(f,jobId,{maxSteps=10,...extra}={}) {
  let state;for(let n=0;n<40;n++){
    state=await advanceExecutionJob({...f.base,jobId,maxSteps,...extra});
    if(state.state==='retry_wait'){f.tick(100000);continue;}
    if(!['queued','running'].includes(state.state))return state;
  }throw new Error('Synthetic job did not settle');
}
const book=p=>({...metadataFor(parsePaper(p)),collections:[USER_COLLECTION],tags:[{tag:'User tag',type:0}],relations:{}});

test('fresh explicit Save only; pinned selected-only job, keys persist and repeated Save is idempotent',async()=>{
  const f=await setup();
  await assert.rejects(f.create({liveGate:false}),{code:'EXECUTION_DISABLED'});
  await assert.rejects(f.create({request:{...f.request,action:'save_to_zotero'}}));
  await assert.rejects(f.create({request:{...f.request,paper_ids:['not-in-catalog']}}));
  const job=await f.create(),savedKeys=clone(f.db.rows('zotero_execution_items'));
  assert.equal(job.created,true);assert.equal(f.transport.calls.length,0);
  assert.equal(savedKeys.length,1);assert.ok(savedKeys[0].reserved_key);assert.ok(savedKeys[0].attachment_reserved_key);
  const again=await f.create();assert.equal(again.job_id,job.job_id);assert.equal(again.created,false);
  assert.deepEqual(clone(f.db.rows('zotero_execution_items')),savedKeys);
  const result=await run(f,job.job_id);assert.equal(result.state,'complete');
  assert.equal(f.transport.count('createItem'),1);assert.equal(f.transport.items.size,1);
  assert.equal([...f.transport.items.values()][0].data.DOI,f.papers[0].doi);
  assert.equal((await run(f,job.job_id)).state,'complete');assert.equal(f.transport.count('createItem'),1);
  assert.deepEqual(f.db.rows('zotero_import_intents'),[]);
});

test('job identity binds owner, catalog, PDF intent and active connection version',async()=>{
  const f=await setup(),first=await f.create();
  f.db.sqlite.prepare('UPDATE zotero_active_connections SET connection_version = 2').run();f.connection.connection_version=2;
  const second=await f.create();assert.notEqual(first.job_id,second.job_id);
  await assert.rejects(advanceExecutionJob({...f.base,jobId:first.job_id}),{code:'CONNECTION_STALE'});
  await assert.rejects(readExecutionJob({...f.base,ownerId:'other-owner',jobId:first.job_id}),{code:'EXECUTION_NOT_FOUND'});
  assert.equal((await readExecutionJob({...f.base,jobId:first.job_id})).state,'connection_required');
  assert.equal(f.transport.calls.length,0);
});

test('tampered catalog bytes, registration binding or embedded rows fail closed before transport',async()=>{
  for(const mutation of [db=>db.sqlite.prepare("UPDATE zotero_report_catalogs SET catalog_blob_json = catalog_blob_json || ' '").run(),
    db=>db.sqlite.prepare("UPDATE zotero_report_catalogs SET registration_sha256 = ?").run('0'.repeat(64)),
    db=>db.sqlite.prepare("UPDATE zotero_catalog_papers SET metadata_json = '{}'").run()]){
    const f=await setup();mutation(f.db);await assert.rejects(f.create());assert.equal(f.transport.calls.length,0);assert.equal(f.db.rows('zotero_execution_jobs').length,0);
  }
});

test('normalizes DOI/arXiv, exact conservative title identity, trash and conflicting identifiers',()=>{
  assert.equal(normalizeDOI('https://doi.org/10.1234%2FABC.'),'10.1234/abc.');
  assert.equal(normalizeArxiv('https://arxiv.org/pdf/2601.01234v2.pdf'),'2601.01234');
  const p={doi:'10.1234/abc',arxiv:'2601.01234',title:'A',authors:['Test Author'],year:'2026',venue:'V'};
  assert.throws(()=>matchPaper(p,[{key:KEY1,data:{itemType:'preprint',DOI:p.doi,extra:'arXiv: 2601.09999'}}]),{code:'IDENTITY_CONFLICT'});
  assert.throws(()=>matchPaper(p,[{key:KEY1,data:{itemType:'preprint',DOI:p.doi,deleted:1}}]),{code:'MATCH_IN_TRASH'});
  const fallback={...p,doi:'',arxiv:'',title:'Ａ  title'};
  const item={key:KEY1,data:{itemType:'journalArticle',title:'a title',creators:[{creatorType:'author',name:'Test Author'}],date:'2026',publicationTitle:'V'}};
  assert.equal(matchPaper(fallback,[item]).key,KEY1);
  assert.throws(()=>matchPaper({...fallback,authors:['T. Author']},[item]),{code:'FALLBACK_IDENTITY_REVIEW_REQUIRED'});
  assert.throws(()=>matchPaper({...fallback,title:'a title!'},[item]),{code:'FALLBACK_IDENTITY_REVIEW_REQUIRED'});
});

test('existing item adds membership while preserving user tags, collections and inaccessible notes',async()=>{
  const f=await setup();f.transport.put(KEY1,{...book(f.papers[0]),abstractNote:'Private abstract'});
  f.transport.put(NOTE,{itemType:'note',parentItem:KEY1,note:'Child note must stay exact'});
  const notes=clone(f.transport.items.get(NOTE));
  const job=await f.create(),result=await run(f,job.job_id);
  assert.equal(result.items[0].state,'already_exists');assert.equal(f.transport.count('createItem'),0);
  const saved=f.transport.items.get(KEY1).data;
  assert.equal(saved.abstractNote,'Private abstract');assert.deepEqual(f.transport.items.get(NOTE),notes);
  assert.equal(f.transport.calls.some(call=>call.name==='getItem'&&call.input===NOTE),false);
  assert.ok(f.transport.calls.filter(call=>call.name==='verifyCurrentKey').every(call=>call.input.requireNotes===false));
  assert.ok(saved.collections.includes(USER_COLLECTION));assert.deepEqual(saved.tags.find(t=>t.tag==='User tag'),{tag:'User tag',type:0});
  assert.ok(saved.tags.some(t=>t.tag==='topic:agents'));
  for(const c of f.transport.calls.filter(c=>c.name==='patchItemMembership'))assert.deepEqual(Object.keys(c.input).sort(),['collections','expectedVersion','itemKey','tags']);
});

test('library-wide lease prevents concurrent workers, including separate jobs',async()=>{
  const f=await setup(),a=await f.create(),b=await f.create({request:{...f.request,paper_ids:['paper-2']}});
  let release,entered;const started=new Promise(r=>{entered=r;});const hold=new Promise(r=>{release=r;});
  f.transport.hooks.createItem=async()=>{assert.ok(f.db.rows('zotero_execution_items')[0].reserved_key);entered();await hold;};
  const running=advanceExecutionJob({...f.base,jobId:a.job_id});await started;
  await assert.rejects(advanceExecutionJob({...f.base,jobId:b.job_id}),{code:'EXECUTION_BUSY'});
  release();await running;delete f.transport.hooks.createItem;
  assert.equal(f.transport.count('createItem'),1);assert.equal((await run(f,b.job_id)).state,'complete');
});

test('expired worker cannot execute next operation or commit after a newer lease takeover',async()=>{
  const f=await setup(),job=await f.create();let release,entered;
  const start=new Promise(r=>{entered=r;}),hold=new Promise(r=>{release=r;});let first=true;
  f.transport.hooks.verifyCurrentKey=async()=>{if(first){first=false;entered();await hold;}};
  const stale=advanceExecutionJob({...f.base,jobId:job.job_id,leaseMs:1000});await start;f.tick(1001);
  const newer=await advanceExecutionJob({...f.base,jobId:job.job_id,leaseMs:1000});assert.equal(newer.items[0].metadata_saved,true);
  release();await assert.rejects(stale,{code:'LEASE_OR_CONNECTION_LOST'});
  assert.equal(f.transport.count('createItem'),1);
});

test('connection change during an in-flight write fences commit and preserves uncertain journal',async()=>{
  const f=await setup(),job=await f.create();
  f.transport.hooks.createItem=async()=>{f.db.sqlite.prepare('UPDATE zotero_active_connections SET connection_version = 2').run();};
  await assert.rejects(advanceExecutionJob({...f.base,jobId:job.job_id}),{code:'LEASE_OR_CONNECTION_LOST'});
  const receipt=f.db.rows('zotero_execution_items')[0];assert.equal(receipt.metadata_saved,0);assert.equal(receipt.pending_operation,'create_item');assert.equal(receipt.uncertain,1);
  assert.equal(f.transport.items.size,1);assert.equal((await readExecutionJob({...f.base,jobId:job.job_id})).state,'connection_required');
});

test('transport factory request guard stops a second internal request after revocation',async()=>{
  const f=await setup(),job=await f.create();let requests=0;
  const factory=({guardRequest})=>({...f.transport,verifyCurrentKey:async()=>{
    await guardRequest();requests++;f.db.sqlite.prepare("UPDATE zotero_active_connections SET state = 'revoked'").run();
    await guardRequest();requests++;return {userID:'1234',library:true,write:true,notes:false};}});
  await assert.rejects(advanceExecutionJob({...f.base,jobId:job.job_id,transportFactory:factory}),{code:'LEASE_OR_CONNECTION_LOST'});
  assert.equal(requests,1);assert.equal(f.transport.items.size,0);
});

test('uncertain create that reached server reconciles reserved key after restart without a duplicate',async()=>{
  const f=await setup(),job=await f.create();let once=true;
  f.transport.hooks.createItem=async({key,data},transport)=>{if(once){once=false;transport.put(key,data);throw error('NETWORK_FAILURE',{uncertain:true});}};
  const pending=await advanceExecutionJob({...f.base,jobId:job.job_id});assert.equal(pending.state,'retry_wait');assert.equal(pending.items[0].uncertain,true);
  f.tick(1001);const result=await run(f,job.job_id);assert.equal(result.state,'complete');assert.equal(f.transport.count('createItem'),1);
});

test('uncertain missing create blocks after readback rather than blindly replaying',async()=>{
  const f=await setup(),job=await f.create();f.transport.hooks.createItem=async()=>{throw error('NETWORK_FAILURE',{uncertain:true});};
  await advanceExecutionJob({...f.base,jobId:job.job_id});f.tick(1001);delete f.transport.hooks.createItem;
  const result=await advanceExecutionJob({...f.base,jobId:job.job_id});assert.equal(result.state,'retry_wait');assert.equal(result.items[0].error_code,'UNCERTAIN_CREATE_NOT_FOUND');
  assert.equal(f.transport.count('createItem'),1);
});

test('uncertain collection creation reconciles same key, with no duplicate collection',async()=>{
  const f=await setup(),job=await f.create();let once=true;
  f.transport.hooks.createCollection=async(input,t)=>{if(once){once=false;t.collections.set(input.key,t.object(input.key,{name:input.name,parentCollection:input.parentCollection}));throw error('NETWORK_FAILURE',{uncertain:true});}};
  const result=await run(f,job.job_id);assert.equal(result.state,'complete');
  assert.equal(f.transport.collections.size,5);assert.equal(f.transport.count('createCollection'),5);
});

test('412 retry rereads identity; changed identity cannot receive the old paper classification',async()=>{
  const f=await setup();f.transport.put(KEY1,book(f.papers[0]));const job=await f.create();let once=true;
  f.transport.hooks.patchItemMembership=async(input,t)=>{if(once){once=false;const old=t.items.get(input.itemKey);t.put(input.itemKey,{...old.data,DOI:'10.1234/different',tags:[...old.data.tags,{tag:'Concurrent user tag',type:0}]});throw error('WRITE_CONFLICT');}};
  const result=await run(f,job.job_id);assert.equal(result.state,'failure');assert.equal(result.items[0].error_code,'ITEM_IDENTITY_CHANGED');
  assert.equal(f.transport.count('patchItemMembership'),1);assert.ok(f.transport.items.get(KEY1).data.tags.some(t=>t.tag==='Concurrent user tag'));
});

test('Retry-After persists exact earliest resume time; early advance makes zero transport calls',async()=>{
  const f=await setup(),job=await f.create();f.transport.hooks.inventory=async()=>{throw error('RATE_LIMITED',{retryAfterMs:120000});};
  const result=await advanceExecutionJob({...f.base,jobId:job.job_id});assert.equal(result.state,'retry_wait');assert.equal(result.retry_at,'2026-10-10T12:02:00.000Z');
  const calls=f.transport.calls.length;assert.equal((await advanceExecutionJob({...f.base,jobId:job.job_id})).state,'retry_wait');assert.equal(f.transport.calls.length,calls);
  f.tick(120000);delete f.transport.hooks.inventory;assert.equal((await run(f,job.job_id)).state,'complete');
});

test('default bounded advance handles one stage and never processes unselected or second selected paper',async()=>{
  const f=await setup(),job=await f.create({request:{...f.request,paper_ids:['paper-1','paper-2']}});
  const result=await advanceExecutionJob({...f.base,jobId:job.job_id});assert.equal(result.items[0].metadata_saved,true);assert.equal(result.items[0].classification_saved,false);
  assert.equal(result.items[1].state,'queued');assert.equal(f.transport.count('createItem'),1);
});

test('PDF is pending after upload registration, complete only after byte readback',async()=>{
  const f=await setup({withPdf:true}),job=await f.create();let result;
  for(let n=0;n<20;n++){result=await advanceExecutionJob({...f.base,jobId:job.job_id});if(f.transport.count('registerPdfUpload'))break;}
  assert.equal(result.items[0].state,'pdf_pending');assert.equal(result.items[0].pdf_verified,false);assert.equal(f.transport.count('verifyStoredPdf'),0);
  result=await advanceExecutionJob({...f.base,jobId:job.job_id});assert.equal(result.state,'complete');assert.equal(result.items[0].pdf_verified,true);
  assert.equal(f.transport.count('createStoredAttachment'),1);assert.equal(f.transport.count('transferPdfUpload'),1);
});

test('registered PDF recovers lost verification response even after local source disappears',async()=>{
  const f=await setup({withPdf:true}),job=await f.create();let once=true;
  f.transport.hooks.verifyStoredPdf=async()=>{if(once){once=false;throw error('NETWORK_FAILURE',{uncertain:true});}};
  let result;for(let n=0;n<20;n++){result=await advanceExecutionJob({...f.base,jobId:job.job_id,maxSteps:10});if(result.state==='retry_wait')break;}
  assert.equal(result.items[0].state,'pdf_pending');assert.equal(result.items[0].pdf_verified,false);
  f.base.blobs={readBlob:async()=>null};f.tick(1001);result=await run(f,job.job_id);
  assert.equal(result.state,'complete');assert.equal(f.transport.count('authorizePdfUpload'),1);assert.equal(f.transport.count('createStoredAttachment'),1);
});

test('existing registered attachment verifies without fetching unavailable private source',async()=>{
  const f=await setup({withPdf:true});f.transport.put(KEY1,book(f.papers[0]));
  const name=`${f.hashes.sha256.slice(0,16)}.pdf`;f.transport.put(KEY2,{itemType:'attachment',parentItem:KEY1,filename:name,linkMode:'imported_file',contentType:'application/pdf',md5:f.hashes.md5,mtime:123});f.transport.files.set(KEY2,pdfBytes);
  f.base.blobs={readBlob:async()=>{throw new Error('Must not require local source');}};
  const job=await f.create(),result=await run(f,job.job_id);assert.equal(result.state,'complete');assert.equal(result.items[0].pdf_verified,true);
  assert.equal(f.transport.count('createStoredAttachment'),0);assert.equal(f.transport.count('authorizePdfUpload'),0);
});

test('missing private PDF stays pending after successful metadata and can safely continue later',async()=>{
  const f=await setup({withPdf:true});const saved=f.base.blobs;f.base.blobs={readBlob:async()=>null};const job=await f.create();
  let result=await advanceExecutionJob({...f.base,jobId:job.job_id,maxSteps:10});
  assert.equal(result.state,'retry_wait');assert.equal(result.items[0].state,'pdf_pending');assert.equal(result.items[0].metadata_saved,true);
  assert.equal(result.items[0].classification_saved,true);assert.equal(f.transport.count('createStoredAttachment'),0);
  f.base.blobs=saved;f.tick(60000);result=await run(f,job.job_id);assert.equal(result.state,'complete');assert.equal(f.transport.count('createItem'),1);
});

test('missing reviewed PDF reference is not mislabeled complete',async()=>{
  const f=await setup(),job=await f.create({request:{...f.request,pdf_intent:'open_access_if_available'}});
  const result=await run(f,job.job_id);assert.equal(result.state,'pdf_pending');assert.equal(result.items[0].error_code,'VERIFIED_PDF_UNAVAILABLE');assert.equal(result.items[0].retryable,false);
});

test('unregistered uncertain transfer is not blindly retransmitted or marked complete',async()=>{
  const f=await setup({withPdf:true}),job=await f.create();f.transport.hooks.transferPdfUpload=async()=>{throw error('NETWORK_FAILURE',{uncertain:true});};
  await advanceExecutionJob({...f.base,jobId:job.job_id,maxSteps:10});f.tick(1001);
  const result=await advanceExecutionJob({...f.base,jobId:job.job_id,maxSteps:10});assert.equal(result.state,'retry_wait');assert.equal(result.items[0].error_code,'UNCERTAIN_PDF_NOT_REGISTERED');assert.equal(result.items[0].retryable,true);
  assert.equal(f.transport.count('transferPdfUpload'),1);assert.equal(f.transport.count('registerPdfUpload'),0);
});

test('mismatched source bytes fail before any attachment write and do not erase metadata success',async()=>{
  const f=await setup({withPdf:true}),job=await f.create();f.base.blobs={readBlob:async()=>new Uint8Array(pdfBytes).fill(65)};
  const result=await run(f,job.job_id);assert.equal(result.state,'pdf_pending');assert.equal(result.items[0].metadata_saved,true);assert.equal(result.items[0].pdf_verified,false);
  assert.equal(result.items[0].error_code,'INVALID_PDF_BYTES');assert.equal(f.transport.count('createStoredAttachment'),0);
});

test('partial success survives one ambiguous identity failure and later selected paper succeeds',async()=>{
  const f=await setup();f.transport.put(KEY1,book(f.papers[0]));f.transport.put(KEY2,book(f.papers[0]));
  const job=await f.create({request:{...f.request,paper_ids:['paper-1','paper-2']}}),result=await run(f,job.job_id);
  assert.equal(result.state,'partial_failure');assert.equal(result.items[0].error_code,'AMBIGUOUS_IDENTITY');assert.equal(result.items[1].state,'complete');assert.equal(f.transport.count('createItem'),1);
});

test('a new overlapping selection finds earlier items/attachments instead of duplicating them',async()=>{
  const f=await setup({withPdf:true}),one=await f.create();assert.equal((await run(f,one.job_id)).state,'complete');
  const two=await f.create({request:{...f.request,paper_ids:['paper-1','paper-2']}});assert.notEqual(one.job_id,two.job_id);
  const result=await run(f,two.job_id);assert.equal(result.state,'complete');assert.equal(result.items[0].state,'already_exists');
  assert.equal(f.transport.count('createItem'),2);assert.equal(f.transport.count('createStoredAttachment'),2);
});

test('an uncertain create journal survives a failed recovery read and still never retries a missing create',async()=>{
  const f=await setup(),job=await f.create();
  f.transport.hooks.createItem=async()=>{throw error('NETWORK_FAILURE',{uncertain:true});};
  await advanceExecutionJob({...f.base,jobId:job.job_id});f.tick(1001);delete f.transport.hooks.createItem;
  f.transport.hooks.inventory=async()=>{throw error('SERVICE_UNAVAILABLE',{uncertain:false});};
  await advanceExecutionJob({...f.base,jobId:job.job_id});
  let receipt=f.db.rows('zotero_execution_items')[0];assert.equal(receipt.pending_operation,'create_item');assert.equal(receipt.uncertain,1);
  f.tick(1001);delete f.transport.hooks.inventory;
  const result=await advanceExecutionJob({...f.base,jobId:job.job_id});assert.equal(result.items[0].error_code,'UNCERTAIN_CREATE_NOT_FOUND');
  assert.equal(f.transport.count('createItem'),1);
});

test('an uncertain PDF transfer journal survives a failed GET and never restarts transfer',async()=>{
  const f=await setup({withPdf:true}),job=await f.create();
  f.transport.hooks.transferPdfUpload=async()=>{throw error('NETWORK_FAILURE',{uncertain:true});};
  await advanceExecutionJob({...f.base,jobId:job.job_id,maxSteps:10});f.tick(1001);delete f.transport.hooks.transferPdfUpload;
  f.transport.hooks.getItem=async()=>{throw error('SERVICE_UNAVAILABLE',{uncertain:false});};
  await advanceExecutionJob({...f.base,jobId:job.job_id});
  assert.equal(f.db.rows('zotero_execution_items')[0].pending_operation,'transfer_pdf');
  f.tick(1001);delete f.transport.hooks.getItem;
  const result=await advanceExecutionJob({...f.base,jobId:job.job_id});assert.equal(result.items[0].error_code,'UNCERTAIN_PDF_NOT_REGISTERED');
  assert.equal(f.transport.count('transferPdfUpload'),1);assert.equal(f.transport.count('authorizePdfUpload'),1);
});

test('library Retry-After blocks second selected paper and independent jobs, not just first receipt',async()=>{
  const f=await setup(),job=await f.create({request:{...f.request,paper_ids:['paper-1','paper-2']}}),other=await f.create();
  f.transport.hooks.inventory=async()=>{throw error('RATE_LIMITED',{retryAfterMs:120000});};
  const delayed=await advanceExecutionJob({...f.base,jobId:job.job_id});assert.equal(delayed.state,'retry_wait');
  const count=f.transport.calls.length;
  await advanceExecutionJob({...f.base,jobId:job.job_id});await advanceExecutionJob({...f.base,jobId:other.job_id});
  assert.equal(f.transport.calls.length,count);assert.equal((await readExecutionJob({...f.base,jobId:other.job_id})).retry_at,delayed.retry_at);
});

test('successful response Backoff persists across fresh adapters and jobs without another fetch',async()=>{
  const f=await setup(),job=await f.create();
  const factory=({guardRequest,noteBackoff})=>new Proxy(f.transport,{get(target,name){if(name==='verifyCurrentKey')return async input=>{await guardRequest();const value=await target.verifyCurrentKey(input);await noteBackoff(90000);return value;};const value=target[name];return typeof value==='function'?value.bind(target):value;}});
  const delayed=await advanceExecutionJob({...f.base,jobId:job.job_id,transportFactory:factory});assert.equal(delayed.state,'retry_wait');assert.equal(delayed.retry_at,'2026-10-10T12:01:30.000Z');
  const count=f.transport.calls.length;await advanceExecutionJob({...f.base,jobId:job.job_id});assert.equal(f.transport.calls.length,count);assert.equal(f.transport.count('inventory'),0);
  f.tick(90000);assert.equal((await run(f,job.job_id)).state,'complete');
});

test('a late old create appearing after new job inventory forces fresh identity reconciliation',async()=>{
  const f=await setup(),old=await f.create();let stored;
  f.transport.hooks.createItem=async input=>{stored=clone(input);throw error('NETWORK_FAILURE',{uncertain:true});};
  await advanceExecutionJob({...f.base,jobId:old.job_id});delete f.transport.hooks.createItem;
  const newer=await f.create({request:{...f.request,paper_ids:['paper-1','paper-2']}});
  let appeared=false;f.transport.hooks.getItem=async(key,t)=>{if(key===stored.key&&!appeared){appeared=true;t.put(stored.key,stored.data);}};
  const result=await advanceExecutionJob({...f.base,jobId:newer.job_id});assert.equal(result.items[0].error_code,'INVENTORY_CHANGED');assert.equal(f.transport.count('createItem'),1);assert.equal(f.transport.items.size,1);
  delete f.transport.hooks.getItem;f.tick(1001);
  assert.equal((await run(f,newer.job_id)).state,'complete');assert.equal(f.transport.count('createItem'),2);
});

test('a late old collection forces refresh before journals and never creates a duplicate path',async()=>{
  const f=await setup(),old=await f.create();await advanceExecutionJob({...f.base,jobId:old.job_id});let stored;
  f.transport.hooks.createCollection=async input=>{stored=clone(input);throw error('NETWORK_FAILURE',{uncertain:true});};
  await advanceExecutionJob({...f.base,jobId:old.job_id});delete f.transport.hooks.createCollection;
  const newer=await f.create({request:{...f.request,paper_ids:['paper-1','paper-2']}});await advanceExecutionJob({...f.base,jobId:newer.job_id});
  let appeared=false;f.transport.hooks.getCollection=async(key,t)=>{if(key===stored.key&&!appeared){appeared=true;t.collections.set(key,t.object(key,{name:stored.name,parentCollection:stored.parentCollection}));}};
  const result=await advanceExecutionJob({...f.base,jobId:newer.job_id});assert.equal(result.items[0].error_code,'INVENTORY_CHANGED');
  assert.equal(f.transport.count('createCollection'),1);
  assert.ok(f.db.rows('zotero_execution_collections').filter(r=>r.job_id===newer.job_id).every(r=>r.pending_operation===null));
  delete f.transport.hooks.getCollection;f.tick(1001);
  assert.equal((await run(f,newer.job_id)).state,'complete');assert.equal(f.transport.collections.size,5);
});

test('a late old attachment appearing after inventory refreshes rather than allocating duplicate child',async()=>{
  const f=await setup({withPdf:true}),old=await f.create();let stored;
  f.transport.hooks.createStoredAttachment=async input=>{stored=clone(input);throw error('NETWORK_FAILURE',{uncertain:true});};
  await advanceExecutionJob({...f.base,jobId:old.job_id,maxSteps:10});delete f.transport.hooks.createStoredAttachment;
  assert.ok(stored);
  const newer=await f.create({request:{...f.request,paper_ids:['paper-1','paper-2']}});
  // Six stages find the existing parent and five existing collection paths; seventh
  // adds the already-present membership, then the next stage reaches PDF planning.
  await advanceExecutionJob({...f.base,jobId:newer.job_id,maxSteps:7});
  let appeared=false;f.transport.hooks.getItem=async(key,t)=>{if(key===stored.key&&!appeared){appeared=true;t.put(key,{itemType:'attachment',parentItem:stored.parentItem,filename:stored.filename,linkMode:'imported_file',contentType:'application/pdf',md5:null,mtime:null});}};
  const result=await advanceExecutionJob({...f.base,jobId:newer.job_id});assert.equal(result.items[0].error_code,'INVENTORY_CHANGED');
  assert.equal(f.transport.count('createStoredAttachment'),1);assert.equal([...f.transport.items.values()].filter(i=>i.data.itemType==='attachment').length,1);
});

test('full casefold remains conservative and supports Unicode exact fallback identities',()=>{
  const paper={doi:'',arxiv:'',title:'Straße Σς',authors:['Éva'],year:'2026',venue:'V'};
  const candidate={key:KEY1,data:{itemType:'journalArticle',title:'STRASSE σσ',creators:[{creatorType:'author',name:'ÉVA'}],date:'2026',publicationTitle:'v'}};
  assert.equal(matchPaper(paper,[candidate]).key,KEY1);
  assert.throws(()=>matchPaper({...paper,title:'Straße Σς!'},[candidate]),{code:'FALLBACK_IDENTITY_REVIEW_REQUIRED'});
});

test('title-only document creation retains exact fallback venue in Extra without fabricating publication',async()=>{
  const f=await setup();const paper=parsePaper({...f.papers[0],doi:'',item_type:'document',publication:'unverified',publication_evidence_sha256:''});
  const data=metadataFor(paper);assert.equal(data.itemType,'document');assert.match(data.extra,/Venue: Test Venue/);
  assert.equal(matchPaper(paper,[{key:KEY1,data}]).key,KEY1);
});

test('new explicit execution never upgrades, mutates or executes an old blocked intent',async()=>{
  const f=await setup();f.db.sqlite.prepare(`INSERT INTO zotero_import_intents
    (owner_id,job_id,idempotency_key,report_id,catalog_id,catalog_version,catalog_sha256,paper_ids_json,pdf_intent,state,created_at)
    VALUES (?,?,?,?,?,?,?,?,?,?,?)`).run('owner-1',`zj_${'9'.repeat(64)}`,'9'.repeat(64),'report-1','catalog-1',1,f.row.content_sha256,'["paper-2"]','metadata_only','blocked_connection','2026-10-09T00:00:00Z');
  const before=clone(f.db.rows('zotero_import_intents'));const job=await f.create();assert.equal((await run(f,job.job_id)).state,'complete');
  assert.deepEqual(clone(f.db.rows('zotero_import_intents')),before);assert.equal([...f.transport.items.values()].some(i=>i.data.DOI===f.papers[1].doi),false);
});

test('job and every reservation are atomic, including failure and concurrent repeated Save',async()=>{
  const f=await setup(),batch=f.db.batch.bind(f.db);
  f.db.batch=statements=>batch([...statements,f.db.prepare('INSERT INTO nonexistent_fixture_table VALUES (1)')]);
  await assert.rejects(f.create());assert.equal(f.db.rows('zotero_execution_jobs').length,0);assert.equal(f.db.rows('zotero_execution_items').length,0);assert.equal(f.db.rows('zotero_execution_collections').length,0);
  f.db.batch=batch;
  const [a,b]=await Promise.all([f.create(),f.create()]);assert.equal(a.job_id,b.job_id);assert.equal([a,b].filter(j=>j.created).length,1);
  assert.equal(f.db.rows('zotero_execution_items').length,1);assert.equal(f.db.rows('zotero_execution_collections').length,5);assert.equal(f.transport.calls.length,0);
});

test('notes permission may be absent while full metadata and PDF import succeeds without reading any note',async()=>{
  for(const withPdf of [false,true]){
    const f=await setup({withPdf});
    f.db.sqlite.prepare('UPDATE zotero_active_connections SET permissions_json = ?').run(JSON.stringify({library:true,write:true,files:true}));
    f.transport.put(NOTE,{itemType:'note',parentItem:KEY1,note:'SYNTHETIC PRIVATE NOTE, unavailable to importer'});
    const note=clone(f.transport.items.get(NOTE));
    f.transport.verifyCurrentKey=async input=>{assert.equal(input.requireNotes,false);return f.transport.call('verifyCurrentKey',input,()=>({userID:'1234',library:true,write:true,files:true}));};
    const job=await f.create(),result=await run(f,job.job_id);
    assert.equal(result.state,'complete');assert.equal(result.items[0].pdf_verified,withPdf);
    assert.deepEqual(f.transport.items.get(NOTE),note);
    assert.equal(f.transport.calls.some(call=>call.name==='getItem'&&call.input===NOTE),false);
    assert.ok(f.transport.calls.filter(call=>call.name==='patchItemMembership').every(call=>Object.keys(call.input).every(key=>['itemKey','expectedVersion','collections','tags'].includes(key))));
  }
});

test('runner rejects inventory without proof of non-note scope or containing private note-like objects',async()=>{
  for(const mode of ['missing-scope','false-scope','note','annotation']){
    const f=await setup(),job=await f.create();
    f.transport.inventory=async()=>({libraryVersion:1,complete:true,includesTrashed:true,
      ...(mode==='missing-scope'?{}:{excludesNotes:mode!=='false-scope'}),
      items:['note','annotation'].includes(mode)?[{key:NOTE,version:1,data:{itemType:mode}}]:[],collections:[]});
    const result=await advanceExecutionJob({...f.base,jobId:job.job_id});
    assert.equal(result.state,'failure');assert.equal(result.items[0].error_code,['note','annotation'].includes(mode)?'INVENTORY_SCOPE_MISMATCH':'INCOMPLETE_INVENTORY');
    assert.equal(f.transport.count('createItem'),0);assert.equal(f.transport.count('getItem'),0);
  }
});

test('reviewed mode preserves metadata/classification when PDF permission is absent, then retries only PDF',async()=>{
  const f=await setup({withPdf:true,paperCount:1});
  f.db.sqlite.prepare('UPDATE zotero_active_connections SET permissions_json=?').run(JSON.stringify({library:true,write:true,files:false,notes:false}));
  const verify=f.transport.verifyCurrentKey.bind(f.transport);
  f.transport.verifyCurrentKey=async input=>({...await verify(input),files:false});
  await assert.rejects(f.create(),e=>e.code==='CONNECTION_UNVERIFIED'); // Legacy gate remains strict.
  const job=await f.create({metadataFirst:true});
  const pending=await advanceExecutionJob({...f.base,jobId:job.job_id,metadataFirst:true,maxSteps:10});
  assert.equal(pending.items[0].metadata_saved,true);assert.equal(pending.items[0].classification_saved,true);
  assert.equal(pending.items[0].error_code,'PDF_PERMISSION_UNAVAILABLE');assert.equal(f.transport.count('createStoredAttachment'),0);
  const creates=f.transport.count('createItem'),patches=f.transport.count('patchItemMembership');
  f.tick(60001);f.db.sqlite.prepare('UPDATE zotero_active_connections SET permissions_json=?').run(JSON.stringify({library:true,write:true,files:true,notes:false}));
  f.transport.verifyCurrentKey=verify;
  const complete=await run(f,job.job_id,{metadataFirst:true});assert.equal(complete.state,'complete');assert.equal(complete.items[0].pdf_verified,true);
  assert.equal(f.transport.count('createItem'),creates);assert.equal(f.transport.count('patchItemMembership'),patches);
});
test('reviewed mode missing PDF leaves successful metadata; later verified blob completes without duplicate item',async()=>{
  const f=await setup({withPdf:true,paperCount:1}),job=await f.create({metadataFirst:true});
  const pending=await advanceExecutionJob({...f.base,jobId:job.job_id,metadataFirst:true,maxSteps:10,blobs:{readBlob:async()=>null}});
  assert.equal(pending.items[0].metadata_saved,true);assert.equal(pending.items[0].classification_saved,true);assert.equal(pending.items[0].pdf_verified,false);assert.equal(pending.items[0].error_code,'VERIFIED_BLOB_UNAVAILABLE');
  assert.equal(f.transport.count('createStoredAttachment'),0);const count=f.transport.count('createItem');
  f.tick(60001);assert.equal((await run(f,job.job_id,{metadataFirst:true})).state,'complete');assert.equal(f.transport.count('createItem'),count);
});
test('reviewed mode preserves connection fencing for an existing job after reconnect',async()=>{
  const f=await setup({withPdf:true,paperCount:1}),job=await f.create({metadataFirst:true});
  f.db.sqlite.prepare('UPDATE zotero_active_connections SET connection_version=2').run();
  await assert.rejects(advanceExecutionJob({...f.base,jobId:job.job_id,metadataFirst:true}),e=>e.code==='CONNECTION_STALE');
  assert.equal(f.transport.count('createItem'),0);
});

test('reviewed unsupported source without verified cache retains citation success and stops endless PDF retries',async()=>{
  const f=await setup({withPdf:true,paperCount:1}),job=await f.create({metadataFirst:true});let reads=0;
  const blobs={readBlob:async()=>{reads++;throw error('REVIEWED_PDF_SOURCE_UNAVAILABLE');}};
  const result=await advanceExecutionJob({...f.base,jobId:job.job_id,metadataFirst:true,maxSteps:10,blobs});
  assert.equal(result.state,'pdf_pending');assert.equal(result.items[0].metadata_saved,true);assert.equal(result.items[0].classification_saved,true);
  assert.equal(result.items[0].pdf_verified,false);assert.equal(result.items[0].error_code,'REVIEWED_PDF_SOURCE_UNAVAILABLE');assert.equal(result.items[0].retryable,false);assert.equal(result.retry_at,null);
  assert.equal(f.transport.count('createStoredAttachment'),0);const before=f.transport.calls.length;f.tick(3600000);
  await advanceExecutionJob({...f.base,jobId:job.job_id,metadataFirst:true,maxSteps:10,blobs});assert.equal(reads,1);assert.equal(f.transport.calls.length,before);
});
