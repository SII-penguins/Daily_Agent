import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtemp,mkdir,writeFile,readFile,rm,symlink} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {resolve,dirname} from 'node:path';
import {spawnSync} from 'node:child_process';
import {reviewedFixture,sha} from '../test/reviewed-issue-fixture.mjs';
import {buildReviewedIssue,verifyIssueAgainstArchive,verifyCatalogAppend,loadReviewedIssues,CATALOG_PREFIX} from './reviewed-catalogs.mjs';
import {appendReviewedIssue} from './append-reviewed-issue.mjs';
import {PROFILE_SCHEMA} from './publication-proof.mjs';
import {issueJson,issueHash} from '../src/reviewed-issues.mjs';
const put=async(root,path,value)=>{await mkdir(dirname(resolve(root,path)),{recursive:true});await writeFile(resolve(root,path),typeof value==='string'||value instanceof Uint8Array?value:JSON.stringify(value));};
const git=(root,...args)=>{const r=spawnSync('git',['-C',root,...args],{encoding:'utf8'});assert.equal(r.status,0,r.stderr);return r.stdout.trim();};
const commit=root=>{git(root,'add','.');git(root,'-c','user.name=Synthetic','-c','user.email=synthetic@example.invalid','commit','-qm','fixture');return git(root,'rev-parse','HEAD');};
test('projection validates source seals, canonical complete paper sets and anchor identity without changing HTML',async()=>{
  const f=await reviewedFixture(),before=sha(f.html);await verifyIssueAgainstArchive(f.issue,f.manifest,async()=>f.html);assert.equal(sha(f.html),before);
  const swapped=f.issue.anchors.map(({paper_id,anchor})=>({paper_id,anchor:anchor==='paper-1'?'paper-2':'paper-1'}));
  await assert.rejects(buildReviewedIssue({issue_id:f.issue.issue_id,entry:f.entry,htmlBytes:f.html,batches:f.issue.batches,anchors:swapped,paper_ids:f.issue.paper_ids,issueReportBytes:new TextEncoder().encode(f.issue.issue_report),issueReportSha256:f.issue.issue_report_sha256}),/anchor validation/);
  const bad=structuredClone(f.issue);bad.anchors[0].offset++;const {projection_sha256,...payload}=bad;bad.projection_sha256=await issueHash(payload);
  await assert.rejects(verifyIssueAgainstArchive(bad,f.manifest,async()=>f.html),/mapping differs/);
  await assert.rejects(verifyIssueAgainstArchive(f.issue,{...f.manifest,entries:[{...f.entry,approval_sha256:'0'.repeat(64)}]},async()=>f.html),/seal mismatch/);
});
test('ordinary publication allows new catalog data only; refuses runtime edits, replaced catalogs and retroactive additions',async()=>{
  const f=await reviewedFixture(),path=CATALOG_PREFIX+f.issue.issue_id+'.json',files=new Map([[path,Buffer.from(issueJson(f.issue)+'\n')],['public/'+f.entry.page,Buffer.from(f.html)]]);
  const runtime={path:'zotero/src/application.mjs',blob:'a'.repeat(40),mode:'100644'},projection={path,blob:'b'.repeat(40),mode:'100644'};
  const options={prior:[runtime],current:[runtime,projection],readCurrent:p=>files.get(p),manifest:f.manifest,oldManifest:{entries:[]}};
  await verifyCatalogAppend(options);
  await assert.rejects(verifyCatalogAppend({...options,current:[{...runtime,blob:'c'.repeat(40)},projection]}),/runtime/);
  await assert.rejects(verifyCatalogAppend({...options,prior:[runtime,{...projection,blob:'d'.repeat(40)}]}),/prior catalog/);
  await assert.rejects(verifyCatalogAppend({...options,oldManifest:f.manifest}),/newly sealed/);
  await assert.rejects(verifyCatalogAppend({...options,current:[runtime,{...projection,path:CATALOG_PREFIX+'escape.txt'}]}),/runtime/);
});
test('separate post-seal transaction appends two fixture dates, keeps old mapping exact, rejects mutation and fences partial writes',async()=>{
  const root=await mkdtemp(resolve(tmpdir(),'reviewed-append-'));
  try{
    git(root,'init','-q');await put(root,'.gitignore','.sites-runtime/\n');await put(root,'.openai/hosting.json',{project_id:'synthetic-project',d1:'DB',r2:'BUCKET'});
    await put(root,'public/manifest.json',{schema_version:1,entries:[],files:{}});await put(root,'zotero/src/runtime.mjs','immutable runtime');let head=commit(root);
    const registry={schema:'daily-agent-future-operations-registry-v1',global_delivery:{audience:'owner_private',append_archive_entries_only:true,site_runtime_profile:{schema:PROFILE_SCHEMA,mode:'worker-assets',state:'active',project_id:'synthetic-project',site_source_commit_sha:head,version_id:'v-fixture',deployment_id:'d-fixture'}}};
    const all=[],manifest={schema_version:1,entries:[],files:{}};
    for(const day of ['2000-01-01','2000-01-02']){
      const f=await reviewedFixture(day);manifest.entries.push(f.entry);Object.assign(manifest.files,f.manifest.files);await put(root,'public/'+f.entry.page,f.html);await put(root,'public/manifest.json',manifest);
      const args={siteDir:root,registry,expectedCommit:head,projection:f.issue,expectedProjectionHash:f.issue.projection_sha256};
      const dry=await appendReviewedIssue(args);assert.equal(dry.apply,false);assert.equal((await loadReviewedIssues(root)).length,all.length);
      await appendReviewedIssue({...args,apply:true});all.push(f.issue);assert.deepEqual(await loadReviewedIssues(root),all);
      assert.equal((await appendReviewedIssue({...args,apply:true})).created,false);
      head=commit(root);registry.global_delivery.site_runtime_profile.site_source_commit_sha=head;
    }
    const bad=structuredClone(all[0]);bad.paper_ids.reverse();const {projection_sha256,...payload}=bad;bad.projection_sha256=await issueHash(payload);
    await assert.rejects(appendReviewedIssue({siteDir:root,registry,expectedCommit:head,projection:bad,expectedProjectionHash:bad.projection_sha256,apply:true}),/Immutable projection/);
    await put(root,'.sites-runtime/catalog-append-pending.json','pending');await assert.rejects(appendReviewedIssue({siteDir:root,registry,expectedCommit:head,projection:all[0],expectedProjectionHash:all[0].projection_sha256}),/Pending/);
    assert.equal(await readFile(resolve(root,'zotero/src/runtime.mjs'),'utf8'),'immutable runtime');
  }finally{await rm(root,{recursive:true,force:true});}
});
test('loader rejects unexpected data paths, symlinks and accumulated budget excess',async()=>{
  const root=await mkdtemp(resolve(tmpdir(),'reviewed-load-'));
  try{
    const f=await reviewedFixture();await put(root,'public/manifest.json',f.manifest);await put(root,'public/'+f.entry.page,f.html);
    await put(root,CATALOG_PREFIX+f.issue.issue_id+'.json',issueJson(f.issue)+'\n');assert.equal((await loadReviewedIssues(root)).length,1);
    await symlink('missing',resolve(root,CATALOG_PREFIX+'other.json'));await assert.rejects(loadReviewedIssues(root),/Unexpected/);
    await rm(resolve(root,CATALOG_PREFIX+'other.json'));await put(root,CATALOG_PREFIX+'large.json',' '.repeat(2*1024*1024+1));await assert.rejects(loadReviewedIssues(root),/Bounded/);
  }finally{await rm(root,{recursive:true,force:true});}
});

test('post-seal CLI input is pinned, directory-bound and never overwrites its reviewed output',async()=>{
  const {prepareReviewedIssueFile}=await import('./prepare-reviewed-issue.mjs');
  const root=await mkdtemp(resolve(tmpdir(),'reviewed-prepare-'));
  try{
    const f=await reviewedFixture();await put(root,'source/report.html',f.html);await put(root,'source/issue-report.json',f.issue.issue_report);
    const batches=[];for(let n=0;n<f.issue.batches.length;n++){const b=f.issue.batches[n];await put(root,'source/report-'+n+'.json',b.report);await put(root,'source/catalog-'+n+'.json',b.catalog);batches.push({binding:b.binding,trustedPins:b.trustedPins,pdf_sources:b.pdf_sources,report_file:'source/report-'+n+'.json',catalog_file:'source/catalog-'+n+'.json'});}
    const input={issue_id:f.issue.issue_id,entry:f.entry,html_file:'source/report.html',issue_report_file:'source/issue-report.json',issue_report_sha256:f.issue.issue_report_sha256,paper_ids:f.issue.paper_ids,anchors:f.issue.anchors.map(({paper_id,anchor})=>({paper_id,anchor})),batches};
    const inputFile=resolve(root,'input.json'),outputFile=resolve(root,'projection.json');await put(root,'input.json',input);const inputSha256=sha(await readFile(inputFile));
    await assert.rejects(prepareReviewedIssueFile({inputFile,inputSha256:'0'.repeat(64),outputFile}),/pin required/);
    await prepareReviewedIssueFile({inputFile,inputSha256,outputFile});assert.equal(await readFile(outputFile,'utf8'),issueJson(f.issue)+'\n');
    await assert.rejects(prepareReviewedIssueFile({inputFile,inputSha256,outputFile}),e=>e.code==='EEXIST');
    input.html_file='../outside.html';await put(root,'input.json',input);await assert.rejects(prepareReviewedIssueFile({inputFile,inputSha256:sha(await readFile(inputFile)),outputFile:resolve(root,'new.json')}),/Relative approved/);
  }finally{await rm(root,{recursive:true,force:true});}
});

async function legacyFixture(){
  const f=await reviewedFixture(),htmlHash=sha(f.html),componentHash='c'.repeat(64);
  const review={schema:'daily-agent-independent-static-composition-review-v1',verdict:'PASS_STATIC_ENVELOPE',html_sha256:htmlHash,paper_component_sha256:componentHash,
    checks:[{check:'synthetic-seal',pass:true,detail:'Synthetic static check only'}],papers:['synthetic-paper-1','synthetic-paper-2'],required_fixes:[],browser_qa:{status:'NOT_PERFORMED',scope:'static-only'}};
  const reviewBytes=Buffer.from(JSON.stringify(review));
  const composition={schema:'daily-agent-oct13-host-composition-v1',edition_id:'synthetic-legacy',issue_date:f.entry.date,html:{path:'index.html',sha256:htmlHash,bytes:f.html.length},
    paper_component_seal:{path:'synthetic-paper-component.json',sha256:componentHash,qualified:2,sealed_at:'2000-01-01T00:00:00.000Z'},counts:{papers:2,repositories:0,pending_papers:0,rejected_papers:0,original_figures:0},
    paper_articles_preserved_byte_exact:true,independent_static_review_sha256:sha(reviewBytes),independent_static_review_decision:'PASS_STATIC_ENVELOPE'};
  const compositionBytes=Buffer.from(JSON.stringify(composition)),entry={...f.entry};delete entry.approval_sha256;
  Object.assign(entry,{composition_schema:composition.schema,composition_manifest_sha256:sha(compositionBytes),independent_static_review_sha256:sha(reviewBytes)});
  const manifest={...f.manifest,entries:[entry]};
  const args={issue_id:f.issue.issue_id,entry,htmlBytes:f.html,batches:f.issue.batches,anchors:f.issue.anchors.map(({paper_id,anchor})=>({paper_id,anchor})),paper_ids:f.issue.paper_ids,
    issueReportBytes:Buffer.from(f.issue.issue_report),issueReportSha256:f.issue.issue_report_sha256,legacyMigration:{approved:true,previousManifest:structuredClone(manifest),compositionBytes,independentStaticReviewBytes:reviewBytes}};
  return {...f,entry,manifest,args,composition,review};
}
test('explicit legacy migration rehashes exact evidence, admits only an existing sealed entry and keeps it readable',async()=>{
  const f=await legacyFixture(),issue=await buildReviewedIssue(f.args);
  assert.equal(Object.hasOwn(issue,'approval_sha256'),false);assert.equal(issue.legacy_seal.kind,'legacy-sealed-migration-v1');assert.equal(issue.legacy_seal.full_adapter_sha256,f.issue.issue_report_sha256);
  await verifyIssueAgainstArchive(issue,f.manifest,async()=>f.html);
  const path=CATALOG_PREFIX+issue.issue_id+'.json',row={path,blob:'a'.repeat(40),mode:'100644'},read=p=>p===path?Buffer.from(issueJson(issue)+'\n'):Buffer.from(f.html);
  await verifyCatalogAppend({prior:[],current:[row],readCurrent:read,manifest:f.manifest,oldManifest:f.manifest,migrationAdmission:true});
  await verifyCatalogAppend({prior:[row],current:[row],readCurrent:read,manifest:f.manifest,oldManifest:f.manifest});
  await assert.rejects(verifyCatalogAppend({prior:[],current:[row],readCurrent:read,manifest:f.manifest,oldManifest:f.manifest}),/restricted to explicit/);
  await assert.rejects(verifyCatalogAppend({prior:[],current:[row],readCurrent:read,manifest:f.manifest,oldManifest:{entries:[]},migrationAdmission:true}),/already-existing/);
  const root=await mkdtemp(resolve(tmpdir(),'legacy-reviewed-load-'));try{await put(root,'public/manifest.json',f.manifest);await put(root,'public/'+f.entry.page,f.html);await put(root,path,issueJson(issue)+'\n');assert.deepEqual(await loadReviewedIssues(root),[issue]);}finally{await rm(root,{recursive:true,force:true});}
});
test('missing normal approval never implicitly admits legacy or a new daily append',async()=>{
  const f=await legacyFixture();await assert.rejects(buildReviewedIssue({...f.args,legacyMigration:undefined}),/Normal daily approval/);
  await assert.rejects(buildReviewedIssue({...f.args,legacyMigration:{...f.args.legacyMigration,approved:false}}),/Explicit legacy/);
  await assert.rejects(buildReviewedIssue({...f.args,legacyMigration:{...f.args.legacyMigration,previousManifest:{entries:[]}}}),/already-existing/);
  await assert.rejects(buildReviewedIssue({...f.args,entry:{...f.entry,approval_sha256:'a'.repeat(64)}}),/fabricated approval/);
});
test('legacy evidence rejects changed bytes, missing hashes, altered bindings, counts, verdict aliases and failed checks',async()=>{
  const f=await legacyFixture();
  for(const key of ['composition_manifest_sha256','independent_static_review_sha256']){const entry={...f.entry};delete entry[key];await assert.rejects(buildReviewedIssue({...f.args,entry,legacyMigration:{...f.args.legacyMigration,previousManifest:{...f.manifest,entries:[entry]}}}));}
  await assert.rejects(buildReviewedIssue({...f.args,issueReportSha256:'0'.repeat(64)}));
  await assert.rejects(buildReviewedIssue({...f.args,legacyMigration:{...f.args.legacyMigration,compositionBytes:Buffer.from('changed')}}),/bytes\/hash/);
  const mutations=[
    (c,r)=>c.html.sha256='0'.repeat(64),(c,r)=>r.html_sha256='0'.repeat(64),(c,r)=>r.paper_component_sha256='0'.repeat(64),
    (c,r)=>c.html.bytes++,(c,r)=>c.counts.papers++,(c,r)=>r.papers.pop(),(c,r)=>r.verdict='PASS',(c,r)=>c.independent_static_review_decision='APPROVED',
    (c,r)=>r.checks[0].pass=false,(c,r)=>r.required_fixes.push('synthetic-fix'),(c,r)=>c.schema='guessed-schema',(c,r)=>r.schema='guessed-review',
  ];
  for(const mutate of mutations){
    const c=structuredClone(f.composition),r=structuredClone(f.review);mutate(c,r);const reviewBytes=Buffer.from(JSON.stringify(r));c.independent_static_review_sha256=sha(reviewBytes);const compositionBytes=Buffer.from(JSON.stringify(c));
    const entry={...f.entry,composition_schema:c.schema,composition_manifest_sha256:sha(compositionBytes),independent_static_review_sha256:sha(reviewBytes)};
    await assert.rejects(buildReviewedIssue({...f.args,entry,legacyMigration:{approved:true,previousManifest:{...f.manifest,entries:[entry]},compositionBytes,independentStaticReviewBytes:reviewBytes}}),/evidence binding/);
  }
});

test('private integration CLI accepts explicit legacy evidence files without modifying the original manifest',async()=>{
  const {prepareReviewedIssueFile}=await import('./prepare-reviewed-issue.mjs');
  const root=await mkdtemp(resolve(tmpdir(),'legacy-reviewed-cli-'));
  try{
    const f=await legacyFixture();await put(root,'html.html',f.html);await put(root,'issue.json',f.args.issueReportBytes);await put(root,'previous.json',f.manifest);
    await put(root,'composition.json',f.args.legacyMigration.compositionBytes);await put(root,'review.json',f.args.legacyMigration.independentStaticReviewBytes);
    const before=await readFile(resolve(root,'previous.json')),batches=[];
    for(let n=0;n<f.args.batches.length;n++){const b=f.args.batches[n];await put(root,'report-'+n+'.json',b.report);await put(root,'catalog-'+n+'.json',b.catalog);batches.push({binding:b.binding,trustedPins:b.trustedPins,pdf_sources:b.pdf_sources,report_file:'report-'+n+'.json',catalog_file:'catalog-'+n+'.json'});}
    await put(root,'input.json',{issue_id:f.args.issue_id,entry:f.entry,html_file:'html.html',issue_report_file:'issue.json',issue_report_sha256:f.args.issueReportSha256,paper_ids:f.args.paper_ids,anchors:f.args.anchors,batches,
      legacy_migration:{approved:true,previous_manifest_file:'previous.json',composition_file:'composition.json',independent_static_review_file:'review.json'}});
    const inputFile=resolve(root,'input.json'),outputFile=resolve(root,'projection.json');await prepareReviewedIssueFile({inputFile,inputSha256:sha(await readFile(inputFile)),outputFile});
    const issue=JSON.parse(await readFile(outputFile));assert.equal(issue.legacy_seal.kind,'legacy-sealed-migration-v1');assert.equal(Object.hasOwn(issue,'approval_sha256'),false);assert.deepEqual(await readFile(resolve(root,'previous.json')),before);
  }finally{await rm(root,{recursive:true,force:true});}
});

// Explanatory detail is byte-bounded, hash-bound and never approval authority.
test('legacy descriptive detail permits bounded real shapes and rejects unsafe shapes',async()=>{
  const f=await legacyFixture();
  const accepted=[null,'synthetic description','',{links:351,network_liveness:'static syntax only'},{files:63},{remote_site_state:'not independently rechecked'}, {optional:null,flag:true,count:1}];
  const rejected=[{},[],42,true,undefined,{nested:{}},{nested:[]},'x'.repeat(4097),{text:'x'.repeat(4097)},{a:'x'.repeat(4096),b:'x'.repeat(4096)},Object.fromEntries(Array.from({length:17},(_,i)=>['field'+i,i])),JSON.parse('{"__proto__":"bad"}'),{constructor:'bad'},{['x'.repeat(65)]:1},{['bad\u0000key']:1},{count:Number.MAX_SAFE_INTEGER+1}];
  for(const [allow,details] of [[true,accepted],[false,rejected]])for(const detail of details){
    const c=structuredClone(f.composition),r=structuredClone(f.review);r.checks[0].detail=detail;
    const reviewBytes=Buffer.from(JSON.stringify(r));c.independent_static_review_sha256=sha(reviewBytes);const compositionBytes=Buffer.from(JSON.stringify(c));
    const entry={...f.entry,composition_manifest_sha256:sha(compositionBytes),independent_static_review_sha256:sha(reviewBytes)};
    const args={...f.args,entry,legacyMigration:{approved:true,previousManifest:{...f.manifest,entries:[entry]},compositionBytes,independentStaticReviewBytes:reviewBytes}};
    if(allow)assert.equal((await buildReviewedIssue(args)).legacy_seal.independent_static_review_sha256,sha(reviewBytes));
    else await assert.rejects(buildReviewedIssue(args),/evidence binding/);
  }
});
