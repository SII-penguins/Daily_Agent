/** Opt-in pinned public PDF fetch. Independent of every Zotero/auth transport. */
import { reviewedPdfUrl } from './reviewed-issues.mjs';
import { sha256Bytes } from './hash-bytes.mjs';
let inFlight=0;
export async function fetchReviewedPdf({url,sha256,size,fetchImpl=globalThis.fetch,timeoutMs=15000}={}) {
  if (!reviewedPdfUrl(url) || !/^[a-f0-9]{64}$/.test(sha256) || !Number.isSafeInteger(size) || size<8 || size>16*1024*1024
      || !Number.isSafeInteger(timeoutMs) || timeoutMs<1 || timeoutMs>15000 || inFlight>=2) return null;
  inFlight++;
  const controller=new AbortController(); let reader;
  let timer;
  const timeout=new Promise((_,reject)=>{timer=setTimeout(()=>{controller.abort();void reader?.cancel().catch(()=>{});reject(new Error('PDF_TIMEOUT'));},timeoutMs);});
  const run=async()=>{
    for(let redirects=0;redirects<=3;redirects++) {
      if(!reviewedPdfUrl(url))return null;
      const response=await fetchImpl(url,{method:'GET',redirect:'manual',credentials:'omit',referrerPolicy:'no-referrer',headers:{Accept:'application/pdf'},signal:controller.signal});
      if([301,302,303,307,308].includes(response.status)) {
        const next=response.headers.get('Location'); await response.body?.cancel();
        if(!next || redirects===3)return null;
        url=new URL(next,url).href;continue;
      }
      if(response.status!==200 || response.headers.get('Content-Type')?.split(';')[0].trim().toLowerCase()!=='application/pdf'
          || response.headers.has('Content-Length') && response.headers.get('Content-Length')!==String(size) || !response.body?.getReader) {await response.body?.cancel();return null;}
      reader=response.body.getReader();const chunks=[];let length=0;
      for(;;){const r=await reader.read();if(r.done)break;if(!(r.value instanceof Uint8Array)||(length+=r.value.length)>size){await reader.cancel();return null;}chunks.push(r.value);}
      if(length!==size)return null;const bytes=new Uint8Array(length);let pos=0;for(const chunk of chunks){bytes.set(chunk,pos);pos+=chunk.length;}
      if(new TextDecoder().decode(bytes.subarray(0,5))!=='%PDF-' || await sha256Bytes(bytes)!==sha256)return null;
      return bytes;
    }
    return null;
  };
  try {return await Promise.race([run(),timeout]);}catch{return null;}finally{clearTimeout(timer);controller.abort();try{reader?.releaseLock();}catch{}inFlight--;}
}
