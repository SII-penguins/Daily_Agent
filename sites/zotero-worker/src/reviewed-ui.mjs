/** Reversible response-only enhancement. Original sealed assets are never written. */
const escape=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const encoder=new TextEncoder(),decoder=new TextDecoder('utf8',{fatal:true});
export function renderReviewedPage(bytes,issue) {
  const edits=issue.csp_ranges.map(([start,end])=>({start,end,text:''}));
  for(const a of issue.anchors)edits.push({start:a.offset,end:a.offset,text:`<label class="da-z-choice"><input type="checkbox" data-da-z-paper="${escape(a.paper_id)}" autocomplete="off"> 选择保存到 Zotero <span data-da-z-result="${escape(a.paper_id)}"></span><small data-da-z-pdf="${escape(a.paper_id)}"></small></label>`});
  edits.push({start:issue.head_offset,end:issue.head_offset,text:`<base href="/${escape(issue.entry_path.slice(0,issue.entry_path.lastIndexOf('/')+1))}"><style>
.da-z-navigation{position:sticky;top:0;z-index:90;display:flex;align-items:center;gap:20px;padding:12px 24px;background:#f7f5edf2;border-bottom:1px solid #d5ded6;font:14px/1.5 system-ui;backdrop-filter:blur(8px)}.da-z-navigation a{color:#175f4b;text-decoration:none}.da-z-navigation a:hover{text-decoration:underline}
.da-z-choice{display:flex;align-items:center;flex-wrap:wrap;gap:6px 10px;padding:8px 0;margin:8px 0 16px;font:14px/1.5 system-ui;color:#234b40}.da-z-choice input{accent-color:#17634e;width:17px;height:17px}.da-z-choice small{font-size:12px;color:#687970}
.da-z-panel{position:fixed;right:max(20px,env(safe-area-inset-right));bottom:max(20px,env(safe-area-inset-bottom));z-index:100;font:14px/1.6 system-ui;color:#193d33;max-width:calc(100vw - 32px)}
.da-z-panel>summary{display:flex;align-items:center;justify-content:center;gap:8px;min-height:46px;padding:0 18px;list-style:none;cursor:pointer;background:#175f4b;color:#fff;border:1px solid #175f4b;border-radius:24px;box-shadow:0 4px 18px #173d3320;font-weight:600;user-select:none}.da-z-panel>summary::-webkit-details-marker{display:none}.da-z-panel>summary:focus-visible,.da-z-panel button:focus-visible{outline:3px solid #b18b3b;outline-offset:3px}
.da-z-count{display:inline-flex;align-items:center;justify-content:center;min-width:22px;height:22px;border-radius:11px;background:#ffffff24;font-size:12px}
.da-z-panel[open]{width:360px}.da-z-panel[open]>summary{width:max-content;margin-left:auto;margin-bottom:8px}
.da-z-sheet{padding:18px;background:#fffdf7;border:1px solid #d5ded6;border-radius:16px;box-shadow:0 10px 40px #173d3326;max-height:55vh;max-height:55dvh;overflow:auto;overscroll-behavior:contain}.da-z-sheet header{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:10px}.da-z-sheet h2{font:600 16px/1.4 system-ui;margin:0}.da-z-sheet p{font:14px/1.65 system-ui;margin:8px 0 12px}.da-z-sheet fieldset{border:0;padding:0;margin:14px 0}.da-z-sheet legend{font-weight:600;margin-bottom:8px}.da-z-sheet fieldset label{display:block;margin:8px 0}.da-z-sheet input{accent-color:#175f4b}.da-z-sheet button{font:14px system-ui;cursor:pointer;border:1px solid #cad6cd;border-radius:8px;background:#fffdf7;color:#193d33;padding:10px 12px}.da-z-sheet #da-z-close{border:0;padding:5px 8px;font-size:20px;line-height:1}.da-z-sheet #da-z-save{display:block;width:100%;background:#175f4b;color:white;border-color:#175f4b;margin:12px 0}.da-z-sheet button:disabled{opacity:.45;cursor:default}.da-z-sheet a{color:#175f4b}.da-z-links{display:flex;gap:12px;flex-wrap:wrap;margin-top:12px;font-size:12px}.da-z-sheet ul{padding-left:20px;font-size:12px}.da-z-sheet [hidden]{display:none!important}
@media(max-width:480px){.da-z-panel{right:12px;bottom:max(12px,env(safe-area-inset-bottom));max-width:calc(100vw - 24px)}.da-z-panel[open]{width:calc(100vw - 24px)}.da-z-sheet{max-height:48vh;max-height:48dvh;padding:16px}}@media print{.da-z-panel,.da-z-choice{display:none}}
</style>`});
  edits.push({start:issue.body_offset,end:issue.body_offset,text:`<nav class="da-z-navigation" aria-label="日报导航"><a href="/archive/">往期日报</a><a href="/">日报首页</a></nav><details class="da-z-panel" id="da-z-panel" data-issue="${escape(issue.issue_id)}"><summary id="da-z-toggle" aria-label="展开 Zotero 保存选项">保存到 Zotero <span class="da-z-count" id="da-z-count" aria-label="已选论文数">0</span></summary><div class="da-z-sheet"><header><h2>保存所选论文</h2><button id="da-z-close" type="button" aria-label="收起保存选项">×</button></header><p id="da-z-status" role="status">正在读取本期已审阅目录。勾选不会保存。</p><fieldset id="da-z-intent"><legend>附件处理</legend><label><input type="radio" name="da-z-pdf" value="metadata_only" autocomplete="off">仅元数据</label> <label><input type="radio" name="da-z-pdf" value="open_access_if_available" autocomplete="off">同时尝试已核验开放 PDF</label></fieldset><button id="da-z-save" disabled>保存所选论文到 Zotero</button><button id="da-z-refresh">刷新连接状态</button><button id="da-z-retry" hidden>仅重试未完成步骤</button><div class="da-z-links"><a href="/zotero">连接设置</a><a href="/${escape(issue.entry_path)}">查看原始日报</a></div><ul id="da-z-results"></ul></div></details><script src="/zotero/reviewed-client.js" defer></script>`});
  edits.sort((a,b)=>a.start-b.start || a.end-b.end);const chunks=[];let cursor=0;
  for(const e of edits){if(e.start<cursor || e.end>bytes.length)throw new Error('INVALID_REVIEWED_OFFSETS');chunks.push(bytes.subarray(cursor,e.start),encoder.encode(e.text));cursor=e.end;}
  chunks.push(bytes.subarray(cursor));const size=chunks.reduce((n,b)=>n+b.length,0),result=new Uint8Array(size);let pos=0;for(const b of chunks){result.set(b,pos);pos+=b.length;}
  // The asset base must never turn in-page anchors into exits to sealed originals.
  const readerPath="/zotero/reports/"+encodeURIComponent(issue.issue_id);
  const rendered=decoder.decode(result).replace(/(<a\b[^>]*?\bhref\s*=\s*)(["'])#([^"']*)\2/gi,(_,prefix,quote,fragment)=>prefix+quote+escape(readerPath)+"#"+fragment+quote);
  return new Response(rendered,{headers:{'Content-Type':'text/html; charset=utf-8','Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer',
    'Content-Security-Policy':"default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; img-src 'self' https: data:; connect-src 'self'; base-uri 'self'; form-action 'none'; frame-ancestors 'none'",
    'X-Reviewed-Issue':issue.issue_id,'X-Reviewed-Projection-SHA256':issue.projection_sha256,'X-Original-HTML-SHA256':issue.html_sha256}});
}
export function reviewedClient(platform=globalThis) {
  const {document,fetch,AbortController}=platform,byId=id=>document.getElementById('da-z-'+id),panel=byId('panel');
  const issue=panel.dataset.issue,base='/api/zotero/reviewed-issues/'+encodeURIComponent(issue),choices=[...document.querySelectorAll('[data-da-z-paper]')],radios=[...document.querySelectorAll('input[name="da-z-pdf"]')];
  let info=null,busy=false,jobs=[],alive=true,epoch=0,interrupted=false;const requests=new Set();
  choices.forEach(c=>{c.checked=false;});radios.forEach(c=>{c.checked=false;});
  const status=text=>{byId('status').textContent=text;};
  const selected=()=>choices.filter(c=>c.checked).map(c=>c.dataset.daZPaper);
  const intent=()=>radios.find(c=>c.checked)?.value;
  function update(){if(byId('count'))byId('count').textContent=String(selected().length);const locked=busy || !alive;byId('save').disabled=locked || !info?.live_ready || !selected().length || selected().length>100 || !intent();choices.forEach(c=>c.disabled=locked);radios.forEach(c=>c.disabled=locked);byId('refresh').disabled=locked;byId('retry').disabled=locked;}
  async function request(path,body){const started=epoch;if(!alive)throw new Error('page_inactive');const controller=new AbortController();requests.add(controller);try{
    const response=await fetch(path,{method:body?'POST':'GET',credentials:'same-origin',cache:'no-store',redirect:'error',signal:controller.signal,
      headers:body?{'Content-Type':'application/json','X-Zotero-Action':'1'}:{Accept:'application/json'},...(body?{body:JSON.stringify(body)}:{})});
    const data=await response.json();if(!alive || started!==epoch)throw new Error('page_interrupted');if(!response.ok)throw new Error(data.error||'request_failed');return data;
  }finally{requests.delete(controller);}}
  function results(){byId('results').replaceChildren();for(const job of jobs)for(const item of job.items){
    const name=info.papers.find(p=>p.paper_id===item.paper_id)?.title || item.paper_id;
    const unavailable=['REVIEWED_PDF_SOURCE_UNAVAILABLE','VERIFIED_PDF_UNAVAILABLE'].includes(item.error_code);
    const pdfLabel=item.pdf_verified?'已核验':job.pdf_intent==='metadata_only'?'未请求':unavailable?'无法取得（无可用的已核验缓存或受支持来源）':item.error_code?'待处理（'+item.error_code+'）':'待处理';
    const message=`${name}：元数据${item.metadata_saved?'已保存':'待处理'}；分类${item.classification_saved?'已保存':'待处理'}；PDF${pdfLabel}`;
    const li=document.createElement('li');li.textContent=message;byId('results').append(li);
    const target=[...document.querySelectorAll('[data-da-z-result]')].find(e=>e.dataset.daZResult===item.paper_id);if(target)target.textContent=item.metadata_saved?' · 元数据已保存'+(item.pdf_verified?' / PDF 已核验':''):'';
  }byId('retry').hidden=!jobs.some(j=>['queued','running','retry_wait'].includes(j.state));}
  function resultStatus(){status(jobs.every(j=>j.state==='complete')?'本次保存已完成，逐项结果如下。':jobs.some(j=>['queued','running','retry_wait'].includes(j.state))?'本次保存尚未完成。已保存的元数据与分类会保留；请查看逐项状态，再明确重试未完成步骤。':'部分请求未完成，逐项原因如下。已保存的元数据与分类会保留；无可用来源的 PDF 需补充已核验来源，当前不会持续重试。');}
  async function advance(){for(let index=0;index<jobs.length;index++)for(let step=0;step<120 && alive;step++){
    if(!['queued','running'].includes(jobs[index].state))break;
    jobs[index]=await request(base+'/executions/'+jobs[index].job_id+'/advance',{action:'continue_import',projection_sha256:info.projection_sha256,connection_version:info.connection_version});results();
  }if(alive)resultStatus();}
  async function perform(fn){if(busy || !alive)return;const started=epoch;busy=true;update();try{await fn();}catch(e){if(alive && started===epoch)status('尚未完成：'+e.message+'。刷新只读取状态；需再次明确保存或重试。');}finally{if(started===epoch){busy=false;update();}}}
  async function refresh(){info=await request(base);
    for(const node of document.querySelectorAll('[data-da-z-pdf]')){const p=info.papers.find(p=>p.paper_id===node.dataset.daZPdf);
      node.textContent=!p?.pdf_reviewed?' · 当前无已审阅 PDF；仍可保存引用。':p.pdf_fetch_supported?' · 保存时核验私有缓存，或尝试已审阅开放来源。':' · 仅能使用已核验私有缓存；无缓存时 PDF 无法取得，引用仍可保存。';}
    for(let n=0;n<jobs.length;n++)jobs[n]=await request(base+'/executions/'+jobs[n].job_id);
    if(jobs.length){results();resultStatus();}
    else status(interrupted?'页面已恢复，上次请求可能已提交。当前只读取状态；请明确保存同一选择以核对并继续。':info.live_ready?'本期目录已就绪。勾选论文并选择附件处理方式后保存。':'本期目录已就绪，请先在连接设置中核验个人文库。');update();}
  choices.forEach(c=>c.addEventListener('change',update));radios.forEach(c=>c.addEventListener('change',update));
  const close=()=>{panel.open=false;byId('toggle')?.focus?.();};
  byId('close')?.addEventListener('click',close);
  panel.addEventListener('keydown',event=>{if(event?.key==='Escape'){close();event.preventDefault?.();}});
  byId('refresh').addEventListener('click',()=>perform(refresh));
  byId('save').addEventListener('click',()=>perform(async()=>{const result=await request(base+'/save',{action:'save_selected',projection_sha256:info.projection_sha256,connection_version:info.connection_version,paper_ids:selected(),pdf_intent:intent()});jobs=result.jobs;results();await advance();}));
  byId('retry').addEventListener('click',()=>perform(async()=>{for(let n=0;n<jobs.length;n++)jobs[n]=await request(base+'/executions/'+jobs[n].job_id);results();await advance();}));
  platform.addEventListener?.('pagehide',()=>{interrupted=interrupted || busy || jobs.some(j=>j.state!=='complete');alive=false;epoch++;busy=false;requests.forEach(c=>c.abort());update();});
  platform.addEventListener?.('pageshow',event=>{if(event.persisted){alive=true;return perform(refresh);}});
  void perform(refresh);return {refresh:()=>perform(refresh)};
}
export const reviewedClientSource=()=>`(${reviewedClient.toString()})(globalThis);`;
