/** Server-pinned private deployment bundles. No URL, path, pin, credential or
 * owner supplied by a browser can become configuration. No outbound fetch. */
import { registrationConfig, REGISTRATION_LIMITS, boundedBody, finalSources } from './registration-routes.mjs';
import { prepareTrustedCatalog, registerTrustedCatalog, readRegisteredCatalog,
  createPrivateR2BlobStore, CatalogRegistrationError, parseCatalogJson } from './catalog-registration.mjs';
import { sha256Bytes } from './hash-bytes.mjs';
import { bundleUploadDescriptor } from './bundle-upload-format.mjs';
const COMPILED = typeof __ZOTERO_PRIVATE_BUNDLE_MANIFEST__ === 'undefined' ? null : __ZOTERO_PRIVATE_BUNDLE_MANIFEST__;
const ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$/;
const SHA = /^[a-f0-9]{64}$/;
const exact = (v, keys) => v && typeof v === 'object' && !Array.isArray(v) && Object.keys(v).length === keys.length && keys.every(k => Object.hasOwn(v, k));
const fail = code => { throw new CatalogRegistrationError(code); };
const sql = (db, query, ...args) => db.prepare(query).bind(...args);
export const deploymentBundleManifest = () => COMPILED;
// A private manifest alone cannot enable preparation. The supported upload mode
// must be explicitly compiled into this server build; absent mode stays contained.
export const deploymentBundleContainment = () => Boolean(COMPILED) &&
  (typeof __ZOTERO_PRIVATE_BUNDLE_UPLOAD_MODE__ === 'undefined' || __ZOTERO_PRIVATE_BUNDLE_UPLOAD_MODE__ !== 'owner-upload-v1');
export function bundleConfig(env, auth) {
  const m = env.__privateBundleManifest;
  if (!m) { if(env.ZOTERO_PRIVATE_BUNDLES_REQUIRED==='approved')fail('PRIVATE_BUNDLE_CONFIGURATION_INVALID');return null; }
  if (!exact(m, ['schema','approved','owner_id','personal_scope','connection_version','paper_ids','batches'])
      || m.schema !== 'daily-agent-zotero-private-bundles-v1' || m.approved !== true || m.owner_id !== auth.owner
      || !/^personal:[1-9]\d{0,15}$/.test(m.personal_scope) || !Number.isSafeInteger(m.connection_version) || m.connection_version < 1
      || !Array.isArray(m.paper_ids) || !m.paper_ids.length || m.paper_ids.length > 100
      || new Set(m.paper_ids).size !== m.paper_ids.length || m.paper_ids.some(x => typeof x !== 'string')
      || !Array.isArray(m.batches) || !m.batches.length || m.batches.length > 16) fail('PRIVATE_BUNDLE_CONFIGURATION_INVALID');
  const ids = new Set(), catalogs = new Set(), papers = new Set(), configs = [];
  for (const b of m.batches) {
    if (!exact(b,['id','config','sources','paper_ids']) || !ID.test(b.id) || ids.has(b.id)
        || !exact(b.sources,['report','html','catalog']) || !Array.isArray(b.paper_ids) || !b.paper_ids.length
        || new Set(b.paper_ids).size !== b.paper_ids.length) fail('PRIVATE_BUNDLE_CONFIGURATION_INVALID');
    ids.add(b.id);
    const cfg = registrationConfig({ ...env, ZOTERO_VERIFIED_PERSONAL_LIBRARY_ID: m.personal_scope.slice(9),
      ZOTERO_CATALOG_REGISTRATION_JSON: JSON.stringify(b.config) }, auth);
    if (!cfg || cfg.enable_after_registration || cfg.binding.personal_scope !== m.personal_scope) fail('PRIVATE_BUNDLE_CONFIGURATION_INVALID');
    const catalogKey = JSON.stringify([cfg.binding.report_id,cfg.binding.catalog_id]);
    if (catalogs.has(catalogKey)) fail('PRIVATE_BUNDLE_CONFIGURATION_INVALID');
    catalogs.add(catalogKey);
    let total = 0;
    for (const kind of ['report','html','catalog']) {
      const source = b.sources[kind];
      if (!exact(source,['size','sha256']) || !Number.isSafeInteger(source.size) || source.size < 1
          || source.size > REGISTRATION_LIMITS[kind] || !SHA.test(source.sha256)
          || source.sha256 !== cfg.trustedPins[kind + '_sha256']) fail('PRIVATE_BUNDLE_CONFIGURATION_INVALID');
      total += source.size;
    }
    if (total > REGISTRATION_LIMITS.totalSources) fail('PRIVATE_BUNDLE_CONFIGURATION_INVALID');
    for (const id of b.paper_ids) {
      if (!m.paper_ids.includes(id) || papers.has(id)) fail('PRIVATE_BUNDLE_CONFIGURATION_INVALID');
      papers.add(id);
    }
    if (cfg.pdfs.some(pdf => !b.paper_ids.includes(pdf.paper_id))) fail('PRIVATE_BUNDLE_CONFIGURATION_INVALID');
    configs.push({ ...b, config: cfg });
  }
  if (papers.size !== m.paper_ids.length) fail('PRIVATE_BUNDLE_CONFIGURATION_INVALID');
  return { ...m, batches: configs };
}
function blobContext(auth, b, p) { return { ownerId:auth.owner,reportId:b.config.binding.report_id,catalogId:b.config.binding.catalog_id,paperId:p.paper_id,sha256:p.sha256,size:p.size }; }
function storeFor(env, auth) { return createPrivateR2BlobStore(env.BUCKET,{ownerId:auth.owner}); }
async function readBatch(auth,b) {
  const cfg=b.config;
  const verified=await readRegisteredCatalog({db:auth.db,ownerId:auth.owner,reportId:cfg.binding.report_id,
    catalogId:cfg.binding.catalog_id,personalScope:cfg.binding.personal_scope,requireEnabled:false});
  if (!verified) return null;
  if (verified.row.catalog_version!==cfg.binding.catalog_version || verified.row.report_sha256!==cfg.trustedPins.report_sha256
      || verified.row.source_catalog_sha256!==cfg.trustedPins.catalog_sha256 || verified.papers.length!==b.paper_ids.length
      || verified.papers.some(p=>!b.paper_ids.includes(p.paper_id))) fail('IMMUTABLE_CATALOG_CONFLICT');
  return verified;
}
export function bundleAllowsCatalog(cfg,report,catalog) { return !cfg || cfg.batches.some(b=>b.config.binding.report_id===report && b.config.binding.catalog_id===catalog); }
export async function bundledCatalogsReady(env,auth) {
  if(env.__privateBundleContainment)return false;
  if (!env.__privateBundleManifest) return env.ZOTERO_PRIVATE_BUNDLES_REQUIRED!=='approved';
  try { const cfg=bundleConfig(env,auth); for(const b of cfg.batches){const row=await readBatch(auth,b);if(!row || row.row.enabled!==1)return false;}return true; } catch { return false; }
}
export function bundleConnectionMatches(cfg,connection) {
  return connection && connection.owner_id===cfg.owner_id && connection.library_type==='user'
    && `personal:${connection.library_id}`===cfg.personal_scope && connection.connection_version===cfg.connection_version && connection.state==='active';
}
async function registerBatch(env,auth,b,helpers,source) {
  for(const kind of ['report','html','catalog']){const bytes=source[kind+'Bytes'],pin=b.sources[kind];
    if(bytes.length!==pin.size || await sha256Bytes(bytes)!==pin.sha256)fail('PRIVATE_BUNDLE_SOURCE_MISMATCH');}
  const store=storeFor(env,auth), pdfs=new Map(b.config.pdfs.map(p=>[p.sha256,p]));
  const prepared=await prepareTrustedCatalog({binding:b.config.binding,trustedPins:b.config.trustedPins,...source,includeProtocolMd5:false,
    readPdfBytes:async sha=>{const pdf=pdfs.get(sha);if(!pdf)fail('PDF_NOT_IN_REGISTRATION_MANIFEST');return store.readBlob(blobContext(auth,b,pdf));}});
  const actual=prepared.paperRows.map(p=>JSON.parse(p.metadata_json));
  if(actual.length!==b.paper_ids.length || actual.some(p=>!b.paper_ids.includes(p.paper_id)) || actual.filter(p=>p.pdf).length!==pdfs.size
      || actual.some(p=>p.pdf && (!pdfs.has(p.pdf.sha256) || pdfs.get(p.pdf.sha256).paper_id!==p.paper_id || pdfs.get(p.pdf.sha256).size!==p.pdf.size)))fail('PRIVATE_BUNDLE_PAPER_MISMATCH');
  const guardedDb={prepare:query=>auth.db.prepare(query),batch:async statements=>{if(!bundleConnectionMatches(bundleConfig(env,auth),await helpers.bundleConnection()))fail('PRIVATE_BUNDLE_CONNECTION_REQUIRED');return auth.db.batch(statements);}};
  await registerTrustedCatalog({prepared,expectedBinding:b.config.binding,db:guardedDb,blobs:store});
  if(!await readBatch(auth,b))fail('REGISTRATION_READBACK_FAILED');
  return {registered:true,batch_id:b.id,paper_count:actual.length};
}
async function activate(env,auth,cfg,helpers) {
  const connection=await helpers.bundleConnection();
  if(!bundleConnectionMatches(cfg,connection))fail('PRIVATE_BUNDLE_CONNECTION_REQUIRED');
  const rows=[],store=storeFor(env,auth);
  for(const b of cfg.batches){
    const v=await readBatch(auth,b);if(!v)fail('PRIVATE_BUNDLE_INCOMPLETE');rows.push(v.row);
    // Sequential native SHA readback: never retain an all-edition PDF package.
    for(const pdf of b.config.pdfs){const bytes=await store.readBlob(blobContext(auth,b,pdf));
      if(!bytes || bytes.length!==pdf.size || await sha256Bytes(bytes)!==pdf.sha256)fail('PRIVATE_BUNDLE_BLOB_UNCONFIRMED');}
  }
  const selectors=rows.map(()=>'(report_id=? AND catalog_id=? AND catalog_version=? AND source_catalog_sha256=? AND report_sha256=? AND content_sha256=? AND registration_sha256=?)').join(' OR ');
  const args=rows.flatMap(r=>[r.report_id,r.catalog_id,r.catalog_version,r.source_catalog_sha256,r.report_sha256,r.content_sha256,r.registration_sha256]);
  // One SQL statement atomically activates the complete, prevalidated set only
  // while the exact verified connection still exists. Never touches other rows.
  const result=await sql(auth.db,`UPDATE zotero_report_catalogs SET enabled=1 WHERE owner_id=? AND personal_scope=? AND (${selectors})
    AND EXISTS (SELECT 1 FROM zotero_active_connections WHERE owner_id=? AND library_type='user' AND library_id=? AND connection_version=? AND state='active' AND permissions_json=? AND storage_mode=? AND credential_id=? AND credential_key_id=? AND grant_id=?)`,
    auth.owner,cfg.personal_scope,...args,auth.owner,cfg.personal_scope.slice(9),cfg.connection_version,connection.permissions_json,connection.storage_mode,connection.credential_id,connection.credential_key_id,connection.grant_id).run();
  if(result.success!==true || !bundleConnectionMatches(cfg,await helpers.bundleConnection()) || !await bundledCatalogsReady(env,auth))fail('PRIVATE_BUNDLE_ACTIVATION_UNCONFIRMED');
  return {ready:true,paper_count:cfg.paper_ids.length,batch_count:cfg.batches.length,zotero_requests:0};
}
export async function handlePrivateBundleRoute(request,env,auth,helpers) {
  const root='/api/zotero/private-bundles',url=new URL(request.url);
  if(url.pathname!==root && !url.pathname.startsWith(root+'/'))return null;
  try {
    if(url.origin!==auth.origin || url.search)fail('INVALID_PRIVATE_BUNDLE_TARGET');
    if(url.pathname===root)helpers.requireGet(request);
    else helpers.requireAction(request,auth.origin,/\/pdfs\/[a-f0-9]{64}$/.test(url.pathname)?'application/pdf':'application/json');
    if(env.__privateBundleContainment){
      if(url.pathname===root)return helpers.json({enabled:false,ready:false,state:'temporarily_unavailable',batches:[]});
      fail('PRIVATE_BUNDLE_PREPARATION_TEMPORARILY_UNAVAILABLE');
    }
    const cfg=bundleConfig(env,auth);
    if(!cfg){if(url.pathname===root)return helpers.json({enabled:false,ready:false,batches:[]});fail('PRIVATE_BUNDLES_NOT_CONFIGURED');}
    if(url.pathname===root){const batches=[];for(const b of cfg.batches){const existing=await readBatch(auth,b);batches.push({id:b.id,registered:Boolean(existing),enabled:existing?.row.enabled===1,paper_count:b.paper_ids.length,pdfs:b.config.pdfs.map(p=>({sha256:p.sha256,size:p.size}))});}
      return helpers.json({enabled:true,ready:batches.every(b=>b.enabled),connection_matches:bundleConnectionMatches(cfg,await helpers.bundleConnection()),paper_count:cfg.paper_ids.length,batches,upload:bundleUploadDescriptor(cfg)});}
    if(!bundleConnectionMatches(cfg,await helpers.bundleConnection()))fail('PRIVATE_BUNDLE_CONNECTION_REQUIRED');
    if(url.pathname===root+'/finish'){
      const body=parseCatalogJson(await boundedBody(request,1024));
      if(!exact(body,['action']) || body.action!=='prepare_private_catalogs')fail('INVALID_PRIVATE_BUNDLE_ACTION');
      return helpers.json(await activate(env,auth,cfg,helpers));
    }
    const match=url.pathname.match(/^\/api\/zotero\/private-bundles\/([A-Za-z0-9][A-Za-z0-9._-]{0,79})\/(register|pdfs\/([a-f0-9]{64}))$/);
    const batch=match && cfg.batches.find(b=>b.id===match[1]);if(!batch)fail('PRIVATE_BUNDLE_NOT_ALLOWED');
    if(match[2]==='register')return helpers.json(await registerBatch(env,auth,batch,helpers,finalSources(await boundedBody(request,REGISTRATION_LIMITS.finalBody))));
    const pdf=batch.config.pdfs.find(p=>p.sha256===match[3]);if(!pdf)fail('PDF_NOT_IN_REGISTRATION_MANIFEST');
    // Validate every submitted body even on a retry. Client indexes, filenames,
    // hashes and claims of previous staging never grant authority.
    const bytes=await boundedBody(request,pdf.size,pdf.size);
    if(bytes[0]!==37 || bytes[1]!==80 || bytes[2]!==68 || bytes[3]!==70 || bytes[4]!==45
        || await sha256Bytes(bytes)!==pdf.sha256)fail('REGISTRATION_PDF_HASH_MISMATCH');
    const store=storeFor(env,auth),context=blobContext(auth,batch,pdf),prior=await store.readBlob(context);
    if(prior!==null){if(prior.length!==pdf.size || await sha256Bytes(prior)!==pdf.sha256)fail('IMMUTABLE_BLOB_CONFLICT');
      if(!bundleConnectionMatches(cfg,await helpers.bundleConnection()))fail('PRIVATE_BUNDLE_CONNECTION_REQUIRED');
      return helpers.json({staged:true,created:false,sha256:pdf.sha256});}
    if(!bundleConnectionMatches(cfg,await helpers.bundleConnection()))fail('PRIVATE_BUNDLE_CONNECTION_REQUIRED');
    const created=await store.putBlobIfAbsent(context,bytes),stored=await store.readBlob(context);
    if(!stored || stored.length!==pdf.size || await sha256Bytes(stored)!==pdf.sha256)fail('REGISTRATION_PDF_READBACK_FAILED');
    return helpers.json({staged:true,created,sha256:pdf.sha256});
  }catch(error){if(error instanceof CatalogRegistrationError)return helpers.json({error:error.code.toLowerCase(),ready:false},
    ['REGISTRATION_BODY_TOO_LARGE','REGISTRATION_BODY_SIZE_MISMATCH'].includes(error.code)?413:409);throw error;}
}
