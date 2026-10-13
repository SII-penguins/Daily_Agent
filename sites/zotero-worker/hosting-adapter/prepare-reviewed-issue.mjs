/** CLI for the existing host publication contract, after scientific seal/review.
 * Reads explicit approved inputs; writes one private candidate, never a Site. */
import {readFile,realpath,writeFile,lstat} from 'node:fs/promises';
import {resolve,dirname,relative,isAbsolute} from 'node:path';
import {fileURLToPath,pathToFileURL} from 'node:url';
import {createHash} from 'node:crypto';
import {boundedFile,buildReviewedIssue} from './reviewed-catalogs.mjs';
import {issueJson} from '../src/reviewed-issues.mjs';
const hash=b=>createHash('sha256').update(b).digest('hex');
const inside=(root,path)=>{const r=relative(root,path);return r==='' || !r.startsWith('..')&&!isAbsolute(r);};
export async function prepareReviewedIssueFile({inputFile,inputSha256,outputFile}){
  const input=await boundedFile(inputFile);if(!/^[a-f0-9]{64}$/.test(inputSha256)||hash(input)!==inputSha256)throw new Error('Approved post-seal input pin required');
  const data=JSON.parse(input),root=await realpath(dirname(inputFile)),publicRoot=resolve(fileURLToPath(new URL('../../..',import.meta.url)));
  const outputParent=await realpath(dirname(outputFile));if(inside(publicRoot,outputParent))throw new Error('Private projection output must be outside the public source repository');
  const read=async(name,max)=>{if(typeof name!=='string'||!name||isAbsolute(name)||name.split(/[\\/]/).some(p=>!p||p==='.'||p==='..'))throw new Error('Relative approved input path required');
    const path=resolve(root,name);if(!inside(root,await realpath(path)))throw new Error('Input escapes approved directory');return boundedFile(path,max);};
  const batches=[];for(const b of data.batches||[]){batches.push({binding:b.binding,trustedPins:b.trustedPins,report:(await read(b.report_file)).toString('utf8'),catalog:(await read(b.catalog_file)).toString('utf8'),pdf_sources:b.pdf_sources||[]});}
  let legacyMigration;
  if(data.legacy_migration!==undefined){const m=data.legacy_migration;legacyMigration={approved:m.approved,previousManifest:JSON.parse(await read(m.previous_manifest_file,16*1024*1024)),compositionBytes:await read(m.composition_file,16*1024*1024),independentStaticReviewBytes:await read(m.independent_static_review_file,16*1024*1024)};}
  const issue=await buildReviewedIssue({issue_id:data.issue_id,entry:data.entry,htmlBytes:await read(data.html_file,16*1024*1024),issueReportBytes:await read(data.issue_report_file),issueReportSha256:data.issue_report_sha256,paper_ids:data.paper_ids,anchors:data.anchors,batches,legacyMigration});
  const target=resolve(outputParent,outputFile.split('/').at(-1));
  await writeFile(target,issueJson(issue)+'\n',{flag:'wx',mode:0o600});
  if(!(await lstat(target)).isFile() || (await readFile(target,'utf8'))!==issueJson(issue)+'\n')throw new Error('Projection write unverified');
  return {path:target,projection_sha256:issue.projection_sha256,html_sha256:issue.html_sha256,paper_count:issue.paper_ids.length};
}
if(process.argv[1] && import.meta.url===pathToFileURL(resolve(process.argv[1])).href){
  const args=process.argv.slice(2),o={};for(let i=0;i<args.length;i++){const k=args[i];if(!['--input','--input-sha256','--output'].includes(k)||Object.hasOwn(o,k))throw new Error('Unsupported arguments');o[k]=args[++i];}
  console.log(JSON.stringify(await prepareReviewedIssueFile({inputFile:o['--input'],inputSha256:o['--input-sha256'],outputFile:o['--output']})));
}
