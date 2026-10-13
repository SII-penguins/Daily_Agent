/** Offline host publication guard/proof. No network, registry writes, Git writes,
 * deployment, runtime configuration, or chat delivery. Native evidence authenticity,
 * current registry/head discovery, audience and gateway checks remain with the host.
 */
import assert from 'node:assert/strict';
import { readFile, lstat, realpath } from 'node:fs/promises';
import { resolve, posix } from 'node:path';
import { pathToFileURL, fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';
import { inventory, digest } from './materialize.mjs';

export const PROFILE_SCHEMA = 'daily-agent-site-runtime-profile-v1';
const sha = /^[a-f0-9]{64}$/;
const commitPattern = /^[a-f0-9]{40}$/;
export const BUILD_INPUT_DIRECTORIES = Object.freeze(['app','build','components','hooks','vendor','db','drizzle','lib','scripts','zotero','public','.openai']);
const buildDirs = BUILD_INPUT_DIRECTORIES;
export const BUILD_INPUT_FILES = Object.freeze(['package.json','package-lock.json','vite.config.ts','next.config.ts','tsconfig.json','drizzle.config.ts','postcss.config.mjs','cloudflare-env.d.ts','components.json','.npmrc','eslint.config.mjs']);
const buildFiles = BUILD_INPUT_FILES;
const same = (a,b,message) => assert.deepEqual(a,b,message);
const object = v => v !== null && typeof v === 'object' && !Array.isArray(v);
const opaque = v => typeof v === 'string' && v.length > 0 && v.length <= 512 && !/[\s\x00-\x1f]/.test(v);
export const ARCHIVE_UI_FILES = Object.freeze(['archive/index.html','assets/archive-browser.js']);
export const archivePathAllowed = name => ['index.html','manifest.json',...ARCHIVE_UI_FILES].includes(name) || /^(reports|examples)\/[A-Za-z0-9][A-Za-z0-9._-]*\/(index|original)\.html$/.test(name) || /^reports\/[A-Za-z0-9][A-Za-z0-9._-]*\/reading\.html$/.test(name) || /^assets\/[a-f0-9]{64}\.png$/.test(name);

/** Only the already-published archive UI can use this exact script/search form.
 * Callers separately pin its source bytes to the prior archive. The signature
 * also prevents an append from dropping or repurposing its controls or CSP.
 */
export function inspectArchiveIndex(html,{archiveUi=false}={}) {
  const checked=spawnSync('python3',['-c',`import json,sys,re
from html.parser import HTMLParser
class P(HTMLParser):
 def __init__(self,ui):
  super().__init__();self.ui=ui;self.links=[];self.signature=[];self.ids=set();self.script=False;self.scripts=0;self.form=False;self.forms=0
 def handle_starttag(self,tag,attrs):
  if len({k for k,v in attrs})!=len(attrs): raise ValueError('Duplicate attribute')
  a=dict(attrs)
  if any(k.startswith('on') or k in ('action','formaction','formmethod','formenctype','formtarget','form','srcdoc') for k in a): raise ValueError('Active archive attribute')
  if any(re.sub(r'[\\x00-\\x20]','',v or '').lower().startswith(('javascript:','vbscript:','data:')) for k,v in attrs if k in ('href','src','xlink:href')): raise ValueError('Active URL')
  if tag in ('iframe','object','embed','base'): raise ValueError('Active archive index')
  if tag=='meta' and a.get('http-equiv','').lower()=='refresh': raise ValueError('Active redirect')
  if tag=='script':
   if not self.ui or a!={'src':'/assets/archive-browser.js','defer':None} or self.get_starttag_text()!="<script src=\\"/assets/archive-browser.js\\" defer>" or self.scripts: raise ValueError('Unapproved archive script')
   self.script=True;self.scripts+=1;self.signature.append(['script',sorted(attrs)])
  if tag=='form':
   if not self.ui or a!={'id':'archive-filters','class':'archive-filters','hidden':None,'role':'search','aria-label':'查找往期日报'} or self.forms: raise ValueError('Unapproved archive form')
   self.form=True;self.forms+=1
  if self.form: self.signature.append(['form-start',tag,sorted(attrs)])
  if 'id' in a:
   if a['id'] in self.ids: raise ValueError('Duplicate element id')
   self.ids.add(a['id'])
   if a['id'].startswith('archive-'): self.signature.append(['control',tag,sorted(attrs)])
  if tag=='meta' and a.get('http-equiv','').lower()=='content-security-policy': self.signature.append(['csp',sorted(attrs)])
  if tag=='a' and 'href' in a: self.links.append(a['href'])
 def handle_endtag(self,tag):
  if self.form: self.signature.append(['form-end',tag])
  if tag=='form': self.form=False
  if tag=='script': self.script=False
 def handle_data(self,data):
  if self.script and data: raise ValueError('Inline archive script')
  if self.form: self.signature.append(['form-text',data])
x=json.load(sys.stdin);p=P(x['ui']);p.feed(x['html']);p.close()
if p.script or p.form: raise ValueError('Unclosed active element')
if p.scripts or p.forms:
 required={'archive-filters','archive-query','archive-year','archive-month','archive-date','archive-reset','archive-days','archive-dynamic','archive-status','archive-results','archive-pagination','archive-static'}
 if p.scripts!=1 or p.forms!=1 or not required.issubset(p.ids): raise ValueError('Incomplete archive UI')
print(json.dumps({'links':p.links,'ui_signature':p.signature if p.scripts else []}))
`],{input:JSON.stringify({html:Buffer.isBuffer(html)?html.toString('utf8'):html,ui:archiveUi}),encoding:'utf8',maxBuffer:8*1024*1024,timeout:10000});
  if(checked.error||checked.status!==0)throw new Error('Archive index link validation failed: only the retained archive UI is permitted; active scripts, forms or attributes are forbidden');
  const result=JSON.parse(checked.stdout);
  return {...result,links:new Set(result.links.filter(v=>typeof v==='string').map(v=>v.startsWith('/')?v.slice(1):v))};
}
function safeName(name) {
  if (typeof name !== 'string' || !name || name.startsWith('/') || /[\\\x00-\x1f]/.test(name) || name.split('/').some(p => !p || p === '.' || p === '..') || posix.normalize(name) !== name) throw new Error('Unsafe proof path');
  return name;
}
function rows(value,label) {
  if (!Array.isArray(value) || !value.length || value.length > 50000) throw new Error(`Invalid ${label} inventory`);
  const seen = new Set();
  for (const row of value) {
    if (!object(row) || Object.keys(row).sort().join(',') !== 'bytes,path,sha256' || !sha.test(row.sha256) || !Number.isSafeInteger(row.bytes) || row.bytes < 0 || row.bytes > 256*1024*1024) throw new Error(`Invalid ${label} file`);
    safeName(row.path); if (seen.has(row.path)) throw new Error(`Duplicate ${label} path`); seen.add(row.path);
  }
  return [...value].sort((a,b)=>a.path.localeCompare(b.path,'en'));
}
const metadata = (path, bytes) => ({path,bytes:bytes.length,sha256:digest(bytes)});
const inputPath = name => buildFiles.includes(name) || buildDirs.some(dir=>name.startsWith(dir+'/'));
const url = value => {
  if (typeof value !== 'string' || /[\x00-\x20]/.test(value)) throw new Error('Invalid Site URL');
  const u = new URL(value);
  if (u.protocol !== 'https:' || !u.hostname.endsWith('.chatgpt.site') || u.username || u.password || u.port || u.search || u.hash || !['','/'].includes(u.pathname)) throw new Error('Invalid Site URL');
  return u.origin;
};

/** A missing profile permits only the historical exact static configuration.
 * Once a Worker marker exists, absent/unknown/pending state never falls back.
 * migrationVerification is read-only proof of an explicit pending migration,
 * never a publication or permission override.
 */
export function guardPublication({registry, hosting, projectId, migrationVerification = false}) {
  if (!object(registry) || registry.schema !== 'daily-agent-future-operations-registry-v1' || !object(registry.global_delivery)) throw new Error('Current operations registry required');
  if (!opaque(projectId) || !object(hosting) || hosting.project_id !== projectId) throw new Error('Site project identity mismatch');
  if (registry.global_delivery.audience !== 'owner_private' || registry.global_delivery.append_archive_entries_only !== true) throw new Error('Owner-private append-only operating contract required');
  const profile = registry.global_delivery.site_runtime_profile;
  if (profile === undefined) {
    if (migrationVerification) throw new Error('Explicit migration_pending profile required');
    same(hosting,{project_id:projectId,static:{directory:'dist'}},'Unregistered Worker profile or unsupported static manifest');
    return {mode:'static-archive',schema_version:1};
  }
  if (!object(profile) || profile.schema !== PROFILE_SCHEMA || profile.mode !== 'worker-assets' || !['migration_pending','active'].includes(profile.state)) throw new Error('Unknown Site runtime profile; publication blocked');
  if (profile.project_id !== projectId) throw new Error('Runtime profile belongs to another Site');
  if (profile.state !== 'active' && !migrationVerification) throw new Error('Site migration_pending; scheduled publication blocked');
  if (migrationVerification && profile.state !== 'migration_pending') throw new Error('Migration verification requires migration_pending profile');
  if (!commitPattern.test(profile.site_source_commit_sha) || !opaque(profile.version_id) || !opaque(profile.deployment_id)) throw new Error('Runtime profile must pin the previous verified Site source/version/deployment');
  if (Object.hasOwn(hosting,'static') || hosting.d1 !== 'DB' || hosting.r2 !== 'BUCKET') throw new Error('Worker profile forbids static publication and requires DB/BUCKET');
  return {mode:'worker-assets',schema_version:1,state:profile.state,profile};
}

function gitReader(repo) {
  const run = (...args) => {
    const r = spawnSync('git',['-C',repo,...args],{maxBuffer:256*1024*1024,timeout:30000});
    if (r.error || r.status !== 0) throw new Error('Exact committed Site source unavailable');
    return r.stdout;
  };
  const tree = commit => {
    if (!commitPattern.test(commit)) throw new Error('Exact Site commit SHA required');
    return run('ls-tree','-r','-z',commit).toString('utf8').split('\0').filter(Boolean).map(record=>{
      const match = /^(\d+) (\w+) ([a-f0-9]{40})\t(.+)$/.exec(record);
      if (!match) throw new Error('Invalid Git tree');
      safeName(match[4]);
      if (!['100644','100755'].includes(match[1]) || match[2] !== 'blob') throw new Error('Nonregular committed Site source');
      return {path:match[4],mode:match[1],blob:match[3]};
    });
  };
  return {run,tree,read:(commit,name)=>run('show',`${commit}:${safeName(name)}`)};
}
function nativeResult(raw,label) {
  if (!object(raw) || raw.isError !== false || !object(raw.structuredContent)) throw new Error(`Actual successful raw ${label} tool result required`);
  return raw.structuredContent;
}
async function regularBytes(path,limit=256*1024*1024) {
  const stat = await lstat(path);
  if (!stat.isFile() || stat.isSymbolicLink() || stat.size > limit) throw new Error('Evidence must be a bounded regular file');
  return readFile(path);
}

export async function verifyPublication({registry,repo,dist,buildReceipt,deploymentFile,versionFile,artifact,expectedCommit,entryPath,htmlSha256,migrationVerification=false}) {
  if (!commitPattern.test(expectedCommit) || !sha.test(htmlSha256)) throw new Error('Exact commit and reviewed HTML SHA256 required');
  safeName(entryPath);
  if (!/^reports\/\d{4}-\d{2}-\d{2}-[A-Za-z0-9-]+-[a-f0-9]{12}\/index\.html$/.test(entryPath) || !entryPath.endsWith(`-${htmlSha256.slice(0,12)}/index.html`)) throw new Error('Versioned report path must bind the reviewed HTML hash');
  repo = await realpath(repo); dist = resolve(dist);
  const git = gitReader(repo);
  if (resolve(git.run('rev-parse','--show-toplevel').toString().trim()) !== repo) throw new Error('Use the exact private Site Git checkout root');
  try { await lstat(resolve(repo,'.sites-runtime/archive-refresh-pending.json')); throw new Error('Interrupted archive refresh must be reconciled before publication proof'); } catch (error) { if (error.code!=='ENOENT') throw error; }
  const committed = git.tree(expectedCommit);
  const hosting = JSON.parse(git.read(expectedCommit,'.openai/hosting.json'));
  const guarded = guardPublication({registry,hosting,projectId:hosting.project_id,migrationVerification});
  if (guarded.mode !== 'worker-assets') throw new Error('Framework publication proof requires the explicit Worker profile');
  git.run('merge-base','--is-ancestor',guarded.profile.site_source_commit_sha,expectedCommit);
  // Migration may add Worker sources but can never discard the old static
  // archive. Resolve its real committed serving directory before comparing.
  const priorTree = git.tree(guarded.profile.site_source_commit_sha);
  const priorHosting = JSON.parse(git.read(guarded.profile.site_source_commit_sha,'.openai/hosting.json'));
  if (priorHosting.project_id!==hosting.project_id) throw new Error('Previous source belongs to another Site');
  let archivePrefix = 'public/';
  if (Object.hasOwn(priorHosting,'static')) {
    if (!migrationVerification || priorHosting.static?.directory!=='dist') throw new Error('Unsupported previous runtime profile');
    archivePrefix='dist/';
  }
  const priorFiles = priorTree.filter(r=>r.path.startsWith(archivePrefix) && ![archivePrefix+'index.html',archivePrefix+'manifest.json'].includes(r.path));
  for (const previousFile of priorFiles) {
    const translated = {...previousFile,path:'public/'+previousFile.path.slice(archivePrefix.length)};
    same(committed.find(r=>r.path===translated.path),translated,'Previously published archive bytes changed or disappeared');
  }
  const oldManifest = JSON.parse(git.read(guarded.profile.site_source_commit_sha,archivePrefix+'manifest.json'));
  const newManifest = JSON.parse(git.read(expectedCommit,'public/manifest.json'));
  if (!Array.isArray(oldManifest.entries) || !Array.isArray(newManifest.entries)) throw new Error('Archive manifest entries required');
  for (const oldEntry of oldManifest.entries) {
    const matches = newManifest.entries.filter(e=>e.key===oldEntry.key);
    if (matches.length!==1) throw new Error('Prior archive entry missing or duplicated');
    same(matches[0],oldEntry,'Prior archive entry record changed');
  }
  if (!migrationVerification) {
    same(committed.filter(r=>!r.path.startsWith('public/')),priorTree.filter(r=>!r.path.startsWith('public/')),'Archive-only publication changed framework/runtime/manifests/migrations');
  }
  const [receiptBytes,deploymentBytes,versionBytes,artifactBytes] = await Promise.all([
    regularBytes(buildReceipt,16*1024*1024),regularBytes(deploymentFile,1024*1024),regularBytes(versionFile,1024*1024),regularBytes(artifact)
  ]);
  const receipt = JSON.parse(receiptBytes);
  if (receipt.version !== 1 || receipt.kind !== 'vinext-sites-local-build' || receipt.archive_unchanged !== true || receipt.production_runtime_verified !== false) throw new Error('Supported Vinext build receipt required');
  const inputs = rows(receipt.inputs,'build input');
  const expectedInputs = rows(committed.filter(r=>inputPath(r.path)).map(r=>metadata(r.path,git.read(expectedCommit,r.path))),'committed build input');
  same(inputs,expectedInputs,'Build input receipt differs from the exact committed framework source');
  for (const required of [...buildFiles,'.openai/hosting.json','public/index.html','public/manifest.json','build/sites-worker.ts','zotero/worker/index.js']) {
    if (!inputs.some(r=>r.path===required)) throw new Error('Incomplete framework source: '+required);
  }
  for (const dir of buildDirs) if (!inputs.some(r=>r.path.startsWith(dir+'/'))) throw new Error('Incomplete framework source: '+dir);
  const outputs = rows(receipt.outputs,'build output');
  same(rows(await inventory(dist),'actual output'),outputs,'Build output receipt differs from the complete local artifact');
  const output = new Map(outputs.map(r=>[r.path,r]));
  const readOutput = name => regularBytes(resolve(dist,safeName(name)));
  for (const required of ['server/index.js','server/wrangler.json','.openai/hosting.json','.openai/drizzle/meta/_journal.json','.openai/drizzle/zotero-source-migrations.json']) if (!output.has(required)) throw new Error('Incomplete supported framework output: '+required);
  same(JSON.parse(await readOutput('.openai/hosting.json')),hosting,'Output hosting manifest differs from committed source');
  const wrangler = JSON.parse(await readOutput('server/wrangler.json'));
  if (wrangler.assets?.binding !== 'ASSETS' || wrangler.assets?.directory !== '../client' || wrangler.assets?.run_worker_first !== true || wrangler.assets?.not_found_handling !== 'none' || !wrangler.d1_databases?.some(b=>b.binding==='DB') || !wrangler.r2_buckets?.some(b=>b.binding==='BUCKET')) throw new Error('Worker-first ASSETS/DB/BUCKET build configuration required');
  const archive = inputs.filter(r=>r.path.startsWith('public/')).map(r=>({...r,path:r.path.slice(7)}));
  for (const f of archive) same(output.get('client/'+f.path),{...f,path:'client/'+f.path},'Committed archive bytes differ from built assets');
  const archiveManifest = JSON.parse(git.read(expectedCommit,'public/manifest.json'));
  if (archiveManifest.schema_version !== 1 || !object(archiveManifest.files) || !Array.isArray(archiveManifest.entries)) throw new Error('Supported archive manifest required');
  for (const file of archive.filter(f=>f.path!=='manifest.json')) same(archiveManifest.files[file.path],{sha256:file.sha256,size:file.bytes},'Archive manifest does not bind committed bytes');
  same(Object.keys(archiveManifest.files).sort(),archive.filter(f=>f.path!=='manifest.json').map(f=>f.path).sort(),'Archive manifest file set differs');
  for (const file of archive) {
    if (!archivePathAllowed(file.path) || file.bytes>25*1024*1024 || (file.path.startsWith('assets/') && file.path!=='assets/archive-browser.js' && file.path!==`assets/${file.sha256}.png`)) throw new Error('Unsupported archive public path or asset');
  }
  if (archive.length>20000) throw new Error('Archive exceeds conservative asset count');
  const priorKeys = new Set(oldManifest.entries.map(e=>e.key));
  const newKeys = new Set();
  const entryKeys = new Set();
  for (const entry of archiveManifest.entries) {
    if (!object(entry) || typeof entry.key!=='string' || entry.page!==entry.key+'/index.html' || entryKeys.has(entry.key) || !archiveManifest.files[entry.page]) throw new Error('Invalid or duplicate archive entry');
    entryKeys.add(entry.key);
    if (!priorKeys.has(entry.key)) {
      const source = archive.find(f=>f.path===entry.page);
      const prefix = `${entry.category}/${entry.date}-`;
      if (!['reports','examples'].includes(entry.category) || !/^\d{4}-\d{2}-\d{2}$/.test(entry.date) || !sha.test(entry.identity) || !sha.test(entry.approval_sha256) || entry.identity!==entry.source_sha256 || source.sha256!==entry.source_sha256 || !entry.key.startsWith(prefix) || !entry.key.endsWith('-'+entry.source_sha256.slice(0,12)) || !/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(entry.key.slice(prefix.length,-13))) throw new Error('New archive entry must have a canonical sealed HTML identity');
      newKeys.add(entry.key);
    }
  }
  if (!migrationVerification && !newKeys.has(entryPath.slice(0,-'/index.html'.length))) throw new Error('Ordinary publication must bind a newly appended report edition');
  const previousPublicPaths = new Set(priorTree.filter(r=>r.path.startsWith(archivePrefix)).map(r=>r.path.slice(archivePrefix.length)));
  for (const file of archive.filter(f=>!previousPublicPaths.has(f.path))) {
    if (ARCHIVE_UI_FILES.includes(file.path)) throw new Error('New archive UI files require separate approval; only retained UI is permitted');
    if (/^(reports|examples)\//.test(file.path) && ![...newKeys].some(key=>file.path.startsWith(key+'/'))) throw new Error('Added archive report file lacks a new sealed entry');
  }
  const archiveUi=previousPublicPaths.has('assets/archive-browser.js');
  const previousIndex=inspectArchiveIndex(git.read(guarded.profile.site_source_commit_sha,archivePrefix+'index.html'),{archiveUi});
  const indexCheck=inspectArchiveIndex(git.read(expectedCommit,'public/index.html'),{archiveUi});
  same(indexCheck.ui_signature,previousIndex.ui_signature,'Existing archive UI controls or behavior would change');
  if (archiveManifest.entries.some(e=>!indexCheck.links.has(e.page))) throw new Error('Archive index must link every retained and new entry without unapproved active content');
  if (previousPublicPaths.has('archive/index.html')) inspectArchiveIndex(git.read(expectedCommit,'public/archive/index.html'),{archiveUi});
  const matchedEntries = archiveManifest.entries.filter(e=>e.page===entryPath);
  if (matchedEntries.length!==1 || matchedEntries[0].source_sha256!==htmlSha256) throw new Error('Exact reviewed report archive entry required');
  const html = archive.find(f=>f.path===entryPath);
  if (!html || html.sha256 !== htmlSha256) throw new Error('Reviewed report HTML differs from exact committed/built bytes');
  // Generated migrations must correspond exactly to committed source SQL and
  // generated source history. No D1 is opened or mutated by these checks.
  const mapping = JSON.parse(await readOutput('.openai/drizzle/zotero-source-migrations.json'));
  const journal = JSON.parse(await readOutput('.openai/drizzle/meta/_journal.json'));
  const migrations = inputs.filter(r=>/^zotero\/migrations\/\d{4}_[a-z0-9_]+\.sql$/.test(r.path));
  if (!migrations.length || !Array.isArray(mapping.migrations) || mapping.migrations.length !== migrations.length || !Array.isArray(journal.entries) || journal.entries.length !== migrations.length) throw new Error('Incomplete generated migration history');
  const sources = new Set(), generated = new Set();
  for (const m of mapping.migrations) {
    safeName(m.source); safeName(m.output);
    if (sources.has(m.source) || generated.has(m.output)) throw new Error('Duplicate generated migration mapping');
    sources.add(m.source); generated.add(m.output);
    if (migrations.find(f=>f.path==='zotero/migrations/'+m.source)?.sha256 !== m.source_sha256 || output.get('.openai/drizzle/'+m.output)?.sha256 !== m.output_sha256) throw new Error('Migration source/output mismatch');
  }
  for (const f of outputs.filter(r=>r.path.startsWith('.openai/drizzle/'))) {
    const source = inputs.find(r=>r.path==='drizzle/'+f.path.slice('.openai/drizzle/'.length));
    same(source,{...f,path:'drizzle/'+f.path.slice('.openai/drizzle/'.length)},'Generated deployment migration differs from committed history');
  }
  const deployment = nativeResult(JSON.parse(deploymentBytes),'deployment');
  const version = nativeResult(JSON.parse(versionBytes),'Site version');
  if (deployment.status !== 'succeeded' || deployment.type !== 'publish' || deployment.failure_message !== null || !opaque(deployment.id) || !opaque(deployment.version_id) || deployment.project_id !== hosting.project_id) throw new Error('Successful deployment for this exact Site required');
  if (version.id !== deployment.version_id || version.project_id !== deployment.project_id || version.deployment_id !== deployment.id || version.source?.commit_sha !== expectedCommit) throw new Error('Native Site version/deployment/source identity mismatch');
  const base = url(deployment.url);
  if (base !== url(registry.global_delivery.site_url)) throw new Error('Deployment URL differs from the existing private Site');
  if (typeof deployment.updated_at !== 'string' || !/(Z|[+-]\d\d:\d\d)$/.test(deployment.updated_at) || !Number.isFinite(Date.parse(deployment.updated_at))) throw new Error('Invalid deployment timestamp');
  const stored = version.archive_storage;
  if (!object(stored) || stored.archive_format !== 'tar' || stored.content_hash !== 'sha256:'+digest(artifactBytes) || stored.size_bytes !== artifactBytes.length || !Number.isSafeInteger(stored.file_count) || stored.file_count < 1) throw new Error('Exact native saved artifact hash/size evidence required');
  const tarReader = fileURLToPath(new URL('./verify-artifact-tar.py',import.meta.url));
  const checked = spawnSync('python3',[tarReader,resolve(artifact)],{maxBuffer:16*1024*1024,timeout:30000});
  if (checked.error || checked.status !== 0) throw new Error('Unsafe or unreadable native artifact TAR');
  const tarFiles = rows(JSON.parse(checked.stdout),'TAR artifact');
  same(tarFiles,outputs,'Saved native artifact differs from complete verified build output');
  if (stored.file_count !== tarFiles.length) throw new Error('Native artifact file_count mismatch');
  same(await regularBytes(artifact),artifactBytes,'Saved artifact changed during verification');
  same(rows(await inventory(dist),'final output'),outputs,'Build output changed during verification');
  // Hash raw evidence, not a lossy selected-field reconstruction. The caller
  // stores these original files beside this result in its immutable checkpoint.
  return {
    schema:'daily-agent-worker-publication-proof-v1',project_id:hosting.project_id,site_source_commit_sha:expectedCommit,
    version_id:version.id,deployment_id:deployment.id,site_url:base,entry_path:entryPath,html_sha256:htmlSha256,
    previous_site_source_commit_sha:guarded.profile.site_source_commit_sha,hosting,
    source_inputs:inputs,build_outputs:outputs,archive_files:archive,
    evidence:{build_receipt:metadata('build_receipt',receiptBytes),deployment:metadata('deployment',deploymentBytes),site_version:metadata('site_version',versionBytes),artifact:metadata('artifact',artifactBytes)},
    native_saved_artifact_verified:true,remote_http_verified:false,owner_audience_verified:false,gateway_identity_verified:false,
    migration_verification:migrationVerification,registry_changed:false,chat_sent:false
  };
}

async function main() {
  const [command,...args] = process.argv.slice(2), options = {};
  const names = new Set(['--registry','--hosting','--project-id','--repo','--dist','--build-receipt','--deployment','--site-version','--artifact','--expected-commit','--entry-path','--html-sha256','--migration-verification']);
  for (let i=0;i<args.length;i++) {
    const name=args[i];if (!names.has(name) || Object.hasOwn(options,name)) throw new Error('Unknown or duplicate option');
    if (name==='--migration-verification') options[name]=true;
    else {if (!args[i+1] || args[i+1].startsWith('--')) throw new Error('Missing option value');options[name]=args[++i];}
  }
  const required = command==='guard' ? ['--registry','--hosting','--project-id'] : command==='prove' ? ['--registry','--repo','--dist','--build-receipt','--deployment','--site-version','--artifact','--expected-commit','--entry-path','--html-sha256'] : [];
  if (!required.length || required.some(k=>!options[k])) throw new Error('Use guard or prove with all required inputs');
  const registry = JSON.parse(await regularBytes(options['--registry'],16*1024*1024));
  const result = command==='guard' ? guardPublication({registry,hosting:JSON.parse(await regularBytes(options['--hosting'])),projectId:options['--project-id']}) : await verifyPublication({registry,repo:options['--repo'],dist:options['--dist'],buildReceipt:options['--build-receipt'],deploymentFile:options['--deployment'],versionFile:options['--site-version'],artifact:options['--artifact'],expectedCommit:options['--expected-commit'],entryPath:options['--entry-path'],htmlSha256:options['--html-sha256'],migrationVerification:options['--migration-verification']===true});
  console.log(JSON.stringify(result,null,2));
}
if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) main().catch(error=>{console.error(error.message);process.exitCode=1;});
