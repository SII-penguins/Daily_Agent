// Entirely synthetic in-memory catalogs, credentials and asset bytes. No network.
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { MockD1 } from './mock-d1.mjs';
import worker, {handleZoteroRequest} from '../worker/index.js';
import {denyPrivateBundleRequest} from '../src/private-bundle-paths.mjs';
const origin='https://daily.example.test',owner='synthetic-bundle-owner',library='123456',base='/api/zotero/private-bundles';
const utf8=s=>new TextEncoder().encode(s),sha=b=>createHash('sha256').update(b).digest('hex');
export function fixture(){
  const db=new MockD1(),assets=new Map(),objects=new Map(),calls=[],upstream=[];
  const manifest={schema:'daily-agent-zotero-private-bundles-v1',approved:true,owner_id:owner,personal_scope:'personal:'+library,connection_version:1,paper_ids:[],batches:[]};
  for(let n=1;n<=2;n++){
    const id='batch-'+n,pdf=utf8('%PDF-1.4\nSYNTHETIC BUNDLE '+n+' ONLY\n%%EOF\n'),html=utf8('<title>Artificial bundle '+n+'</title>');
    const paper={paper_id:'paper-'+n,title:'Synthetic paper '+n,source_url:'https://publisher.example.test/paper-'+n,doi:'10.9999/synthetic-'+n,arxiv:'',authors:['Synthetic Author'],year:'2000',venue:'Synthetic Journal',item_type:'journalArticle',publication:'published_verified',publication_evidence_sha256:'f'.repeat(64),topics:['other-reviewed'],pdf:{blob_id:sha(pdf),sha256:sha(pdf),size:pdf.length},review_sha256:'a'.repeat(64),qualification_receipt_sha256:'b'.repeat(64),source_pdf_sha256:sha(pdf)};
    const report=utf8(JSON.stringify({schema:'paper-first-corrected-edition-v1',edition_id:'edition-'+n,date:'2000-01-0'+n,html_sha256:sha(html),html_bytes:html.length,qualified_papers:1,paper_records:[{key:paper.paper_id,...Object.fromEntries(['title','source_url','review_sha256','qualification_receipt_sha256','source_pdf_sha256'].map(k=>[k,paper[k]]))}]}));
    const catalog=utf8(JSON.stringify({schema:'daily-agent-zotero-catalog-v1',report_sha256:sha(report),papers:[paper]}));
    const binding={owner_id:owner,report_id:'report-'+n,catalog_id:'catalog-'+n,catalog_version:1,personal_scope:'personal:'+library};
    const config={schema:'daily-agent-zotero-registration-config-v1',approved:true,binding,trustedPins:{...binding,report_sha256:sha(report),html_sha256:sha(html),catalog_sha256:sha(catalog),report_edition_id:'edition-'+n,allowed_source_hosts:['publisher.example.test']},pdfs:[{sha256:sha(pdf),size:pdf.length,paper_id:paper.paper_id}],filenames:{report:'report.json',html:'report.html',catalog:'catalog.json'},enable_after_registration:false};
    const sources={report,html,catalog};manifest.paper_ids.push(paper.paper_id);manifest.batches.push({id,config,paper_ids:[paper.paper_id],sources:Object.fromEntries(Object.entries(sources).map(([k,v])=>[k,{size:v.length,sha256:sha(v)}]))});
    for(const [k,v]of Object.entries(sources))assets.set('/__zotero_private_bundles/'+id+'/'+config.filenames[k],v);assets.set('/__zotero_private_bundles/'+id+'/pdfs/'+sha(pdf),pdf);
  }
  db.sqlite.prepare(`INSERT INTO zotero_provider_credentials VALUES (?, 'user', ?, ?, ?, ?, ?)`).run(owner,library,'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa','a'.repeat(64),'SYNTHETICBUNDLEKEY12345','2000-01-01T00:00:00.000Z');
  db.sqlite.prepare(`INSERT INTO zotero_active_connections (owner_id,library_type,library_id,connection_version,state,verified_at,permissions_json,storage_mode,credential_id) VALUES (?,'user',?,1,'active',? ,?,'provider-managed-d1',?)`).run(owner,library,'2000-01-01T00:00:00.000Z',JSON.stringify({library:true,write:true,files:true,notes:false}),'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa');
  const env={DB:db,BUCKET:{async get(key){calls.push(['get',key]);const b=objects.get(key);return b?{size:b.length,body:new Response(b).body}:null;},async put(key,b,opts){calls.push(['put',key]);assert.deepEqual(opts.onlyIf,{etagDoesNotMatch:'*'});if(objects.has(key))return null;objects.set(key,new Uint8Array(b));return{key};}},
    ASSETS:{async fetch(req){calls.push(['asset',req.url]);assert.equal(req.redirect,'error');const b=assets.get(new URL(req.url).pathname);return new Response(b || 'missing',{status:b?200:404,headers:{'Content-Length':String(b?.length || 7)}});}},
    SITES_GATEWAY_IDENTITY_TRUSTED:'platform-managed',ZOTERO_OWNER_ID:owner,ZOTERO_SITE_ORIGIN:origin,ZOTERO_CREDENTIAL_STORAGE:'provider-managed-d1',ZOTERO_EXECUTION_ENABLED:'approved'};
  const call=async(path=base,{body,method=body?'POST':'GET',identity=owner,headers={},bundleManifest=manifest,bundleContainment=false}={})=>{
    const response=await handleZoteroRequest(new Request(origin+path,{method,headers:{'oai-authenticated-user-id':identity,Origin:origin,'X-Zotero-Action':'1','Content-Type':'application/json',...headers},...(body===undefined?{}:{body:typeof body==='string'||body instanceof Uint8Array?body:JSON.stringify(body)})}),env,{bundleManifest,bundleContainment,fetchImpl:async(...args)=>{upstream.push(args);throw new Error('No Zotero request permitted during preparation');}});
    const text=await response.text();assert.equal(text.includes('SYNTHETICBUNDLEKEY12345'),false);return {status:response.status,data:response.headers.get('Content-Type')?.includes('application/json')?JSON.parse(text):null,text};};
  const action=path=>{
    const match=path.match(/\/([^/]+)\/(register|pdfs\/([a-f0-9]{64}))$/),b=match && manifest.batches.find(x=>x.id===match[1]);
    if(b && match[3])return call(path,{body:assets.get('/__zotero_private_bundles/'+b.id+'/pdfs/'+match[3]) || utf8('missing'),headers:{'Content-Type':'application/pdf'}});
    if(b && match[2]==='register')return call(path,{body:{action:'register_catalog',...Object.fromEntries(['report','html','catalog'].map(k=>[k+'_b64',Buffer.from(assets.get('/__zotero_private_bundles/'+b.id+'/'+b.config.filenames[k]) || utf8('missing')).toString('base64')]))}});
    return call(path,{body:{action:'prepare_private_catalogs'}});
  };
  const stage=async b=>{for(const p of b.config.pdfs)assert.equal((await action(base+'/'+b.id+'/pdfs/'+p.sha256)).status,200);return action(base+'/'+b.id+'/register');};
  const all=async()=>{for(const b of manifest.batches)assert.equal((await stage(b)).status,200);return action(base+'/finish');};
  return{db,manifest,assets,objects,calls,upstream,env,call,action,stage,all};
}
