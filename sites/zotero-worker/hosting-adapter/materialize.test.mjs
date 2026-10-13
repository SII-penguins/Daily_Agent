import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, writeFile, mkdir, symlink, rm, access } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';
import { inventory } from './materialize.mjs';
const script=fileURLToPath(new URL('./materialize.mjs',import.meta.url));
const source=fileURLToPath(new URL('../',import.meta.url));
const temp=async fn=>{const dir=await mkdtemp(resolve(tmpdir(),'zotero-hosting-test-'));try{await fn(dir);}finally{await rm(dir,{recursive:true,force:true});}};
test('archive inventory preserves bytes and refuses missing homepage',()=>temp(async dir=>{
  await assert.rejects(inventory(dir,{archive:true}),/index.html/);
  await writeFile(resolve(dir,'index.html'),'synthetic archive test');
  const files=await inventory(dir,{archive:true});assert.equal(files.length,1);assert.equal(files[0].bytes,22);assert.match(files[0].sha256,/^[a-f0-9]{64}$/);
}));
test('archive inventory refuses symlinks and private/reserved files',()=>temp(async dir=>{
  await writeFile(resolve(dir,'index.html'),'synthetic');await symlink('index.html',resolve(dir,'linked.html'));
  await assert.rejects(inventory(dir,{archive:true}),/Symlinks/);await rm(resolve(dir,'linked.html'));
  await writeFile(resolve(dir,'.secret'),'synthetic');await assert.rejects(inventory(dir,{archive:true}),/private path/);
}));
test('materializer rejects destinations inside source before writing',()=>temp(async dir=>{
  const destination=resolve(source,'test-public-output-forbidden');
  const r=spawnSync(process.execPath,[script,'--destination',destination,'--archive-dir',dir,'--plugin-root',dir],{encoding:'utf8'});
  assert.equal(r.status,1);assert.match(r.stderr,/inside the public source/);await assert.rejects(access(destination));
}));
test('materializer rejects destination symlink before any copy',()=>temp(async dir=>{
  const target=resolve(dir,'target');await mkdir(target);const link=resolve(dir,'link');await symlink(target,link);
  const r=spawnSync(process.execPath,[script,'--destination',link,'--archive-dir',dir,'--plugin-root',dir],{encoding:'utf8'});
  assert.equal(r.status,1);assert.match(r.stderr,/not a symlink/);
}));
