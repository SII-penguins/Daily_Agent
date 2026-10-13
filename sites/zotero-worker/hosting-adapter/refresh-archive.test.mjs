import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,mkdir,writeFile,readFile,cp,symlink,truncate,rename,rm,readdir} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {resolve,dirname} from 'node:path';
import {fileURLToPath} from 'node:url';
import {createHash} from 'node:crypto';
import {spawnSync} from 'node:child_process';
import {readArchive,refreshArchive} from './refresh-archive.mjs';
import {navigationFixture,writeTree,cloneTree,rehashManifest,setManifest,metadata} from './fixtures/navigation-test-fixture.mjs';
import {issueJson,issueHash} from '../src/reviewed-issues.mjs';
import {appendReviewedIssue} from './append-reviewed-issue.mjs';
import {loadReviewedIssues} from './reviewed-catalogs.mjs';
import {composeNavigationAppend} from './compose-navigation-append.mjs';
const sha=b=>createHash('sha256').update(b).digest('hex');
const json=(p,v)=>writeFile(p,JSON.stringify(v)+'\n');
const old={key:'reports/2026-10-09',page:'reports/2026-10-09/index.html',date:'2026-10-09',category:'reports',identity:'a'.repeat(64),source_sha256:'b'.repeat(64),approval_sha256:'c'.repeat(64)};
const freshHtml='<html><p>2026-10-10</p></html>',freshHash=sha(Buffer.from(freshHtml));
const fresh={key:'reports/2026-10-10-reviewed-01-'+ freshHash.slice(0,12),page:'reports/2026-10-10-reviewed-01-'+ freshHash.slice(0,12)+'/index.html',date:'2026-10-10',category:'reports',identity:freshHash,source_sha256:freshHash,approval_sha256:'f'.repeat(64)};
const archiveUiIndex=links=>`<html><head><script src="/assets/archive-browser.js" defer></script></head><body><form id="archive-filters" class="archive-filters" hidden role="search" aria-label="查找往期日报"><input id="archive-query" type="search" name="q"><select id="archive-year" name="year"></select><select id="archive-month" name="month"></select><input id="archive-date" type="date" name="date"><button type="submit">搜索</button><button id="archive-reset" type="button">重置筛选</button></form><div id="archive-days"><div id="archive-dynamic" hidden><p id="archive-status" aria-live="polite"></p><div id="archive-results"></div><nav id="archive-pagination"></nav></div><div id="archive-static">${links}</div></div></body></html>`;
async function changeArchiveFile(root,name,data){const bytes=Buffer.from(data),path=resolve(root,name);await mkdir(dirname(path),{recursive:true});await writeFile(path,bytes);const m=JSON.parse(await readFile(resolve(root,'manifest.json'),'utf8'));m.files[name]={size:bytes.length,sha256:sha(bytes)};await json(resolve(root,'manifest.json'),m);}
async function recommit(f){for(const args of [['add','.'],['-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-qm','approved fixture archive'],['rev-parse','HEAD']]){const r=spawnSync('git',['-C',f.siteDir,...args],{encoding:'utf8'});assert.equal(r.status,0,r.stderr);if(args[0]==='rev-parse')f.expectedCommit=r.stdout.trim();}const r=JSON.parse(await readFile(f.registryFile,'utf8'));r.global_delivery.site_runtime_profile.site_source_commit_sha=f.expectedCommit;await json(f.registryFile,r);}
async function uiFixture(){const f=await fixture(),oldIndex=archiveUiIndex(`<article><a href="${old.page}">old</a></article>`);for(const root of [resolve(f.siteDir,'public'),f.archiveDir]){await changeArchiveFile(root,'assets/archive-browser.js','/* approved immutable search behavior and dataset */');await changeArchiveFile(root,'archive/index.html',oldIndex);await changeArchiveFile(root,old.key+'/reading.html','immutable reading edition');await changeArchiveFile(root,'index.html',root===f.archiveDir?archiveUiIndex(`<article><a href="${old.page}">old</a></article><article><a href="${fresh.page}">new</a></article>`):oldIndex);}await recommit(f);return f;}
async function archive(dir,entries,omitLinks=[]){await mkdir(dir,{recursive:true});const files={};for(const e of entries){const bytes=Buffer.from('<html><p>'+e.date+'</p></html>');await mkdir(resolve(dir,e.key),{recursive:true});await writeFile(resolve(dir,e.page),bytes);files[e.page]={size:bytes.length,sha256:sha(bytes)};}const index=Buffer.from('<html>'+entries.filter(e=>!omitLinks.includes(e.key)).map(e=>`<a href="${e.page}">Report</a>`).join('')+'</html>');await writeFile(resolve(dir,'index.html'),index);files['index.html']={size:index.length,sha256:sha(index)};await json(resolve(dir,'manifest.json'),{schema_version:1,entries,files});}
async function fixture(){const dir=await mkdtemp(resolve(tmpdir(),'archive-refresh-test-')),site=resolve(dir,'site'),input=resolve(dir,'input');await mkdir(site);for(const d of ['zotero','build','app','components','hooks','vendor','lib','db','drizzle','scripts','.openai','.sites-runtime']){await mkdir(resolve(site,d));if(!d.startsWith('.'))await writeFile(resolve(site,d,'retained.txt'),d+' unchanged');}for(const f of ['package.json','package-lock.json','vite.config.ts','next.config.ts','drizzle.config.ts','tsconfig.json','postcss.config.mjs','cloudflare-env.d.ts','next-env.d.ts','components.json','.npmrc','eslint.config.mjs'])await writeFile(resolve(site,f),'{}');await writeFile(resolve(site,'.gitignore'),'.sites-runtime/\n');const hosting={project_id:'test-site',d1:'DB',r2:'BUCKET'};await json(resolve(site,'.openai/hosting.json'),hosting);await archive(resolve(site,'public'),[old]);await archive(input,[old,fresh]);const git=args=>{const r=spawnSync('git',['-C',site,...args],{encoding:'utf8'});assert.equal(r.status,0,r.stderr);return r.stdout.trim();};git(['init','-q']);git(['-c','user.name=Fixture','-c','user.email=fixture@example.invalid','add','.']);git(['-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','-qm','fixture']);const expectedCommit=git(['rev-parse','HEAD']),registryFile=resolve(dir,'registry.json');await json(registryFile,{schema:'daily-agent-future-operations-registry-v1',global_delivery:{audience:'owner_private',append_archive_entries_only:true,site_runtime_profile:{schema:'daily-agent-site-runtime-profile-v1',mode:'worker-assets',state:'active',project_id:'test-site',site_source_commit_sha:expectedCommit,version_id:'version-test',deployment_id:'deployment-test'}}});return {dir,siteDir:site,archiveDir:input,registryFile,projectId:'test-site',expectedCommit};}
test('dry run and append preserve protected source and every legacy edition',async()=>{const f=await fixture(),before=await readFile(resolve(f.siteDir,'public',old.page));const dry=await refreshArchive(f);assert.equal(dry.dry_run,true);assert.equal(dry.new_entries,1);assert.equal((await readArchive(resolve(f.siteDir,'public'))).entries.size,1);const result=await refreshArchive({...f,apply:true});assert.equal(result.protected_unchanged,true);assert.deepEqual(await readFile(resolve(f.siteDir,'public',old.page)),before);assert.equal((await readArchive(resolve(f.siteDir,'public'))).entries.size,2);assert.equal(await readFile(resolve(f.siteDir,'zotero/retained.txt'),'utf8'),'zotero unchanged');});
test('old index links cannot disappear',async()=>{const f=await fixture();await archive(f.archiveDir,[old,fresh],[old.key]);await assert.rejects(refreshArchive(f),/link every/);});
test('old entry metadata cannot change',async()=>{const f=await fixture();await archive(f.archiveDir,[{...old,audit:'changed'},fresh]);await assert.rejects(refreshArchive(f),/metadata/);});
test('new date-only or mismatched hash edition is blocked',async()=>{const f=await fixture();const changed={...fresh,key:'reports/2026-10-10',page:'reports/2026-10-10/index.html'};await archive(resolve(f.dir,'bad'),[old,changed]);await assert.rejects(refreshArchive({...f,archiveDir:resolve(f.dir,'bad')}),/canonical/);});
test('static downgrade and pending migration are blocked',async()=>{const f=await fixture();await json(resolve(f.siteDir,'.openai/hosting.json'),{project_id:'test-site',static:{directory:'dist'}});await assert.rejects(refreshArchive(f));});
test('fresh head mismatch and dirty source are blocked',async()=>{const f=await fixture();await assert.rejects(refreshArchive({...f,expectedCommit:'0'.repeat(40)}),/commit/);await writeFile(resolve(f.siteDir,'zotero/retained.txt'),'mutation');await assert.rejects(refreshArchive(f),/clean/);});
test('interrupted refresh is fenced',async()=>{const f=await fixture();await json(resolve(f.siteDir,'.sites-runtime/archive-refresh-pending.json'),{state:'pending'});await assert.rejects(refreshArchive(f),/interrupted/);});
test('archive symlinks and oversized sparse files are rejected',async()=>{const f=await fixture();await symlink(resolve(f.archiveDir,'index.html'),resolve(f.archiveDir,'leak'));await assert.rejects(readArchive(f.archiveDir),/Symlinks/);const separate=resolve(f.dir,'oversized');await archive(separate,[old]);await truncate(resolve(separate,old.page),25*1024*1024+1);await assert.rejects(readArchive(separate),/oversized/);});
test('reserved and encoded asset paths are rejected',async()=>{const f=await fixture();await mkdir(resolve(f.archiveDir,'zotero'));await writeFile(resolve(f.archiveDir,'zotero/index.html'),'no');await assert.rejects(readArchive(f.archiveDir),/Unsupported/);});

test('missing Worker marker and explicitly pending profile are rejected',async()=>{const f=await fixture(),r=JSON.parse(await readFile(f.registryFile,'utf8'));r.global_delivery.site_runtime_profile.state='migration_pending';await json(f.registryFile,r);await assert.rejects(refreshArchive(f),/migration_pending/);delete r.global_delivery.site_runtime_profile;await json(f.registryFile,r);await json(resolve(f.siteDir,'.openai/hosting.json'),{project_id:'test-site',static:{directory:'dist'}});await assert.rejects(refreshArchive(f),/registered Worker/);});
test('rewriting a historical page with a matching new manifest is rejected',async()=>{const f=await fixture(),p=resolve(f.archiveDir,old.page),b=Buffer.from('replacement');await writeFile(p,b);const m=JSON.parse(await readFile(resolve(f.archiveDir,'manifest.json'),'utf8'));m.files[old.page]={size:b.length,sha256:sha(b)};await json(resolve(f.archiveDir,'manifest.json'),m);await assert.rejects(refreshArchive(f),/Existing archive edition/);});
test('runtime-state symlink cannot redirect the local transaction',async()=>{const f=await fixture();await rename(resolve(f.siteDir,'.sites-runtime'),resolve(f.dir,'external-runtime'));await symlink(resolve(f.dir,'external-runtime'),resolve(f.siteDir,'.sites-runtime'));await assert.rejects(refreshArchive(f),/symlinks/);});
test('actual build wrapper blocks a pending refresh before starting framework',async()=>{const f=await fixture();await json(resolve(f.siteDir,'.sites-runtime/archive-refresh-pending.json'),{state:'pending'});const wrapper=fileURLToPath(new URL('./template/scripts/build-zotero-site.mjs',import.meta.url));const r=spawnSync(process.execPath,[wrapper],{cwd:f.siteDir,encoding:'utf8'});assert.notEqual(r.status,0);assert.match(r.stderr,/Interrupted archive refresh/);});

test('daily append retains approved archive UI and every immutable reading/browser byte',async()=>{
 const f=await uiFixture(),before=await readArchive(resolve(f.siteDir,'public'));const result=await refreshArchive({...f,apply:true});assert.equal(result.new_entries,1);
 const after=await readArchive(resolve(f.siteDir,'public'));assert.equal(after.entries.size,2);assert.deepEqual(after.ui_signature,before.ui_signature);
 for(const path of ['assets/archive-browser.js','archive/index.html',old.key+'/reading.html'])assert.deepEqual(after.files.find(x=>x.path===path),before.files.find(x=>x.path===path));
});
test('matching updated manifest cannot authorize historical browser or reading mutations',async()=>{
 for(const path of ['assets/archive-browser.js',old.key+'/reading.html']){const f=await uiFixture();await changeArchiveFile(f.archiveDir,path,'changed immutable content');await assert.rejects(refreshArchive(f),/Existing archive edition or asset/);}
});
test('refresh refuses active index markup and changed or missing UI controls',async()=>{
 const mutations=[html=>html.replace('<form ','<form action="https://example.invalid" '),html=>html.replace('<article>','<article onmouseover="alert(1)">'),html=>html.replace('/assets/archive-browser.js','/assets/arbitrary.js'),html=>html.replace('id="archive-query" type="search"','id="archive-query" type="text"'),()=>`<a href="${old.page}">old</a><a href="${fresh.page}">new</a>`];
 for(const mutate of mutations){const f=await uiFixture(),html=await readFile(resolve(f.archiveDir,'index.html'),'utf8');await changeArchiveFile(f.archiveDir,'index.html',mutate(html));await assert.rejects(refreshArchive(f),/validation failed|controls or behavior/);}
});
test('archive UI allowances do not permit arbitrary or newly introduced JS',async()=>{
 const f=await fixture();await changeArchiveFile(f.archiveDir,'assets/other.js','alert(1)');await assert.rejects(readArchive(f.archiveDir),/Unsupported/);
 const g=await fixture();await changeArchiveFile(g.archiveDir,'assets/archive-browser.js','unapproved script');await assert.rejects(refreshArchive(g),/New archive UI files/);
});
test('legacy missing approval is retained exactly but never accepted for new editions',async()=>{
 const f=await fixture();for(const root of [resolve(f.siteDir,'public'),f.archiveDir]){const path=resolve(root,'manifest.json'),m=JSON.parse(await readFile(path,'utf8'));delete m.entries[0].approval_sha256;await json(path,m);}await recommit(f);assert.equal((await refreshArchive(f)).new_entries,1);
 const path=resolve(f.archiveDir,'manifest.json'),m=JSON.parse(await readFile(path,'utf8'));delete m.entries[1].approval_sha256;await json(path,m);await assert.rejects(refreshArchive(f),/requires a sealed approval/);
});

async function mappedFixture({append=true}={}){
 const f=await fixture(),navigation=await navigationFixture({append});
 await rm(resolve(f.siteDir,'public'),{recursive:true});await writeTree(resolve(f.siteDir,'public'),navigation.before);
 await rm(f.archiveDir,{recursive:true});await writeTree(f.archiveDir,navigation.after);
 for(const issue of append?navigation.priorIssues:navigation.issues){const path=resolve(f.siteDir,'zotero/reviewed-issues',issue.issue_id+'.json');await mkdir(dirname(path),{recursive:true});await writeFile(path,issueJson(issue)+'\n');}
 await recommit(f);f.navigationProjections=[];
 if(append){const path=resolve(f.dir,'prepared-projection.json');await writeFile(path,issueJson(navigation.fresh.issue)+'\n');f.navigationProjections.push({path,projectionSha256:navigation.fresh.issue.projection_sha256});}
 return {...f,navigation};
}
test('refresh accepts only exact current remap from already compiled canonical projection',async()=>{
 const f=await mappedFixture({append:false}),result=await refreshArchive({...f,apply:true});assert.equal(result.new_entries,0);assert.equal(result.navigation_proof.exact_reverse_verified,true);for(const [path,b]of f.navigation.after)assert.deepEqual(await readFile(resolve(f.siteDir,'public',path)),b);
});
test('two-date refresh validates prepared pins then ordinary catalog transaction compiles same issue',async()=>{
 const f=await mappedFixture(),result=await refreshArchive({...f,apply:true});assert.equal(result.new_entries,1);assert.equal(result.navigation_proof.historical_reading_output_reused,true);assert.equal((await loadReviewedIssues(f.siteDir)).length,1);
 const registry=JSON.parse(await readFile(f.registryFile));await appendReviewedIssue({siteDir:f.siteDir,registry,expectedCommit:f.expectedCommit,projection:f.navigation.fresh.issue,expectedProjectionHash:f.navigation.fresh.issue.projection_sha256,apply:true});assert.equal((await loadReviewedIssues(f.siteDir)).length,2);
 for(const [path,b]of f.navigation.before)if(!['index.html','archive/index.html','assets/archive-browser.js','manifest.json'].includes(path))assert.deepEqual(await readFile(resolve(f.siteDir,'public',path)),b);
});
test('refresh rejects absent, altered and retroactive prepared navigation pins',async()=>{
 const f=await mappedFixture();await assert.rejects(refreshArchive({...f,navigationProjections:[]}),/Every new report/);
 await assert.rejects(refreshArchive({...f,navigationProjections:[{...f.navigationProjections[0],projectionSha256:'0'.repeat(64)}]}),/exact hash pin/);
 await writeFile(f.navigationProjections[0].path,issueJson(f.navigation.old.issue)+'\n');await assert.rejects(refreshArchive({...f,navigationProjections:[{path:f.navigationProjections[0].path,projectionSha256:f.navigation.old.issue.projection_sha256}]}),/newly sealed normal/);
});
test('refresh rejects a legacy prepared issue and noncanonical prepared file',async()=>{
 const f=await mappedFixture(),issue={...f.navigation.fresh.issue,legacy_seal:{kind:'legacy-sealed-migration-v1'}};
 await writeFile(f.navigationProjections[0].path,issueJson(issue)+'\n');await assert.rejects(refreshArchive(f),/newly sealed normal/);
 await writeFile(f.navigationProjections[0].path,JSON.stringify(f.navigation.fresh.issue,null,2)+'\n');await assert.rejects(refreshArchive(f),/Canonical prepared/);
});
test('mapped refresh rejects changed historical HTML, JS, entry and unbound mapping even with repaired manifests',async()=>{
 for(const mutation of ['old-html','js-tail','old-entry','mapping']){
  const f=await mappedFixture(),files=cloneTree(f.navigation.after);
  if(mutation==='old-html')files.set(f.navigation.old.entry.key+'/reading.html',Buffer.from('altered historical HTML'));
  if(mutation==='js-tail')files.set('assets/archive-browser.js',Buffer.concat([files.get('assets/archive-browser.js'),Buffer.from('\nalert(1)')]));
  if(mutation==='old-entry'){const m=JSON.parse(files.get('manifest.json'));m.entries[0].audit='tampered';setManifest(files,m);}
  if(mutation==='mapping')for(const path of ['index.html','archive/index.html','assets/archive-browser.js','manifest.json'])files.set(path,Buffer.from(files.get(path).toString().replaceAll(f.navigation.mappings[1].to,f.navigation.mappings[1].to+'-uncompiled')));
  rehashManifest(files);await writeTree(f.archiveDir,files);await assert.rejects(refreshArchive(f),/Unapproved composed|behavior changed|entry metadata|Navigation row/);
 }
});
test('mapped refresh fails closed on an existing catalog transaction fence',async()=>{
 const f=await mappedFixture();await json(resolve(f.siteDir,'.sites-runtime/catalog-append-pending.json'),{state:'pending'});await assert.rejects(refreshArchive(f),/interrupted catalog/);
});
test('stage-only tamper is rejected and leaves the existing transaction fence',async()=>{
 const f=await mappedFixture(),candidate=cloneTree(f.navigation.after),m=JSON.parse(candidate.get('manifest.json'));
 for(let i=0;i<250;i++){const bytes=Buffer.from('synthetic staging asset '+i+' '.repeat(4096)),path='assets/'+sha(bytes)+'.png';candidate.set(path,bytes);m.files[path]=metadata(bytes);}setManifest(candidate,m);
 const composed=composeNavigationAppend({baseline:f.navigation.before,candidate,approvedEntries:[f.navigation.fresh.entry],mappings:f.navigation.mappings});await writeTree(f.archiveDir,composed.files);
 let finished=false,tampered=false;
 const watcher=(async()=>{while(!finished){for(const dir of await readdir(resolve(f.siteDir,'.sites-runtime'))){if(!dir.startsWith('archive-refresh-')||dir.endsWith('.json'))continue;const path=resolve(f.siteDir,'.sites-runtime',dir,'next-public/archive/index.html');try{const bytes=await readFile(path);await writeFile(path,Buffer.concat([bytes,Buffer.from('unapproved staged text')]));tampered=true;return;}catch(e){if(e.code!=='ENOENT')throw e;}}await new Promise(r=>setTimeout(r,1));}})();
 try{await assert.rejects(refreshArchive({...f,apply:true}),/manifest|staging|composed|Archive/);}finally{finished=true;await watcher;}
 assert.equal(tampered,true);assert.equal(JSON.parse(await readFile(resolve(f.siteDir,'.sites-runtime/archive-refresh-pending.json'))).state,'pending');assert.deepEqual(await readFile(resolve(f.siteDir,'public/index.html')),f.navigation.before.get('index.html'));
});
