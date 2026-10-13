/** Local-only integrity/readiness preflight. No provisioning, account calls, or secrets. */
import assert from 'node:assert/strict';
import { readFile, access, readdir } from 'node:fs/promises';
import { resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { DatabaseSync } from 'node:sqlite';
import { createHash } from 'node:crypto';
import { inventory } from '../hosting-adapter/materialize.mjs';
import { BUILD_INPUT_DIRECTORIES, BUILD_INPUT_FILES } from '../hosting-adapter/publication-proof.mjs';
const root=fileURLToPath(new URL('../',import.meta.url));
const sha=bytes=>createHash('sha256').update(bytes).digest('hex');
const exists=async path=>{try{await access(path);return true;}catch{return false;}};
const args=process.argv.slice(2),options={};
for(let i=0;i<args.length;i++){
  if(!['--archive-dir','--adapter-dir','--require-hostable'].includes(args[i])||Object.hasOwn(options,args[i]))throw new Error('Unknown or duplicate preflight option');
  if(args[i]==='--require-hostable')options[args[i]]=true;
  else{if(!args[i+1]||args[i+1].startsWith('--'))throw new Error('Missing preflight option');options[args[i++]]=args[i];}
}
const result={local_core:false,archive:null,adapter:{built:false,artifact_ready_for_owner_packaging:false,blockers:[]},production_runtime_verified:false,live_zotero_verified:false};
const status=JSON.parse(await readFile(resolve(root,'dist/BUILD-STATUS.json'),'utf8'));
assert.equal(status.kind,'local-esm-graph');
for(const file of status.files){
  const original=await readFile(resolve(root,file.path));
  const output=await readFile(resolve(root,file.path.startsWith('migrations/')?'dist/'+file.path:'dist/server/'+file.path));
  assert.equal(sha(original),file.sha256,'Core source changed since build');assert.equal(sha(output),file.sha256,'Core build changed');
}
const oldFetch=globalThis.fetch;let attempts=0;
try{
  globalThis.fetch=async()=>{attempts++;throw new Error('Outbound network is disabled in preflight');};
  const worker=await import(pathToFileURL(resolve(root,'dist/server/index.js')).href+`?preflight=${Date.now()}`);
  assert.equal(typeof worker.default?.fetch,'function');
  const blocked=await worker.default.fetch(new Request('https://local.example/api/zotero/status'),{},{});
  assert.equal(blocked.status,503,'Missing production configuration must fail closed');
  assert.notEqual((await blocked.json()).live_ready,true);
  const preserved=await worker.default.fetch(new Request('https://local.example/archive-preflight-only'),{ASSETS:{fetch:async()=>new Response('archive-preserved',{status:200})}},{});
  assert.equal(await preserved.text(),'archive-preserved');
  assert.equal(attempts,0,'Readiness probes must not call Zotero');
}finally{globalThis.fetch=oldFetch;}
const db=new DatabaseSync(':memory:');
const migrations=(await readdir(resolve(root,'migrations'))).filter(x=>/^\d{4}_[a-z0-9_]+\.sql$/.test(x)).sort();
for(const name of migrations)db.exec(await readFile(resolve(root,'migrations',name),'utf8'));
const tableCount=db.prepare("SELECT count(*) AS n FROM sqlite_master WHERE type='table' AND name LIKE 'zotero_%'").get().n;
db.close();result.local_core=true;result.migrations={count:migrations.length,sqlite_tables:tableCount,live_database_changed:false};
let archive;
if(options['--archive-dir']){
  archive=await inventory(options['--archive-dir'],{archive:true});
  result.archive={files:archive.length,bytes:archive.reduce((n,f)=>n+f.bytes,0),largest_asset_bytes:Math.max(...archive.map(f=>f.bytes)),read_only:true};
}
if(options['--adapter-dir']){
  const directory=resolve(options['--adapter-dir']);const block=message=>result.adapter.blockers.push(message);
  const read=async name=>JSON.parse(await readFile(resolve(directory,name),'utf8'));
  assert.equal(await exists(resolve(directory,'.sites-runtime/archive-refresh-pending.json')),false,'Interrupted archive refresh is fenced');
  const source=await read('.openai/hosting.json');
  assert.equal(source.d1,'DB');assert.equal(source.r2,'BUCKET');assert.equal(source.static,undefined);
  for(const directoryName of ['worker','src','migrations']){
    const originals=await inventory(resolve(root,directoryName));const staged=await inventory(resolve(directory,'zotero',directoryName));
    assert.deepEqual(staged,originals,'Materialized runtime differs from current source; refresh before build');
  }
  if(typeof source.project_id!=='string'||!source.project_id.trim())block('Existing Site identity has not been supplied by owner');
  const receiptPath='.sites-runtime/zotero-build-receipt.json';
  if(!await exists(resolve(directory,receiptPath)))block('Verified Vinext build receipt is missing');
  else{
    const receipt=await read(receiptPath);
    const expectedInputs=[];for(const name of BUILD_INPUT_DIRECTORIES)expectedInputs.push(...(await inventory(resolve(directory,name))).map(file=>({...file,path:name+'/'+file.path})));
    for(const name of BUILD_INPUT_FILES){const bytes=await readFile(resolve(directory,name));expectedInputs.push({path:name,bytes:bytes.length,sha256:sha(bytes)});}
    const sorted=rows=>[...rows].sort((a,b)=>a.path.localeCompare(b.path,'en'));
    assert.deepEqual(sorted(receipt.inputs),sorted(expectedInputs),'Adapter build input receipt omits or changes actual source');
    assert.deepEqual(sorted(receipt.outputs),sorted(await inventory(resolve(directory,'dist'))),'Adapter build output receipt omits or changes actual output');
    for(const file of receipt.inputs)assert.equal(sha(await readFile(resolve(directory,file.path))),file.sha256,'Adapter source changed since build');
    for(const file of receipt.outputs)assert.equal(sha(await readFile(resolve(directory,'dist',file.path))),file.sha256,'Adapter output changed since build');
    const config=await read('dist/server/wrangler.json');
    assert.equal(config.assets?.binding,'ASSETS');assert.equal(config.assets?.run_worker_first,true);assert.equal(config.assets?.directory,'../client');
    assert.ok(config.d1_databases?.some(binding=>binding.binding==='DB'));assert.ok(config.r2_buckets?.some(binding=>binding.binding==='BUCKET'));
    assert.deepEqual(await read('dist/.openai/hosting.json'),source);
    const mapping=await read('dist/.openai/drizzle/zotero-source-migrations.json');
    assert.equal(mapping.migrations.length,migrations.length);
    const journal=await read('dist/.openai/drizzle/meta/_journal.json');assert.equal(journal.entries.length,migrations.length);
    for(const migration of mapping.migrations){
      assert.equal(sha(await readFile(resolve(root,'migrations',migration.source))),migration.source_sha256);
      assert.equal(sha(await readFile(resolve(directory,'dist/.openai/drizzle',migration.output))),migration.output_sha256);
    }
    const retained=await read('.sites-runtime/archive-inventory.json');
    if(archive)assert.deepEqual(retained.files,archive,'Supplied original archive differs');
    for(const file of retained.files)for(const prefix of ['public','dist/client'])assert.equal(sha(await readFile(resolve(directory,prefix,file.path))),file.sha256,'Retained archive differs');
    result.adapter.built=true;result.adapter.artifact_directory=resolve(directory,'dist');result.adapter.archive_files_preserved=retained.files.length;
    result.adapter.worker_bytes=receipt.outputs.filter(f=>f.path.startsWith('server/')).reduce((n,f)=>n+f.bytes,0);
  }
  result.adapter.artifact_ready_for_owner_packaging=result.adapter.built&&result.adapter.blockers.length===0;
}else result.adapter.blockers.push('No private Sites adapter checkout was supplied');
console.log(JSON.stringify(result,null,2));
if(options['--require-hostable']&&!result.adapter.artifact_ready_for_owner_packaging)process.exitCode=2;
