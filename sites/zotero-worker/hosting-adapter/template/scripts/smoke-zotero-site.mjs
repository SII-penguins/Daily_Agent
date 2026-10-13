/** Local, no-credential workerd HTTP checks. Starts/stops its own loopback server. */
import { spawn } from 'node:child_process';
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { resolve } from 'node:path';
import { createHash } from 'node:crypto';
const root=process.cwd(),state=resolve(root,'.sites-runtime');
await mkdir(resolve(state,'xdg-config'),{recursive:true});
const files=JSON.parse(await readFile(resolve(state,'archive-inventory.json'),'utf8')).files;
const assetFirst=process.argv.includes('--asset-first');
let configPath='dist/server/wrangler.json';
if(assetFirst){const config=JSON.parse(await readFile(resolve(root,configPath),'utf8'));config.main=resolve(root,'dist/server/index.js');config.assets.directory=resolve(root,'dist/client');config.assets.run_worker_first=false;configPath='.sites-runtime/asset-first-smoke-wrangler.json';await writeFile(resolve(root,configPath),JSON.stringify(config));}
const port=18789,origin=`http://127.0.0.1:${port}`;
const child=spawn(process.execPath,['--import','./scripts/sites-env.mjs','./node_modules/wrangler/bin/wrangler.js','dev','--config',configPath,'--local','--persist-to','.wrangler/state','--ip','127.0.0.1','--inspector-port','0','--port',String(port)],{cwd:root,env:{...process.env,XDG_CONFIG_HOME:resolve(state,'xdg-config'),WRANGLER_SEND_METRICS:'false',CLOUDFLARE_CF_FETCH_ENABLED:'false'},stdio:['ignore','pipe','pipe']});
let log='';child.stdout.on('data',b=>{log=(log+b.toString()).slice(-100000);});child.stderr.on('data',b=>{log=(log+b.toString()).slice(-100000);});
let closed=false;child.on('close',()=>closed=true);
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
try{
  let ready=false;const deadline=Date.now()+45000;
  while(Date.now()<deadline&&!closed){try{const r=await fetch(origin+'/api/zotero/status',{signal:AbortSignal.timeout(1000)});await r.arrayBuffer();ready=true;break;}catch{await sleep(150);}}
  if(!ready)throw new Error('Local Wrangler did not become ready; inspect ignored local-smoke.log');
  let redirects=0;
  for(const file of files){const r=await fetch(origin+'/'+file.path,{signal:AbortSignal.timeout(10000)});if(r.status!==200)throw new Error('Archive HTTP status changed');if(r.redirected)redirects++;const bytes=Buffer.from(await r.arrayBuffer());if(createHash('sha256').update(bytes).digest('hex')!==file.sha256)throw new Error('Archive HTTP bytes changed');}
  const blocked=await fetch(origin+'/api/zotero/status');await blocked.arrayBuffer();if(blocked.status!==503)throw new Error('Absent owner/runtime configuration did not fail closed');
  const privatePaths=['/__zotero_private_bundles','/__zotero_private_bundles/manifest.json','/__ZOTERO_PRIVATE_BUNDLES/probe','//__zotero_private_bundles/probe','/%5f_zotero_private_bundles/probe','/%255f_zotero_private_bundles/probe','/%2f__zotero_private_bundles/probe','/%ZZ','/public/__zotero_private_bundles/probe','/__zotero_private_bundles%5cprobe'];
  try{const m=JSON.parse(await readFile(resolve(root,'zotero/bundles/manifest.json'),'utf8'));for(const b of m.batches){for(const name of Object.values(b.config.filenames))privatePaths.push('/__zotero_private_bundles/'+b.id+'/'+name);for(const p of b.config.pdfs)privatePaths.push('/__zotero_private_bundles/'+b.id+'/pdfs/'+p.sha256);}}catch(e){if(e.code!=='ENOENT')throw e;}
  for(const path of privatePaths)for(const query of ['', '?probe=%E4%B8%AD'])for(const method of ['GET','HEAD']){const response=await fetch(origin+path+query,{method,redirect:'manual',headers:{Range:'bytes=0-31'},signal:AbortSignal.timeout(10000)});await response.arrayBuffer();if(response.status!==404 || response.headers.has('Location'))throw new Error('Private asset boundary did not fail closed');}
  const result={routing_mode:assetFirst?'asset-first':'as-built',private_path_probes:privatePaths.length*4,private_assets_denied:true,archive_http_files:files.length,identical_bytes:true,canonical_redirects:redirects,zotero_without_config:blocked.status,production_gateway_tested:false,live_zotero_tested:false};
  await writeFile(resolve(state,assetFirst?'asset-first-smoke-result.json':'local-smoke-result.json'),JSON.stringify(result,null,2)+'\n');console.log(JSON.stringify(result));
}finally{
  child.kill('SIGINT');for(let i=0;i<30&&!closed;i++)await sleep(100);if(!closed)child.kill('SIGKILL');
  await writeFile(resolve(state,assetFirst?'asset-first-smoke.log':'local-smoke.log'),log);
}
