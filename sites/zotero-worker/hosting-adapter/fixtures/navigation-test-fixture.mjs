// Synthetic fixtures only. The historical renderer runs in a disposable temp
// directory; its old report output is never admitted to a serving tree.
import assert from 'node:assert/strict';
import {mkdtemp,mkdir,readFile,writeFile,readdir,rm,cp} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {dirname,resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
import {spawnSync} from 'node:child_process';
import {reviewedFixture} from '../../test/reviewed-issue-fixture.mjs';
import {digest,planNavigationRemap} from '../reading-navigation.mjs';
import {composeNavigationAppend,verifyNavigationAppend} from '../compose-navigation-append.mjs';
export const metadata=b=>({sha256:digest(b),size:b.length});
export const cloneTree=files=>new Map([...files].map(([p,b])=>[p,Buffer.from(b)]));
export const setManifest=(files,m)=>files.set('manifest.json',Buffer.from(JSON.stringify(m,null,2)+'\n'));
export function rehashManifest(files){const m=JSON.parse(files.get('manifest.json'));m.files=Object.fromEntries([...files].filter(([p])=>p!=='manifest.json').map(([p,b])=>[p,metadata(b)]));setManifest(files,m);return files;}
export async function writeTree(root,files){for(const [p,b]of files){await mkdir(dirname(resolve(root,p)),{recursive:true});await writeFile(resolve(root,p),b);}}
async function readTree(root,prefix=''){const files=new Map();for(const ent of await readdir(root,{withFileTypes:true})){const p=prefix+ent.name;if(ent.isDirectory()){for(const [n,b]of await readTree(resolve(root,ent.name),p+'/'))files.set(n,b);}else files.set(p,await readFile(resolve(root,ent.name)));}return files;}
async function render(files){const work=await mkdtemp(resolve(tmpdir(),'navigation-integration-render-'));try{await cp(fileURLToPath(new URL('./renderer/scripts',import.meta.url)),resolve(work,'scripts'),{recursive:true});await writeTree(resolve(work,'dist'),files);const run=spawnSync('python3',[resolve(work,'scripts/build_archive_navigation.py')],{encoding:'utf8',timeout:20000,maxBuffer:4*1024*1024});assert.equal(run.status,0,run.stderr);return await readTree(resolve(work,'dist'));}finally{await rm(work,{recursive:true,force:true});}}
const mapping=f=>({from:'/'+f.entry.key+'/reading.html',to:'/zotero/reports/'+f.issue.issue_id});
export async function navigationFixture({append=false,mapped=true}={}){
 const old=await reviewedFixture('2000-01-01');Object.assign(old.entry,{category:'reports',created_at:'2000-01-01T01:00:00Z'});
 const seed=new Map([[old.entry.page,Buffer.from(old.html)],['index.html',Buffer.from('<!doctype html><html><head><title>Archive</title><meta http-equiv="Content-Security-Policy" content="default-src \'none\'"></head><body></body></html>')]]);
 setManifest(seed,{schema_version:1,entries:[old.entry],files:Object.fromEntries([...seed].map(([p,b])=>[p,metadata(b)]))});
 let before=await render(seed);const mappings=[mapping(old)];
 if(!append)return {before,after:planNavigationRemap({files:before,mappings}).files,mappings,old,issues:[old.issue],priorIssues:[],target:old};
 if(mapped)before=planNavigationRemap({files:before,mappings}).files;
 const fresh=await reviewedFixture('2000-01-02');Object.assign(fresh.entry,{category:'reports',created_at:'2000-01-02T01:00:00Z'});
 const input=cloneTree(before),m=JSON.parse(input.get('manifest.json'));input.set(fresh.entry.page,Buffer.from(fresh.html));m.entries.push(fresh.entry);m.files[fresh.entry.page]=metadata(fresh.html);setManifest(input,m);
 const candidate=await render(input);mappings.push(mapping(fresh));
 const result=composeNavigationAppend({baseline:before,candidate,approvedEntries:[fresh.entry],mappings});
 // Publication receives only committed output; prove re-derivation is idempotent.
 verifyNavigationAppend({baseline:before,candidate:result.files,approvedEntries:[fresh.entry],mappings,after:result.files});
 return {before,after:result.files,candidate,mappings,old,fresh,issues:[old.issue,fresh.issue],priorIssues:[old.issue],target:fresh};
}
