/** Reversible response-only enhancement. Original sealed assets are never written. */
const escape=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const encoder=new TextEncoder(),decoder=new TextDecoder('utf8',{fatal:true});
export function renderReviewedPage(bytes,issue) {
  const edits=issue.csp_ranges.map(([start,end])=>({start,end,text:''}));
  for(const a of issue.anchors)edits.push({start:a.offset,end:a.offset,text:`<label class="da-z-choice"><input type="checkbox" data-da-z-paper="${escape(a.paper_id)}" autocomplete="off"> 选择保存到 Zotero <span data-da-z-result="${escape(a.paper_id)}"></span><small data-da-z-pdf="${escape(a.paper_id)}"></small></label>`});
  edits.push({start:issue.head_offset,end:issue.head_offset,text:`<base href="/${escape(issue.entry_path.slice(0,issue.entry_path.lastIndexOf('/')+1))}"><style>.da-z-choice{display:block;padding:10px;border:1px solid #bccbd5;margin:12px 0;font:16px system-ui;color:#183348;background:#f2f8fc}.da-z-panel{position:sticky;bottom:0;padding:16px;background:#eef5fa;color:#183348;border:1px solid #8eaabe;z-index:100;font:16px system-ui}.da-z-panel button{padding:8px;margin:4px}.da-z-panel a{color:#164a7b}</style>`});
  edits.push({start:issue.body_offset,end:issue.body_offset,text:`<section class="da-z-panel" id="da-z-panel" data-issue="${escape(issue.issue_id)}"><p id="da-z-status" role="status">正在读取本期已审阅目录。勾选不会保存。</p><fieldset id="da-z-intent"><legend>附件处理</legend><label><input type="radio" name="da-z-pdf" value="metadata_only" autocomplete="off">仅元数据</label> <label><input type="radio" name="da-z-pdf" value="open_access_if_available" autocomplete="off">同时尝试已核验开放 PDF</label></fieldset><button id="da-z-save" disabled>保存所选论文到 Zotero</button><button id="da-z-refresh">刷新连接状态</button><button id="da-z-retry" hidden>仅重试未完成步骤</button><a href="/zotero">连接设置</a> · <a href="/${escape(issue.entry_path)}">查看原始日报</a><ul id="da-z-results"></ul></section><script src="/zotero/reviewed-client.js" defer></script>`});
  edits.sort((a,b)=>a.start-b.start || a.end-b.end);const chunks=[];let cursor=0;
  for(const e of edits){if(e.start<cursor || e.end>bytes.length)throw new Error('INVALID_REVIEWED_OFFSETS');chunks.push(bytes.subarray(cursor,e.start),encoder.encode(e.text));cursor=e.end;}
  chunks.push(bytes.subarray(cursor));const size=chunks.reduce((n,b)=>n+b.length,0),result=new Uint8Array(size);let pos=0;for(const b of chunks){result.set(b,pos);pos+=b.length;}
  return new Response(decoder.decode(result),{headers:{'Content-Type':'text/html; charset=utf-8','Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer',
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
  function update(){const locked=busy || !alive;byId('save').disabled=locked || !info?.live_ready || !selected().length || selected().length>100 || !intent();choices.forEach(c=>c.disabled=locked);radios.forEach(c=>c.disabled=locked);byId('refresh').disabled=locked;byId('retry').disabled=locked;}
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
  byId('refresh').addEventListener('click',()=>perform(refresh));
  byId('save').addEventListener('click',()=>perform(async()=>{const result=await request(base+'/save',{action:'save_selected',projection_sha256:info.projection_sha256,connection_version:info.connection_version,paper_ids:selected(),pdf_intent:intent()});jobs=result.jobs;results();await advance();}));
  byId('retry').addEventListener('click',()=>perform(async()=>{for(let n=0;n<jobs.length;n++)jobs[n]=await request(base+'/executions/'+jobs[n].job_id);results();await advance();}));
  platform.addEventListener?.('pagehide',()=>{interrupted=interrupted || busy || jobs.some(j=>j.state!=='complete');alive=false;epoch++;busy=false;requests.forEach(c=>c.abort());update();});
  platform.addEventListener?.('pageshow',event=>{if(event.persisted){alive=true;return perform(refresh);}});
  void perform(refresh);return {refresh:()=>perform(refresh)};
}
export const reviewedClientSource=()=>`(${reviewedClient.toString()})(globalThis);`;
