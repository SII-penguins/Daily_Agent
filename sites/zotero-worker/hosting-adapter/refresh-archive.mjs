/** Append reviewed archive bytes to an existing private Worker source checkout.
 * Offline only; default is read-only. Never creates a Site or publishes.
 */
import { readFile, writeFile, readdir, lstat, realpath, mkdir, cp, rename, unlink, open } from 'node:fs/promises';
import { resolve, relative, dirname, sep, isAbsolute } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createHash, randomUUID } from 'node:crypto';
import { spawnSync } from 'node:child_process';
import { guardPublication, ARCHIVE_UI_FILES, archivePathAllowed, inspectArchiveIndex, reviewedNavigationMappings, orderedNavigationTree } from './publication-proof.mjs';
import { boundedFile, loadReviewedIssues, verifyIssueAgainstArchive } from './reviewed-catalogs.mjs';
import { issueJson } from '../src/reviewed-issues.mjs';
import { verifyNavigationDelta } from './reading-navigation.mjs';
import { verifyNavigationAppend } from './compose-navigation-append.mjs';
const sha=bytes=>createHash('sha256').update(bytes).digest('hex');
const inside=(root,child)=>{const p=relative(root,child);return !p||(!p.startsWith('..'+sep)&&p!=='..'&&!isAbsolute(p));};
const canonical=value=>JSON.stringify(value,(_,v)=>v&&typeof v==='object'&&!Array.isArray(v)?Object.fromEntries(Object.keys(v).sort().map(k=>[k,v[k]])):v);
const pathAllowed=archivePathAllowed;
const fail=message=>{throw new Error(message);};
async function exists(path){try{await lstat(path);return true;}catch(e){if(e.code==='ENOENT')return false;throw e;}}
async function regularRoot(path){const stat=await lstat(path);if(!stat.isDirectory()||stat.isSymbolicLink())fail('Directory symlinks are forbidden');return realpath(path);}
async function inventory(path,prefix='',options={}){
  const result=[];options.count??=0;
  for(const e of(await readdir(path,{withFileTypes:true})).sort((a,b)=>a.name.localeCompare(b.name,'en'))){
    const name=prefix+e.name,target=resolve(path,e.name),stat=await lstat(target);
    if(stat.isSymbolicLink()||(!stat.isFile()&&!stat.isDirectory()))fail('Symlinks and special files are forbidden');
    if(stat.isDirectory()){
      if(options.archive&&!/^(?:reports|examples)(?:\/[A-Za-z0-9][A-Za-z0-9._-]*)?$|^(?:assets|archive)$/.test(name))fail('Unsupported archive directory');
      result.push(...await inventory(target,name+'/',options));
    }else{
      if(options.archive&&name==='.export.lock')continue;
      if(options.archive&&(!pathAllowed(name)||++options.count>20000||stat.size>25*1024*1024||(name==='manifest.json'&&stat.size>4*1024*1024)))fail('Unsupported or oversized archive file');
      const bytes=await readFile(target);if(bytes.length!==stat.size)fail('File changed while reading');
      result.push({path:name,bytes:bytes.length,sha256:sha(bytes)});
    }
  }return result;
}
function currentHead(site,expected){
  if(!/^[a-f0-9]{40}$/.test(expected??''))fail('A freshly observed exact Site source commit is required');
  const git=args=>{const child=spawnSync('git',['-C',site,...args],{encoding:'utf8',timeout:10000});if(child.error||child.status!==0)fail('Private Site Git source cannot be verified');return child.stdout.trim();};
  if(resolve(git(['rev-parse','--show-toplevel']))!==site||git(['rev-parse','HEAD'])!==expected)fail('Private Site checkout differs from the freshly observed exact commit');
  if(git(['status','--porcelain','--untracked-files=all']))fail('Private Site source must be clean before appending an archive');
}
export async function readArchive(path){
  const root=await regularRoot(path);
  const files=await inventory(root,'',{archive:true});
  if(files.length>20000||files.some(f=>!pathAllowed(f.path)||f.bytes>25*1024*1024))fail('Archive contains an unsupported path, oversized asset or too many files');
  const bytes=await readFile(resolve(root,'manifest.json'));if(bytes.length>4*1024*1024)fail('Archive manifest exceeds the conservative 4 MiB guard limit');
  const manifest=JSON.parse(bytes);
  if(manifest.schema_version!==1||!manifest.files||Array.isArray(manifest.files)||!Array.isArray(manifest.entries))fail('Invalid archive manifest');
  const actual=new Map(files.map(f=>[f.path,f]));
  const declared=Object.keys(manifest.files).sort();
  if(canonical(declared)!==canonical(files.map(f=>f.path).filter(p=>p!=='manifest.json').sort()))fail('Archive files do not match the exact manifest');
  if(!actual.has('index.html'))fail('Missing archive index');
  for(const [name,metadata]of Object.entries(manifest.files)){
    if(!pathAllowed(name)||name==='manifest.json'||!metadata||!/^[a-f0-9]{64}$/.test(metadata.sha256)||!Number.isSafeInteger(metadata.size)||metadata.size<0)fail('Invalid archive file metadata');
    const file=actual.get(name);if(name.startsWith('assets/')&&name!=='assets/archive-browser.js'&&name!==`assets/${file.sha256}.png`)fail('Asset filename must be its content hash');if(file.sha256!==metadata.sha256||file.bytes!==metadata.size)fail('Archive bytes fail their manifest');
  }
  const entries=new Map();for(const entry of manifest.entries){
    // Some already-published editions predate approval_sha256. Their exact
    // records remain immutable; refreshArchive requires the seal for new ones.
    if(!entry||typeof entry.key!=='string'||entries.has(entry.key)||entry.page!==entry.key+'/index.html'||!actual.has(entry.page)||!/^\d{4}-\d{2}-\d{2}$/.test(entry.date)||!['reports','examples'].includes(entry.category)||!entry.key.startsWith(entry.category+'/')||!/^[a-f0-9]{64}$/.test(entry.identity)||!/^[a-f0-9]{64}$/.test(entry.source_sha256)||(Object.hasOwn(entry,'approval_sha256')&&!/^[a-f0-9]{64}$/.test(entry.approval_sha256)))fail('Invalid or duplicate sealed archive entry');
    entries.set(entry.key,entry);
  }
  const archiveUi=actual.has('assets/archive-browser.js'),index=inspectArchiveIndex(await readFile(resolve(root,'index.html')),{archiveUi});for(const entry of entries.values())if(!index.links.has(entry.page))fail('Archive index must link every retained and new entry');
  if(actual.has('archive/index.html'))inspectArchiveIndex(await readFile(resolve(root,'archive/index.html')),{archiveUi});
  return {root,files,manifest,entries,ui_signature:index.ui_signature,manifest_sha256:sha(bytes)};
}
async function protectedFiles(site){
  const rows=[];for(const directory of ['zotero','build','app','components','hooks','vendor','lib','db','drizzle','scripts']){
    await regularRoot(resolve(site,directory));
    rows.push(...(await inventory(resolve(site,directory))).map(f=>({...f,path:directory+'/'+f.path})));
  }
  for(const name of ['.openai/hosting.json','package.json','package-lock.json','vite.config.ts','next.config.ts','drizzle.config.ts','tsconfig.json','postcss.config.mjs','cloudflare-env.d.ts','components.json','.npmrc','eslint.config.mjs']){const bytes=await readFile(resolve(site,name));rows.push({path:name,bytes:bytes.length,sha256:sha(bytes)});}
  return rows;
}
async function archiveTree(archive){
  const files=new Map();
  for(const file of archive.files){
    const bytes=await boundedFile(resolve(archive.root,file.path),25*1024*1024);
    if(bytes.length!==file.bytes || sha(bytes)!==file.sha256)fail('Archive changed while deriving navigation proof');
    files.set(file.path,bytes);
  }
  return orderedNavigationTree(files);
}
/** Prepared projection pins are the only pre-append mapping input. The existing
 * appendReviewedIssue transaction must compile these exact canonical files after
 * refresh; final publication independently binds them to the native artifact.
 */
async function refreshNavigationMappings({site,old,next,navigationProjections}){
  if(!Array.isArray(navigationProjections)||navigationProjections.length>100)fail('Bounded prepared navigation projection pins required');
  const issues=await loadReviewedIssues(site);
  for(const pin of navigationProjections){
    if(!pin || Object.keys(pin).sort().join(',')!=='path,projectionSha256' || typeof pin.path!=='string' || !/^[a-f0-9]{64}$/.test(pin.projectionSha256??''))fail('Explicit prepared projection path/hash pin required');
    const bytes=await boundedFile(pin.path),issue=JSON.parse(bytes);
    if(bytes.toString()!==issueJson(issue)+'\n' || issue.projection_sha256!==pin.projectionSha256)fail('Canonical prepared projection bytes and exact hash pin required');
    if(issue.legacy_seal || old.manifest.entries.some(e=>e.page===issue.entry_path))fail('Prepared projection must belong to a newly sealed normal archive edition');
    await verifyIssueAgainstArchive(issue,next.manifest,path=>boundedFile(resolve(next.root,path),16*1024*1024));
    issues.push(issue);
  }
  return reviewedNavigationMappings({issues,manifest:next.manifest,readHtml:path=>boundedFile(resolve(next.root,path),16*1024*1024)});
}
export async function refreshArchive({siteDir,archiveDir,registryFile,projectId,expectedCommit,navigationProjections=[],apply=false}){
  const site=await regularRoot(siteDir),sourceRoot=await realpath(fileURLToPath(new URL('../../../',import.meta.url)));
  if(inside(sourceRoot,site))fail('Private Site source must be outside the public source snapshot');
  const input=await regularRoot(archiveDir);if(inside(site,input)||inside(input,site))fail('Archive input must be separate from the Site checkout');
  const registry=JSON.parse(await readFile(registryFile,'utf8')),hosting=JSON.parse(await readFile(resolve(site,'.openai/hosting.json'),'utf8'));
  if(guardPublication({registry,hosting,projectId}).mode!=='worker-assets')fail('Archive refresh requires the registered Worker profile');
  if(await exists(resolve(site,'.sites-runtime')))await regularRoot(resolve(site,'.sites-runtime'));
  currentHead(site,expectedCommit);
  const pending=resolve(site,'.sites-runtime/archive-refresh-pending.json');if(await exists(pending))fail('An interrupted archive refresh is fenced; reconcile its saved transaction before publishing');
  if(await exists(resolve(site,'.sites-runtime/catalog-append-pending.json')))fail('An interrupted catalog append must be reconciled before archive refresh');
  const old=await readArchive(resolve(site,'public')),next=await readArchive(input);
  const observed=new Map(next.files.map(f=>[f.path,f]));
  for(const [key,entry]of old.entries){if(!next.entries.has(key)||canonical(next.entries.get(key))!==canonical(entry))fail('Existing archive entry metadata would be changed or removed');}
  if(canonical(old.ui_signature)!==canonical(next.ui_signature))fail('Existing archive UI controls or behavior would change');
  const added=next.files.filter(f=>!old.files.some(p=>p.path===f.path));
  const addedEntries=[...next.entries.keys()].filter(key=>!old.entries.has(key));
  for(const key of addedEntries){const entry=next.entries.get(key);const html=observed.get(entry.page);if(!/^[a-f0-9]{64}$/.test(entry.approval_sha256))fail('New archive edition requires a sealed approval SHA256');if(entry.identity!==entry.source_sha256||entry.source_sha256!==html.sha256)fail('New edition identity/source must equal the exact reviewed HTML SHA256');const prefix=entry.category+'/'+entry.date+'-';if(!key.startsWith(prefix)||!key.endsWith('-'+entry.identity.slice(0,12))||!/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(key.slice(prefix.length,-13)))fail('New archive editions require DATE-EDITION-IDENTITY12 canonical paths');}
  for(const file of added){if(ARCHIVE_UI_FILES.includes(file.path))fail('New archive UI files require separate approval; only retained UI is permitted');if(file.path.startsWith('reports/')||file.path.startsWith('examples/')){if(!addedEntries.some(key=>file.path.startsWith(key+'/')))fail('Added report files lack a new sealed entry');}}
  const navigationMappings=await refreshNavigationMappings({site,old,next,navigationProjections});
  let navigationProof=null,beforeTree;
  const proveNavigation=after=>addedEntries.length
    ? verifyNavigationAppend({baseline:beforeTree,candidate:after,approvedEntries:addedEntries.map(key=>next.entries.get(key)),mappings:navigationMappings,after})
    : verifyNavigationDelta({before:beforeTree,after,mappings:navigationMappings,requireChange:false});
  if(navigationMappings.length){beforeTree=await archiveTree(old);navigationProof=proveNavigation(await archiveTree(next));}
  for(const file of old.files){
    if(['index.html','manifest.json'].includes(file.path) || (navigationProof && ARCHIVE_UI_FILES.includes(file.path)))continue;
    const candidate=observed.get(file.path);if(!candidate||candidate.bytes!==file.bytes||candidate.sha256!==file.sha256)fail('Existing archive edition or asset would be changed or removed');
  }
  const protectedBefore=await protectedFiles(site);
  const result={profile:'worker-assets',parent_site_source_commit_sha:expectedCommit,prior_archive_manifest_sha256:old.manifest_sha256,dry_run:!apply,old_archive_files:old.files.length,new_archive_files:next.files.length,new_entries:addedEntries.length,added_files:added.length,archive_manifest_sha256:next.manifest_sha256,existing_archive_unchanged:true,navigation_proof:navigationProof,services_changed:false,deployed:false};
  if(!apply)return result;
  await mkdir(resolve(site,'.sites-runtime'),{recursive:true});
  const transaction=resolve(site,'.sites-runtime',`archive-refresh-${randomUUID()}`);await mkdir(transaction,{mode:0o700});
  const marker=await open(pending,'wx',0o600);await marker.writeFile(JSON.stringify({version:1,state:'pending',transaction,prior_manifest_sha256:old.manifest_sha256,next_manifest_sha256:next.manifest_sha256,navigation_proof:navigationProof,protected:protectedBefore},null,2)+'\n');await marker.close();
  // Preserve the entire old public tree for interrupted-operation reconciliation.
  const staged=resolve(transaction,'next-public');await mkdir(staged,{mode:0o700});
  for(const file of next.files){const target=resolve(staged,file.path);await mkdir(dirname(target),{recursive:true});await cp(resolve(input,file.path),target,{errorOnExist:true,force:false});}
  if(canonical((await readArchive(staged)).files)!==canonical(next.files)||canonical((await readArchive(input)).files)!==canonical(next.files))fail('Input changed while staging; mutation fence retained');
  if(navigationProof)proveNavigation(await archiveTree(await readArchive(staged)));
  if(canonical((await readArchive(resolve(site,'public'))).files)!==canonical(old.files)||canonical(await protectedFiles(site))!==canonical(protectedBefore))fail('Site source changed concurrently; mutation fence retained');
  currentHead(site,expectedCommit);
  await rename(resolve(site,'public'),resolve(transaction,'previous-public'));
  await rename(staged,resolve(site,'public'));
  if(canonical((await readArchive(resolve(site,'public'))).files)!==canonical(next.files)||canonical(await protectedFiles(site))!==canonical(protectedBefore))fail('Refresh verification failed; mutation fence retained');
  await writeFile(resolve(site,'.sites-runtime/archive-inventory.json'),JSON.stringify({version:1,files:next.files},null,2)+'\n');
  await writeFile(resolve(transaction,'complete.json'),JSON.stringify({...result,dry_run:false,protected_unchanged:true,build_required:true},null,2)+'\n');
  await unlink(pending);
  return {...result,protected_unchanged:true,build_required:true};
}
async function main(){const args=process.argv.slice(2),o={};for(let i=0;i<args.length;i++){const key=args[i];if(key==='--apply'){if(o.apply)fail('Duplicate option');o.apply=true;continue;}if(!['--site-dir','--archive-dir','--registry','--project-id','--expected-commit','--navigation-projections'].includes(key)||o[key]||!args[i+1]||args[i+1].startsWith('--'))fail('Invalid options');o[key]=args[++i];}
  for(const key of ['--site-dir','--archive-dir','--registry','--project-id','--expected-commit'])if(!o[key])fail(`Required ${key}`);
  console.log(JSON.stringify(await refreshArchive({siteDir:o['--site-dir'],archiveDir:o['--archive-dir'],registryFile:o['--registry'],projectId:o['--project-id'],expectedCommit:o['--expected-commit'],navigationProjections:o['--navigation-projections']?JSON.parse(await boundedFile(o['--navigation-projections'])):[],apply:o.apply}),null,2));}
if(process.argv[1]&&import.meta.url===pathToFileURL(resolve(process.argv[1])).href)main().catch(error=>{console.error(error.message);process.exitCode=1;});
