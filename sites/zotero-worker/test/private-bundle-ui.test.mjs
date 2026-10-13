import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {File} from 'node:buffer';
import {createHash,webcrypto} from 'node:crypto';
import {renderPrivateBundlePage} from '../src/private-bundle-ui.mjs';

const sha=bytes=>createHash('sha256').update(bytes).digest('hex');
const canonical=value=>JSON.stringify(value,(_,v)=>v && typeof v==='object' && !Array.isArray(v)?Object.fromEntries(Object.entries(v).sort(([a],[b])=>a.localeCompare(b))):v);
function fixture({pdfSizes=[]}={}){
  const contents=new Map(),members=[],batches=[];let offset=0;
  for(const n of [1,2]){
    const id='batch-'+n,pdf=pdfSizes[n-1]?Buffer.alloc(pdfSizes[n-1],n):Buffer.from('%PDF-1.7\nsynthetic paper '+n+'\n');
    if(pdfSizes[n-1])pdf.write('%PDF-1.7\nsynthetic paper '+n+'\n');
    const pdfSha=sha(pdf);
    batches.push({id,registered:false,enabled:false,paper_count:1,pdfs:[{sha256:pdfSha,size:pdf.length}]});
    for(const [kind,bytes] of [['report',Buffer.from('{"report":'+n+'}')],['html',Buffer.from('<p>论文 '+n+'</p>')],['catalog',Buffer.from('{"catalog":'+n+'}')],['pdf',pdf]]){
      const memberId=id+'/'+kind+(kind==='pdf'?'/'+pdfSha:'');
      members.push({id:memberId,batch_id:id,kind,offset,size:bytes.length,sha256:sha(bytes)});contents.set(memberId,bytes);offset+=bytes.length;
    }
  }
  const header_json=canonical({schema:'daily-agent-owner-bundle-v1',members}),header=Buffer.from(header_json),prefix=Buffer.alloc(12);
  prefix.write('DAZB0001');prefix.writeUInt32BE(header.length,8);
  const bytes=Buffer.concat([prefix,header,...contents.values()]);
  const manifest={enabled:true,ready:false,connection_matches:true,paper_count:2,batches,upload:{header_json,total_size:bytes.length}};
  return{manifest,bytes,contents,members,payloadStart:12+header.length};
}
class Element{
  constructor(){this.disabled=true;this.textContent='';this.listeners={};this.files=[];this._value='';}
  set value(value){this._value=value;if(value==='')this.files=[];}
  get value(){return this._value;}
  addEventListener(type,fn){this.listeners[type]=fn;}
  emit(type){return this.listeners[type]?.({});}
}
function watchedFile(bytes,options={}){
  const file=new File([bytes],options.name??'owner-papers.dazb',{type:'application/octet-stream'}),reads=[];let active=0,maxActive=0,wholeReads=0;
  const rawSlice=file.slice.bind(file);
  file.arrayBuffer=async()=>{wholeReads++;throw new Error('whole-file read forbidden');};
  file.slice=(start,end)=>{
    const blob=rawSlice(start,end),rawRead=blob.arrayBuffer.bind(blob);
    blob.arrayBuffer=async()=>{reads.push({start,end});active++;maxActive=Math.max(maxActive,active);try{await options.onRead?.({start,end});return await rawRead();}finally{active--;}};
    return blob;
  };
  return{file,reads,maxActive:()=>maxActive,wholeReads:()=>wholeReads};
}
async function harness({handler,data=fixture(),contained=false,crypto=webcrypto}={}){
  const response=renderPrivateBundlePage({contained}),html=await response.text(),elements=Object.fromEntries(['bundle-status','bundle-details','bundle-prepare','bundle-refresh','bundle-file'].map(id=>[id,new Element()]));
  const calls=[],events={};let reloads=0;
  const platform={document:{getElementById:id=>elements[id]},AbortController,TextEncoder,crypto,btoa,
    addEventListener:(type,fn)=>events[type]=fn,location:{reload:()=>reloads++},fetch:async(path,options)=>{
      assert.equal(options.credentials,'same-origin');assert.equal(options.cache,'no-store');assert.equal(options.redirect,'error');assert.equal(options.headers['X-Zotero-Action'],'1');
      const call={path,...options};calls.push(call);let result=await handler?.(call);
      if(result?.failure)return{ok:false,json:async()=>({error:'synthetic'})};
      if(result===undefined){if(options.method==='GET')result=structuredClone(data.manifest);
        else if(path.endsWith('/finish'))result={ready:true,paper_count:2,batch_count:2,zotero_requests:0};
        else if(path.endsWith('/register'))result={registered:true,batch_id:path.split('/').at(-2),paper_count:1};
        else result={staged:true,sha256:path.split('/').at(-1)};}
      return{ok:true,json:async()=>result};
    }};
  const script=html.match(/<script nonce="[^"]+">([\s\S]*?)<\/script>/)[1],client=vm.runInNewContext(script,platform);await client.ready;
  const select=(...files)=>{elements['bundle-file'].files=files;return elements['bundle-file'].emit('change');};
  return{response,html,e:elements,calls,client,events,reloads:()=>reloads,select,data,posts:()=>calls.filter(c=>c.method==='POST')};
}
const deferred=()=>{let resolve;const promise=new Promise(r=>resolve=r);return{promise,resolve};};
const selectOriginal=h=>{const file=watchedFile(h.data.bytes);h.select(file.file);return file;};
function editedDescriptor(m,edit){const header=JSON.parse(m.upload.header_json);edit(header);m.upload.header_json=canonical(header);return m;}

test('CSP-hardened page has one file selector; load, refresh, and selection never read or mutate',async()=>{
  const h=await harness(),f=watchedFile(h.data.bytes);
  assert.match(h.response.headers.get('Content-Security-Policy'),/default-src 'none'/);assert.equal(h.response.headers.get('Cache-Control'),'no-store, private');
  assert.equal((h.html.match(/type="file"/g)??[]).length,1);assert.doesNotMatch(h.html,/webkitdirectory|\smultiple(?:[\s=>])|localStorage|sessionStorage|indexedDB/);
  assert.match(h.html,/\.dazb/);assert.match(h.html,/无需.*重新输入 Zotero 密钥/);assert.equal(h.e['bundle-prepare'].disabled,true);
  h.select(f.file);assert.equal(h.e['bundle-prepare'].disabled,false);assert.equal(h.calls.length,1);assert.deepEqual(f.reads,[]);
  await h.e['bundle-refresh'].emit('click');assert.ok(h.calls.every(c=>c.method==='GET'));assert.equal(h.e['bundle-prepare'].disabled,true);assert.deepEqual(f.reads,[]);
  h.select(f.file,f.file);assert.equal(h.e['bundle-prepare'].disabled,true);h.select();assert.equal(h.e['bundle-prepare'].disabled,true);
});

test('one explicit click hashes bounded slices, then sequentially posts raw PDFs, base64 sources, and finish',async()=>{
  const h=await harness(),f=selectOriginal(h);await h.e['bundle-prepare'].emit('click');
  assert.equal(h.calls.filter(c=>c.method==='GET').length,2);const posts=h.posts(),expected=[];
  for(const b of h.data.manifest.batches){expected.push('/api/zotero/private-bundles/'+b.id+'/pdfs/'+b.pdfs[0].sha256,'/api/zotero/private-bundles/'+b.id+'/register');}
  expected.push('/api/zotero/private-bundles/finish');assert.deepEqual(posts.map(c=>c.path),expected);
  for(const post of posts){
    if(post.path.includes('/pdfs/')){assert.equal(post.headers['Content-Type'],'application/pdf');const b=post.path.split('/').at(-3),pin=post.path.split('/').at(-1);assert.deepEqual(Buffer.from(post.body),h.data.contents.get(b+'/pdf/'+pin));}
    else{assert.equal(post.headers['Content-Type'],'application/json');const body=JSON.parse(post.body);
      if(post.path.endsWith('/finish'))assert.deepEqual(body,{action:'prepare_private_catalogs'});
      else{const b=post.path.split('/').at(-2);assert.deepEqual(Object.keys(body),['action','report_b64','html_b64','catalog_b64']);assert.equal(body.action,'register_catalog');for(const kind of ['report','html','catalog'])assert.deepEqual(Buffer.from(body[kind+'_b64'],'base64'),h.data.contents.get(b+'/'+kind));}
    }
  }
  const reads=[{start:0,end:12},{start:12,end:h.data.payloadStart}];
  for(const b of h.data.manifest.batches){for(const id of [b.id+'/pdf/'+b.pdfs[0].sha256,...['report','html','catalog'].map(k=>b.id+'/'+k)]){const member=h.data.members.find(m=>m.id===id);reads.push({start:h.data.payloadStart+member.offset,end:h.data.payloadStart+member.offset+member.size});}}
  assert.deepEqual(f.reads,reads);assert.equal(f.maxActive(),1);assert.equal(f.wholeReads(),0);assert.equal(h.e['bundle-prepare'].disabled,true);assert.equal(h.e['bundle-file'].disabled,true);
  assert.match(h.e['bundle-status'].textContent,/尚未向 Zotero 保存/);assert.ok(h.calls.every(c=>c.path.startsWith('/api/zotero/private-bundles')));
});

test('a 29 MiB owner file is consumed only through bounded per-member slices',async()=>{
  const data=fixture({pdfSizes:[15*1024*1024,14*1024*1024]}),h=await harness({data}),f=selectOriginal(h);
  assert.ok(f.file.size>29*1024*1024);await h.e['bundle-prepare'].emit('click');
  assert.equal(h.posts().length,5);assert.equal(f.wholeReads(),0);assert.equal(f.maxActive(),1);
  assert.ok(f.reads.every(r=>r.end-r.start<=16*1024*1024));assert.equal(f.reads.filter(r=>r.start>=data.payloadStart).length,8);
});

test('selected filename and file paths are never routing authority',async()=>{
  const h=await harness(),f=watchedFile(h.data.bytes,{name:'../../other-batch/evil.pdf'});h.select(f.file);await h.e['bundle-prepare'].emit('click');assert.equal(h.posts().length,5);assert.ok(h.posts().every(c=>!c.path.includes('evil') && !c.path.includes('other-batch')));
});

test('wrong magic, header length, header bytes, truncation, and trailing bytes fail before any POST',async()=>{
  const data=fixture(),cases=[['truncated',data.bytes.subarray(0,-1)],['trailing',Buffer.concat([data.bytes,Buffer.from('x')])]];
  const magic=Buffer.from(data.bytes);magic[0]^=1;cases.push(['magic',magic]);
  const length=Buffer.from(data.bytes);length.writeUInt32BE(65537,8);cases.push(['oversized header',length]);
  const header=Buffer.from(data.bytes);header[12]='!'.charCodeAt(0);cases.push(['malformed header',header]);
  const wrongHeader=Buffer.from(data.bytes);wrongHeader[data.payloadStart-2]^=1;cases.push(['changed header',wrongHeader]);
  for(const [name,bytes] of cases){const h=await harness({data}),f=watchedFile(bytes);h.select(f.file);await h.e['bundle-prepare'].emit('click');assert.equal(h.posts().length,0,name);assert.equal(h.e['bundle-prepare'].disabled,true,name);assert.match(h.e['bundle-status'].textContent,/重新选择原始 \.dazb/);assert.equal(f.wholeReads(),0);}
});

test('trusted descriptors reject duplicate, missing, extra, noncontiguous, malformed and oversized members before reading a file',async()=>{
  const changes=[
    m=>editedDescriptor(m,h=>{h.members[1]={...h.members[0]};}),
    m=>editedDescriptor(m,h=>h.members.pop()),
    m=>editedDescriptor(m,h=>h.members.push({...h.members[0],id:'unknown/report'})),
    m=>editedDescriptor(m,h=>{h.members[1].offset=0;}),
    m=>editedDescriptor(m,h=>{h.members[1].offset++;}),
    m=>editedDescriptor(m,h=>{h.members[0].offset=-1;}),
    m=>editedDescriptor(m,h=>{h.members[0].offset=0.5;}),
    m=>editedDescriptor(m,h=>{h.members[0].batch_id='../different';}),
    m=>editedDescriptor(m,h=>{h.members[0].kind='pdf';}),
    m=>editedDescriptor(m,h=>{h.members[0].sha256='f'.repeat(63);}),
    m=>editedDescriptor(m,h=>{h.members[0].size=1024*1024+1;}),
    m=>editedDescriptor(m,h=>{h.members[0].size=0;}),
    m=>editedDescriptor(m,h=>{h.members[3].sha256='f'.repeat(64);}),
    m=>editedDescriptor(m,h=>{h.members[3].size++;}),
    m=>editedDescriptor(m,h=>{h.members[0].extra='untrusted';}),
    m=>editedDescriptor(m,h=>{h.schema='wrong';}),
    m=>{m.upload.total_size++;},m=>{m.upload.total_size=Number.MAX_SAFE_INTEGER+1;},
    m=>{m.upload.header_json='{';},m=>{m.upload.header_json=' '.repeat(65537);},
    m=>{m.upload.header_json='{"schema":"daily-agent-owner-bundle-v1",'+m.upload.header_json.slice(1);},
    m=>{m.batches[0]=null;},m=>{m.batches[0].pdfs[0]=null;},m=>{m.upload.extra=true;}
  ];
  for(const [index,edit] of changes.entries()){
    const data=fixture();edit(data.manifest);const h=await harness({data}),f=watchedFile(data.bytes);h.select(f.file);await h.e['bundle-prepare'].emit('click');assert.equal(h.e['bundle-file'].disabled,true,'case '+index);assert.equal(h.posts().length,0,'case '+index);assert.deepEqual(f.reads,[],'case '+index);
  }
});

test('each payload SHA mismatch stops before that member is posted and never auto-retries',async()=>{
  for(const kind of ['pdf','report','html','catalog']){
    const h=await harness(),bytes=Buffer.from(h.data.bytes),member=h.data.members.find(m=>m.batch_id==='batch-1'&&m.kind===kind);bytes[h.data.payloadStart+member.offset]^=1;
    const f=watchedFile(bytes);h.select(f.file);await h.e['bundle-prepare'].emit('click');assert.equal(h.posts().length,kind==='pdf'?0:1,kind);assert.ok(h.posts().every(c=>c.path.includes('/pdfs/')));assert.match(h.e['bundle-status'].textContent,/不会自动重试/);
    await h.e['bundle-prepare'].emit('click');h.select(f.file);assert.equal(h.posts().length,kind==='pdf'?0:1);
  }
});

test('double click cannot duplicate an upload; refresh, reselect, and a new click explicitly resume',async()=>{
  const held=deferred(),entered=deferred();let stages=0,fail=true;
  const h=await harness({handler:async c=>{if(c.method==='POST'&&c.path.includes('/pdfs/')){stages++;if(fail){entered.resolve();await held.promise;return{failure:true};}}}}),f=selectOriginal(h);
  const first=h.e['bundle-prepare'].emit('click');await entered.promise;assert.equal(h.e['bundle-file'].disabled,true);const second=h.e['bundle-prepare'].emit('click');await h.e['bundle-refresh'].emit('click');held.resolve();await Promise.all([first,second]);
  assert.equal(stages,1);assert.equal(h.posts().length,1);assert.equal(h.e['bundle-prepare'].disabled,true);assert.match(h.e['bundle-status'].textContent,/不会自动重试/);
  fail=false;await h.e['bundle-refresh'].emit('click');assert.equal(h.posts().length,1);assert.equal(h.e['bundle-prepare'].disabled,true);h.select(f.file);assert.equal(h.posts().length,1);await h.e['bundle-prepare'].emit('click');assert.equal(h.posts().length,6);assert.match(h.e['bundle-status'].textContent,/已准备好/);
});

test('navigation aborts in-flight requests, disables file selection, and BFCache never resumes mutations',async()=>{
  const held=deferred(),entered=deferred();const h=await harness({handler:async c=>{if(c.method==='POST'){entered.resolve();await held.promise;}}});selectOriginal(h);
  const active=h.e['bundle-prepare'].emit('click');await entered.promise;h.events.pagehide();held.resolve();await active;
  assert.equal(h.posts().length,1);assert.equal(h.calls.at(-1).signal.aborted,true);assert.equal(h.e['bundle-file'].disabled,true);assert.equal(h.e['bundle-refresh'].disabled,true);h.events.pageshow({persisted:true});assert.equal(h.reloads(),1);assert.equal(h.posts().length,1);
});

test('navigation during a bounded file read or SHA calculation prevents the next POST',async()=>{
  for(const step of ['read','hash']){
    const held=deferred(),entered=deferred();let hashes=0;
    const crypto=step==='hash'?{subtle:{digest:async(...args)=>{hashes++;if(hashes===1){entered.resolve();await held.promise;}return webcrypto.subtle.digest(...args);}}}:webcrypto;
    const h=await harness({crypto}),f=watchedFile(h.data.bytes,{onRead:async range=>{if(step==='read'&&range.start>=h.data.payloadStart){entered.resolve();await held.promise;}}});h.select(f.file);
    const active=h.e['bundle-prepare'].emit('click');await entered.promise;h.events.pagehide();held.resolve();await active;assert.equal(h.posts().length,0,step);assert.equal(h.e['bundle-prepare'].disabled,true);
  }
});

test('changed fingerprint, header, total size or connection fails closed on pre-mutation re-fetch',async()=>{
  for(const mode of ['paper','pdf','header','size','connection','unavailable']){
    let gets=0;const data=fixture();const h=await harness({data,handler:c=>{if(c.method==='GET'){gets++;const m=structuredClone(data.manifest);if(gets>1){
      if(mode==='paper'){m.paper_count=3;m.batches[0].paper_count=2;}
      else if(mode==='pdf')m.batches[0].pdfs[0].sha256='3'.repeat(64);
      else if(mode==='header')editedDescriptor(m,header=>{header.members[0].sha256='3'.repeat(64);});
      else if(mode==='size')m.upload.total_size++;
      else if(mode==='connection')m.connection_matches=false;
      else return{enabled:false,state:'temporarily_unavailable'};
    }return m;}}});const f=selectOriginal(h);await h.e['bundle-prepare'].emit('click');assert.equal(h.posts().length,0,mode);assert.deepEqual(f.reads,[],mode);assert.equal(h.e['bundle-prepare'].disabled,true);
  }
});

test('registered state may advance safely while immutable pins remain unchanged',async()=>{
  let gets=0;const data=fixture(),h=await harness({data,handler:c=>{if(c.method==='GET'){const m=structuredClone(data.manifest);if(++gets>1){m.batches[0].registered=true;m.batches[0].enabled=true;}return m;}}});selectOriginal(h);await h.e['bundle-prepare'].emit('click');assert.equal(h.posts().length,5);
});

test('unconfirmed server responses stop orchestration before following stages',async()=>{
  for(const mode of ['pdf','register','finish']){
    const h=await harness({handler:c=>{if(c.method==='POST' && (mode==='pdf'&&c.path.includes('/pdfs/') || mode==='register'&&c.path.endsWith('/register') || mode==='finish'&&c.path.endsWith('/finish')))return{};}});selectOriginal(h);await h.e['bundle-prepare'].emit('click');assert.equal(h.posts().length,{pdf:1,register:2,finish:5}[mode]);assert.equal(h.e['bundle-prepare'].disabled,true);assert.match(h.e['bundle-status'].textContent,/尚未确认/);
  }
});

test('ready and mismatching connections cannot select or submit; contained status retains connection explanation',async()=>{
  for(const mode of ['ready','connection']){const data=fixture();if(mode==='ready')data.manifest.ready=true;else data.manifest.connection_matches=false;const h=await harness({data});selectOriginal(h);await h.e['bundle-prepare'].emit('click');assert.equal(h.e['bundle-file'].disabled,true);assert.equal(h.posts().length,0);}
  const h=await harness({contained:true,handler:()=>({enabled:false,ready:false,state:'temporarily_unavailable',batches:[]})});assert.equal(h.e['bundle-prepare'].disabled,true);assert.equal(h.e['bundle-file'].disabled,true);assert.match(h.e['bundle-status'].textContent,/无需重新输入密钥/);await h.e['bundle-prepare'].emit('click');assert.ok(h.calls.every(c=>c.method==='GET'));assert.match(h.html,/目录准备暂时不可用/);assert.match(h.html,/disabled>暂时不可用/);
});
