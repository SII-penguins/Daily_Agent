/** Owner-authenticated, same-origin report selection. Only Save admits metadata;
 * only its guarded execution steps contact Zotero or fetch optional PDFs. */
import {validateReviewedRegistry,validateReviewedIssue} from './reviewed-issues.mjs';
import {registerTrustedCatalog,readRegisteredCatalog,createPrivateR2BlobStore} from './catalog-registration.mjs';
import {createExecutionJob,advanceExecutionJob,readExecutionJob} from './job-runner.mjs';
import {hashBytes,sha256Bytes} from './hash-bytes.mjs';
import {fetchReviewedPdf} from './reviewed-pdf.mjs';
import {renderReviewedPage,reviewedClientSource} from './reviewed-ui.mjs';
const sql=(db,q,...args)=>db.prepare(q).bind(...args);
const exact=(v,keys)=>v && typeof v==='object' && !Array.isArray(v) && Object.keys(v).sort().join(',')===[...keys].sort().join(',');
async function originalBytes(env,auth,issue) {
  const original=new URL('/'+issue.entry_path,auth.origin);
  const assetRequest=url=>new Request(url,{redirect:'manual',credentials:'omit'});
  let response=await env.ASSETS.fetch(assetRequest(original.href));
  if([301,302,303,307,308].includes(response.status)){
    const canonical=new URL('./',original),location=response.headers.get('Location');
    // Only the ASSETS index.html -> exact same directory slash canonicalization.
    // No arbitrary relative URL resolution, caller headers, credentials or second hop.
    if(!original.pathname.endsWith('/index.html') || ![canonical.pathname,canonical.href].includes(location)){
      await response.body?.cancel();throw new Error('REVIEWED_ORIGINAL_REDIRECT');
    }
    await response.body?.cancel();response=await env.ASSETS.fetch(assetRequest(canonical.href));
  }
  if(response.status!==200 || !response.body?.getReader)throw new Error('REVIEWED_ORIGINAL_UNAVAILABLE');
  const reader=response.body.getReader(),chunks=[];let size=0,timer;
  try {
    const read=async()=>{for(;;){const r=await reader.read();if(r.done)break;if(!(r.value instanceof Uint8Array)||(size+=r.value.length)>issue.html_bytes)throw new Error('REVIEWED_ORIGINAL_SIZE');chunks.push(r.value);}};
    await Promise.race([read(),new Promise((_,reject)=>{timer=setTimeout(()=>reject(new Error('REVIEWED_ORIGINAL_TIMEOUT')),10000);})]);
    const bytes=new Uint8Array(size);let pos=0;for(const c of chunks){bytes.set(c,pos);pos+=c.length;}
    if(size!==issue.html_bytes || await sha256Bytes(bytes)!==issue.html_sha256)throw new Error('REVIEWED_ORIGINAL_CHANGED');
    return bytes;
  }finally{clearTimeout(timer);try{await reader.cancel();reader.releaseLock();}catch{}}
}
export async function handleReviewedIssueRoute(request,env,auth,helpers,services) {
  const {json,requireGet,requireAction,readJson,fail}=helpers,path=new URL(request.url).pathname;
  if(path==='/zotero/reviewed-client.js'){requireGet(request);return new Response(reviewedClientSource(),{headers:{'Content-Type':'text/javascript; charset=utf-8','Cache-Control':'no-store','X-Content-Type-Options':'nosniff'}});}
  const page=path.match(/^\/zotero\/reports\/([A-Za-z0-9_-]+)$/),api=path.match(/^\/api\/zotero\/reviewed-issues\/([A-Za-z0-9_-]+)(?:\/(save|executions\/ze_[a-f0-9]{64}(?:\/advance)?))?$/);
  if(!page&&!api)return null;
  if(page || !api?.[2] || api[2].startsWith('executions/')&&!api[2].endsWith('/advance'))requireGet(request);else requireAction(request,auth.origin);
  await validateReviewedRegistry(services.issues);
  const issue=services.issues.find(i=>i.issue_id===(page||api)[1] && i.owner_id===auth.owner);
  if(!issue)fail(404,'reviewed_issue_unavailable');
  const bytes=await originalBytes(env,auth,issue),prepared=await validateReviewedIssue(issue,bytes);
  const current=await services.connection();
  const matching=c=>c?.connection.owner_id===auth.owner && 'personal:'+c.connection.library_id===issue.personal_scope;
  const ready=matching(current);
  const papers=issue.batches.flatMap(b=>JSON.parse(b.catalog).papers);
  if(page)return renderReviewedPage(bytes,issue);
  if(!api[2])return json({issue_id:issue.issue_id,projection_sha256:issue.projection_sha256,html_sha256:issue.html_sha256,day:issue.day,
    metadata_ready:true,live_ready:ready,connection_version:ready?current.connection.connection_version:null,
    papers:papers.map(p=>{
      const supported=Boolean(p.pdf && issue.batches.some(b=>b.pdf_sources.some(s=>s.paper_id===p.paper_id)));
      return {paper_id:p.paper_id,title:p.title,pdf_reviewed:Boolean(p.pdf),pdf_fetch_supported:supported,pdf_cache_status:p.pdf?'not_checked':'not_applicable',
        pdf_status:!p.pdf?'unavailable':supported?'cache_or_reviewed_fetch_on_save':'cache_only_on_save'};
    })});
  const gate=async()=>{const c=await services.connection();return matching(c) && c.connection.connection_version===current?.connection.connection_version;};
  if(!ready)fail(409,'connection_required');
  const action=api[2];
  if(action==='save') {
    const body=await readJson(request);
    if(!exact(body,['action','projection_sha256','connection_version','paper_ids','pdf_intent']) || body.action!=='save_selected'
        || body.projection_sha256!==issue.projection_sha256 || body.connection_version!==current.connection.connection_version)fail(409,'reviewed_intent_stale');
    if(!['metadata_only','open_access_if_available'].includes(body.pdf_intent) || !Array.isArray(body.paper_ids) || !body.paper_ids.length || body.paper_ids.length>100
        || new Set(body.paper_ids).size!==body.paper_ids.length || body.paper_ids.some(id=>!issue.paper_ids.includes(id)))fail(400,'invalid_selection');
    const jobs=[];
    for(let n=0;n<prepared.length;n++){
      const p=prepared[n],ids=body.paper_ids.filter(id=>p.paperRows.some(r=>r.paper_id===id));if(!ids.length)continue;
      if(!await gate())fail(409,'connection_changed');
      await registerTrustedCatalog({prepared:p,expectedBinding:issue.batches[n].binding,db:auth.db});
      const row=p.catalogRow;
      const verified=await readRegisteredCatalog({db:auth.db,ownerId:auth.owner,reportId:row.report_id,catalogId:row.catalog_id,personalScope:issue.personal_scope,requireEnabled:false});
      if(!verified || verified.row.registration_sha256!==row.registration_sha256)fail(409,'reviewed_catalog_changed');
      await sql(auth.db,`UPDATE zotero_report_catalogs SET enabled=1 WHERE owner_id=? AND report_id=? AND catalog_id=? AND registration_sha256=? AND content_sha256=?`,auth.owner,row.report_id,row.catalog_id,row.registration_sha256,row.content_sha256).run();
      jobs.push(await createExecutionJob({db:auth.db,ownerId:auth.owner,connection:current.connection,liveGate:gate,metadataFirst:true,
        request:{action:'save_selected',report_id:row.report_id,catalog_id:row.catalog_id,catalog_version:row.catalog_version,paper_ids:ids,pdf_intent:body.pdf_intent}}));
    }
    return json({issue_id:issue.issue_id,projection_sha256:issue.projection_sha256,jobs},201);
  }
  const jobId=action.split('/')[1];
  const job=await sql(auth.db,'SELECT * FROM zotero_execution_jobs WHERE owner_id=? AND job_id=?',auth.owner,jobId).first();
  const expected=prepared.find(p=>p.catalogRow.report_id===job?.report_id && p.catalogRow.catalog_id===job?.catalog_id)?.catalogRow;
  if(!expected || job.catalog_sha256!==expected.content_sha256 || job.catalog_version!==expected.catalog_version || job.library_id!==current.connection.library_id)fail(404,'execution_not_found');
  if(!action.endsWith('/advance'))return json(await readExecutionJob({db:auth.db,ownerId:auth.owner,jobId,connection:current.connection,liveGate:gate}));
  const body=await readJson(request);
  if(!exact(body,['action','projection_sha256','connection_version']) || body.action!=='continue_import' || body.projection_sha256!==issue.projection_sha256
      || body.connection_version!==current.connection.connection_version || job.connection_version!==current.connection.connection_version)fail(409,'reviewed_intent_stale');
  const blobs={async readBlob(context){
    const stored=await services.readBlob(context);if(stored!==null)return stored;
    if(job.pdf_intent!=='open_access_if_available' || !await gate())return null;
    const batch=issue.batches.find(b=>b.binding.report_id===context.reportId && b.binding.catalog_id===context.catalogId);
    const source=batch?.pdf_sources.find(s=>s.paper_id===context.paperId);
    if(!source)throw Object.assign(new Error('REVIEWED_PDF_SOURCE_UNAVAILABLE'),{code:'REVIEWED_PDF_SOURCE_UNAVAILABLE'});
    const bytes=await fetchReviewedPdf({url:source.url,sha256:context.sha256,size:context.size,...(services.pdfFetch?{fetchImpl:services.pdfFetch}:{})});
    if(!bytes || !await gate())return null;
    const store=createPrivateR2BlobStore(env.BUCKET,{ownerId:auth.owner});await store.putBlobIfAbsent(context,bytes);
    return store.readBlob(context);
  }};
  return json(await advanceExecutionJob({db:auth.db,ownerId:auth.owner,jobId,connection:current.connection,liveGate:gate,metadataFirst:true,
    transportFactory:({guardRequest,noteBackoff})=>services.transportFactory(current.credential,guardRequest,noteBackoff),blobs,hashBytes,maxSteps:1}));
}
