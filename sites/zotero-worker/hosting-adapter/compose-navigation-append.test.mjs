import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp,mkdir,readFile,writeFile,readdir,rm } from 'node:fs/promises';
import { dirname,resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';
import { composeNavigationAppend,verifyNavigationAppend } from './compose-navigation-append.mjs';
import { digest,inventory,planNavigationRemap } from './reading-navigation.mjs';
const ROOT=dirname(fileURLToPath(import.meta.url));
const SITE=resolve(ROOT,'fixtures/renderer');
const meta=b=>({sha256:digest(b),size:b.length});
const cloneFiles=files=>new Map([...files].map(([p,b])=>[p,Buffer.from(b)]));
const json=(files,name='manifest.json')=>JSON.parse(files.get(name));
const setJSON=(files,m,name='manifest.json')=>files.set(name,Buffer.from(JSON.stringify(m,null,2)+'\n'));
function entry(date,html,edition='original') {const hash=digest(html),key=`reports/${date}-${edition}-${hash.slice(0,12)}`;return {key,page:key+'/index.html',date,category:'reports',kind:'report',format:'html',identity:hash,source_sha256:hash,approval_sha256:'a'.repeat(64),created_at:date+'T01:00:00Z',audit:'Reviewed synthetic source'};}
const original=(date,title='A &amp; B')=>Buffer.from(`<!doctype html><html><head><title>${date}</title><meta http-equiv="Content-Security-Policy" content="default-src 'none'"><style>body{color:black}</style></head><body><article id="paper-1"><h2>${title}</h2><p>Scientific source must stay exact.</p></article><article id="repo-1"><h3>工具 &lt;One&gt;</h3><p>Repo.</p></article></body></html>`);
async function readTree(root,prefix=''){const files=new Map();for(const ent of await readdir(root,{withFileTypes:true})){const p=prefix+ent.name;if(ent.isDirectory()){for(const [n,b]of await readTree(resolve(root,ent.name),p+'/'))files.set(n,b);}else files.set(p,await readFile(resolve(root,ent.name)));}return files;}
async function writeTree(root,files){for(const [p,b]of files){await mkdir(dirname(resolve(root,p)),{recursive:true});await writeFile(resolve(root,p),b);}}
async function render(files){const work=await mkdtemp(resolve(ROOT,'render-fixture-'));try{await mkdir(resolve(work,'scripts/archive'),{recursive:true});for(const f of ['build_archive_navigation.py','archive_page.py','archive/archive-browser.js','archive/archive-browser.css'])await writeFile(resolve(work,'scripts',f),await readFile(resolve(SITE,'scripts',f)));await writeTree(resolve(work,'dist'),files);const run=spawnSync('python3',[resolve(work,'scripts/build_archive_navigation.py')],{encoding:'utf8',timeout:20000,maxBuffer:4*1024*1024});assert.equal(run.status,0,run.stderr);return await readTree(resolve(work,'dist'));}finally{await rm(work,{recursive:true,force:true});}}
async function fixture({mapped=false,sameDate=false}={}){
 let baseline;
 {const html=original('2030-01-01'),old=entry('2030-01-01',html),seed=new Map([[old.page,html],['index.html',Buffer.from('<!doctype html><html><head><title>Archive</title><meta http-equiv="Content-Security-Policy" content="default-src \'none\'"></head><body></body></html>')]]);setJSON(seed,{schema_version:1,entries:[old],files:Object.fromEntries([...seed].map(([p,b])=>[p,meta(b)]))});baseline=await render(seed);}
 const oldEntry=json(baseline).entries.filter(e=>e.category==='reports').at(-1),oldMapping={from:'/'+oldEntry.key+'/reading.html',to:'/zotero/reports/'+oldEntry.date+'-original'};
 if(mapped)baseline=planNavigationRemap({files:baseline,mappings:[oldMapping]}).files;
 const date=sameDate?'2030-01-01':'2030-01-02',html=original(date,'New &amp; Verified'),fresh=entry(date,html,sameDate?'corrected-02':'original');if(sameDate)fresh.created_at=date+'T02:00:00Z';
 const input=cloneFiles(baseline),m=json(input);input.set(fresh.page,html);m.entries.push(fresh);m.files[fresh.page]=meta(html);setJSON(input,m);const candidate=await render(input);
 const mapping={from:'/'+fresh.key+'/reading.html',to:'/zotero/reports/'+date+(sameDate?'-corrected-02':'-original')};
 return {baseline,candidate,approvedEntries:[fresh],mappings:[oldMapping,mapping],oldEntry,fresh};
}
const proof=input=>composeNavigationAppend(input);
const dataset=files=>JSON.parse(files.get('assets/archive-browser.js').toString().split('\n')[0].slice('window.DAILY_ARCHIVE = '.length,-1));
const changeCandidate=(input,name,fn)=>{const copy={...input,candidate:cloneFiles(input.candidate)};copy.candidate.set(name,Buffer.from(fn(copy.candidate.get(name).toString())));return copy;};
let synthetic;
test('synthetic two-date disposable render restores historical reading bytes and all old metadata',async()=>{
 synthetic=await fixture();const {baseline,candidate,oldEntry,fresh}=synthetic,before=inventory(baseline),result=proof(synthetic);
 assert.notEqual(digest(candidate.get(oldEntry.key+'/reading.html')),digest(baseline.get(oldEntry.key+'/reading.html')),'Fixture must reproduce historical reading regeneration');
 for(const [p,b]of baseline)if(!['index.html','archive/index.html','assets/archive-browser.js','manifest.json'].includes(p))assert.ok(b.equals(result.files.get(p)),p);
 assert.deepEqual(json(result.files).entries[0],json(baseline).entries[0]);assert.deepEqual(json(result.files).files[oldEntry.key+'/reading.html'],json(baseline).files[oldEntry.key+'/reading.html']);assert.notDeepEqual(json(result.files).files[oldEntry.key+'/reading.html'],json(candidate).files[oldEntry.key+'/reading.html']);
 assert.equal(result.proof.new_titles_bound_to_sealed_originals,true);assert.equal(result.proof.historical_entries_preserved,1);assert.equal(result.files.size,baseline.size+2);assert.deepEqual(dataset(result.files).map(d=>d.date),['2030-01-02','2030-01-01']);assert.equal(dataset(result.files)[0].editions[0].reading,synthetic.mappings[1].to);assert.equal(dataset(result.files)[0].editions[0].titles[0].title,'New & Verified');assert.ok(result.files.get('index.html').toString().includes(synthetic.mappings[1].to+'#paper-1'));assert.equal(json(result.files).navigation.search_title_count,4);assert.deepEqual(inventory(baseline),before);verifyNavigationAppend({...synthetic,after:result.files});
});
test('old entries and file metadata are restored even when renderer changed or omitted them',()=>{
 const input={...synthetic,candidate:cloneFiles(synthetic.candidate)},m=json(input.candidate);m.entries[0].audit='regenerated old audit';m.files[synthetic.oldEntry.page]={size:0,sha256:'0'.repeat(64)};delete m.files[synthetic.oldEntry.key+'/reading.html'];setJSON(input.candidate,m);const result=proof(input);assert.deepEqual(json(result.files).entries[0],json(input.baseline).entries[0]);assert.deepEqual(json(result.files).files[synthetic.oldEntry.page],json(input.baseline).files[synthetic.oldEntry.page]);
});
test('same-day addition preserves old edition and makes sealed latest edition preferred',async()=>{const input=await fixture({sameDate:true}),result=proof(input),d=dataset(result.files);assert.equal(d.length,1);assert.equal(d[0].editions.length,2);assert.equal(d[0].editions[0].original,'/'+input.fresh.page);assert.equal(d[0].editions[1].original,'/'+input.oldEntry.page);assert.equal(json(result.files).navigation.preferred_by_date['2030-01-01'],input.mappings[1].to);});
test('already mapped baseline tolerates disposable renderer legacy URLs and reprojects exactly',async()=>{const input=await fixture({mapped:true}),result=proof(input);assert.equal(dataset(result.files)[1].editions[0].reading,input.mappings[0].to);});
test('unapproved new entry, missing new mapping, mismatched approval and altered sealed source reject',()=>{
 assert.throws(()=>proof({...synthetic,approvedEntries:[]}),/new sealed entries/);assert.throws(()=>proof({...synthetic,mappings:[synthetic.mappings[0]]}),/Every new report/);assert.throws(()=>proof({...synthetic,approvedEntries:[{...synthetic.fresh,approval_sha256:'wrong'}]}),/sealed report/);const altered=changeCandidate(synthetic,synthetic.fresh.page,s=>s+'tamper');assert.throws(()=>proof(altered),/sealed report/);
});
test('new navigation title, anchor, original URL and historical row edits reject',()=>{
 for(const [from,to]of [['New & Verified','Unapproved title'],['"anchor": "paper-1"','"anchor": "missing"'],['/'+synthetic.fresh.page,'/wrong/index.html'],['A & B','Changed old title']]){const input=changeCandidate(synthetic,'assets/archive-browser.js',s=>s.replace(from,to));assert.throws(()=>proof(input),/Navigation row|Unapproved navigation row|Missing retained/);}
});
test('JS tail, arbitrary HTML, original links and fragment tampering reject',()=>{
 assert.throws(()=>proof(changeCandidate(synthetic,'assets/archive-browser.js',s=>s+'\nalert(1)')),/behavior changed/);
 for(const mutate of [s=>s.replace('阅读最新日报','Unapproved label'),s=>s.replace('#paper-1','#paper-99'),s=>s.replace('/'+synthetic.fresh.page,'/wrong/index.html'),s=>s+'<script>alert(1)</script>'])assert.throws(()=>proof(changeCandidate(synthetic,'index.html',mutate)),/unrelated changes|invalid projected/);
});
test('unapproved new paths, forged asset hash and orphan metadata reject',()=>{
 const extra={...synthetic,candidate:cloneFiles(synthetic.candidate)};extra.candidate.set('assets/extra.js',Buffer.from('x'));assert.throws(()=>proof(extra),/Unsupported public path/);
 const badAsset={...synthetic,candidate:cloneFiles(synthetic.candidate)};badAsset.candidate.set('assets/'+ 'a'.repeat(64)+'.png',Buffer.from('x'));assert.throws(()=>proof(badAsset),/content hash/);
 const orphan={...synthetic,candidate:cloneFiles(synthetic.candidate)},m=json(orphan.candidate);m.files['reports/orphan/index.html']=meta(Buffer.from('x'));setJSON(orphan.candidate,m);assert.throws(()=>proof(orphan),/unadmitted/);
});
test('new reading scientific body cannot differ from sealed original',()=>{assert.throws(()=>proof(changeCandidate(synthetic,synthetic.fresh.key+'/reading.html',s=>s.replace('Scientific source must stay exact.','Modified science.'))),/New file does not match|scientific source/);});
test('post-composition mutations fail independently even if new manifest hashes match',()=>{
 const result=proof(synthetic),after=cloneFiles(result.files);after.set(synthetic.oldEntry.key+'/reading.html',Buffer.from('regenerated'));const m=json(after);m.files[synthetic.oldEntry.key+'/reading.html']=meta(after.get(synthetic.oldEntry.key+'/reading.html'));setJSON(after,m);assert.throws(()=>verifyNavigationAppend({...synthetic,after}),/Unapproved composed/);
});

test('new reading navigation injection is rejected after attacker repairs manifest metadata',()=>{
 const input=changeCandidate(synthetic,synthetic.fresh.key+'/reading.html',s=>s.replace('aria-label="日报历史导航"','aria-label="日报历史导航" onclick="alert(1)"')),m=json(input.candidate);m.files[synthetic.fresh.key+'/reading.html']=meta(input.candidate.get(synthetic.fresh.key+'/reading.html'));setJSON(input.candidate,m);assert.throws(()=>proof(input),/exact retained renderer template/);
});
test('valid content-addressed new asset is admitted while forged metadata is rejected',()=>{
 const input={...synthetic,candidate:cloneFiles(synthetic.candidate)},bytes=Buffer.from('synthetic png bytes'),path='assets/'+digest(bytes)+'.png';input.candidate.set(path,bytes);const m=json(input.candidate);m.files[path]=meta(bytes);setJSON(input.candidate,m);assert.ok(proof(input).files.get(path).equals(bytes));m.files[path].size++;setJSON(input.candidate,m);assert.throws(()=>proof(input),/rendered manifest/);
});
test('historical dataset row formatting survives regenerated canonical JSON',()=>{
 const input={...synthetic,baseline:cloneFiles(synthetic.baseline)},path='assets/archive-browser.js';input.baseline.set(path,Buffer.from(input.baseline.get(path).toString().replace('[{"date":','[{  "date":')));const m=json(input.baseline);m.files[path]=meta(input.baseline.get(path));setJSON(input.baseline,m);const result=proof(input);assert.ok(result.files.get(path).toString().includes('{  "date": "2030-01-01"'));
});
test('duplicate candidate JSON keys and missing old navigation rows reject',()=>{
 const duplicate=changeCandidate(synthetic,'assets/archive-browser.js',s=>s.replace('"date": "2030-01-02"','"date": "2030-01-02", "date": "2030-01-02"'));assert.throws(()=>proof(duplicate),/Duplicate JSON/);
 const missing=changeCandidate(synthetic,'assets/archive-browser.js',s=>{const [line,...tail]=s.split('\n'),d=JSON.parse(line.slice('window.DAILY_ARCHIVE = '.length,-1));d.pop();return 'window.DAILY_ARCHIVE = '+JSON.stringify(d)+';\n'+tail.join('\n');});assert.throws(()=>proof(missing),/Missing retained/);
});
