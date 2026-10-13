import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, readFile, rm, cp, symlink, rename } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { resolve, dirname } from 'node:path';
import { spawnSync } from 'node:child_process';
import { guardPublication, verifyPublication, inspectArchiveIndex, archivePathAllowed, PROFILE_SCHEMA } from './publication-proof.mjs';
import { inventory, digest } from './materialize.mjs';

const projectId='synthetic-project', siteUrl='https://synthetic.example.chatgpt.site';
const hosting={project_id:projectId,d1:'DB',r2:'BUCKET'};
const registryBase=()=>({schema:'daily-agent-future-operations-registry-v1',global_delivery:{audience:'owner_private',append_archive_entries_only:true,site_url:siteUrl}});
const registryFor=commit=>({ ...registryBase(), global_delivery:{...registryBase().global_delivery,site_runtime_profile:{schema:PROFILE_SCHEMA,mode:'worker-assets',state:'active',project_id:projectId,site_source_commit_sha:commit,version_id:'synthetic-previous-version',deployment_id:'synthetic-previous-deployment'}}});
const put=async (root,name,data)=>{await mkdir(dirname(resolve(root,name)),{recursive:true});await writeFile(resolve(root,name),typeof data==='object'&&!Buffer.isBuffer(data)?JSON.stringify(data):data);};
function git(repo,...args){const r=spawnSync('git',['-C',repo,...args],{encoding:'utf8'});assert.equal(r.status,0,r.stderr);return r.stdout.trim();}
const commit=repo=>{git(repo,'add','.');git(repo,'-c','user.name=Synthetic Test','-c','user.email=test@example.invalid','commit','-qm','synthetic test fixture');return git(repo,'rev-parse','HEAD');};
const archiveUiIndex=links=>`<html><head><script src="/assets/archive-browser.js" defer></script></head><body><form id="archive-filters" class="archive-filters" hidden role="search" aria-label="查找往期日报"><input id="archive-query" type="search" name="q"><select id="archive-year" name="year"></select><select id="archive-month" name="month"></select><input id="archive-date" type="date" name="date"><button type="submit">搜索</button><button id="archive-reset" type="button">重置筛选</button></form><div id="archive-days"><div id="archive-dynamic" hidden><p id="archive-status" aria-live="polite"></p><div id="archive-results"></div><nav id="archive-pagination"></nav></div><div id="archive-static">${links}</div></div></body></html>`;
async function manifest(repo,entries){const files={};for(const f of await inventory(resolve(repo,'public'))){if(f.path!=='manifest.json')files[f.path]={size:f.bytes,sha256:f.sha256};}await put(repo,'public/manifest.json',{schema_version:1,entries,files});}
async function fixture(fn,{staticBaseline=false,archiveUi=false}={}){
 const root=await mkdtemp(resolve(tmpdir(),'worker-proof-'));
 try{
  const repo=resolve(root,'repo');await mkdir(repo);git(repo,'init','-q');
  await put(repo,'.gitignore','dist/\n.sites-runtime/\n');
  for(const dir of ['app','build','components','hooks','vendor','db','lib','scripts'])await put(repo,dir+'/synthetic.txt','synthetic framework source');
  for(const file of ['package.json','package-lock.json','vite.config.ts','next.config.ts','tsconfig.json','drizzle.config.ts','postcss.config.mjs','cloudflare-env.d.ts','components.json','.npmrc','eslint.config.mjs'])await put(repo,file,'{}');
  await put(repo,'.openai/hosting.json',hosting);await put(repo,'build/sites-worker.ts','synthetic worker adapter');await put(repo,'zotero/worker/index.js','synthetic runtime');
  const sql='CREATE TABLE synthetic(id TEXT);';await put(repo,'zotero/migrations/0001_synthetic.sql',sql);
  const mapping={migrations:[{source:'0001_synthetic.sql',source_sha256:digest(sql),output:'0000_synthetic.sql',output_sha256:digest(sql)}]};
  await put(repo,'drizzle/zotero-source-migrations.json',mapping);await put(repo,'drizzle/0000_synthetic.sql',sql);await put(repo,'drizzle/meta/_journal.json',{entries:[{tag:'0000_synthetic'}]});
  await put(repo,'public/index.html','<a href="reports/old/index.html">old</a>');await put(repo,'public/reports/old/index.html','immutable old report');
  if(archiveUi){const html=archiveUiIndex('<article><a href="reports/old/index.html">old</a></article>');await put(repo,'public/index.html',html);await put(repo,'public/archive/index.html',html);await put(repo,'public/assets/archive-browser.js','/* previously approved and immutable archive browser */');await put(repo,'public/reports/old/reading.html','immutable reading page');}
  const oldEntry={key:'reports/old',page:'reports/old/index.html',source_sha256:digest('immutable old report')};
  await manifest(repo,[oldEntry]);
  if(staticBaseline){await rename(resolve(repo,'public'),resolve(repo,'dist'));await put(repo,'.gitignore','.sites-runtime/\n');await put(repo,'.openai/hosting.json',{project_id:projectId,static:{directory:'dist'}});}
  const baseline=commit(repo);
  if(staticBaseline){await rename(resolve(repo,'dist'),resolve(repo,'public'));await put(repo,'.gitignore','dist/\n.sites-runtime/\n');await put(repo,'.openai/hosting.json',hosting);}
  const html='<html>synthetic reviewed edition</html>',htmlSha256=digest(html),entryPath=`reports/2026-10-11-test-01-${htmlSha256.slice(0,12)}/index.html`;
  const links=`<article><a href="reports/old/index.html">old</a></article><article><a href="${entryPath}">new</a></article>`;
  await put(repo,'public/'+entryPath,html);await put(repo,'public/index.html',archiveUi?archiveUiIndex(links):links);await manifest(repo,[oldEntry,{key:entryPath.slice(0,-11),page:entryPath,source_sha256:htmlSha256,identity:htmlSha256,approval_sha256:'a'.repeat(64),category:'reports',date:'2026-10-11'}]);const current=commit(repo);
  const dist=resolve(repo,'dist');await mkdir(dist);await cp(resolve(repo,'public'),resolve(dist,'client'),{recursive:true});await cp(resolve(repo,'drizzle'),resolve(dist,'.openai/drizzle'),{recursive:true});
  await put(dist,'.openai/hosting.json',hosting);await put(dist,'server/index.js','synthetic compiled worker');await put(dist,'server/chunk.js','synthetic imported chunk');
  await put(dist,'server/wrangler.json',{assets:{binding:'ASSETS',directory:'../client',run_worker_first:true,not_found_handling:'none'},d1_databases:[{binding:'DB'}],r2_buckets:[{binding:'BUCKET'}]});
  const tracked=git(repo,'ls-files').split('\n').filter(p=>p!=='.gitignore');const inputs=[];for(const path of tracked){const bytes=await readFile(resolve(repo,path));inputs.push({path,bytes:bytes.length,sha256:digest(bytes)});}
  const buildReceipt=resolve(root,'receipt.json'),deploymentFile=resolve(root,'deployment.json'),versionFile=resolve(root,'version.json'),artifact=resolve(root,'artifact.tar.gz');
  await put(root,'receipt.json',{version:1,kind:'vinext-sites-local-build',inputs,outputs:await inventory(dist),archive_unchanged:true,production_runtime_verified:false});
  const deployment={id:'synthetic-deployment',project_id:projectId,version_id:'synthetic-version',type:'publish',status:'succeeded',failure_message:null,url:siteUrl,updated_at:'2026-10-10T00:00:00Z'};
  const tar=spawnSync('python3',['-c',"import sys,tarfile; t=tarfile.open(sys.argv[1],'w:gz');t.add(sys.argv[2],arcname='dist');t.close()",artifact,dist]);assert.equal(tar.status,0);
  const packed=await readFile(artifact);const version={id:deployment.version_id,project_id:projectId,deployment_id:deployment.id,source:{commit_sha:current},archive_storage:{archive_format:'tar',content_hash:'sha256:'+digest(packed),size_bytes:packed.length,file_count:(await inventory(dist)).length}};
  await put(root,'deployment.json',{isError:false,structuredContent:deployment});await put(root,'version.json',{isError:false,structuredContent:version});
  const registry=registryFor(baseline);if(staticBaseline)registry.global_delivery.site_runtime_profile.state='migration_pending';
  const args={registry,repo,dist,buildReceipt,deploymentFile,versionFile,artifact,expectedCommit:current,entryPath,htmlSha256};
  const rebuild = async()=>{
   args.expectedCommit=commit(repo);await rm(resolve(dist,'client'),{recursive:true});await cp(resolve(repo,'public'),resolve(dist,'client'),{recursive:true});
   const source=[];for(const path of git(repo,'ls-files').split('\n').filter(p=>p!=='.gitignore')){const body=await readFile(resolve(repo,path));source.push({path,bytes:body.length,sha256:digest(body)});}
   await put(root,'receipt.json',{version:1,kind:'vinext-sites-local-build',inputs:source,outputs:await inventory(dist),archive_unchanged:true,production_runtime_verified:false});
   const packedResult=spawnSync('python3',['-c',"import sys,tarfile; t=tarfile.open(sys.argv[1],'w:gz');t.add(sys.argv[2],arcname='dist');t.close()",artifact,dist]);assert.equal(packedResult.status,0);
   const data=await readFile(artifact);version.source.commit_sha=args.expectedCommit;version.archive_storage={archive_format:'tar',content_hash:'sha256:'+digest(data),size_bytes:data.length,file_count:(await inventory(dist)).length};await put(root,'version.json',{isError:false,structuredContent:version});
  };
  await fn({root,args,baseline,oldEntry,deployment,version,rebuild,async amendReceipt(edit){const data=JSON.parse(await readFile(buildReceipt));edit(data);await writeFile(buildReceipt,JSON.stringify(data));}});
 }finally{await rm(root,{recursive:true,force:true});}
}

test('legacy profile remains exact static and never silently accepts a Worker',()=>{
 const registry=registryBase();assert.equal(guardPublication({registry,hosting:{project_id:projectId,static:{directory:'dist'}},projectId}).mode,'static-archive');
 assert.throws(()=>guardPublication({registry,hosting,projectId}),/Unregistered Worker/);
});
test('active Worker profile rejects static, lost bindings, wrong identity and malformed profile',()=>{
 const registry=registryFor('a'.repeat(40));assert.equal(guardPublication({registry,hosting,projectId}).mode,'worker-assets');
 for(const changed of [{...hosting,static:{directory:'dist'}},{...hosting,d1:null},{...hosting,r2:'OTHER'},{...hosting,project_id:'other'}])assert.throws(()=>guardPublication({registry,hosting:changed,projectId}));
 registry.global_delivery.site_runtime_profile={mode:'worker-assets'};assert.throws(()=>guardPublication({registry,hosting,projectId}),/Unknown/);
});
test('pending profile fences publication; explicit read-only migration verification is separate',()=>{
 const registry=registryFor('a'.repeat(40));registry.global_delivery.site_runtime_profile.state='migration_pending';
 assert.throws(()=>guardPublication({registry,hosting,projectId}),/scheduled publication blocked/);
 assert.equal(guardPublication({registry,hosting,projectId,migrationVerification:true}).state,'migration_pending');
 assert.throws(()=>guardPublication({registry,hosting:{...hosting,static:{directory:'dist'}},projectId,migrationVerification:true}),/forbids static/);
});
test('valid synthetic native artifact proof binds version, commit, raw evidence and complete built assets',()=>fixture(async({args})=>{
 const before=await readFile(args.deploymentFile);const result=await verifyPublication(args);
 assert.equal(result.native_saved_artifact_verified,true);assert.equal(result.remote_http_verified,false);assert.equal(result.owner_audience_verified,false);assert.equal(result.registry_changed,false);assert.equal(result.chat_sent,false);
 assert.equal(result.site_source_commit_sha,args.expectedCommit);assert.equal(result.evidence.deployment.sha256,digest(before));
 assert.ok(result.build_outputs.some(f=>f.path==='server/chunk.js'));assert.ok(result.archive_files.some(f=>f.path==='reports/old/index.html'));
}));
test('raw native version/commit mismatch and fabricated selected fields fail',()=>fixture(async({args,version,root})=>{
 await put(root,'version.json',{isError:false,structuredContent:{...version,source:{commit_sha:'b'.repeat(40)}}});await assert.rejects(verifyPublication(args),/identity mismatch/);
 await put(root,'version.json',version);await assert.rejects(verifyPublication(args),/raw Site version/);
}));
test('failed deployment and wrong Site URL fail',()=>fixture(async({args,deployment,root})=>{
 await put(root,'deployment.json',{isError:false,structuredContent:{...deployment,status:'failed'}});await assert.rejects(verifyPublication(args),/Successful deployment/);
 await put(root,'deployment.json',{isError:false,structuredContent:{...deployment,url:'https://other.example.chatgpt.site'}});await assert.rejects(verifyPublication(args),/URL differs/);
}));
test('missing input, duplicate input and extra output are rejected',()=>fixture(async({args,amendReceipt})=>{
 const original=await readFile(args.buildReceipt);await amendReceipt(r=>r.inputs.pop());await assert.rejects(verifyPublication(args),/input receipt differs/);
 await writeFile(args.buildReceipt,original);await amendReceipt(r=>r.inputs.push(r.inputs[0]));await assert.rejects(verifyPublication(args),/Duplicate/);
 await writeFile(args.buildReceipt,original);await put(args.dist,'server/unreceipted.js','extra');await assert.rejects(verifyPublication(args),/complete local artifact/);
}));
test('stale built archives and symlink outputs fail',()=>fixture(async({args})=>{
 await writeFile(resolve(args.dist,'client',args.entryPath),'tampered');await assert.rejects(verifyPublication(args),/complete local artifact/);
 await symlink('index.js',resolve(args.dist,'server/link.js'));await assert.rejects(verifyPublication(args),/Symlinks/);
}));
test('archive-only proof rejects changed runtime, migration or hosting source',()=>fixture(async({args})=>{
 await put(args.repo,'zotero/worker/index.js','runtime replacement');const changed=commit(args.repo);
 await assert.rejects(verifyPublication({...args,expectedCommit:changed}),/framework\/runtime/);
}));
test('archive-only proof rejects changed or removed historical files and entry records',()=>fixture(async({args})=>{
 await put(args.repo,'public/reports/old/index.html','changed history');const changed=commit(args.repo);
 await assert.rejects(verifyPublication({...args,expectedCommit:changed}),/Previously published/);
}));
test('wrong saved artifact hash and unbound reviewed HTML fail',()=>fixture(async({args,root,version})=>{
 await put(root,'version.json',{isError:false,structuredContent:{...version,archive_storage:{...version.archive_storage,content_hash:'sha256:'+'0'.repeat(64)}}});await assert.rejects(verifyPublication(args),/artifact hash\/size/);
 await assert.rejects(verifyPublication({...args,htmlSha256:'f'.repeat(64)}),/path must bind/);
}));
test('unsafe proof inputs and working-tree-only source are never accepted',()=>fixture(async({args,amendReceipt})=>{
 await amendReceipt(r=>r.inputs[0].path='../escape');await assert.rejects(verifyPublication(args),/Unsafe proof path/);
 await assert.rejects(verifyPublication({...args,expectedCommit:'HEAD'}),/Exact commit/);
}));

test('proof refuses a pending partial refresh even when an old build exists',()=>fixture(async({args})=>{
 await put(args.repo,'.sites-runtime/archive-refresh-pending.json',{state:'pending'});await assert.rejects(verifyPublication(args),/Interrupted archive refresh/);
}));
test('proof refuses historical entry metadata changes independently of refresh tool',()=>fixture(async({args})=>{
 const archive=JSON.parse(await readFile(resolve(args.repo,'public/manifest.json')));archive.entries[0].audit='rewritten';await put(args.repo,'public/manifest.json',archive);
 await assert.rejects(verifyPublication({...args,expectedCommit:commit(args.repo)}),/entry record changed/);
}));
test('unsafe native artifact links and duplicate members fail without extraction',()=>fixture(async({args,version,root})=>{
 for(const kind of ['symlink','duplicate','traversal']){
  const result=spawnSync('python3',['-c',`import sys,tarfile,io
with tarfile.open(sys.argv[1],'w:gz') as t:
 p=tarfile.TarInfo('dist/../../escape' if sys.argv[2]=='traversal' else 'dist/server/index.js')
 if sys.argv[2]=='symlink': p.type=tarfile.SYMTYPE;p.linkname='/etc/passwd'
 else: p.size=1
 t.addfile(p,None if sys.argv[2]=='symlink' else io.BytesIO(b'x'))
 if sys.argv[2]=='duplicate': t.addfile(p,io.BytesIO(b'x'))
`,args.artifact,kind]);assert.equal(result.status,0);
  const body=await readFile(args.artifact);await put(root,'version.json',{isError:false,structuredContent:{...version,archive_storage:{...version.archive_storage,content_hash:'sha256:'+digest(body),size_bytes:body.length}}});
  await assert.rejects(verifyPublication(args),/Unsafe or unreadable/);
 }
}));

test('explicit initial migration preserves committed static history in public assets',()=>fixture(async({args})=>{
 await assert.rejects(verifyPublication(args),/scheduled publication blocked/);
 const result=await verifyPublication({...args,migrationVerification:true});assert.equal(result.migration_verification,true);assert.equal(result.registry_changed,false);
 await put(args.repo,'public/reports/old/index.html','discarded history');
 await assert.rejects(verifyPublication({...args,migrationVerification:true,expectedCommit:commit(args.repo)}),/Previously published/);
},{staticBaseline:true}));

test('fully rebuilt source cannot hide old reports from the archive index',()=>fixture(async({args,rebuild})=>{
 await put(args.repo,'public/index.html',`<a href="${args.entryPath}">new only</a>`);
 const archive=JSON.parse(await readFile(resolve(args.repo,'public/manifest.json')));await manifest(args.repo,archive.entries);await rebuild();
 await assert.rejects(verifyPublication(args),/index must link every/);
}));
test('fully rebuilt source cannot append arbitrary public files',()=>fixture(async({args,rebuild})=>{
 await put(args.repo,'public/private-export.json','unrequested data');const archive=JSON.parse(await readFile(resolve(args.repo,'public/manifest.json')));await manifest(args.repo,archive.entries);await rebuild();
 await assert.rejects(verifyPublication(args),/Unsupported archive public path/);
}));
test('fully rebuilt source cannot add an unversioned or orphan report beside the target',()=>fixture(async({args,rebuild})=>{
 await put(args.repo,'public/reports/unversioned/index.html','orphan');const archive=JSON.parse(await readFile(resolve(args.repo,'public/manifest.json')));await manifest(args.repo,archive.entries);await rebuild();
 await assert.rejects(verifyPublication(args),/lacks a new sealed entry/);
}));
test('ordinary proof does not certify an existing edition as a newly appended report',()=>fixture(async({args})=>{
 args.registry.global_delivery.site_runtime_profile.site_source_commit_sha=args.expectedCommit;
 await assert.rejects(verifyPublication(args),/newly appended report/);
}));

test('build source receipt cannot omit vendor, component, hook or root configuration inputs',()=>fixture(async({args,amendReceipt})=>{
 const original=await readFile(args.buildReceipt);
 for(const path of ['vendor/synthetic.txt','components/synthetic.txt','hooks/synthetic.txt','.npmrc','components.json','cloudflare-env.d.ts','eslint.config.mjs']){
  await writeFile(args.buildReceipt,original);await amendReceipt(r=>r.inputs=r.inputs.filter(f=>f.path!==path));
  await assert.rejects(verifyPublication(args),/input receipt differs/);
 }
}));

test('retained archive browser, search form and reading pages survive static migration and daily append',async()=>{
 for(const staticBaseline of [false,true])await fixture(async({args})=>{
  const proof=await verifyPublication({...args,migrationVerification:staticBaseline});
  for(const path of ['assets/archive-browser.js','archive/index.html','reports/old/reading.html'])assert.ok(proof.archive_files.some(f=>f.path===path));
 },{staticBaseline,archiveUi:true});
});
test('UI path allowance remains exact',()=>{
 for(const path of ['assets/archive-browser.js','archive/index.html','reports/edition/reading.html'])assert.equal(archivePathAllowed(path),true);
 for(const path of ['assets/other.js','archive/other.html','archive/nested/index.html','examples/edition/reading.html','reports/edition/script.js'])assert.equal(archivePathAllowed(path),false);
});
test('retained archive UI rejects new scripts, forms, handlers and form submission overrides',()=>{
 const valid=archiveUiIndex('<a href="reports/old/index.html">old</a>');assert.ok(inspectArchiveIndex(valid,{archiveUi:true}).links.has('reports/old/index.html'));
 const mutations=[
  html=>html.replace('/assets/archive-browser.js','https://example.invalid/evil.js'),
  html=>html.replace(' defer>',' defer async>'),
  html=>html.replace('</script>','alert(1)</script>'),
  html=>html.replace('</body>','<script>alert(1)</script></body>'),
  html=>html.replace('<form ','<form action="https://example.invalid" '),
  html=>html.replace('<form ','<form method="post" '),
  html=>html.replace('<button type="submit"','<button formaction="https://example.invalid" type="submit"'),
  html=>html.replace('<input ','<input onfocus="alert(1)" '),
  html=>html.replace('</body>','<form></form></body>'),
  html=>html.replace('href="reports/old/index.html"','href="java&#x09;script:alert(1)"'),
  html=>html.replace('<div id="archive-results">','<div id="archive-status">')
 ];
 for(const mutate of mutations)assert.throws(()=>inspectArchiveIndex(mutate(valid),{archiveUi:true}),/validation failed/);
 assert.throws(()=>inspectArchiveIndex(valid),/validation failed/);
});
test('fully rebuilt proof rejects mutated historical JS and reading pages',async()=>{
 for(const path of ['assets/archive-browser.js','reports/old/reading.html','archive/index.html'])await fixture(async({args,rebuild})=>{
  await put(args.repo,'public/'+path,'changed historical bytes');const archive=JSON.parse(await readFile(resolve(args.repo,'public/manifest.json')));await manifest(args.repo,archive.entries);await rebuild();
  await assert.rejects(verifyPublication(args),/Previously published archive bytes/);
 },{archiveUi:true});
});
test('fully rebuilt proof rejects injected active markup and lost UI controls',async()=>{
 for(const mutate of [html=>html.replace('<form ','<form action="https://example.invalid" '),html=>html.replace('<article>','<article onclick="alert(1)">'),html=>html.replace('id="archive-query" type="search"','id="archive-query" type="text"')])await fixture(async({args,rebuild})=>{
  const path=resolve(args.repo,'public/index.html');await writeFile(path,mutate(await readFile(path,'utf8')));const archive=JSON.parse(await readFile(resolve(args.repo,'public/manifest.json')));await manifest(args.repo,archive.entries);await rebuild();
  await assert.rejects(verifyPublication(args),/validation failed|controls or behavior/);
 },{archiveUi:true});
});
test('ordinary proof cannot introduce the allowed JS filename as new executable content',()=>fixture(async({args,rebuild})=>{
 await put(args.repo,'public/assets/archive-browser.js','unapproved script');const archive=JSON.parse(await readFile(resolve(args.repo,'public/manifest.json')));await manifest(args.repo,archive.entries);await rebuild();
 await assert.rejects(verifyPublication(args),/New archive UI files/);
}));
