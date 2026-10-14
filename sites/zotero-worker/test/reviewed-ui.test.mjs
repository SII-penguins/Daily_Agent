import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {reviewedClientSource} from '../src/reviewed-ui.mjs';
class Element{
  constructor(){this.dataset={};this.listeners={};this.children=[];this.checked=true;this.disabled=false;this.hidden=false;this.textContent='';}
  addEventListener(type,listener){this.listeners[type]=listener;}
  append(e){this.children.push(e);}
  replaceChildren(){this.children=[];}
  emit(type){return this.listeners[type]?.();}
  set innerHTML(_){throw new Error('Untrusted HTML insertion');}
}
const tick=()=>new Promise(resolve=>setImmediate(resolve));
async function harness(){
  const elements=new Map(['panel','status','save','refresh','retry','results','close','toggle','count'].map(id=>['da-z-'+id,new Element()]));elements.get('da-z-panel').dataset.issue='synthetic-issue';
  const choices=[1,2].map(n=>{const e=new Element();e.dataset.daZPaper='paper-'+n;return e;}),radios=['metadata_only','open_access_if_available'].map(value=>{const e=new Element();e.value=value;return e;}),labels=[1,2].map(n=>{const e=new Element();e.dataset.daZResult='paper-'+n;return e;});
  const pdfLabels=[1,2].map(n=>{const e=new Element();e.dataset.daZPdf='paper-'+n;return e;});
  const calls=[],info={live_ready:true,projection_sha256:'a'.repeat(64),connection_version:7,papers:[1,2].map(n=>({paper_id:'paper-'+n,title:'Synthetic title '+n}))};
  const jobs=[1,2].map(n=>({job_id:'ze_'+String(n).repeat(64),state:'queued',pdf_intent:'metadata_only',items:[{paper_id:'paper-'+n,metadata_saved:false,classification_saved:false,pdf_verified:false}]}));
  let delaySave=null,delayAdvance=null,delayRead=null;const events=new Map();
  const platform={AbortController,addEventListener:(type,fn)=>events.set(type,fn),document:{getElementById:id=>elements.get(id),createElement:()=>new Element(),querySelectorAll:q=>q==='[data-da-z-paper]'?choices:q==='[data-da-z-result]'?labels:q==='[data-da-z-pdf]'?pdfLabels:radios},fetch:async(path,opts)=>{
    assert.ok(path.startsWith('/api/zotero/reviewed-issues/synthetic-issue'));assert.equal(opts.credentials,'same-origin');assert.equal(opts.redirect,'error');
    const body=opts.body?JSON.parse(opts.body):null;calls.push({path,method:opts.method,body});let data=info;
    if(opts.method==='GET' && delayRead){const delay=delayRead;delayRead=null;await delay;}
    if(path.endsWith('/save')){if(delaySave)await delaySave;assert.deepEqual(body.paper_ids,['paper-1','paper-2']);assert.equal(body.connection_version,7);data={jobs:structuredClone(jobs)};}
    if(opts.method==='GET' && path.includes('/executions/'))data=structuredClone(jobs.find(j=>path.endsWith(j.job_id)));
    if(path.endsWith('/advance')){if(delayAdvance){const delay=delayAdvance;delayAdvance=null;await delay;}const j=jobs.find(j=>path.includes(j.job_id));data={...j,state:'complete',items:j.items.map(i=>({...i,metadata_saved:true,classification_saved:true}))};Object.assign(j,data);}
    return {ok:true,json:async()=>data};
  }};
  vm.runInNewContext(reviewedClientSource(),platform);await tick();
  return {calls,choices,radios,jobs,labels,pdfLabels,info,get:id=>elements.get('da-z-'+id),setDelay:p=>delaySave=p,setAdvanceDelay:p=>delayAdvance=p,setReadDelay:p=>delayRead=p,emitWindow:(type,event={})=>events.get(type)?.(event)};
}
test('real client script resets all choices; reading, selection and refresh never POST',async()=>{
  const h=await harness();assert.ok(h.choices.every(c=>!c.checked));assert.ok(h.radios.every(c=>!c.checked));assert.ok(h.get('save').disabled);
  h.choices[0].checked=true;await h.choices[0].emit('change');assert.ok(h.get('save').disabled);
  h.radios[0].checked=true;await h.radios[0].emit('change');assert.equal(h.get('save').disabled,false);
  await h.get('refresh').emit('click');assert.ok(h.calls.every(c=>c.method==='GET'));
});
test('one explicit Save submits both batches once, locks duplicate clicks and displays per-paper outcomes',async()=>{
  const h=await harness();h.choices.forEach(c=>c.checked=true);h.radios[0].checked=true;await h.radios[0].emit('change');
  let resolve;h.setDelay(new Promise(r=>resolve=r));const saving=h.get('save').emit('click');await tick();assert.equal(h.get('save').disabled,true);await h.get('save').emit('click');resolve();await saving;
  assert.equal(h.calls.filter(c=>c.path.endsWith('/save')).length,1);assert.equal(h.calls.filter(c=>c.path.endsWith('/advance')).length,2);
  assert.equal(h.get('results').children.length,2);assert.ok(h.labels.every(e=>e.textContent.includes('元数据已保存')));assert.equal(h.get('retry').hidden,true);
});
test('page reload does not resume any job or previous selection',async()=>{
  const h=await harness();assert.equal(h.calls.length,1);assert.ok(h.calls.every(c=>c.method==='GET'));assert.equal(h.get('results').children.length,0);
});

test('BFCache restore reads status only; a subsequent explicit Save advances normally',async()=>{
  const h=await harness();h.choices.forEach(c=>c.checked=true);h.radios[0].checked=true;await h.radios[0].emit('change');
  await h.emitWindow('pagehide',{persisted:true});assert.equal(h.get('save').disabled,true);
  const restoreStart=h.calls.length;await h.emitWindow('pageshow',{persisted:true});
  assert.ok(h.calls.slice(restoreStart).every(c=>c.method==='GET'));assert.equal(h.get('save').disabled,false);
  assert.equal(h.calls.filter(c=>c.method==='POST').length,0);
  await h.get('save').emit('click');assert.equal(h.calls.filter(c=>c.path.endsWith('/advance')).length,2);assert.match(h.get('status').textContent,/本次保存已完成/);
});
test('interrupted Save cannot resume after BFCache restore, including a late response that ignores abort',async()=>{
  const h=await harness();h.choices.forEach(c=>c.checked=true);h.radios[0].checked=true;
  let finish;h.setDelay(new Promise(r=>finish=r));const saving=h.get('save').emit('click');await tick();
  await h.emitWindow('pagehide',{persisted:true});const restoreStart=h.calls.length;await h.emitWindow('pageshow',{persisted:true});
  assert.ok(h.calls.slice(restoreStart).every(c=>c.method==='GET'));assert.match(h.get('status').textContent,/上次请求可能已提交/);
  finish();await saving;assert.equal(h.calls.filter(c=>c.path.endsWith('/advance')).length,0);assert.doesNotMatch(h.get('status').textContent,/本次保存已完成/);
  await h.get('save').emit('click');assert.equal(h.calls.filter(c=>c.path.endsWith('/advance')).length,2);assert.match(h.get('status').textContent,/本次保存已完成/);
});
test('BFCache restores known pending jobs read-only and explicit retry advances; repeated hides stay fenced',async()=>{
  const h=await harness();h.choices.forEach(c=>c.checked=true);h.radios[0].checked=true;
  let finish;h.setAdvanceDelay(new Promise(r=>finish=r));const saving=h.get('save').emit('click');await tick();
  assert.equal(h.calls.filter(c=>c.path.endsWith('/advance')).length,1);
  await h.emitWindow('pagehide',{persisted:true});let restoreStart=h.calls.length;await h.emitWindow('pageshow',{persisted:true});
  assert.ok(h.calls.slice(restoreStart).every(c=>c.method==='GET'));assert.match(h.get('status').textContent,/尚未完成/);assert.equal(h.get('retry').hidden,false);
  // A second lifecycle cycle must still abort/fence: pagehide cannot be once-only.
  await h.emitWindow('pagehide',{persisted:true});restoreStart=h.calls.length;await h.emitWindow('pageshow',{persisted:true});
  assert.ok(h.calls.slice(restoreStart).every(c=>c.method==='GET'));finish();await saving;
  assert.equal(h.calls.filter(c=>c.path.endsWith('/advance')).length,1);assert.match(h.get('status').textContent,/尚未完成/);
  await h.get('retry').emit('click');assert.equal(h.calls.filter(c=>c.path.endsWith('/advance')).length,2);assert.match(h.get('status').textContent,/本次保存已完成/);assert.equal(h.get('retry').hidden,true);
});

test('capability labels promise no download from an unsupported source and preserve citation-only choice',async()=>{
  const h=await harness();Object.assign(h.info.papers[0],{pdf_reviewed:true,pdf_fetch_supported:false});Object.assign(h.info.papers[1],{pdf_reviewed:true,pdf_fetch_supported:true});
  await h.get('refresh').emit('click');assert.match(h.pdfLabels[0].textContent,/仅能使用已核验私有缓存/);assert.match(h.pdfLabels[0].textContent,/引用仍可保存/);assert.match(h.pdfLabels[1].textContent,/尝试已审阅开放来源/);
  assert.ok(h.calls.every(c=>c.method==='GET'));
});
test('unsupported missing attachment shows citation partial success without a saved-PDF claim or retry loop',async()=>{
  const h=await harness();h.choices.forEach(c=>c.checked=true);h.radios[1].checked=true;
  h.jobs.forEach(j=>{j.state='pdf_pending';j.pdf_intent='open_access_if_available';j.items.forEach(i=>Object.assign(i,{metadata_saved:true,classification_saved:true,pdf_verified:false,error_code:'REVIEWED_PDF_SOURCE_UNAVAILABLE',retryable:false}));});
  await h.get('save').emit('click');assert.equal(h.calls.filter(c=>c.path.endsWith('/advance')).length,0);assert.equal(h.get('retry').hidden,true);
  assert.ok(h.get('results').children.every(e=>/元数据已保存；分类已保存；PDF无法取得/.test(e.textContent)));assert.ok(h.labels.every(e=>!e.textContent.includes('PDF 已核验')));assert.match(h.get('status').textContent,/不会持续重试/);assert.doesNotMatch(h.get('status').textContent,/本次保存已完成/);
});

test('late pre-hide Save finally cannot unlock or overwrite a newer explicit Save',async()=>{
  const h=await harness();h.choices.forEach(c=>c.checked=true);h.radios[0].checked=true;
  let oldFinish,newFinish;h.setDelay(new Promise(r=>oldFinish=r));const oldSave=h.get('save').emit('click');await tick();
  await h.emitWindow('pagehide',{persisted:true});await h.emitWindow('pageshow',{persisted:true});
  h.setDelay(new Promise(r=>newFinish=r));const newSave=h.get('save').emit('click');await tick();const before=h.get('status').textContent;
  oldFinish();await oldSave;assert.equal(h.get('save').disabled,true);assert.equal(h.get('refresh').disabled,true);assert.equal(h.get('status').textContent,before);assert.equal(h.calls.filter(c=>c.path.endsWith('/advance')).length,0);
  newFinish();await newSave;assert.equal(h.calls.filter(c=>c.path.endsWith('/advance')).length,2);assert.match(h.get('status').textContent,/本次保存已完成/);
});
test('late pre-hide read cannot replace the restored page status or resume any mutation',async()=>{
  const h=await harness();let finish;h.setReadDelay(new Promise(r=>finish=r));const oldRefresh=h.get('refresh').emit('click');await tick();
  await h.emitWindow('pagehide',{persisted:true});await h.emitWindow('pageshow',{persisted:true});const status=h.get('status').textContent;
  finish();await oldRefresh;assert.equal(h.get('status').textContent,status);assert.ok(h.calls.every(c=>c.method==='GET'));assert.equal(h.get('refresh').disabled,false);
});

test('collapsed controls count selection and close without importing',async()=>{
 const h=await harness();assert.equal(h.get('count').textContent,'0');
 h.choices[0].checked=true;await h.choices[0].emit('change');assert.equal(h.get('count').textContent,'1');
 h.get('panel').open=true;await h.get('close').emit('click');assert.equal(h.get('panel').open,false);
 assert.ok(h.calls.every(c=>c.method==='GET'));assert.equal(h.get('save').disabled,true);
});
test('Escape collapses without a save or loss of selections',async()=>{
 const h=await harness();h.choices[0].checked=true;h.get('panel').open=true;
 h.get('panel').listeners.keydown({key:'Escape',preventDefault(){}});
 assert.equal(h.get('panel').open,false);assert.equal(h.choices[0].checked,true);assert.ok(h.calls.every(c=>c.method==='GET'));
});
