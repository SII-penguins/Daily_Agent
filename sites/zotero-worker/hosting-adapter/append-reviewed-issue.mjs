/** Independent narrow post-seal append. Never changes refreshArchive's allowlist. */
import {readFile,lstat,mkdir,writeFile,unlink,realpath} from 'node:fs/promises';
import {resolve} from 'node:path';
import {spawnSync} from 'node:child_process';
import {pathToFileURL} from 'node:url';
import {guardPublication} from './publication-proof.mjs';
import {boundedFile,loadReviewedIssues,verifyIssueAgainstArchive,CATALOG_PREFIX} from './reviewed-catalogs.mjs';
import {issueJson,validateReviewedRegistry} from '../src/reviewed-issues.mjs';
export async function appendReviewedIssue({siteDir,registry,expectedCommit,projection,expectedProjectionHash,apply=false}) {
  siteDir=await realpath(siteDir);
  const git=(...args)=>{const r=spawnSync('git',['-C',siteDir,...args],{encoding:'utf8',timeout:30000,maxBuffer:32*1024*1024});if(r.error||r.status!==0)throw new Error('Exact private Site checkout required');return r.stdout.trim();};
  if(!/^[a-f0-9]{40}$/.test(expectedCommit)||git('rev-parse','HEAD')!==expectedCommit||git('rev-parse','--show-toplevel')!==siteDir)throw new Error('Site source changed');
  const hosting=JSON.parse(await boundedFile(resolve(siteDir,'.openai/hosting.json')));
  const mode=guardPublication({registry,hosting,projectId:hosting.project_id});
  if(mode.mode!=='worker-assets')throw new Error('Active reviewed Worker runtime required');
  const fence=resolve(siteDir,'.sites-runtime/catalog-append-pending.json');
  for(const name of ['.sites-runtime/archive-refresh-pending.json','.sites-runtime/catalog-append-pending.json']) {
    try{await lstat(resolve(siteDir,name));throw new Error('Pending publication transaction must be reconciled');}catch(e){if(e.code!=='ENOENT')throw e;}
  }
  if(projection.projection_sha256!==expectedProjectionHash)throw new Error('Reviewed projection pin required');
  const manifestBytes=await boundedFile(resolve(siteDir,'public/manifest.json'),16*1024*1024),manifest=JSON.parse(manifestBytes);
  await verifyIssueAgainstArchive(projection,manifest,path=>boundedFile(resolve(siteDir,'public',path),16*1024*1024));
  const path=CATALOG_PREFIX+projection.issue_id+'.json',body=issueJson(projection)+'\n';
  const existing=await loadReviewedIssues(siteDir),prior=existing.find(i=>i.issue_id===projection.issue_id);
  if(prior){if(issueJson(prior)!==issueJson(projection))throw new Error('Immutable projection conflict');return {created:false,path,projection_sha256:expectedProjectionHash};}
  if(projection.legacy_seal)throw new Error('Ordinary append requires normal approval; legacy catalogs are migration-only');
  const oldManifest=JSON.parse(git('show',expectedCommit+':public/manifest.json'));
  if(oldManifest.entries.some(e=>e.page===projection.entry_path))throw new Error('Daily append requires a newly sealed archive edition');
  await validateReviewedRegistry([...existing,projection]);
  const changed=()=>[...git('diff','--name-only','HEAD').split('\n'),...git('ls-files','--others','--exclude-standard').split('\n')].filter(Boolean).sort();
  const before=changed();if(before.some(p=>!p.startsWith('public/')))throw new Error('Non-archive work pending before catalog append');
  const result={created:true,apply,path,projection_sha256:expectedProjectionHash,html_sha256:projection.html_sha256,prior_catalogs:existing.length};
  if(!apply)return result;
  // Only regular in-checkout ancestors. Never follow an output-directory symlink.
  for(const dir of ['zotero','zotero/reviewed-issues','.sites-runtime']){
    const dest=resolve(siteDir,dir);try{const s=await lstat(dest);if(!s.isDirectory()||s.isSymbolicLink())throw new Error('Unsafe catalog append directory');}catch(e){if(e.code!=='ENOENT')throw e;await mkdir(dest);}
  }
  if(git('rev-parse','HEAD')!==expectedCommit||JSON.stringify(changed())!==JSON.stringify(before)
      ||!(await readFile(resolve(siteDir,'public/manifest.json'))).equals(manifestBytes))throw new Error('Source changed during catalog preflight');
  await writeFile(fence,JSON.stringify({schema:'daily-agent-catalog-append-pending-v1',expectedCommit,...result})+'\n',{flag:'wx'});
  // A crash leaves the fence; recovery requires inspection of the exact pin.
  await writeFile(resolve(siteDir,path),body,{flag:'wx'});
  await loadReviewedIssues(siteDir);
  if((await readFile(resolve(siteDir,path),'utf8'))!==body)throw new Error('Projection write verification failed');
  await unlink(fence);
  return result;
}
if(process.argv[1] && import.meta.url===pathToFileURL(resolve(process.argv[1])).href){
  const args=process.argv.slice(2),o={};for(let i=0;i<args.length;i++){const k=args[i];if(!['--site-dir','--registry','--expected-commit','--projection','--projection-sha256','--apply'].includes(k)||Object.hasOwn(o,k))throw new Error('Unsupported arguments');o[k]=k==='--apply'?true:args[++i];}
  const result=await appendReviewedIssue({siteDir:o['--site-dir'],registry:JSON.parse(await boundedFile(o['--registry'])),expectedCommit:o['--expected-commit'],projection:JSON.parse(await boundedFile(o['--projection'])),expectedProjectionHash:o['--projection-sha256'],apply:o['--apply']===true});
  console.log(JSON.stringify(result));
}
