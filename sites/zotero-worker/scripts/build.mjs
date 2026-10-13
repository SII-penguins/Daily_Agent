/** Dependency-free, LOCAL-ONLY staging of the complete ESM application graph.
 * The deployable Worker + ASSETS bundle is built separately by hosting-adapter.
 */
import { mkdir, copyFile, readFile, writeFile, readdir, lstat } from 'node:fs/promises';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createHash } from 'node:crypto';
import { spawnSync } from 'node:child_process';
const root = fileURLToPath(new URL('../', import.meta.url));
const files=[];
async function copyTree(directory, target, prefix) {
  for (const entry of (await readdir(directory,{withFileTypes:true})).sort((a,b)=>a.name.localeCompare(b.name,'en'))) {
    const source=resolve(directory,entry.name), output=resolve(target,entry.name), name=`${prefix}/${entry.name}`;
    if(entry.isSymbolicLink()||(!entry.isDirectory()&&!entry.isFile()))throw new Error('Runtime source may not contain symlinks or special files');
    if(entry.isDirectory()){await copyTree(source,output,name);continue;}
    if(!/\.(?:mjs|js|sql)$/.test(entry.name))throw new Error(`Unexpected runtime source type: ${name}`);
    if(!name.endsWith('.sql')){
      const checked=spawnSync(process.execPath,['--check',source],{encoding:'utf8'});
      if(checked.error||checked.status!==0)throw new Error(`Syntax check failed: ${name}\n${checked.stderr||''}`);
    }
    await mkdir(dirname(output),{recursive:true});
    await copyFile(source,output);
    const bytes=await readFile(output);
    files.push({path:name,bytes:bytes.length,sha256:createHash('sha256').update(bytes).digest('hex')});
  }
}
// Never remove the existing dist tree: it could contain retained archive files.
await mkdir(resolve(root,'dist/server'),{recursive:true});
for(const name of ['worker','src'])await copyTree(resolve(root,name),resolve(root,'dist/server',name),name);
await copyTree(resolve(root,'migrations'),resolve(root,'dist/migrations'),'migrations');
await writeFile(resolve(root,'dist/server/package.json'),'{"private":true,"type":"module"}\n');
await writeFile(resolve(root,'dist/server/index.js'),"// LOCAL-ONLY module graph. Publish the separately built Vinext adapter instead.\nexport * from './worker/index.js';\nexport { default } from './worker/index.js';\n");
await writeFile(resolve(root,'dist/BUILD-STATUS.json'),JSON.stringify({kind:'local-esm-graph',sites_deployable:false,reason:'Vinext/ASSETS adapter build, identity, generated migrations, and deployment checks are separate',files},null,2)+'\n');
console.log(JSON.stringify({built:'local-esm-graph',runtime_files:files.length,sites_deployable:false,entrypoint:'dist/server/index.js'}));
