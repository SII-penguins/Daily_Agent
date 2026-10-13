/** Local-only validated packaging. Private input belongs ONLY to the private
 * deployment checkout, never a public/reusable source tree. */
import { readFile, readdir, lstat, rm } from 'node:fs/promises';
import { resolve } from 'node:path';
import { createHash } from 'node:crypto';
import { bundleConfig } from '../src/private-bundles.mjs';
import { parseCatalogJson, prepareTrustedCatalog } from '../src/catalog-registration.mjs';
import { PRIVATE_BUNDLE_PREFIX } from '../src/private-bundle-paths.mjs';
const sha=bytes=>createHash('sha256').update(bytes).digest('hex');
export async function loadPrivateBundles(root,{copyTo}={}){
  const directory=resolve(root,'zotero/bundles');let stat;
  try{stat=await lstat(directory);}catch(e){if(e.code==='ENOENT'){if(copyTo)await rm(resolve(copyTo,PRIVATE_BUNDLE_PREFIX.slice(1)),{recursive:true,force:true});return null;}throw e;}
  if(!stat.isDirectory() || stat.isSymbolicLink())throw new Error('Private bundle directory must be a real directory');
  const files=new Map();
  async function walk(dir,prefix=''){
    for(const e of await readdir(dir,{withFileTypes:true})){
      if(e.isSymbolicLink() || (!e.isFile()&&!e.isDirectory()) || !/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(e.name))throw new Error('Unsafe private bundle input');
      if(e.isDirectory())await walk(resolve(dir,e.name),prefix+e.name+'/');
      else{const path=resolve(dir,e.name),s=await lstat(path);if(s.size>16*1024*1024)throw new Error('Oversized private bundle input');files.set(prefix+e.name,new Uint8Array(await readFile(path)));}
    }
  }
  await walk(directory);
  const raw=files.get('manifest.json');if(!raw || raw.length>40_000)throw new Error('Private bundle manifest missing or oversized');
  const manifest=parseCatalogJson(raw),db={prepare(){},batch(){}},origin='https://bundle-build.example.test';
  const auth={owner:manifest.owner_id,db,origin};
  const env={__privateBundleManifest:manifest,DB:db,BUCKET:{get(){},put(){}},ASSETS:{fetch(){}},SITES_GATEWAY_IDENTITY_TRUSTED:'platform-managed',ZOTERO_OWNER_ID:manifest.owner_id,ZOTERO_SITE_ORIGIN:origin};
  const config=bundleConfig(env,auth),expected=new Set(['manifest.json']);
  for(const b of config.batches){const sources={};
    for(const kind of ['report','html','catalog']){const path=b.id+'/'+b.config.filenames[kind],bytes=files.get(path),pin=b.sources[kind];expected.add(path);
      if(!bytes || bytes.length!==pin.size || sha(bytes)!==pin.sha256)throw new Error('Private bundle source pin mismatch');sources[kind+'Bytes']=bytes;}
    for(const pdf of b.config.pdfs){const path=b.id+'/pdfs/'+pdf.sha256,bytes=files.get(path);expected.add(path);
      if(!bytes || bytes.length!==pdf.size || sha(bytes)!==pdf.sha256)throw new Error('Private bundle PDF pin mismatch');}
    const prepared=await prepareTrustedCatalog({binding:b.config.binding,trustedPins:b.config.trustedPins,...sources,
      readPdfBytes:hash=>files.get(b.id+'/pdfs/'+hash)});
    if(prepared.paperRows.length!==b.paper_ids.length || prepared.paperRows.some(p=>!b.paper_ids.includes(p.paper_id))
      || prepared.blobs.length!==b.config.pdfs.length)throw new Error('Private bundle paper allowlist mismatch');
  }
  if(files.size!==expected.size || [...files.keys()].some(p=>!expected.has(p)))throw new Error('Unlisted private bundle input');
  // Containment: native hosted dispatch may serve existing assets before Worker
  // authorization. Never package private payloads into this static namespace.
  if(copyTo)await rm(resolve(copyTo,PRIVATE_BUNDLE_PREFIX.slice(1)),{recursive:true,force:true});
  return manifest;
}
