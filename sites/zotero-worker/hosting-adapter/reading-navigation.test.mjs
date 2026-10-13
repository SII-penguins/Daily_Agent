import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { planNavigationRemap,verifyNavigationDelta,reverseNavigationRemap,remapReadingURL,validateMappings,inventory,digest,NAVIGATION_FILES } from './reading-navigation.mjs';
const key='reports/2030-01-01-original-0123456789ab',from='/'+key+'/reading.html',to='/zotero/reports/2030-01-01-original',mappings=[{from,to}];
const clone=files=>new Map([...files].map(([p,b])=>[p,Buffer.from(b)]));
function fixture(){
 const html=`<!doctype html><html><head><style>.a::after{content:'href="${from}"'}</style></head><body><!-- <a href="${from}">comment</a> --><a title='href="${from}"' href="${from}">阅读</a><a href="${from}#paper-1">论文</a><a href='${from}#repo-2'>项目</a><a href="/${key}/index.html">原始页面</a><a href="${from}?unchanged=1">Query untouched</a><p>${from}</p></body></html>`;
 const data=[{date:'2030-01-01',editions:[{reading:from,original:`/${key}/index.html`,titles:[{title:from,anchor:'paper-1'}]}]}];
 const files=new Map([['index.html',Buffer.from(html)],['archive/index.html',Buffer.from(html)],['assets/archive-browser.js',Buffer.from('window.DAILY_ARCHIVE = '+JSON.stringify(data)+';\n// '+from+'\nconsole.log("behavior preserved");\n')],[key+'/index.html',Buffer.from('original')],[key+'/reading.html',Buffer.from('sealed reading')]]);
 const meta=Object.fromEntries([...files].map(([name,b])=>[name,{sha256:digest(b),size:b.length}]));
 files.set('manifest.json',Buffer.from(JSON.stringify({schema_version:1,entries:[{key,page:key+'/index.html',date:'2030-01-01',extra:'留存'}],files:meta,navigation:{schema:'daily-agent-archive-navigation-v2',reading_pages:{[key]:from.slice(1)},preferred_by_date:{'2030-01-01':from.slice(1)},other:from.slice(1)}},null,2)+'\n'));
 return files;
}
function refreshManifest(files){const m=JSON.parse(files.get('manifest.json'));m.files=Object.fromEntries([...files].filter(([name])=>name!=='manifest.json').map(([name,b])=>[name,{sha256:digest(b),size:b.length}]));files.set('manifest.json',Buffer.from(JSON.stringify(m,null,2)+'\n'));return files;}
const remap=before=>planNavigationRemap({files:before,mappings});
const prove=(before,after,options={})=>verifyNavigationDelta({before,after,mappings,...options});

test('exact URL match preserves all fragments and ignores lookalikes and queries',()=>{
 assert.equal(remapReadingURL(from,mappings),to);assert.equal(remapReadingURL(from+'#paper-1',mappings),to+'#paper-1');assert.equal(remapReadingURL(from+'#含中文',mappings),to+'#含中文');
 for(const v of ['https://elsewhere.invalid'+from,'/other'+from,from+'?q=x',from+'/suffix',from+'2',from.slice(1)])assert.equal(remapReadingURL(v,mappings),v);
 assert.equal(remapReadingURL(from.slice(1),mappings,{relativeSource:true}),to);
});
test('generic maps contain no date or Site identity assumptions',()=>{assert.deepEqual(validateMappings(mappings),mappings);});
test('reject cross-origin, traversal, active URL, query, fragment, duplicate and ambiguous rules',()=>{
 for(const bad of [[],[{from,to:'https://example.invalid/path'}],[{from:'//evil.invalid/reading.html',to}],[{from:from+'/../reading.html',to}],[{from,to:'javascript:alert(1)'}],[{from,to:to+'?x=1'}],[{from,to:to+'#x'}],[{from,to,extra:true}],[{from,to},{from,to:to+'-other'}],[{from,to},{from:'/reports/other/reading.html',to}]])assert.throws(()=>validateMappings(bad));
});
test('pure exact remap keeps originals, titles, comments, text, CSS, query links and script behavior',()=>{
 const before=fixture(),baseline=inventory(before),planned=remap(before),proof=prove(before,planned.files);
 assert.deepEqual(inventory(before),baseline);assert.equal(proof.exact_reverse_verified,true);assert.deepEqual(proof.changed_files,[...NAVIGATION_FILES].sort());assert.equal(proof.sealed_files,2);assert.deepEqual(proof.patch_counts,{'index.html':3,'archive/index.html':3,'assets/archive-browser.js':1,'manifest.json':8});
 const h=planned.files.get('index.html').toString();assert.ok(h.includes('href="'+to+'#paper-1"'));assert.ok(h.includes("href='"+to+"#repo-2'"));assert.ok(h.includes('<!-- <a href="'+from+'">comment</a> -->'));assert.ok(h.includes("title='href=\""+from+"\"'"));assert.ok(h.includes('<p>'+from+'</p>'));
 assert.ok(planned.files.get('assets/archive-browser.js').toString().includes('"title":"'+from+'"'));
 assert.equal(JSON.parse(planned.files.get('manifest.json')).navigation.other,from.slice(1));
 const reversed=reverseNavigationRemap({files:planned.files,patches:planned.patches});for(const [name,bytes]of before)assert.ok(bytes.equals(reversed.get(name)));
});
test('already mapped baseline is idempotent; change-required proof rejects no-op',()=>{
 const mapped=remap(fixture()).files,again=remap(mapped);assert.deepEqual(inventory(mapped),inventory(again.files));assert.throws(()=>prove(mapped,again.files),/no changes/);assert.deepEqual(prove(mapped,again.files,{requireChange:false}).changed_files,[]);
});
test('mutated first-line original URL and title both fail even with refreshed manifest hashes',()=>{
 for(const mutate of [s=>s.replace('"original":"/'+key+'/index.html"','"original":"/wrong"'),s=>s.replace('"title":"'+from+'"','"title":"changed"')]){const before=fixture(),after=remap(before).files;after.set('assets/archive-browser.js',Buffer.from(mutate(after.get('assets/archive-browser.js').toString())));refreshManifest(after);assert.throws(()=>prove(before,after),/Unapproved/);}
});
test('JavaScript tail mutation fails even with internally consistent manifest',()=>{const before=fixture(),after=remap(before).files;after.set('assets/archive-browser.js',Buffer.concat([after.get('assets/archive-browser.js'),Buffer.from('alert(1);')]));refreshManifest(after);assert.throws(()=>prove(before,after),/Unapproved/);});
test('any HTML non-URL byte, fragment, original link or arbitrary extra target is rejected',()=>{
 for(const mutate of [s=>s.replace('阅读','改写'),s=>s.replace('#paper-1','#paper-2'),s=>s.replace(key+'/index.html','other/index.html'),s=>s+'<a href="'+to+'">extra</a>',s=>s+' ']){const before=fixture(),after=remap(before).files;after.set('index.html',Buffer.from(mutate(after.get('index.html').toString())));refreshManifest(after);assert.throws(()=>prove(before,after),/Unapproved/);}
});
test('sealed files cannot be changed, removed or added',()=>{
 const before=fixture();for(const name of [key+'/index.html',key+'/reading.html']){const after=remap(before).files;after.set(name,Buffer.from('replacement'));refreshManifest(after);assert.throws(()=>prove(before,after),/Unapproved/);}
 const removed=remap(before).files;removed.delete(key+'/reading.html');assert.throws(()=>prove(before,removed),/add or remove/);
 const added=remap(before).files;added.set('assets/extra.js',Buffer.from('extra'));assert.throws(()=>prove(before,added),/add or remove/);
});
test('manifest entry, file entry, metadata, navigation, whitespace changes rejected',()=>{
 for(const mutate of [m=>m.entries[0].extra='changed',m=>m.entries.push(m.entries[0]),m=>m.files[key+'/reading.html'].sha256='a'.repeat(64),m=>m.navigation.other=to,m=>m.navigation.preferred_by_date['2030-01-02']=to]){const before=fixture(),after=remap(before).files,m=JSON.parse(after.get('manifest.json'));mutate(m);after.set('manifest.json',Buffer.from(JSON.stringify(m,null,2)+'\n'));assert.throws(()=>prove(before,after),/Unapproved/);}
 const before=fixture(),after=remap(before).files;after.set('manifest.json',Buffer.from(JSON.stringify(JSON.parse(after.get('manifest.json')))));assert.throws(()=>prove(before,after),/Unapproved/);
});
test('incomplete remap and arbitrary before/after reversal are rejected',()=>{
 const before=fixture(),after=remap(before).files;after.set('archive/index.html',before.get('archive/index.html'));refreshManifest(after);assert.throws(()=>prove(before,after),/Unapproved/);
 const planned=remap(before),corrupt=clone(planned.files);corrupt.set('index.html',Buffer.from('broken'));assert.throws(()=>reverseNavigationRemap({files:corrupt,patches:planned.patches}),/stale/);
});
test('malformed or duplicate-key JSON, wrong script header, missing source and invalid UTF-8 fail closed',()=>{
 let files=fixture();files.set('manifest.json',Buffer.from(files.get('manifest.json').toString().replace('"schema_version": 1','"schema_version": 1, "schema_version": 1')));assert.throws(()=>remap(files),/Duplicate JSON/);
 files=fixture();files.set('assets/archive-browser.js',Buffer.from('window.UNAPPROVED = [];\n'));refreshManifest(files);assert.throws(()=>remap(files),/data first line/);
 files=fixture();files.set('assets/archive-browser.js',Buffer.from('window.DAILY_ARCHIVE = [{"editions":[],"editions":[]}];\n'));refreshManifest(files);assert.throws(()=>remap(files),/Duplicate JSON/);
 files=fixture();files.delete(key+'/reading.html');refreshManifest(files);assert.throws(()=>remap(files),/sealed source/);
 files=fixture();files.set('index.html',Buffer.from([0xff]));refreshManifest(files);assert.throws(()=>remap(files),/encoded data/);
});
test('future ordinary append is separately retained and mapped without regenerating old reading files',()=>{
 const refreshed=fixture(),oldReading=Buffer.from(refreshed.get(key+'/reading.html')),newKey='reports/2030-01-02-original-fedcba987654';
 refreshed.set(newKey+'/index.html',Buffer.from('new original'));refreshed.set(newKey+'/reading.html',Buffer.from('new reading'));
 const manifest=JSON.parse(refreshed.get('manifest.json'));manifest.entries.push({key:newKey,page:newKey+'/index.html',date:'2030-01-02'});refreshed.set('manifest.json',Buffer.from(JSON.stringify(manifest,null,2)+'\n'));refreshManifest(refreshed);
 const after=remap(refreshed).files;assert.equal(prove(refreshed,after).sealed_files,4);assert.ok(after.get(key+'/reading.html').equals(oldReading));assert.ok(after.get(newKey+'/reading.html').equals(refreshed.get(newKey+'/reading.html')));
});
