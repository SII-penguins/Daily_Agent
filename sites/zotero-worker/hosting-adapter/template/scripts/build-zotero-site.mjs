/** Build through the official framework runner and prove retained inputs/outputs. */
import { readFile, readdir, lstat, writeFile, mkdir } from 'node:fs/promises';
import { resolve } from 'node:path';
import { createHash } from 'node:crypto';
import { spawnSync } from 'node:child_process';
const root=process.cwd();
const hash=b=>createHash('sha256').update(b).digest('hex');
async function tree(path,prefix=''){
  const rows=[];for(const e of(await readdir(path,{withFileTypes:true})).sort((a,b)=>a.name.localeCompare(b.name,'en'))){
    if(e.isSymbolicLink()||(!e.isFile()&&!e.isDirectory()))throw new Error('Build inputs/outputs must not contain symlinks');
    if(e.isDirectory())rows.push(...await tree(resolve(path,e.name),prefix+e.name+'/'));
    else{const bytes=await readFile(resolve(path,e.name));rows.push({path:prefix+e.name,bytes:bytes.length,sha256:hash(bytes)});}
  }return rows;
}
async function inputs(){
  const rows=[];for(const name of ['app','build','components','hooks','vendor','db','drizzle','lib','scripts','zotero','public','.openai'])rows.push(...await tree(resolve(root,name),name+'/'));
  for(const name of ['package.json','package-lock.json','vite.config.ts','next.config.ts','tsconfig.json','drizzle.config.ts','postcss.config.mjs','cloudflare-env.d.ts','components.json','.npmrc','eslint.config.mjs']){const bytes=await readFile(resolve(root,name));rows.push({path:name,bytes:bytes.length,sha256:hash(bytes)});}
  return rows;
}
async function assertRefreshComplete(){
  for(const pending of ['archive-refresh-pending.json','catalog-append-pending.json']){try{await lstat(resolve(root,'.sites-runtime',pending));throw new Error(pending==='archive-refresh-pending.json'?'Interrupted archive refresh: build is fenced until reconciled':'Interrupted catalog append: build is fenced until reconciled');}catch(error){if(error.code!=='ENOENT')throw error;}}
}
await assertRefreshComplete();
const { loadPrivateBundles } = await import('./private-bundle-assets.mjs');
const { loadReviewedIssues } = await import('../zotero/hosting-adapter/reviewed-catalogs.mjs');
const reviewedIssues=await loadReviewedIssues(root);
const before=await inputs();
const privateManifest=await loadPrivateBundles(root);
const result=spawnSync(process.execPath,['scripts/run-framework.mjs','build'],{cwd:root,stdio:'inherit'});
if(result.error||result.status!==0)throw result.error||new Error('Official framework build failed');
await loadPrivateBundles(root,{copyTo:resolve(root,'dist/client')});
if(privateManifest){
  const worker=await readFile(resolve(root,'dist/server/index.js'),'utf8');
  const pins=[privateManifest.schema,privateManifest.owner_id,privateManifest.personal_scope,...privateManifest.batches.flatMap(b=>Object.values(b.sources).map(s=>s.sha256))];
  const assets=await tree(resolve(root,'dist/client'));
  if(assets.some(a=>a.path.split('/').at(-1)==='_redirects'))throw new Error('Private bundle builds do not permit static redirect/rewrite files');
  if(assets.some(a=>a.path.toLowerCase().includes('__zotero_private_bundles')))throw new Error('Private payload unexpectedly packaged as static asset');
  const baseline=JSON.parse(await readFile(resolve(root,'.sites-runtime/archive-inventory.json'),'utf8')).files;
  const payloadHashes=new Set(privateManifest.batches.flatMap(b=>[...Object.values(b.sources).map(p=>p.sha256),...b.config.pdfs.map(p=>p.sha256)]));
  if(assets.some(a=>payloadHashes.has(a.sha256) && !baseline.some(b=>b.path===a.path && b.sha256===a.sha256 && b.bytes===a.bytes)))throw new Error('Private payload copied outside preserved archive baseline');
  for(const entry of assets.filter(a=>!a.path.startsWith('__zotero_private_bundles/'))){
    const text=(await readFile(resolve(root,'dist/client',entry.path))).toString('utf8');
    if([privateManifest.owner_id,privateManifest.personal_scope,privateManifest.schema].some(pin=>text.includes(pin)))throw new Error('Private bundle configuration leaked into client output');
  }
  if(pins.some(pin=>!worker.includes(pin)) || !worker.includes('PRIVATE_BUNDLE_PREPARATION_TEMPORARILY_UNAVAILABLE') || worker.includes('typeof __ZOTERO_PRIVATE_BUNDLE_MANIFEST__') || worker.includes('typeof __ZOTERO_PRIVATE_BUNDLE_UPLOAD_MODE__') || !worker.includes('daily-agent-owner-bundle-v1') || worker.includes('PRIVATE_ASSET_HASH_MISMATCH'))throw new Error('Private bundle manifest was not compiled into the actual server Worker');
}
if(reviewedIssues.length){
  const worker=await readFile(resolve(root,'dist/server/index.js'),'utf8');
  for(const issue of reviewedIssues){
    if(!worker.includes(issue.projection_sha256))throw new Error('Reviewed issue projection missing from server Worker');
    for(const asset of await tree(resolve(root,'dist/client'))){
      const text=(await readFile(resolve(root,'dist/client',asset.path))).toString('utf8');
      if([issue.projection_sha256,issue.owner_id,issue.personal_scope,'daily-agent-reviewed-issue-v1'].some(pin=>text.includes(pin)))throw new Error('Server reviewed projection leaked into client output');
    }
  }
  if((await tree(resolve(root,'dist'))).some(f=>f.path.endsWith('.map')))throw new Error('Reviewed projection builds forbid source maps');
}
await loadReviewedIssues(root);
const verify=spawnSync(process.execPath,['scripts/verify-archive.mjs','public','dist/client'],{cwd:root,stdio:'inherit'});
if(verify.error||verify.status!==0)throw verify.error||new Error('Archive preservation failed');
await assertRefreshComplete();
const after=await inputs();if(JSON.stringify(before)!==JSON.stringify(after))throw new Error('Inputs changed during build; rebuild stable inputs');
const config=JSON.parse(await readFile(resolve(root,'dist/server/wrangler.json'),'utf8'));
if(config.assets?.binding!=='ASSETS'||config.assets?.run_worker_first!==true||config.assets?.directory!=='../client')throw new Error('Expected actual Worker-first ASSETS binding');
const outputs=await tree(resolve(root,'dist'));
await mkdir(resolve(root,'.sites-runtime'),{recursive:true});
await writeFile(resolve(root,'.sites-runtime/zotero-build-receipt.json'),JSON.stringify({version:1,kind:'vinext-sites-local-build',inputs:before,outputs,archive_unchanged:true,production_runtime_verified:false,private_bundle_containment:false,private_bundle_upload_mode:privateManifest?'owner-upload-v1':null,private_static_payloads:0},null,2)+'\n');
console.log(JSON.stringify({build:'vinext-sites',worker:'dist/server/index.js',assets:'dist/client',archive_unchanged:true,production_runtime_verified:false}));
