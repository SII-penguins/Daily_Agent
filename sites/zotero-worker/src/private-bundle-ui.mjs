/** Owner click only. Loading, file selection, and restoring this page never mutate. */
export function privateBundleClient(platform=globalThis){
  const {document,fetch,AbortController}=platform;
  const status=document.getElementById('bundle-status'),details=document.getElementById('bundle-details'),submit=document.getElementById('bundle-prepare'),refresh=document.getElementById('bundle-refresh'),input=document.getElementById('bundle-file');
  const state={manifest:null,file:null,busy:false,alive:true,requests:new Set()};
  const sourceLimits={report:1024*1024,html:4*1024*1024,catalog:1024*1024};
  const exact=(v,keys)=>v!==null && typeof v==='object' && !Array.isArray(v) && Object.keys(v).length===keys.length && keys.every(k=>Object.hasOwn(v,k));
  const active=()=>{if(!state.alive)throw new Error('disposed');};
  function update(){submit.disabled=!state.alive || state.busy || !state.file || !state.manifest?.enabled || state.manifest?.ready || !state.manifest?.connection_matches;refresh.disabled=!state.alive || state.busy;input.disabled=!state.alive || state.busy || !state.manifest?.enabled || state.manifest?.ready || !state.manifest?.connection_matches;}
  function clearSelection(){state.file=null;input.value='';}
  async function request(path,body,type='application/json'){active();const controller=new AbortController();state.requests.add(controller);try{
    const action=body!==undefined;
    const r=await fetch(path,{method:action?'POST':'GET',credentials:'same-origin',cache:'no-store',redirect:'error',signal:controller.signal,
      headers:{'Accept':'application/json','X-Zotero-Action':'1',...(action?{'Content-Type':type}:{})},
      ...(action?{body:type==='application/json'?JSON.stringify(body):body}:{})});active();const data=await r.json();active();if(!r.ok)throw new Error('unconfirmed');return data;
  }finally{state.requests.delete(controller);}}
  function valid(m){return m?.enabled===true && typeof m.ready==='boolean' && typeof m.connection_matches==='boolean'
    && Number.isSafeInteger(m.paper_count) && m.paper_count>0 && m.paper_count<=100 && Array.isArray(m.batches) && m.batches.length>0 && m.batches.length<=16
    && m.batches.every(b=>b && typeof b.id==='string' && /^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$/.test(b.id)
      && typeof b.registered==='boolean' && typeof b.enabled==='boolean' && Number.isSafeInteger(b.paper_count) && b.paper_count>0
      && Array.isArray(b.pdfs) && b.pdfs.length<=100
      && b.pdfs.every(p=>p && typeof p.sha256==='string' && /^[a-f0-9]{64}$/.test(p.sha256) && Number.isSafeInteger(p.size) && p.size>=8 && p.size<=16*1024*1024)
      && new Set(b.pdfs.map(p=>p.sha256)).size===b.pdfs.length && b.pdfs.reduce((n,p)=>n+p.size,0)<=24*1024*1024)
    && new Set(m.batches.map(b=>b.id)).size===m.batches.length && m.batches.reduce((n,b)=>n+b.paper_count,0)===m.paper_count;}
  // Parse only the trusted server descriptor. The selected file's header is never parsed.
  function descriptor(m){
    if(!valid(m) || !exact(m.upload,['header_json','total_size']) || typeof m.upload.header_json!=='string'
      || m.upload.header_json.length>65536 || !Number.isSafeInteger(m.upload.total_size) || m.upload.total_size<=12)throw new Error('invalid');
    const headerBytes=new platform.TextEncoder().encode(m.upload.header_json);
    if(!headerBytes.length || headerBytes.length>65536)throw new Error('invalid');
    const header=JSON.parse(m.upload.header_json);
    if(!exact(header,['schema','members']) || header.schema!=='daily-agent-owner-bundle-v1' || !Array.isArray(header.members)
      || JSON.stringify(header)!==m.upload.header_json)throw new Error('invalid');
    const expected=new Map(),members=new Map(),sourceTotals=new Map();
    for(const b of m.batches){
      sourceTotals.set(b.id,0);
      for(const kind of ['report','html','catalog'])expected.set(b.id+'/'+kind,{batch:b.id,kind});
      for(const p of b.pdfs)expected.set(b.id+'/pdf/'+p.sha256,{batch:b.id,kind:'pdf',size:p.size,sha256:p.sha256});
    }
    if(header.members.length!==expected.size)throw new Error('invalid');
    let offset=0;
    for(const member of header.members){
      if(!exact(member,['id','batch_id','kind','offset','size','sha256']) || typeof member.id!=='string'
        || !expected.has(member.id) || members.has(member.id))throw new Error('invalid');
      const pin=expected.get(member.id);
      if(member.batch_id!==pin.batch || member.kind!==pin.kind || !Number.isSafeInteger(member.offset) || member.offset!==offset
        || !Number.isSafeInteger(member.size) || member.size<1 || typeof member.sha256!=='string' || !/^[a-f0-9]{64}$/.test(member.sha256))throw new Error('invalid');
      if(pin.kind==='pdf'){
        if(member.size!==pin.size || member.sha256!==pin.sha256)throw new Error('invalid');
      }else{
        if(member.size>sourceLimits[pin.kind])throw new Error('invalid');
        sourceTotals.set(pin.batch,sourceTotals.get(pin.batch)+member.size);
        if(sourceTotals.get(pin.batch)>4*1024*1024)throw new Error('invalid');
      }
      offset+=member.size;if(!Number.isSafeInteger(offset))throw new Error('invalid');members.set(member.id,member);
    }
    const payloadStart=12+headerBytes.length;
    if(payloadStart+offset!==m.upload.total_size)throw new Error('invalid');
    return {headerBytes,payloadStart,members};
  }
  const fingerprint=m=>JSON.stringify([m.paper_count,m.batches.map(b=>[b.id,b.paper_count,b.pdfs]),m.upload.header_json,m.upload.total_size]);
  async function slice(file,start,end){active();const bytes=new Uint8Array(await file.slice(start,end).arrayBuffer());active();if(bytes.length!==end-start)throw new Error('truncated');return bytes;}
  async function verifyFile(file,m,d){
    if(!file || file.size!==m.upload.total_size || typeof file.slice!=='function')throw new Error('file');
    const prefix=await slice(file,0,12);
    if('DAZB0001'.split('').some((c,i)=>prefix[i]!==c.charCodeAt(0)) || new DataView(prefix.buffer,prefix.byteOffset,12).getUint32(8,false)!==d.headerBytes.length)throw new Error('header');
    const actual=await slice(file,12,d.payloadStart);
    if(actual.some((byte,i)=>byte!==d.headerBytes[i]))throw new Error('header');
  }
  async function readMember(file,d,id){
    const member=d.members.get(id);if(!member)throw new Error('invalid');
    const bytes=await slice(file,d.payloadStart+member.offset,d.payloadStart+member.offset+member.size);
    const digest=new Uint8Array(await platform.crypto.subtle.digest('SHA-256',bytes));active();
    if(Array.from(digest,b=>b.toString(16).padStart(2,'0')).join('')!==member.sha256)throw new Error('hash');
    return bytes;
  }
  function base64(bytes){let binary='';for(let i=0;i<bytes.length;i+=32768)binary+=String.fromCharCode(...bytes.subarray(i,i+32768));return platform.btoa(binary);}
  function idleStatus(m){return m.ready?'私有目录已就绪。返回论文管理页，勾选论文并明确点击保存后才会写入 Zotero。':m.connection_matches?'连接已核验，无需重新输入密钥。请选择已下载的 .dazb 文件，再点击「准备私有论文目录」。':'请先在论文管理页连接并核验对应个人文库。当前不会准备目录。';}
  async function load(){if(state.busy || !state.alive)return;state.busy=true;state.manifest=null;clearSelection();update();try{
    const m=await request('/api/zotero/private-bundles');
    if(m?.state==='temporarily_unavailable'){details.textContent='目录准备正在修复，当前不会暂存、登记或导入论文。';status.textContent='Zotero 连接信息仍保留，无需重新输入密钥。目录准备与保存暂时不可用。';return;}
    descriptor(m);state.manifest=m;
    details.textContent=m.paper_count+' 篇已核验论文，分 '+m.batches.length+' 批准备。请选择已下载的单个 .dazb 论文包，无需选择文件夹。';
    status.textContent=idleStatus(m);
  }catch{if(state.alive){details.textContent='尚未确认服务器固定的论文清单。';status.textContent='无法确认目录状态，请刷新重试。没有自动开始准备。';}}finally{state.busy=false;if(state.alive)update();}}
  input.addEventListener('change',()=>{if(state.busy || !state.alive || input.disabled)return;
    state.file=input.files?.length===1?input.files[0]:null;
    status.textContent=state.file?'已选择一个论文包。点击「准备私有论文目录」后才会校验并逐份上传；选择文件本身不会上传。':idleStatus(state.manifest);update();
  });
  submit.addEventListener('click',async()=>{if(submit.disabled || state.busy || !state.manifest || !state.file || !state.alive)return;
    state.busy=true;update();const pinned=fingerprint(state.manifest),file=state.file;try{
      const m=await request('/api/zotero/private-bundles'),d=descriptor(m);
      if(fingerprint(m)!==pinned || !m.connection_matches)throw new Error('changed');
      await verifyFile(file,m,d);
      for(let n=0;n<m.batches.length;n++){
        const batch=m.batches[n];
        // Idempotent replays recheck each immutable member after an interruption.
        for(let i=0;i<batch.pdfs.length;i++){
          status.textContent='正在准备第 '+(n+1)+' / '+m.batches.length+' 批 PDF：'+(i+1)+' / '+batch.pdfs.length;
          const p=batch.pdfs[i],bytes=await readMember(file,d,batch.id+'/pdf/'+p.sha256);
          const r=await request('/api/zotero/private-bundles/'+batch.id+'/pdfs/'+p.sha256,bytes,'application/pdf');
          if(r.staged!==true || r.sha256!==p.sha256)throw new Error('unconfirmed');
        }
        status.textContent='正在登记第 '+(n+1)+' / '+m.batches.length+' 批目录…';
        const body={action:'register_catalog'};
        for(const kind of ['report','html','catalog'])body[kind+'_b64']=base64(await readMember(file,d,batch.id+'/'+kind));
        const r=await request('/api/zotero/private-bundles/'+batch.id+'/register',body);
        if(r.registered!==true || r.batch_id!==batch.id || r.paper_count!==batch.paper_count)throw new Error('unconfirmed');
      }
      status.textContent='正在核验完整论文清单与当前连接…';
      const r=await request('/api/zotero/private-bundles/finish',{action:'prepare_private_catalogs'});
      if(r.ready!==true || r.paper_count!==m.paper_count || r.batch_count!==m.batches.length || r.zotero_requests!==0)throw new Error('unconfirmed');
      state.manifest={...m,ready:true};clearSelection();status.textContent='私有论文目录已准备好。尚未向 Zotero 保存论文；请返回管理页勾选并明确保存。';
    }catch{if(state.alive){state.manifest=null;clearSelection();status.textContent='文件校验或准备结果尚未确认。已完成的私有暂存可以保留；请刷新状态、重新选择原始 .dazb 文件，再点击继续。不会自动重试或导入 Zotero。';}}
    finally{state.busy=false;if(state.alive)update();}
  });
  refresh.addEventListener('click',load);
  const dispose=()=>{state.alive=false;state.requests.forEach(r=>r.abort());state.requests.clear();clearSelection();update();};
  platform.addEventListener('pagehide',dispose);platform.addEventListener('pageshow',e=>{if(e.persisted)platform.location.reload();});
  const ready=load();return {ready,dispose};
}
export function renderPrivateBundlePage({contained=false}={}){
  const nonce=btoa(String.fromCharCode(...crypto.getRandomValues(new Uint8Array(18))));
  const html=`<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>准备私有论文目录 · Daily Agent</title><style nonce="${nonce}">:root{color-scheme:light dark;--bg:#f6f4ed;--card:#fffef9;--ink:#172c29;--muted:#5d6c66;--line:#ccd3c8;--accent:#14634c}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.8 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",sans-serif}main{max-width:900px;margin:auto;padding:28px 32px}header{border-bottom:1px solid var(--ink);padding-bottom:18px}.brand{font:700 30px Georgia,serif}a{color:var(--accent);text-underline-offset:5px}h1{font-size:clamp(28px,5vw,38px);margin:28px 0 12px}.panel{margin:24px 0;padding:24px;background:var(--card);border:1px solid var(--line);border-radius:4px}.note{color:var(--muted);font-size:14px}button{font:inherit;min-height:48px;padding:10px 18px;border:1px solid var(--accent);border-radius:4px;background:var(--accent);color:var(--card);cursor:pointer;margin:12px 12px 0 0}button.secondary{background:transparent;color:var(--accent);border-color:var(--line)}button:disabled{opacity:.5;cursor:not-allowed}:focus-visible{outline:3px solid #925025;outline-offset:4px}@media(prefers-color-scheme:dark){:root{--bg:#142320;--card:#1c2e29;--ink:#e6ece0;--muted:#adbdb3;--line:#41564b;--accent:#a7d3ae}}@media(max-width:600px){main{padding:20px}.panel{padding:18px}button{width:100%;margin-right:0}}</style></head><body><main><header><a class="brand" href="/">Daily Agent</a></header><h1>${contained?'目录准备暂时不可用':'准备私有论文目录'}</h1><p>${contained?'正在修复目录准备流程。Zotero 连接信息仍保留，无需重新输入密钥。':'连接已完成后，在这里把已核验的日报论文加入你的私有管理页。'}</p><section class="panel"><p id="bundle-details">正在读取服务器固定的论文清单…</p><p class="note">请选择已下载的 .dazb 论文包。无需解压、选择文件夹或重新输入 Zotero 密钥。点击准备后，文件会逐份校验并上传，全部目录核验通过后一起开放选择。</p><label for="bundle-file">已下载的论文包（单个 .dazb 文件）</label><br><input id="bundle-file" type="file" accept=".dazb,application/octet-stream" disabled><p class="note">选择文件不会开始上传。</p><button id="bundle-prepare" type="button" disabled>${contained?'暂时不可用':'准备私有论文目录'}</button><button id="bundle-refresh" class="secondary" type="button" disabled>刷新状态</button></section><section class="panel"><p id="bundle-status" role="status" aria-live="polite">等待确认。</p><p class="note">准备目录不会向 Zotero 导入论文。每次导入都需要你在管理页勾选论文并明确保存。离开本页会停止发起下一步，已完成部分可在刷新后继续。</p></section><noscript><p>需要启用 JavaScript 才能由你明确点击准备。</p></noscript><a href="/zotero">返回论文管理页</a></main><script nonce="${nonce}">(${privateBundleClient.toString()})();</script></body></html>`;
  return new Response(html,{headers:{'Content-Type':'text/html; charset=utf-8','Cache-Control':'no-store, private','Pragma':'no-cache','X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer','X-Frame-Options':'DENY','Cross-Origin-Resource-Policy':'same-origin','Permissions-Policy':'camera=(), microphone=(), geolocation=()',
    'Content-Security-Policy':`default-src 'none'; script-src 'nonce-${nonce}'; style-src 'nonce-${nonce}'; connect-src 'self'; form-action 'none'; base-uri 'none'; frame-ancestors 'none'`}});
}
