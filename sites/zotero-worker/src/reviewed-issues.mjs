/** Immutable server-only post-seal projections. No request may supply this data. */
import { canonicalCatalogJson, prepareReviewedCatalog } from './catalog-registration.mjs';
import { sha256Bytes } from './hash-bytes.mjs';
export const REVIEWED_SCHEMA = 'daily-agent-reviewed-issue-v1';
export const MAX_REVIEWED_BYTES = 8 * 1024 * 1024;
export const MAX_ISSUE_BYTES = 2 * 1024 * 1024;
const encoder = new TextEncoder();
export const issueJson = canonicalCatalogJson;
export const issueHash = value => sha256Bytes(encoder.encode(issueJson(value)));
const id = v => typeof v === 'string' && /^[A-Za-z0-9][A-Za-z0-9_-]{0,95}$/.test(v);
const sha = v => typeof v === 'string' && /^[a-f0-9]{64}$/.test(v);
const exact = (v, keys) => v && typeof v === 'object' && !Array.isArray(v) && Object.keys(v).sort().join(',') === [...keys].sort().join(',');
const fail = () => { throw new Error('INVALID_REVIEWED_ISSUE'); };
export function deploymentReviewedIssues() {
  return typeof __ZOTERO_REVIEWED_ISSUES__ === 'undefined' ? [] : __ZOTERO_REVIEWED_ISSUES__;
}
export async function validateReviewedIssue(value, htmlBytes) {
  const legacy=Boolean(value && Object.hasOwn(value,'legacy_seal'));
  if (!exact(value,['schema','issue_id','day','entry_path','html_sha256','html_bytes',legacy?'legacy_seal':'approval_sha256','issue_report','issue_report_sha256','owner_id','personal_scope','paper_ids','batches','anchors','csp_ranges','head_offset','body_offset','projection_sha256'])) fail();
  if(legacy){
    const seal=value.legacy_seal;
    if(!exact(seal,['kind','entry_path','html_sha256','composition_schema','composition_manifest_sha256','independent_static_review_sha256','full_adapter_sha256'])
        || seal.kind!=='legacy-sealed-migration-v1' || seal.entry_path!==value.entry_path || seal.html_sha256!==value.html_sha256
        || seal.composition_schema!=='daily-agent-oct13-host-composition-v1'
        || !sha(seal.composition_manifest_sha256) || !sha(seal.independent_static_review_sha256)
        || !sha(seal.full_adapter_sha256) || seal.full_adapter_sha256!==value.issue_report_sha256)fail();
  }else if(!sha(value.approval_sha256))fail();
  const { projection_sha256, ...payload } = value;
  if (encoder.encode(issueJson(value)).length > MAX_ISSUE_BYTES || !sha(projection_sha256) || await issueHash(payload) !== projection_sha256
      || value.schema !== REVIEWED_SCHEMA || !id(value.issue_id) || !sha(value.html_sha256)
      || !/^\d{4}-\d{2}-\d{2}$/.test(value.day) || !Number.isFinite(Date.parse(value.day)) || new Date(value.day).toISOString().slice(0,10) !== value.day
      || !new RegExp('^reports/'+value.day+'-[A-Za-z0-9-]+-'+value.html_sha256.slice(0,12)+'/index\\.html$').test(value.entry_path)
      || !Number.isSafeInteger(value.html_bytes) || value.html_bytes < 1 || value.html_bytes > 16*1024*1024
      || typeof value.owner_id !== 'string' || !value.owner_id || !/^personal:[1-9]\d{0,15}$/.test(value.personal_scope)
      || !Array.isArray(value.batches) || !value.batches.length || value.batches.length > 16
      || !Array.isArray(value.paper_ids) || !value.paper_ids.length || value.paper_ids.length > 1000
      || !Array.isArray(value.anchors) || !Array.isArray(value.csp_ranges)) fail();
  if(typeof value.issue_report!=='string' || !sha(value.issue_report_sha256) || await sha256Bytes(encoder.encode(value.issue_report))!==value.issue_report_sha256)fail();
  const issueReport=JSON.parse(value.issue_report);
  if(issueReport.schema!=='paper-first-corrected-edition-v1' || issueReport.date!==value.day || issueReport.html_sha256!==value.html_sha256
      || issueReport.html_bytes!==value.html_bytes || !Array.isArray(issueReport.paper_records) || issueReport.qualified_papers!==issueReport.paper_records.length
      || issueReport.paper_records.length!==value.paper_ids.length || new Set(issueReport.paper_records.map(p=>p.key)).size!==value.paper_ids.length)fail();
  const qualified=new Map(issueReport.paper_records.map(p=>[p.key,p]));
  if(value.paper_ids.some(id=>!qualified.has(id)))fail();
  const seen = new Set(), catalogs = new Set(), prepared = [];
  for (const b of value.batches) {
    if (!exact(b,['binding','trustedPins','report','catalog','pdf_sources']) || typeof b.report !== 'string' || typeof b.catalog !== 'string'
        || !Array.isArray(b.pdf_sources) || b.binding?.owner_id !== value.owner_id || b.binding?.personal_scope !== value.personal_scope
        || b.trustedPins?.html_sha256 !== value.html_sha256) fail();
    const key = b.binding.report_id+'\0'+b.binding.catalog_id;
    if (catalogs.has(key)) fail(); catalogs.add(key);
    const report = JSON.parse(b.report), catalog = JSON.parse(b.catalog);
    if (report.date !== value.day || report.html_bytes !== value.html_bytes || !Array.isArray(catalog.papers)) fail();
    // Even without HTML (browser read/API identity checks), bind exact source bytes.
    if (await sha256Bytes(encoder.encode(b.report)) !== b.trustedPins.report_sha256 || await sha256Bytes(encoder.encode(b.catalog)) !== b.trustedPins.catalog_sha256) fail();
    for (const p of catalog.papers) {
      const evidence=qualified.get(p.paper_id);
      if (typeof p.paper_id !== 'string' || seen.has(p.paper_id) || !evidence || ['title','source_url','review_sha256','qualification_receipt_sha256','source_pdf_sha256'].some(k=>evidence[k]!==p[k])) fail();
      seen.add(p.paper_id);
    }
    const sourceIds = new Set();
    for (const s of b.pdf_sources) {
      const p = catalog.papers.find(p => p.paper_id === s.paper_id);
      if (!exact(s,['paper_id','url']) || !p?.pdf || sourceIds.has(s.paper_id) || !reviewedPdfUrl(s.url)) fail();
      sourceIds.add(s.paper_id);
    }
    if (htmlBytes) prepared.push(await prepareReviewedCatalog({binding:b.binding,trustedPins:b.trustedPins,
      reportBytes:encoder.encode(b.report),catalogBytes:encoder.encode(b.catalog),htmlBytes,includeProtocolMd5:false}));
  }
  if (value.paper_ids.length !== seen.size || new Set(value.paper_ids).size !== seen.size || value.paper_ids.some(p=>!seen.has(p))) fail();
  const offset = n => Number.isSafeInteger(n) && n >= 0 && n <= value.html_bytes;
  if (!offset(value.head_offset) || !offset(value.body_offset) || value.anchors.length !== seen.size) fail();
  const mapped = new Set(), anchorIds = new Set();
  for (const a of value.anchors) {
    if (!exact(a,['paper_id','anchor','offset']) || !seen.has(a.paper_id) || mapped.has(a.paper_id) || !(typeof a.anchor==='string' && /^[A-Za-z0-9][A-Za-z0-9._:()/;-]{0,299}$/.test(a.anchor)) || anchorIds.has(a.anchor) || !offset(a.offset)) fail();
    mapped.add(a.paper_id); anchorIds.add(a.anchor);
  }
  let end = -1;
  for (const r of value.csp_ranges) { if (!Array.isArray(r) || r.length !== 2 || !offset(r[0]) || !offset(r[1]) || r[0] < end || r[1] <= r[0]) fail(); end=r[1]; }
  if (htmlBytes && (htmlBytes.byteLength !== value.html_bytes || await sha256Bytes(htmlBytes) !== value.html_sha256)) fail();
  return prepared;
}
/** Small fixed public host/path policy. No private network, caller URL, login or
 * credential forwarding. Unsupported publishers remain attachment-unavailable. */
export function reviewedPdfUrl(value) {
  if (typeof value !== 'string' || value.length > 2048 || /[\x00-\x20\\]/.test(value)) return false;
  try { const u=new URL(value); return u.href===value && u.protocol==='https:' && !u.username && !u.password && !u.port && !u.search && !u.hash
    && ['arxiv.org','export.arxiv.org'].includes(u.hostname) && /^\/pdf\/(?:\d{4}\.\d{4,5}(?:v\d+)?|[a-z-]+\/\d{7}(?:v\d+)?)(?:\.pdf)?$/.test(u.pathname); } catch { return false; }
}
export async function validateReviewedRegistry(issues) {
  if (!Array.isArray(issues) || issues.length > 1000 || encoder.encode(issueJson(issues)).length > MAX_REVIEWED_BYTES) fail();
  const seen=new Set(), bindings=new Set(); let owner, scope;
  for (const issue of issues) {
    await validateReviewedIssue(issue);
    if (seen.has(issue.issue_id) || owner && (owner!==issue.owner_id || scope!==issue.personal_scope)) fail();
    seen.add(issue.issue_id); owner=issue.owner_id; scope=issue.personal_scope;
    for (const b of issue.batches) { const key=b.binding.report_id+'\0'+b.binding.catalog_id; if(bindings.has(key))fail();bindings.add(key); }
  }
  return issues;
}
