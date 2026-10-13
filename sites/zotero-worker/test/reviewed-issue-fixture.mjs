// Synthetic sealed publications; never opens private source or credentials.
import {createHash} from 'node:crypto';
import {buildReviewedIssue} from '../hosting-adapter/reviewed-catalogs.mjs';
export const utf8=s=>new TextEncoder().encode(s),sha=b=>createHash('sha256').update(b).digest('hex');
export async function reviewedFixture(day='2000-01-01',{publishers=['arxiv.org','arxiv.org']}={}){
  const sourceUrls=publishers.map((host,i)=>host==='arxiv.org'?'https://arxiv.org/abs/2001.0000'+(i+1):'https://'+host+'/synthetic-paper-'+(i+1));
  const html=utf8('<!doctype html><html lang="zh"><head><meta http-equiv="Content-Security-Policy" content="default-src \'none\'"><title>合成日报</title></head><body>'+[1,2].map(n=>`<article id="paper-${n}"><h2>Synthetic paper ${n}</h2><a href="${sourceUrls[n-1]}">Source</a><p>已封存科学内容 ${n}。</p></article>`).join('')+'</body></html>');
  const key=`reports/${day}-synthetic-${sha(html).slice(0,12)}`,entry={key,page:key+'/index.html',date:day,source_sha256:sha(html),identity:sha(html),approval_sha256:'f'.repeat(64)};
  const batches=[],pdfs=new Map();
  for(const n of [1,2]){
    const pdf=utf8('%PDF-1.4\nSYNTHETIC PAPER '+n+'\n%%EOF\n');pdfs.set(sha(pdf),pdf);
    const p={paper_id:'paper-'+n,title:'Synthetic paper '+n,source_url:sourceUrls[n-1],doi:'',arxiv:'2001.0000'+n,authors:['Synthetic Author'],year:'2000',venue:'arXiv',item_type:'preprint',publication:'preprint',publication_evidence_sha256:'',topics:['agents'],pdf:{blob_id:sha(pdf),sha256:sha(pdf),size:pdf.length},review_sha256:'a'.repeat(64),qualification_receipt_sha256:'b'.repeat(64),source_pdf_sha256:sha(pdf)};
    const report=JSON.stringify({schema:'paper-first-corrected-edition-v1',edition_id:day+'-edition-'+n,date:day,html_sha256:sha(html),html_bytes:html.length,qualified_papers:1,paper_records:[{key:p.paper_id,...Object.fromEntries(['title','source_url','review_sha256','qualification_receipt_sha256','source_pdf_sha256'].map(k=>[k,p[k]]))}]}),catalog=JSON.stringify({schema:'daily-agent-zotero-catalog-v1',report_sha256:sha(report),papers:[p]});
    const binding={owner_id:'synthetic-bundle-owner',report_id:day+'-report-'+n,catalog_id:day+'-catalog-'+n,catalog_version:1,personal_scope:'personal:123456'};
    batches.push({binding,trustedPins:{...binding,report_sha256:sha(report),catalog_sha256:sha(catalog),html_sha256:sha(html),report_edition_id:day+'-edition-'+n,allowed_source_hosts:[publishers[n-1]]},report,catalog,pdf_sources:publishers[n-1]==='arxiv.org'?[{paper_id:p.paper_id,url:'https://arxiv.org/pdf/2001.0000'+n}]:[]});
  }
  const issueReportBytes=utf8(JSON.stringify({...JSON.parse(batches[0].report),edition_id:day+'-complete-edition',qualified_papers:2,paper_records:batches.flatMap(b=>JSON.parse(b.report).paper_records)}));
  const issue=await buildReviewedIssue({issue_id:'issue-'+day,entry,htmlBytes:html,batches,anchors:[1,2].map(n=>({paper_id:'paper-'+n,anchor:'paper-'+n})),paper_ids:['paper-1','paper-2'],issueReportBytes,issueReportSha256:sha(issueReportBytes)});
  return {issue,html,entry,pdfs,manifest:{schema_version:1,entries:[entry],files:{[entry.page]:{size:html.length,sha256:sha(html)}}}};
}
