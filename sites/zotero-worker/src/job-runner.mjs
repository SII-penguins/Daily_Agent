/** Durable, selected-only Zotero execution. No fetch/credential/deployment defaults.
 *
 * createExecutionJob({db, ownerId, request, connection, liveGate, now=Date.now})
 * advanceExecutionJob({db, ownerId, jobId, connection, liveGate, transportFactory,
 *   transport, blobs, hashBytes, now=Date.now, maxSteps=1, leaseMs=120000})
 * readExecutionJob({db, ownerId, jobId, connection?, liveGate?, now=Date.now})
 *
 * transportFactory({guardRequest,noteBackoff}) MUST wrap every actual fetch with guardRequest.
 * noteBackoff(milliseconds) persists server response delays, including on HTTP 200.
 * Direct transport injection exists for deterministic tests / already guarded adapters.
 * blobs.readBlob({ownerId,reportId,catalogId,paperId,sha256,size}) returns Uint8Array|null.
 * Only pinned private blob IDs are passed; arbitrary source URLs are never fetched.
 * Notes permission is never required. Inventory must prove its explicit non-note
 * scope; existing notes remain untouched by narrow collections/tags-only patches.
 *
 * Each advance visits at most one selected paper and maxSteps (1..10) durable stages.
 * Upload authorization/transfer/registration are one bounded stage because transport
 * tickets are opaque, ephemeral capabilities and must never be serialized into D1.
 * An in-flight remote write cannot be recalled. Its prewritten journal survives loss
 * of a lease/connection; a new worker reads back before doing anything else. A missing
 * object after an uncertain create stays blocked rather than silently replaying it.
 */
import {ImportDomainError, canonicalJSON, sha256Text, validPersonalLibrary, validZoteroKey,
  loadVerifiedCatalog, selectedPlan, allCollectionPaths, classification, matchPaper,
  requirePaperMatch, metadataFor, mergedMembership, requireAttachment, verifyPdfBytes} from './import-domain.mjs';

export class ExecutionError extends Error {
  constructor(code,status=409) { super(code); this.name='ExecutionError'; this.code=code; this.status=status; }
}
const fail=(code,status)=>{throw new ExecutionError(code,status);};
const statement=(db,sql,...args)=>db.prepare(sql).bind(...args);
const all=async(db,sql,...args)=>{
  const result=await statement(db,sql,...args).all();
  if(result.success===false||!Array.isArray(result.results)) fail('STORAGE_UNAVAILABLE',503);
  return result.results;
};
const first=(db,sql,...args)=>statement(db,sql,...args).first();
const iso=ms=>new Date(ms).toISOString();
const validId=value=>typeof value==='string'&&/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(value);
const randomKey=()=>Array.from(crypto.getRandomValues(new Uint8Array(8)),v=>'23456789ABCDEFGHIJKLMNPQRSTUVWXYZ'[v%32]).join('');
const timestamp=now=>{const value=typeof now==='function'?now():now;if(!Number.isSafeInteger(value)||value<0)fail('INVALID_CLOCK',503);return value;};
async function gate(value) { if ((typeof value==='function'?await value():value)!==true) fail('EXECUTION_DISABLED'); }
function connectionInput(ownerId,connection) {
  if(typeof ownerId!=='string'||!ownerId||ownerId.length>256||!connection||connection.owner_id!==ownerId||
     connection.library_type!=='user'||!validPersonalLibrary(connection.library_id)||
     !Number.isSafeInteger(connection.connection_version)||connection.connection_version<1||connection.state!=='active') fail('CONNECTION_REQUIRED');
}
function sameConnection(a,b) {
  return a&&b&&a.owner_id===b.owner_id&&a.library_type==='user'&&b.library_type==='user'&&
    a.library_id===b.library_id&&a.connection_version===b.connection_version&&a.state==='active'&&b.state==='active';
}
async function activeConnection(db,ownerId,connection,includePdf=false) {
  connectionInput(ownerId,connection);
  const row=await first(db,'SELECT * FROM zotero_active_connections WHERE owner_id = ?',ownerId);
  if(!sameConnection(row,connection)) fail('CONNECTION_STALE');
  let p;try{p=JSON.parse(row.permissions_json);}catch{fail('CONNECTION_UNVERIFIED');}
  if(p.library!==true||p.write!==true||includePdf&&p.files!==true) fail('CONNECTION_UNVERIFIED');
  return row;
}
async function loadCatalog(db,ownerId,reportId,catalogId,libraryId) {
  if(!validId(reportId)||!validId(catalogId))fail('INVALID_CATALOG_ID',400);
  const row=await first(db,'SELECT * FROM zotero_report_catalogs WHERE owner_id = ? AND report_id = ? AND catalog_id = ?',ownerId,reportId,catalogId);
  if(!row||row.enabled!==1)fail('CATALOG_NOT_FOUND',404);
  const papers=await all(db,'SELECT paper_id, display_title, metadata_json FROM zotero_catalog_papers WHERE owner_id = ? AND report_id = ? AND catalog_id = ? ORDER BY paper_id',ownerId,reportId,catalogId);
  return {row,catalog:await loadVerifiedCatalog(row,papers,libraryId)};
}
const ACTIVE_SQL=`EXISTS (SELECT 1 FROM zotero_active_connections a WHERE a.owner_id = ?
  AND a.library_type = 'user' AND a.library_id = ? AND a.connection_version = ? AND a.state = 'active')`;
const activeArgs=connection=>[connection.owner_id,connection.library_id,connection.connection_version];
const PIN_SQL=`EXISTS (SELECT 1 FROM zotero_report_catalogs c WHERE c.owner_id = ? AND c.report_id = ?
  AND c.catalog_id = ? AND c.catalog_version = ? AND c.content_sha256 = ? AND c.enabled = 1)`;
const pinArgs=job=>[job.owner_id,job.report_id,job.catalog_id,job.catalog_version,job.catalog_sha256];
const JOB_SQL='SELECT * FROM zotero_execution_jobs WHERE owner_id = ? AND job_id = ?';

async function createJob({db,ownerId,request,connection,liveGate,now=Date.now}={}) {
  await gate(liveGate);
  if(!db?.prepare||!db?.batch)fail('STORAGE_UNAVAILABLE',503);
  if(!request||typeof request!=='object')fail('INVALID_SAVE_REQUEST',400);
  await activeConnection(db,ownerId,connection,request.pdf_intent==='open_access_if_available');
  const {row,catalog}=await loadCatalog(db,ownerId,request.report_id,request.catalog_id,connection.library_id);
  const selected=selectedPlan(catalog,request);
  if(request.catalog_version!==row.catalog_version)fail('CATALOG_VERSION_MISMATCH');
  const identity={schema:1,owner_id:ownerId,library_type:'user',library_id:connection.library_id,
    connection_version:connection.connection_version,report_id:row.report_id,catalog_id:row.catalog_id,
    catalog_version:row.catalog_version,catalog_sha256:row.content_sha256,
    paper_ids:selected.map(p=>p.paper_id),pdf_intent:request.pdf_intent};
  const jobId=`ze_${await sha256Text(canonicalJSON(identity))}`,time=iso(timestamp(now));
  const job={...identity,job_id:jobId};
  // All reserved object IDs and collection paths commit in the same transaction as
  // the new execution job. Browser intents are neither read nor modified here.
  const statements=[statement(db,`INSERT INTO zotero_execution_jobs
    (owner_id,job_id,library_type,library_id,connection_version,report_id,catalog_id,catalog_version,
     catalog_sha256,paper_ids_json,pdf_intent,state,created_at,updated_at)
    SELECT ?,?,'user',?,?,?,?,?,?,?,?,'queued',?,? WHERE ${ACTIVE_SQL} AND ${PIN_SQL}
    ON CONFLICT(owner_id,job_id) DO NOTHING`,ownerId,jobId,connection.library_id,connection.connection_version,
    row.report_id,row.catalog_id,row.catalog_version,row.content_sha256,canonicalJSON(identity.paper_ids),request.pdf_intent,time,time,
    ...activeArgs(connection),...pinArgs(job))];
  const insertedJob=`EXISTS (SELECT 1 FROM zotero_execution_jobs j WHERE j.owner_id = ? AND j.job_id = ?)`;
  selected.forEach((paper,ordinal)=>statements.push(statement(db,`INSERT INTO zotero_execution_items
    (owner_id,job_id,paper_id,ordinal,reserved_key,attachment_reserved_key,updated_at)
    SELECT ?,?,?,?,?,?,? WHERE ${insertedJob} AND ${ACTIVE_SQL}
    ON CONFLICT(owner_id,job_id,paper_id) DO NOTHING`,ownerId,jobId,paper.paper_id,ordinal,randomKey(),randomKey(),time,
    ownerId,jobId,...activeArgs(connection))));
  for(const path of allCollectionPaths(selected,catalog.day))statements.push(statement(db,`INSERT INTO zotero_execution_collections
    (owner_id,job_id,path_json,depth,reserved_key) SELECT ?,?,?,?,? WHERE ${insertedJob} AND ${ACTIVE_SQL}
    ON CONFLICT(owner_id,job_id,path_json) DO NOTHING`,ownerId,jobId,canonicalJSON(path),path.length,randomKey(),ownerId,jobId,...activeArgs(connection)));
  await gate(liveGate);
  const results=await db.batch(statements);
  if(results.some(r=>r.success===false))fail('STORAGE_UNAVAILABLE',503);
  if(!await first(db,JOB_SQL,ownerId,jobId))fail('CONNECTION_OR_CATALOG_CHANGED');
  const result=await readExecutionJob({db,ownerId,jobId,connection,liveGate,now});
  return {...result,created:results[0].meta?.changes===1};
}
function publicItem(row) {
  return {paper_id:row.paper_id,state:row.status,metadata_saved:Boolean(row.metadata_saved),
    classification_saved:Boolean(row.classification_saved),pdf_verified:Boolean(row.pdf_verified),
    existing_item:Boolean(row.existing_item),error_code:row.error_code||null,retryable:Boolean(row.retryable),
    uncertain:Boolean(row.uncertain),retry_at:row.retry_at===null?null:iso(row.retry_at)};
}
function aggregate(items,time) {
  const success=items.filter(i=>['complete','already_exists'].includes(i.status));
  if(success.length===items.length&&items.length)return {state:'complete',retry_at:null};
  const remaining=items.filter(i=>!['complete','already_exists'].includes(i.status));
  const ready=remaining.filter(i=>(i.retryable||!i.error_code)&&(!i.retry_at||i.retry_at<=time));
  if(ready.length)return {state:items.every(i=>i.status==='queued')?'queued':'running',retry_at:null};
  const next=remaining.filter(i=>i.retryable&&i.retry_at>time).map(i=>i.retry_at);
  if(next.length)return {state:'retry_wait',retry_at:iso(Math.min(...next))};
  if(remaining.some(i=>i.status==='pdf_pending'))return {state:'pdf_pending',retry_at:null};
  return {state:success.length?'partial_failure':'failure',retry_at:null};
}
export async function readExecutionJob({db,ownerId,jobId,connection,liveGate,now=Date.now}={}) {
  if(typeof jobId!=='string'||!/^ze_[a-f0-9]{64}$/.test(jobId))fail('EXECUTION_NOT_FOUND',404);
  const job=await first(db,JOB_SQL,ownerId,jobId);if(!job)fail('EXECUTION_NOT_FOUND',404);
  const items=await all(db,'SELECT * FROM zotero_execution_items WHERE owner_id = ? AND job_id = ? ORDER BY ordinal',ownerId,jobId);
  let state=aggregate(items,timestamp(now));
  const throttle=await first(db,'SELECT not_before FROM zotero_execution_leases WHERE library_id = ?',job.library_id);
  if(throttle?.not_before>timestamp(now)&&['queued','running','retry_wait'].includes(state.state))state={state:'retry_wait',
    retry_at:iso(Math.max(throttle.not_before,state.retry_at?Date.parse(state.retry_at):0))};
  if(connection!==undefined||liveGate!==undefined) {
    let enabled=false;try{await gate(liveGate);enabled=true;}catch{/* Read stays read-only. */}
    const current=await first(db,'SELECT * FROM zotero_active_connections WHERE owner_id = ?',ownerId);
    if(!enabled||!sameConnection(current,connection)||job.library_id!==connection?.library_id||
       job.connection_version!==connection?.connection_version)state={state:'connection_required',retry_at:null};
  }
  return {job_id:jobId,report_id:job.report_id,catalog_id:job.catalog_id,catalog_version:job.catalog_version,
    paper_ids:JSON.parse(job.paper_ids_json),pdf_intent:job.pdf_intent,...state,items:items.map(publicItem),
    created_at:job.created_at,updated_at:job.updated_at};
}
async function claimLibrary(context) {
  const {db,job,connection,now,leaseMs}=context,time=timestamp(now),token=crypto.randomUUID();
  const results=await db.batch([
    statement(db,`INSERT INTO zotero_execution_leases (library_id,owner_id) VALUES (?,?) ON CONFLICT(library_id) DO NOTHING`,job.library_id,job.owner_id),
    statement(db,`UPDATE zotero_execution_leases SET owner_id = ?, job_id = ?, token = ?, generation = generation + 1, expires_at = ?
      WHERE library_id = ? AND expires_at <= ? AND ${ACTIVE_SQL} AND ${PIN_SQL}`,
      job.owner_id,job.job_id,token,time+leaseMs,job.library_id,time,...activeArgs(connection),...pinArgs(job)),
  ]);
  if(results[1].meta?.changes!==1)fail('EXECUTION_BUSY');
  const lease=await first(db,'SELECT * FROM zotero_execution_leases WHERE library_id = ? AND token = ?',job.library_id,token);
  if(!lease)fail('LEASE_LOST');
  context.claim={token,generation:lease.generation};
}
function fence(context) {
  const {job,claim,connection,now}=context;
  return {sql:`EXISTS (SELECT 1 FROM zotero_execution_leases l WHERE l.library_id = ? AND l.owner_id = ?
    AND l.job_id = ? AND l.token = ? AND l.generation = ? AND l.expires_at > ?) AND ${ACTIVE_SQL} AND ${PIN_SQL}`,
    args:[job.library_id,job.owner_id,job.job_id,claim.token,claim.generation,timestamp(now),...activeArgs(connection),...pinArgs(job)]};
}
async function guard(context) {
  const {db,job,claim,connection,now,leaseMs,liveGate}=context;
  await gate(liveGate);
  const time=timestamp(now);
  const update=await statement(db,`UPDATE zotero_execution_leases SET expires_at = ? WHERE library_id = ?
    AND owner_id = ? AND job_id = ? AND token = ? AND generation = ? AND expires_at > ? AND ${ACTIVE_SQL} AND ${PIN_SQL}`,
    time+leaseMs,job.library_id,job.owner_id,job.job_id,claim.token,claim.generation,time,
    ...activeArgs(connection),...pinArgs(job)).run();
  if(update.meta?.changes!==1)fail('LEASE_OR_CONNECTION_LOST');
  // Permission downgrades at the same connection version must also fail closed.
  await activeConnection(db,job.owner_id,connection,job.pdf_intent==='open_access_if_available');
  const cooldown=await first(db,'SELECT not_before FROM zotero_execution_leases WHERE library_id = ?',job.library_id);
  if(cooldown?.not_before>timestamp(now)){const error=new ExecutionError('LIBRARY_BACKOFF_ACTIVE');error.retryAfterMs=cooldown.not_before-timestamp(now);throw error;}
}
async function noteBackoff(context,milliseconds) {
  if(!Number.isSafeInteger(milliseconds)||milliseconds<0||milliseconds>Number.MAX_SAFE_INTEGER-timestamp(context.now))fail('INVALID_BACKOFF',503);
  if(!milliseconds)return;
  const f=fence(context);
  const result=await statement(context.db,`UPDATE zotero_execution_leases SET not_before = MAX(not_before,?)
    WHERE library_id = ? AND ${f.sql}`,timestamp(context.now)+milliseconds,context.job.library_id,...f.args).run();
  if(result.meta?.changes!==1)fail('LEASE_OR_CONNECTION_LOST');
}
const ITEM_FIELDS=new Set(['item_key','attachment_key','status','metadata_saved','classification_saved','existing_item',
  'pdf_verified','pdf_proof_json','pending_operation','pending_key','uncertain','error_code','retryable','retry_at']);
async function saveItem(context,changes) {
  const {db,job}=context;
  const keys=Object.keys(changes);if(!keys.length||keys.some(k=>!ITEM_FIELDS.has(k)))fail('INVALID_LEDGER_UPDATE',503);
  await gate(context.liveGate);
  const f=fence(context),at=iso(timestamp(context.now));
  const result=await statement(db,`UPDATE zotero_execution_items SET ${keys.map(k=>`${k} = ?`).join(',')}, updated_at = ?
    WHERE owner_id = ? AND job_id = ? AND paper_id = ? AND ${f.sql}`,
    ...keys.map(k=>changes[k]),at,job.owner_id,job.job_id,context.item.paper_id,...f.args).run();
  if(result.meta?.changes!==1)fail('LEASE_OR_CONNECTION_LOST');
  context.item={...context.item,...changes,updated_at:at};
}
async function saveCollection(context,row,changes) {
  const keys=Object.keys(changes);
  if(keys.some(k=>!['collection_key','pending_operation','uncertain'].includes(k)))fail('INVALID_LEDGER_UPDATE',503);
  await gate(context.liveGate);const f=fence(context);
  const result=await statement(context.db,`UPDATE zotero_execution_collections SET ${keys.map(k=>`${k} = ?`).join(',')}
    WHERE owner_id = ? AND job_id = ? AND path_json = ? AND ${f.sql}`,
    ...keys.map(k=>changes[k]),context.job.owner_id,context.job.job_id,row.path_json,...f.args).run();
  if(result.meta?.changes!==1)fail('LEASE_OR_CONNECTION_LOST');Object.assign(row,changes);
}
async function remote(context,method,input) {
  await guard(context);
  if(typeof context.transport?.[method]!=='function')fail('TRANSPORT_ADAPTER_REQUIRED',503);
  try{return await context.transport[method](input);}
  catch(error){context.failedWrite=new Set(['createItem','createCollection','patchItemMembership','createStoredAttachment',
    'authorizePdfUpload','transferPdfUpload','registerPdfUpload']).has(method);throw error;}
}
async function inventory(context) {
  const result=await remote(context,'inventory');
  if(!result||result.complete!==true||result.includesTrashed!==true||result.excludesNotes!==true||
      !Array.isArray(result.items)||!Array.isArray(result.collections))throw new ImportDomainError('INCOMPLETE_INVENTORY');
  // Completeness is relative to the transport's explicitly filtered non-note
  // inventory, including trash. Do not request notes to prove their preservation.
  if(result.items.some(item=>typeof item?.data?.itemType!=='string'||
      ['note','annotation'].includes(item.data.itemType)))throw new ImportDomainError('INVENTORY_SCOPE_MISMATCH');
  return result;
}
const clear={pending_operation:null,pending_key:null,uncertain:0,error_code:null,retryable:0,retry_at:null};
async function beginWrite(context,operation,key) {
  await guard(context);
  await checkOtherUncertainWrites(context,operation,key);
  await saveItem(context,{pending_operation:operation,pending_key:key,uncertain:1,error_code:null,retryable:0,retry_at:null});
}
async function checkOtherUncertainWrites(context,operation,key) {
  const rows=await all(context.db,`SELECT i.pending_operation,i.pending_key,c.path_json AS collection_path FROM zotero_execution_items i
    JOIN zotero_execution_jobs j ON j.owner_id=i.owner_id AND j.job_id=i.job_id
    LEFT JOIN zotero_execution_collections c ON c.owner_id=i.owner_id AND c.job_id=i.job_id AND c.reserved_key=i.pending_key
    WHERE j.library_id = ? AND i.pending_operation IS NOT NULL AND i.uncertain = 1
      AND NOT (i.owner_id = ? AND i.job_id = ? AND i.paper_id = ?)`,
    context.job.library_id,context.job.owner_id,context.job.job_id,context.item.paper_id);
  for(const row of rows) {
    if(!validZoteroKey(row.pending_key))throw new ImportDomainError('LIBRARY_UNCERTAIN_WRITE');
    if(row.pending_operation==='create_collection') {
      const found=await remote(context,'getCollection',row.pending_key);
      if(!found)throw new ImportDomainError('LIBRARY_UNCERTAIN_WRITE');
      if(operation==='create_collection'){
        const planned=await first(context.db,'SELECT path_json FROM zotero_execution_collections WHERE owner_id = ? AND job_id = ? AND reserved_key = ?',context.job.owner_id,context.job.job_id,key);
        if(planned?.path_json===row.collection_path)throw new ExecutionError('INVENTORY_CHANGED');
      }
    } else if(['create_item','create_attachment'].includes(row.pending_operation)) {
      const found=await remote(context,'getItem',row.pending_key);
      if(!found)throw new ImportDomainError('LIBRARY_UNCERTAIN_WRITE');
      if(operation==='create_item'&&row.pending_operation==='create_item'&&matchPaper(context.paper,[found]))throw new ExecutionError('INVENTORY_CHANGED');
      if(operation==='create_attachment'&&row.pending_operation==='create_attachment'&&found.data?.parentItem===context.item.item_key&&
         found.data.filename===`${context.paper.pdf.sha256.slice(0,16)}.pdf`)throw new ExecutionError('INVENTORY_CHANGED');
    } else if(['authorize_pdf','transfer_pdf','register_pdf'].includes(row.pending_operation)&&row.pending_key===key&&
        ['authorize_pdf','transfer_pdf','register_pdf'].includes(operation)) {
      const existing=await remote(context,'getItem',key);
      if(!/^[a-f0-9]{32}$/.test(existing?.data?.md5||''))throw new ImportDomainError('LIBRARY_UNCERTAIN_WRITE');
    }
  }
}
async function metadataStep(context) {
  const {paper}=context;
  const index=await inventory(context),owned=await remote(context,'getItem',context.item.reserved_key);
  const existing=matchPaper(paper,index.items);
  let item,already=false;
  if(context.item.item_key) {
    item=requirePaperMatch(paper,await remote(context,'getItem',context.item.item_key));
    if(existing&&existing.key!==item.key)throw new ImportDomainError('IDENTITY_CHANGED');
    already=Boolean(context.item.existing_item);
  } else if(owned) {
    item=requirePaperMatch(paper,owned);
    if(existing&&existing.key!==item.key)throw new ImportDomainError('IDENTITY_CHANGED');
  } else if(existing) {item=requirePaperMatch(paper,await remote(context,'getItem',existing.key));already=true;}
  else {
    if(context.item.pending_operation==='create_item')throw new ImportDomainError('UNCERTAIN_CREATE_NOT_FOUND');
    await beginWrite(context,'create_item',context.item.reserved_key);
    item=await remote(context,'createItem',{key:context.item.reserved_key,data:{...metadataFor(paper),collections:[],tags:[],relations:{}}});
    requirePaperMatch(paper,item);
    if(item.key!==context.item.reserved_key)throw new ImportDomainError('RESERVED_KEY_MISMATCH');
  }
  await saveItem(context,{...clear,item_key:item.key,existing_item:already?1:0,status:'metadata_saved',metadata_saved:1});
}
async function collectionRows(context) {
  return all(context.db,'SELECT * FROM zotero_execution_collections WHERE owner_id = ? AND job_id = ? ORDER BY depth, path_json',context.job.owner_id,context.job.job_id);
}
function matchingCollection(object,key,name,parent) {
  if(!object||object.key!==key||object.data?.name!==name||(object.data.parentCollection||false)!==parent||object.data.deleted)throw new ImportDomainError('COLLECTION_IDENTITY_CHANGED');
  return object;
}
async function classificationStep(context) {
  const paths=classification(context.paper,context.catalog.day).paths;
  const rows=await collectionRows(context),needed=new Set();
  for(const path of paths)for(let n=1;n<=path.length;n++)needed.add(canonicalJSON(path.slice(0,n)));
  const missing=rows.find(row=>needed.has(row.path_json)&&!row.collection_key);
  if(missing) {
    const path=JSON.parse(missing.path_json),name=path.at(-1);
    const parent=path.length===1?false:rows.find(r=>r.path_json===canonicalJSON(path.slice(0,-1)))?.collection_key;
    if(parent!==false&&!validZoteroKey(parent))throw new ImportDomainError('COLLECTION_PARENT_MISSING');
    if(parent)matchingCollection(await remote(context,'getCollection',parent),parent,path.at(-2),path.length===2?false:
      rows.find(r=>r.path_json===canonicalJSON(path.slice(0,-2)))?.collection_key);
    const index=await inventory(context);
    const matches=index.collections.filter(c=>c.data?.name===name&&(c.data.parentCollection||false)===parent);
    if(matches.length>1)throw new ImportDomainError('AMBIGUOUS_COLLECTION');
    const reserved=await remote(context,'getCollection',missing.reserved_key);
    let collection;
    if(reserved) {
      collection=matchingCollection(reserved,missing.reserved_key,name,parent);
      if(matches[0]&&matches[0].key!==reserved.key)throw new ImportDomainError('AMBIGUOUS_COLLECTION');
    } else if(matches[0])collection=matchingCollection(await remote(context,'getCollection',matches[0].key),matches[0].key,name,parent);
    else {
      if(missing.pending_operation)throw new ImportDomainError('UNCERTAIN_COLLECTION_NOT_FOUND');
      await beginWrite(context,'create_collection',missing.reserved_key);
      await saveCollection(context,missing,{pending_operation:'create_collection',uncertain:1});
      collection=await remote(context,'createCollection',{key:missing.reserved_key,name,parentCollection:parent});
      matchingCollection(collection,missing.reserved_key,name,parent);
    }
    await saveCollection(context,missing,{collection_key:collection.key,pending_operation:null,uncertain:0});
    await saveItem(context,{...clear,status:'metadata_saved'});
    return;
  }
  const keys=paths.map(path=>rows.find(r=>r.path_json===canonicalJSON(path))?.collection_key);
  if(keys.some(k=>!validZoteroKey(k)))throw new ImportDomainError('COLLECTION_PLAN_MISSING');
  // Recheck every managed collection path before attaching membership. Never rename
  // collections, replace user tags, or write any note/body/metadata field.
  for(const row of rows.filter(r=>needed.has(r.path_json))) {
    const path=JSON.parse(row.path_json),parent=path.length===1?false:rows.find(r=>r.path_json===canonicalJSON(path.slice(0,-1)))?.collection_key;
    matchingCollection(await remote(context,'getCollection',row.collection_key),row.collection_key,path.at(-1),parent);
  }
  let item=requirePaperMatch(context.paper,await remote(context,'getItem',context.item.item_key));
  const merged=mergedMembership(item,keys,classification(context.paper,context.catalog.day).tags);
  if(merged.changed) {
    await beginWrite(context,'patch_membership',item.key);
    item=await remote(context,'patchItemMembership',{itemKey:item.key,expectedVersion:item.version,collections:merged.collections,tags:merged.tags});
    requirePaperMatch(context.paper,item);
    if(mergedMembership(item,keys,classification(context.paper,context.catalog.day).tags).changed)throw new ImportDomainError('MEMBERSHIP_READBACK_MISMATCH');
  }
  await saveItem(context,{...clear,classification_saved:1,status:context.job.pdf_intent==='metadata_only'?
    (context.item.existing_item?'already_exists':'complete'):'pdf_pending'});
}
async function privatePdf(context) {
  if(typeof context.blobs?.readBlob!=='function')return null;
  await guard(context);
  let bytes;
  try{bytes=await context.blobs.readBlob({ownerId:context.job.owner_id,reportId:context.job.report_id,
    catalogId:context.job.catalog_id,paperId:context.paper.paper_id,sha256:context.paper.pdf.sha256,size:context.paper.pdf.size});}
  catch{return null;}
  if(bytes===null||bytes===undefined)return null;
  return verifyPdfBytes(context.paper.pdf,bytes,context.hashBytes);
}
function proofFor(context,attachment,hashes) {
  const pdf=context.paper.pdf;
  return {itemKey:attachment.key,parentItem:context.item.item_key,filename:`${pdf.sha256.slice(0,16)}.pdf`,
    mtime:hashes?.mtime??Number(attachment.data.mtime),size:pdf.size,md5:hashes?.md5??attachment.data.md5,sha256:pdf.sha256};
}
function validProof(context,proof) {
  return proof&&proof.itemKey===context.item.attachment_key&&proof.parentItem===context.item.item_key&&
    proof.filename===`${context.paper.pdf.sha256.slice(0,16)}.pdf`&&Number.isSafeInteger(proof.mtime)&&proof.mtime>=0&&
    proof.size===context.paper.pdf.size&&proof.sha256===context.paper.pdf.sha256&&/^[a-f0-9]{32}$/.test(proof.md5);
}
async function pdfStep(context) {
  const {paper}=context;
  if(!paper.pdf) {await saveItem(context,{...clear,status:'pdf_pending',error_code:'VERIFIED_PDF_UNAVAILABLE'});return;}
  requirePaperMatch(paper,await remote(context,'getItem',context.item.item_key));
  const filename=`${paper.pdf.sha256.slice(0,16)}.pdf`;
  let key=context.item.attachment_key||context.item.attachment_reserved_key;
  let attachment=await remote(context,'getItem',key);
  if(!context.item.attachment_key) {
    const index=await inventory(context),matches=index.items.filter(item=>item.data?.itemType==='attachment'&&item.data.parentItem===context.item.item_key&&item.data.filename===filename);
    if(matches.length>1)throw new ImportDomainError('AMBIGUOUS_ATTACHMENT');
    if(attachment&&matches[0]&&matches[0].key!==key)throw new ImportDomainError('AMBIGUOUS_ATTACHMENT');
    if(!attachment&&matches[0]){key=matches[0].key;attachment=await remote(context,'getItem',key);}
  }
  if(attachment)requireAttachment(attachment,{key,parentItem:context.item.item_key,filename});
  if(!attachment) {
    if(context.item.attachment_key)throw new ImportDomainError('ATTACHMENT_MISSING');
    if(context.item.pending_operation==='create_attachment')throw new ImportDomainError('UNCERTAIN_ATTACHMENT_NOT_FOUND');
    const content=await privatePdf(context);
    if(!content) {await saveItem(context,{...clear,status:'pdf_pending',error_code:'VERIFIED_BLOB_UNAVAILABLE',retryable:1,retry_at:timestamp(context.now)+60000});return;}
    const proof=proofFor(context,{key,data:{}},{md5:content.md5,mtime:timestamp(context.now)});
    await saveItem(context,{pdf_proof_json:canonicalJSON(proof)});
    await beginWrite(context,'create_attachment',key);
    attachment=await remote(context,'createStoredAttachment',{key,parentItem:context.item.item_key,title:'Reviewed source PDF',filename,tags:[]});
    requireAttachment(attachment,{key,parentItem:context.item.item_key,filename});
    await saveItem(context,{...clear,attachment_key:key,status:'pdf_pending'});
    return;
  }
  if(!context.item.attachment_key)await saveItem(context,{attachment_key:key});
  // A registered attachment is reconciled before requiring a still-available source
  // blob. Expected SHA256 and size remain pinned to the reviewed catalog.
  if(/^[a-f0-9]{32}$/.test(attachment.data.md5||'')&&Number.isSafeInteger(Number(attachment.data.mtime))&&Number(attachment.data.mtime)>=0) {
    const proof=proofFor(context,attachment);
    if(!validProof(context,proof))throw new ImportDomainError('INVALID_PDF_PROOF');
    await saveItem(context,{pdf_proof_json:canonicalJSON(proof)});
    const verified=await remote(context,'verifyStoredPdf',proof);
    if(verified?.storedBytesVerified!==true||verified.sha256!==proof.sha256||verified.md5!==proof.md5||
      verified.size!==proof.size||verified.itemKey!==key||verified.parentItem!==context.item.item_key)throw new ImportDomainError('PDF_READBACK_MISMATCH');
    requirePaperMatch(paper,await remote(context,'getItem',context.item.item_key));
    await saveItem(context,{...clear,pdf_verified:1,status:context.item.existing_item?'already_exists':'complete'});
    return;
  }
  if(context.item.pending_operation&&['authorize_pdf','transfer_pdf','register_pdf'].includes(context.item.pending_operation))throw new ImportDomainError('UNCERTAIN_PDF_NOT_REGISTERED');
  const content=await privatePdf(context);
  if(!content){await saveItem(context,{...clear,status:'pdf_pending',error_code:'VERIFIED_BLOB_UNAVAILABLE',retryable:1,retry_at:timestamp(context.now)+60000});return;}
  let proof;try{proof=JSON.parse(context.item.pdf_proof_json);}catch{/* Derive fresh pinned proof below. */}
  if(!validProof(context,proof))proof=proofFor(context,attachment,{md5:content.md5,mtime:timestamp(context.now)});
  if(proof.md5!==content.md5)throw new ImportDomainError('PDF_DIGEST_MISMATCH');
  await saveItem(context,{pdf_proof_json:canonicalJSON(proof)});
  requirePaperMatch(paper,await remote(context,'getItem',context.item.item_key));
  await beginWrite(context,'authorize_pdf',key);
  const authorization=await remote(context,'authorizePdfUpload',{itemKey:key,parentItem:context.item.item_key,
    bytes:content.bytes,filename,mtime:proof.mtime,previousMd5:null});
  if(authorization.sha256!==proof.sha256||authorization.md5!==proof.md5||authorization.size!==proof.size||authorization.itemKey!==key)throw new ImportDomainError('UPLOAD_AUTHORIZATION_MISMATCH');
  if(!authorization.exists) {
    await beginWrite(context,'transfer_pdf',key);
    await remote(context,'transferPdfUpload',authorization.ticket);
    await beginWrite(context,'register_pdf',key);
    await remote(context,'registerPdfUpload',authorization.ticket);
  }
  // Registration is not success. A subsequent guarded step verifies remote bytes.
  await saveItem(context,{...clear,status:'pdf_pending',pending_operation:'verify_pdf'});
}
const RETRY_CODES=new Set(['WRITE_CONFLICT','CREATE_ALREADY_EXISTS','LIBRARY_LOCKED','RATE_LIMITED','BACKOFF_ACTIVE',
  'BACKOFF_REQUIRED','LIBRARY_BACKOFF_ACTIVE','SERVICE_UNAVAILABLE','STORAGE_UNAVAILABLE','NETWORK_FAILURE','REQUEST_TIMEOUT','RESPONSE_READ_FAILED',
  'INVENTORY_CHANGED','PERMISSIONS_UNVERIFIED','TRANSPORT_FAILURE']);
const SAFE_CODES=new Set([...RETRY_CODES,'AUTHORIZATION_DENIED','INSUFFICIENT_PERMISSION','OBJECT_WRITE_FAILED',
  'QUOTA_OR_SIZE_EXCEEDED','STORAGE_FORBIDDEN','STORAGE_REJECTED','PRECONDITION_REQUIRED','NOT_FOUND',
  'REQUEST_REJECTED','INVALID_WRITE_RESPONSE','WRITE_RESULT_MISMATCH','PATCH_READBACK_MISMATCH','PATCH_READBACK_FAILED',
  'STORED_METADATA_MISMATCH','STORED_BYTES_MISMATCH','STORED_ETAG_MISMATCH','STORED_SIZE_MISMATCH','STORED_METADATA_CHANGED',
  'INVALID_UPLOAD_AUTHORIZATION','UNSAFE_REDIRECT','STORAGE_REQUEST_REJECTED']);
async function recordFailure(context,error) {
  if(error instanceof ExecutionError&&['LEASE_LOST','LEASE_OR_CONNECTION_LOST','CONNECTION_STALE','CONNECTION_UNVERIFIED','EXECUTION_DISABLED'].includes(error.code))throw error;
  const domain=error instanceof ImportDomainError;
  const code=domain?error.code:(SAFE_CODES.has(error?.code)?error.code:'EXECUTION_STEP_FAILED');
  const pending=Boolean(context.item.pending_operation);
  // A failed reconciliation READ cannot erase a previous ambiguous WRITE. Only a
  // definitive rejection of the newly attempted write, or successful readback, can.
  const uncertain=Boolean(error?.uncertain||pending&&(context.item.uncertain&&!context.failedWrite||!error?.code||domain));
  const reconciliationOnly=['UNCERTAIN_CREATE_NOT_FOUND','UNCERTAIN_COLLECTION_NOT_FOUND','UNCERTAIN_ATTACHMENT_NOT_FOUND',
    'UNCERTAIN_PDF_NOT_REGISTERED','LIBRARY_UNCERTAIN_WRITE'].includes(code);
  const retry=RETRY_CODES.has(code)||uncertain&&!domain||reconciliationOnly;
  const delay=Number.isSafeInteger(error?.retryAfterMs)&&error.retryAfterMs>=0?Math.max(1000,error.retryAfterMs):(reconciliationOnly?30000:1000);
  if(Number.isSafeInteger(error?.retryAfterMs)&&error.retryAfterMs>0)await noteBackoff(context,error.retryAfterMs);
  // Definitive rejected writes (429/412/etc.) can safely be attempted later after
  // a new inventory/readback. Unknown outcomes retain their reserved-key journal.
  const clearPending=pending&&!uncertain;
  if(clearPending&&context.item.pending_operation==='create_collection') {
    const rows=await collectionRows(context),row=rows.find(r=>r.reserved_key===context.item.pending_key);
    if(row)await saveCollection(context,row,{pending_operation:null,uncertain:0});
  }
  await saveItem(context,{status:context.item.classification_saved&&context.job.pdf_intent!=='metadata_only'?'pdf_pending':'failure',
    error_code:code,retryable:retry?1:0,retry_at:retry?timestamp(context.now)+delay:null,uncertain:uncertain?1:0,
    ...(clearPending?{pending_operation:null,pending_key:null}:{})});
}

async function advanceJob({db,ownerId,jobId,connection,liveGate,transportFactory,transport,
  blobs,hashBytes,now=Date.now,maxSteps=1,leaseMs=120000}={}) {
  await gate(liveGate);
  if(!Number.isSafeInteger(maxSteps)||maxSteps<1||maxSteps>10||!Number.isSafeInteger(leaseMs)||leaseMs<1000||leaseMs>300000)fail('INVALID_EXECUTION_LIMIT',400);
  const job=await first(db,JOB_SQL,ownerId,jobId);if(!job)fail('EXECUTION_NOT_FOUND',404);
  await activeConnection(db,ownerId,connection,job.pdf_intent==='open_access_if_available');
  if(job.library_id!==connection.library_id||job.connection_version!==connection.connection_version)fail('CONNECTION_STALE');
  const {row,catalog}=await loadCatalog(db,ownerId,job.report_id,job.catalog_id,job.library_id);
  if(row.catalog_version!==job.catalog_version||row.content_sha256!==job.catalog_sha256)fail('CATALOG_CHANGED');
  const cooldown=await first(db,'SELECT not_before FROM zotero_execution_leases WHERE library_id = ?',job.library_id);
  if(cooldown?.not_before>timestamp(now))return readExecutionJob({db,ownerId,jobId,connection,liveGate,now});
  const context={db,job,connection,liveGate,now,leaseMs,catalog,blobs,hashBytes,failedWrite:false};
  await claimLibrary(context);
  try {
    const items=await all(db,'SELECT * FROM zotero_execution_items WHERE owner_id = ? AND job_id = ? ORDER BY ordinal',ownerId,jobId),time=timestamp(now);
    context.item=items.find(item=>!['complete','already_exists'].includes(item.status)&&
      (!item.error_code||item.retryable)&&(!item.retry_at||item.retry_at<=time));
    if(!context.item)return await readExecutionJob({db,ownerId,jobId,connection,liveGate,now});
    context.paper=catalog.papers.find(p=>p.paper_id===context.item.paper_id);
    if(!context.paper)fail('CATALOG_CHANGED');
    try {
      context.transport=transportFactory?await transportFactory({guardRequest:()=>guard(context),noteBackoff:ms=>noteBackoff(context,ms)}):transport;
      const permissions=await remote(context,'verifyCurrentKey',{requireWrite:true,requireNotes:false,requireFiles:job.pdf_intent!=='metadata_only'});
      if(String(permissions?.userID)!==job.library_id||permissions.library!==true||permissions.write!==true||
         job.pdf_intent!=='metadata_only'&&permissions.files!==true)throw new ImportDomainError('KEY_OWNER_OR_PERMISSIONS_CHANGED');
      for(let step=0;step<maxSteps;step++) {
        await guard(context);
        if(!context.item.metadata_saved)await metadataStep(context);
        else if(!context.item.classification_saved)await classificationStep(context);
        else if(job.pdf_intent!=='metadata_only'&&!context.item.pdf_verified)await pdfStep(context);
        else break;
        if(['complete','already_exists'].includes(context.item.status)||context.item.error_code)break;
      }
    } catch(error) {await recordFailure(context,error);}
    const current=await all(db,'SELECT * FROM zotero_execution_items WHERE owner_id = ? AND job_id = ? ORDER BY ordinal',ownerId,jobId);
    const f=fence(context),status=aggregate(current,timestamp(now));
    const saved=await statement(db,`UPDATE zotero_execution_jobs SET state = ?, updated_at = ? WHERE owner_id = ? AND job_id = ? AND ${f.sql}`,
      status.state,iso(timestamp(now)),ownerId,jobId,...f.args).run();
    if(saved.meta?.changes!==1)fail('LEASE_OR_CONNECTION_LOST');
    return await readExecutionJob({db,ownerId,jobId,connection,liveGate,now});
  } finally {
    // Releasing can never release a newer worker's lease or resurrect a connection.
    await statement(db,`UPDATE zotero_execution_leases SET expires_at = 0, token = NULL WHERE library_id = ? AND token = ? AND generation = ?`,
      job.library_id,context.claim.token,context.claim.generation).run();
  }
}

function publicError(error) {
  if(error instanceof ImportDomainError)return new ExecutionError(error.code,
    /^(?:INVALID_|UNEXPECTED_|SELECTION_|INSUFFICIENT_IDENTITY)/.test(error.code)?400:409);
  return error;
}
export async function createExecutionJob(options) {try{return await createJob(options);}catch(error){throw publicError(error);}}
export async function advanceExecutionJob(options) {try{return await advanceJob(options);}catch(error){throw publicError(error);}}
