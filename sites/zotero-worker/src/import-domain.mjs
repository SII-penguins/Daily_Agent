/** Pure, fail-closed import domain. Catalogs are server-reviewed, immutable artifacts.
 * URLs below are metadata only. This module never fetches a URL or accesses secrets.
 */
export class ImportDomainError extends Error {
  constructor(code) { super(code); this.name = 'ImportDomainError'; this.code = code; }
}
const reject = code => { throw new ImportDomainError(code); };
const record = v => v !== null && typeof v === 'object' && !Array.isArray(v);
const SHA256 = /^[a-f0-9]{64}$/;
const TOPICS = new Set(['quantum-error-correction','neural-quantum-states','quantum-optimization',
  'quantum-machine-learning','ai-for-science','agents','world-models','embodied-ai','other-reviewed']);
const encoder = new TextEncoder();
export const MAX_PDF_BYTES = 16 * 1024 * 1024;
export const validZoteroKey = key => typeof key === 'string' && /^[23456789ABCDEFGHIJKLMNPQRSTUVWXYZ]{8}$/.test(key);
export const validPersonalLibrary = id => typeof id === 'string' && /^[1-9]\d{0,15}$/.test(id) && Number.isSafeInteger(Number(id));
export function canonicalJSON(value) {
  const ancestors=new Set();
  const scalar=v=>{for(let i=0;i<v.length;i++){const c=v.charCodeAt(i);if(c>=0xd800&&c<=0xdbff){const n=v.charCodeAt(++i);if(!(n>=0xdc00&&n<=0xdfff))reject('INVALID_UNICODE');}else if(c>=0xdc00&&c<=0xdfff)reject('INVALID_UNICODE');}return v;};
  const visit=(v,depth=0)=>{
    if(depth>40)reject('INVALID_CANONICAL_DEPTH');
    if(v===null||typeof v==='boolean')return v;
    if(typeof v==='string')return scalar(v);
    if(typeof v==='number'&&Number.isSafeInteger(v)&&!Object.is(v,-0))return v;
    if(!record(v)&&!Array.isArray(v)||ancestors.has(v))reject('INVALID_CANONICAL_VALUE');
    ancestors.add(v);let out;
    if(Array.isArray(v)){
      if(Object.keys(v).length!==v.length)reject('INVALID_CANONICAL_VALUE');
      out=v.map(item=>visit(item,depth+1));
    }else{
      out={};for(const key of Object.keys(v).sort()){
        if(['__proto__','prototype','constructor'].includes(key))reject('INVALID_CANONICAL_KEY');
        out[scalar(key)]=visit(v[key],depth+1);
      }
    }
    ancestors.delete(v);return out;
  };
  return JSON.stringify(visit(value));
}
export async function sha256Text(value) {
  const bytes = new Uint8Array(await crypto.subtle.digest('SHA-256', encoder.encode(value)));
  return Array.from(bytes, v => v.toString(16).padStart(2,'0')).join('');
}
function digest(value) { if (typeof value !== 'string' || !SHA256.test(value)) reject('INVALID_DIGEST'); return value; }
function text(value, max = 1000, empty = false) {
  if (typeof value !== 'string' || value.length > max || (!empty && !value.trim()) || /[\u0000-\u001f\u007f]/u.test(value)) reject('INVALID_TEXT');
  return value.trim();
}
function exactKeys(value, keys) {
  if (!record(value) || Object.keys(value).length !== keys.length || keys.some(k => !Object.hasOwn(value,k))) reject('UNEXPECTED_FIELDS');
}
export function normalizeDOI(value) {
  if (value === undefined || value === null || value === '') return '';
  let v = text(value, 300).replace(/^doi:\s*/i, '');
  if (/^https?:\/\//i.test(v)) {
    let url; try { url = new URL(v); } catch { reject('INVALID_DOI'); }
    if (!['doi.org','dx.doi.org'].includes(url.hostname) || url.search || url.hash || url.username || url.password || url.port) reject('INVALID_DOI');
    try { v = decodeURIComponent(url.pathname.replace(/^\/+/,'')); } catch { reject('INVALID_DOI'); }
  }
  if (!/^10\.\d{4,9}\/[^\s]+$/i.test(v) || /[\u0000-\u001f\u007f]/u.test(v)) reject('INVALID_DOI');
  return v.toLowerCase(); // Suffix punctuation is significant: do not strip it.
}
export function normalizeArxiv(value) {
  if (value === undefined || value === null || value === '') return '';
  let v = text(value, 200).replace(/^arxiv:\s*/i, '');
  if (/^https?:\/\//i.test(v)) {
    let url; try { url = new URL(v); } catch { reject('INVALID_ARXIV'); }
    if (!['arxiv.org','www.arxiv.org'].includes(url.hostname) || url.search || url.hash || url.username || url.password || url.port || !/^\/(?:abs|pdf)\//.test(url.pathname)) reject('INVALID_ARXIV');
    v = url.pathname.replace(/^\/(?:abs|pdf)\//,'').replace(/\.pdf$/i,'');
  }
  v = v.replace(/v[1-9]\d*$/i,'').toLowerCase();
  if (!/^(?:\d{4}\.\d{4,5}|[a-z][a-z.\-]*\/\d{7})$/.test(v)) reject('INVALID_ARXIV');
  return v;
}
// Unicode 15.0.0 full casefold exceptions to scalar lowercase, generated from Python str.casefold.
const CASEFOLD=Object.freeze({"\u00b5":"\u03bc","\u00df":"ss","\u0149":"\u02bcn","\u017f":"s","\u01f0":"j\u030c","\u0345":"\u03b9","\u0390":"\u03b9\u0308\u0301","\u03b0":"\u03c5\u0308\u0301","\u03c2":"\u03c3","\u03d0":"\u03b2","\u03d1":"\u03b8","\u03d5":"\u03c6","\u03d6":"\u03c0","\u03f0":"\u03ba","\u03f1":"\u03c1","\u03f5":"\u03b5","\u0587":"\u0565\u0582","\u13a0":"\u13a0","\u13a1":"\u13a1","\u13a2":"\u13a2","\u13a3":"\u13a3","\u13a4":"\u13a4","\u13a5":"\u13a5","\u13a6":"\u13a6","\u13a7":"\u13a7","\u13a8":"\u13a8","\u13a9":"\u13a9","\u13aa":"\u13aa","\u13ab":"\u13ab","\u13ac":"\u13ac","\u13ad":"\u13ad","\u13ae":"\u13ae","\u13af":"\u13af","\u13b0":"\u13b0","\u13b1":"\u13b1","\u13b2":"\u13b2","\u13b3":"\u13b3","\u13b4":"\u13b4","\u13b5":"\u13b5","\u13b6":"\u13b6","\u13b7":"\u13b7","\u13b8":"\u13b8","\u13b9":"\u13b9","\u13ba":"\u13ba","\u13bb":"\u13bb","\u13bc":"\u13bc","\u13bd":"\u13bd","\u13be":"\u13be","\u13bf":"\u13bf","\u13c0":"\u13c0","\u13c1":"\u13c1","\u13c2":"\u13c2","\u13c3":"\u13c3","\u13c4":"\u13c4","\u13c5":"\u13c5","\u13c6":"\u13c6","\u13c7":"\u13c7","\u13c8":"\u13c8","\u13c9":"\u13c9","\u13ca":"\u13ca","\u13cb":"\u13cb","\u13cc":"\u13cc","\u13cd":"\u13cd","\u13ce":"\u13ce","\u13cf":"\u13cf","\u13d0":"\u13d0","\u13d1":"\u13d1","\u13d2":"\u13d2","\u13d3":"\u13d3","\u13d4":"\u13d4","\u13d5":"\u13d5","\u13d6":"\u13d6","\u13d7":"\u13d7","\u13d8":"\u13d8","\u13d9":"\u13d9","\u13da":"\u13da","\u13db":"\u13db","\u13dc":"\u13dc","\u13dd":"\u13dd","\u13de":"\u13de","\u13df":"\u13df","\u13e0":"\u13e0","\u13e1":"\u13e1","\u13e2":"\u13e2","\u13e3":"\u13e3","\u13e4":"\u13e4","\u13e5":"\u13e5","\u13e6":"\u13e6","\u13e7":"\u13e7","\u13e8":"\u13e8","\u13e9":"\u13e9","\u13ea":"\u13ea","\u13eb":"\u13eb","\u13ec":"\u13ec","\u13ed":"\u13ed","\u13ee":"\u13ee","\u13ef":"\u13ef","\u13f0":"\u13f0","\u13f1":"\u13f1","\u13f2":"\u13f2","\u13f3":"\u13f3","\u13f4":"\u13f4","\u13f5":"\u13f5","\u13f8":"\u13f0","\u13f9":"\u13f1","\u13fa":"\u13f2","\u13fb":"\u13f3","\u13fc":"\u13f4","\u13fd":"\u13f5","\u1c80":"\u0432","\u1c81":"\u0434","\u1c82":"\u043e","\u1c83":"\u0441","\u1c84":"\u0442","\u1c85":"\u0442","\u1c86":"\u044a","\u1c87":"\u0463","\u1c88":"\ua64b","\u1e96":"h\u0331","\u1e97":"t\u0308","\u1e98":"w\u030a","\u1e99":"y\u030a","\u1e9a":"a\u02be","\u1e9b":"\u1e61","\u1e9e":"ss","\u1f50":"\u03c5\u0313","\u1f52":"\u03c5\u0313\u0300","\u1f54":"\u03c5\u0313\u0301","\u1f56":"\u03c5\u0313\u0342","\u1f80":"\u1f00\u03b9","\u1f81":"\u1f01\u03b9","\u1f82":"\u1f02\u03b9","\u1f83":"\u1f03\u03b9","\u1f84":"\u1f04\u03b9","\u1f85":"\u1f05\u03b9","\u1f86":"\u1f06\u03b9","\u1f87":"\u1f07\u03b9","\u1f88":"\u1f00\u03b9","\u1f89":"\u1f01\u03b9","\u1f8a":"\u1f02\u03b9","\u1f8b":"\u1f03\u03b9","\u1f8c":"\u1f04\u03b9","\u1f8d":"\u1f05\u03b9","\u1f8e":"\u1f06\u03b9","\u1f8f":"\u1f07\u03b9","\u1f90":"\u1f20\u03b9","\u1f91":"\u1f21\u03b9","\u1f92":"\u1f22\u03b9","\u1f93":"\u1f23\u03b9","\u1f94":"\u1f24\u03b9","\u1f95":"\u1f25\u03b9","\u1f96":"\u1f26\u03b9","\u1f97":"\u1f27\u03b9","\u1f98":"\u1f20\u03b9","\u1f99":"\u1f21\u03b9","\u1f9a":"\u1f22\u03b9","\u1f9b":"\u1f23\u03b9","\u1f9c":"\u1f24\u03b9","\u1f9d":"\u1f25\u03b9","\u1f9e":"\u1f26\u03b9","\u1f9f":"\u1f27\u03b9","\u1fa0":"\u1f60\u03b9","\u1fa1":"\u1f61\u03b9","\u1fa2":"\u1f62\u03b9","\u1fa3":"\u1f63\u03b9","\u1fa4":"\u1f64\u03b9","\u1fa5":"\u1f65\u03b9","\u1fa6":"\u1f66\u03b9","\u1fa7":"\u1f67\u03b9","\u1fa8":"\u1f60\u03b9","\u1fa9":"\u1f61\u03b9","\u1faa":"\u1f62\u03b9","\u1fab":"\u1f63\u03b9","\u1fac":"\u1f64\u03b9","\u1fad":"\u1f65\u03b9","\u1fae":"\u1f66\u03b9","\u1faf":"\u1f67\u03b9","\u1fb2":"\u1f70\u03b9","\u1fb3":"\u03b1\u03b9","\u1fb4":"\u03ac\u03b9","\u1fb6":"\u03b1\u0342","\u1fb7":"\u03b1\u0342\u03b9","\u1fbc":"\u03b1\u03b9","\u1fbe":"\u03b9","\u1fc2":"\u1f74\u03b9","\u1fc3":"\u03b7\u03b9","\u1fc4":"\u03ae\u03b9","\u1fc6":"\u03b7\u0342","\u1fc7":"\u03b7\u0342\u03b9","\u1fcc":"\u03b7\u03b9","\u1fd2":"\u03b9\u0308\u0300","\u1fd3":"\u03b9\u0308\u0301","\u1fd6":"\u03b9\u0342","\u1fd7":"\u03b9\u0308\u0342","\u1fe2":"\u03c5\u0308\u0300","\u1fe3":"\u03c5\u0308\u0301","\u1fe4":"\u03c1\u0313","\u1fe6":"\u03c5\u0342","\u1fe7":"\u03c5\u0308\u0342","\u1ff2":"\u1f7c\u03b9","\u1ff3":"\u03c9\u03b9","\u1ff4":"\u03ce\u03b9","\u1ff6":"\u03c9\u0342","\u1ff7":"\u03c9\u0342\u03b9","\u1ffc":"\u03c9\u03b9","\uab70":"\u13a0","\uab71":"\u13a1","\uab72":"\u13a2","\uab73":"\u13a3","\uab74":"\u13a4","\uab75":"\u13a5","\uab76":"\u13a6","\uab77":"\u13a7","\uab78":"\u13a8","\uab79":"\u13a9","\uab7a":"\u13aa","\uab7b":"\u13ab","\uab7c":"\u13ac","\uab7d":"\u13ad","\uab7e":"\u13ae","\uab7f":"\u13af","\uab80":"\u13b0","\uab81":"\u13b1","\uab82":"\u13b2","\uab83":"\u13b3","\uab84":"\u13b4","\uab85":"\u13b5","\uab86":"\u13b6","\uab87":"\u13b7","\uab88":"\u13b8","\uab89":"\u13b9","\uab8a":"\u13ba","\uab8b":"\u13bb","\uab8c":"\u13bc","\uab8d":"\u13bd","\uab8e":"\u13be","\uab8f":"\u13bf","\uab90":"\u13c0","\uab91":"\u13c1","\uab92":"\u13c2","\uab93":"\u13c3","\uab94":"\u13c4","\uab95":"\u13c5","\uab96":"\u13c6","\uab97":"\u13c7","\uab98":"\u13c8","\uab99":"\u13c9","\uab9a":"\u13ca","\uab9b":"\u13cb","\uab9c":"\u13cc","\uab9d":"\u13cd","\uab9e":"\u13ce","\uab9f":"\u13cf","\uaba0":"\u13d0","\uaba1":"\u13d1","\uaba2":"\u13d2","\uaba3":"\u13d3","\uaba4":"\u13d4","\uaba5":"\u13d5","\uaba6":"\u13d6","\uaba7":"\u13d7","\uaba8":"\u13d8","\uaba9":"\u13d9","\uabaa":"\u13da","\uabab":"\u13db","\uabac":"\u13dc","\uabad":"\u13dd","\uabae":"\u13de","\uabaf":"\u13df","\uabb0":"\u13e0","\uabb1":"\u13e1","\uabb2":"\u13e2","\uabb3":"\u13e3","\uabb4":"\u13e4","\uabb5":"\u13e5","\uabb6":"\u13e6","\uabb7":"\u13e7","\uabb8":"\u13e8","\uabb9":"\u13e9","\uabba":"\u13ea","\uabbb":"\u13eb","\uabbc":"\u13ec","\uabbd":"\u13ed","\uabbe":"\u13ee","\uabbf":"\u13ef","\ufb00":"ff","\ufb01":"fi","\ufb02":"fl","\ufb03":"ffi","\ufb04":"ffl","\ufb05":"st","\ufb06":"st","\ufb13":"\u0574\u0576","\ufb14":"\u0574\u0565","\ufb15":"\u0574\u056b","\ufb16":"\u057e\u0576","\ufb17":"\u0574\u056d"});
export const normalizedText = value => typeof value === 'string' ? Array.from(value.normalize('NFKC'),c=>CASEFOLD[c]??c.toLowerCase()).join('').replace(/\s+/gu,' ').trim() : '';
function sourceURL(value) {
  const v = text(value, 2000); let url;
  try { url = new URL(v); } catch { reject('INVALID_SOURCE_URL'); }
  if (url.protocol !== 'https:' || url.username || url.password || url.port || url.hash || url.search ||
      /[\\\s]/.test(v) || !url.hostname.includes('.') || /(?:\.local|\.localhost|\.internal)$/.test(url.hostname) ||
      /^[\d.]+$/.test(url.hostname) || url.hostname.includes(':')) reject('INVALID_SOURCE_URL');
  return v;
}
export function registrationBinding(row) {
  return {schema:'daily-agent-zotero-registration-v1',owner_id:row.owner_id,report_id:row.report_id,
    catalog_id:row.catalog_id,catalog_version:row.catalog_version,personal_scope:row.personal_scope,
    report_sha256:row.report_sha256,report_day:row.report_day,source_catalog_sha256:row.source_catalog_sha256,
    content_sha256:row.content_sha256};
}
export function parsePaper(row) {
  if (!record(row)) reject('INVALID_PAPER');
  const allowed = new Set(['paper_id','title','source_url','doi','arxiv','authors','year','venue','item_type',
    'publication','publication_evidence_sha256','topics','pdf','review_sha256','qualification_receipt_sha256','source_pdf_sha256']);
  if (Object.keys(row).some(k => !allowed.has(k))) reject('UNEXPECTED_PAPER_FIELDS');
  const paper = { paper_id:text(row.paper_id,512),title:text(row.title),source_url:sourceURL(row.source_url),
    doi:normalizeDOI(row.doi),arxiv:normalizeArxiv(row.arxiv),year:row.year,venue:text(row.venue,1000,true),
    item_type:row.item_type,publication:row.publication,publication_evidence_sha256:row.publication_evidence_sha256 || '' };
  if (!Array.isArray(row.authors) || row.authors.length > 1000) reject('INVALID_AUTHORS');
  paper.authors = row.authors.map(v => text(v,200));
  if (typeof row.year !== 'string' || row.year && !/^\d{4}$/.test(row.year)) reject('INVALID_YEAR');
  if (!['journalArticle','conferencePaper','preprint','document'].includes(paper.item_type) ||
      !['published_verified','preprint','unverified'].includes(paper.publication) ||
      paper.publication !== 'published_verified' && ['journalArticle','conferencePaper'].includes(paper.item_type) ||
      paper.publication === 'preprint' && paper.item_type !== 'preprint') reject('INVALID_PUBLICATION_STATUS');
  if (paper.publication === 'published_verified' || paper.publication_evidence_sha256) digest(paper.publication_evidence_sha256);
  for (const key of ['review_sha256','qualification_receipt_sha256','source_pdf_sha256']) digest(row[key]);
  if (!Array.isArray(row.topics) || !row.topics.length || row.topics.some(t => !TOPICS.has(t))) reject('INVALID_TOPICS');
  paper.topics = [...new Set(row.topics)].sort();
  paper.pdf = null;
  if (row.pdf !== null && row.pdf !== undefined) {
    exactKeys(row.pdf,['blob_id','sha256','size']);
    digest(row.pdf.sha256);
    if (row.pdf.blob_id !== row.pdf.sha256 || row.pdf.sha256 !== row.source_pdf_sha256 ||
        !Number.isSafeInteger(row.pdf.size) || row.pdf.size < 8 || row.pdf.size > MAX_PDF_BYTES) reject('INVALID_PDF_REFERENCE');
    paper.pdf = {...row.pdf};
  }
  if (!paper.doi && !paper.arxiv && !fallbackIdentity(paper)) reject('INSUFFICIENT_IDENTITY');
  return paper;
}
/** Checks the complete server-side paper allowlist, canonical artifact seal and registration binding. */
export async function loadVerifiedCatalog(row, paperRows, libraryId) {
  if (!row || row.enabled !== 1 || !Number.isSafeInteger(row.catalog_version) || row.catalog_version < 1 ||
      !validPersonalLibrary(libraryId) || row.personal_scope !== `personal:${libraryId}` ||
      typeof row.catalog_blob_json !== 'string' || encoder.encode(row.catalog_blob_json).length > 4*1024*1024) reject('CATALOG_UNAVAILABLE');
  digest(row.content_sha256); digest(row.report_sha256); digest(row.source_catalog_sha256); digest(row.registration_sha256);
  if (await sha256Text(row.catalog_blob_json) !== row.content_sha256 ||
      await sha256Text(canonicalJSON(registrationBinding(row))) !== row.registration_sha256) reject('CATALOG_SEAL_MISMATCH');
  let catalog; try { catalog = JSON.parse(row.catalog_blob_json); } catch { reject('INVALID_CATALOG'); }
  exactKeys(catalog,['schema','report_sha256','papers']);
  if (canonicalJSON(catalog) !== row.catalog_blob_json || catalog.schema !== 'daily-agent-zotero-catalog-v1' ||
      catalog.report_sha256 !== row.report_sha256 || !Array.isArray(catalog.papers) ||
      !/^\d{4}-\d{2}-\d{2}$/.test(row.report_day) || new Date(`${row.report_day}T00:00:00Z`).toISOString().slice(0,10) !== row.report_day) reject('INVALID_CATALOG');
  if (!Array.isArray(paperRows) || paperRows.length !== catalog.papers.length || !paperRows.length || paperRows.length > 1000) reject('CATALOG_ALLOWLIST_MISMATCH');
  const index = new Map();
  for (const original of catalog.papers) {
    const paper = parsePaper(original);
    if (index.has(paper.paper_id)) reject('CATALOG_ALLOWLIST_MISMATCH');
    index.set(paper.paper_id,{paper,original});
  }
  const seen = new Set();
  for (const stored of paperRows) {
    const embedded = index.get(stored.paper_id);
    let metadata; try { metadata = JSON.parse(stored.metadata_json); } catch { reject('CATALOG_ALLOWLIST_MISMATCH'); }
    if (!embedded || seen.has(stored.paper_id) || stored.display_title !== embedded.original.title ||
        canonicalJSON(metadata) !== canonicalJSON(embedded.original)) reject('CATALOG_ALLOWLIST_MISMATCH');
    seen.add(stored.paper_id);
  }
  return {day:row.report_day,sha256:row.content_sha256,papers:[...index.values()].map(v=>v.paper)};
}
export function selectedPlan(catalog, request) {
  exactKeys(request,['action','report_id','catalog_id','catalog_version','paper_ids','pdf_intent']);
  if (request.action !== 'save_selected' || !['metadata_only','open_access_if_available'].includes(request.pdf_intent) ||
      !Array.isArray(request.paper_ids) || !request.paper_ids.length || request.paper_ids.length > 100 ||
      request.paper_ids.some(v=>typeof v !== 'string') || new Set(request.paper_ids).size !== request.paper_ids.length) reject('INVALID_SAVE_REQUEST');
  const index = new Map(catalog.papers.map(p=>[p.paper_id,p]));
  if (request.paper_ids.some(id=>!index.has(id))) reject('SELECTION_NOT_IN_CATALOG');
  return [...request.paper_ids].sort().map(id=>index.get(id));
}
export function fallbackIdentity(paper) {
  if (paper.doi || paper.arxiv || !paper.authors?.length || !paper.year || !paper.venue) return '';
  return canonicalJSON([normalizedText(paper.title),paper.authors.map(normalizedText),paper.year,normalizedText(paper.venue)]);
}
function extraIdentifiers(extra, label) {
  if (typeof extra !== 'string') return [];
  return extra.split(/\r?\n/).filter(line => new RegExp(`^${label}:\\s*`,'i').test(line)).map(line=>line.replace(new RegExp(`^${label}:\\s*`,'i'),''));
}
export function identityFromItem(item) {
  const data = item?.data;
  if (!record(data) || ['attachment','note','annotation'].includes(data.itemType)) return null;
  let invalid = false;
  const get = (values,normalize) => {
    const found = new Set();
    for (const value of values.filter(Boolean)) { try { found.add(normalize(value)); } catch { invalid=true; } }
    if (found.size > 1) invalid = true;
    return [...found][0] || '';
  };
  const doi = get([data.DOI,...extraIdentifiers(data.extra,'DOI')],normalizeDOI);
  const arxivValues = extraIdentifiers(data.extra,'arXiv');
  if (typeof data.url === 'string' && /^https?:\/\/(?:www\.)?arxiv\.org\/(?:abs|pdf)\//i.test(data.url)) arxivValues.push(data.url);
  if (typeof data.archiveID === 'string' && /arxiv/i.test(data.archive || '')) arxivValues.push(data.archiveID);
  const arxiv = get(arxivValues,normalizeArxiv);
  const creators = Array.isArray(data.creators) ? data.creators : [];
  const authors = creators.filter(c=>c.creatorType==='author').map(c=>typeof c.name==='string' ? c.name : [c.firstName,c.lastName].filter(v=>typeof v==='string').join(' '));
  // An imprecise date or author field disables fallback rather than guessing.
  const year = typeof data.date==='string' && /^\d{4}$/.test(data.date) ? data.date : '';
  const extraVenues=extraIdentifiers(data.extra,'Venue');
  const venue = data.publicationTitle || data.proceedingsTitle || data.repository || (extraVenues.length===1?extraVenues[0]:'');
  return {doi,arxiv,authors,year,venue,title:data.title || '',source_url:typeof data.url==='string'?data.url:'',invalid};
}
// These deliberately broad signals can only STOP a create. They never prove a
// match, authorize a merge, repair metadata, or override strong identifiers.
const nearTitle=value=>normalizedText(value).replace(/[\p{P}\p{S}\s]/gu,'');
function sourceIdentity(value){
  if(typeof value!=='string' || !value)return '';
  try{const u=new URL(value);if(!['https:','http:'].includes(u.protocol)||u.username||u.password||u.port)return '';
    return u.hostname.toLowerCase().replace(/^www\./,'')+u.pathname.replace(/\/+$/,'');}catch{return '';}
}
function unresolvedFallbackCandidate(paper,identity){
  if(paper.doi || paper.arxiv)return false;
  const title=nearTitle(paper.title),source=sourceIdentity(paper.source_url);
  return Boolean(title && title===nearTitle(identity.title) || source && source===sourceIdentity(identity.source_url));
}
export function matchPaper(paper, candidates) {
  if (!Array.isArray(candidates)) reject('INCOMPLETE_INVENTORY');
  const found=[];
  for (const item of candidates) {
    const identity=identityFromItem(item); if (!identity) continue;
    let match = Boolean(paper.doi && identity.doi===paper.doi || paper.arxiv && identity.arxiv===paper.arxiv);
    if (match && (identity.invalid || paper.doi && identity.doi && paper.doi!==identity.doi ||
        paper.arxiv && identity.arxiv && paper.arxiv!==identity.arxiv)) reject('IDENTITY_CONFLICT');
    if (!match && !identity.invalid && !paper.doi && !paper.arxiv && !identity.doi && !identity.arxiv) {
      const fallback=fallbackIdentity(paper); match=Boolean(fallback && fallback===fallbackIdentity(identity));
    }
    if (!match && unresolvedFallbackCandidate(paper,identity)) {
      if(item.data.deleted)reject('MATCH_IN_TRASH');
      reject('FALLBACK_IDENTITY_REVIEW_REQUIRED');
    }
    if (match) { if (item.data.deleted) reject('MATCH_IN_TRASH'); found.push(item); }
  }
  if (found.length>1) reject('AMBIGUOUS_IDENTITY');
  return found[0] || null;
}
export function requirePaperMatch(paper,item) {
  if (!item || !validZoteroKey(item.key) || !matchPaper(paper,[item])) reject('ITEM_IDENTITY_CHANGED');
  return item;
}
export function classification(paper,day) {
  return {paths:[['DailyAgent',day.slice(0,4),day],...paper.topics.map(topic=>['DailyAgent','Topics',topic])],
    tags:['DailyAgent',`daily:${day}`,`publication:${paper.publication}`,...paper.topics.map(t=>`topic:${t}`)]};
}
export function allCollectionPaths(papers,day) {
  const paths=new Map();
  for (const paper of papers) for (const path of classification(paper,day).paths) for(let n=1;n<=path.length;n++) paths.set(canonicalJSON(path.slice(0,n)),path.slice(0,n));
  return [...paths.values()].sort((a,b)=>a.length-b.length || canonicalJSON(a).localeCompare(canonicalJSON(b),'en'));
}
export function metadataFor(paper) {
  const data={itemType:paper.item_type,title:paper.title,url:paper.source_url,
    creators:paper.authors.map(name=>({creatorType:'author',name})),date:paper.year};
  if(paper.doi) { if (paper.item_type!=='document') data.DOI=paper.doi; else data.extra=`DOI: ${paper.doi}`; }
  if(paper.venue) {
    if(paper.item_type==='journalArticle') data.publicationTitle=paper.venue;
    else if(paper.item_type==='conferencePaper') data.proceedingsTitle=paper.venue;
    else if(paper.item_type==='preprint') data.repository=paper.venue;
    else data.extra=[data.extra,`Venue: ${paper.venue}`].filter(Boolean).join('\n');
  }
  if(paper.arxiv) data.extra=[data.extra,`arXiv: ${paper.arxiv}`].filter(Boolean).join('\n');
  return data;
}
export function mergedMembership(item,collectionKeys,tags) {
  const data=item?.data;
  if(!record(data) || !Array.isArray(data.collections) || !Array.isArray(data.tags) ||
      data.collections.some(k=>!validZoteroKey(k)) || data.tags.some(t=>!record(t)||typeof t.tag!=='string')) reject('INVALID_ITEM_MEMBERSHIP');
  const collections=[...data.collections],merged=data.tags.map(tag=>({...tag})),names=new Set(merged.map(t=>t.tag));
  for(const key of collectionKeys) if(!collections.includes(key)) collections.push(key);
  for(const tag of tags) if(!names.has(tag)) { merged.push({tag,type:1}); names.add(tag); }
  return {collections,tags:merged,changed:canonicalJSON(collections)!==canonicalJSON(data.collections)||canonicalJSON(merged)!==canonicalJSON(data.tags)};
}
export function requireAttachment(item,{key,parentItem,filename}) {
  if(!item || item.key!==key || item.data?.deleted || item.data?.itemType!=='attachment' ||
      item.data.parentItem!==parentItem || item.data.linkMode!=='imported_file' ||
      item.data.contentType!=='application/pdf' || item.data.filename!==filename) reject('ATTACHMENT_IDENTITY_CHANGED');
  return item;
}
export async function verifyPdfBytes(ref,bytes,hashBytes) {
  if(!(bytes instanceof Uint8Array)||bytes.length!==ref.size||bytes.length>MAX_PDF_BYTES||
    ![37,80,68,70,45].every((v,i)=>bytes[i]===v)||typeof hashBytes!=='function') reject('INVALID_PDF_BYTES');
  const owned=new Uint8Array(bytes), hashes=await hashBytes(owned);
  if(!hashes || !/^[a-f0-9]{32}$/.test(hashes.md5)||hashes.sha256!==ref.sha256) reject('PDF_DIGEST_MISMATCH');
  return {bytes:owned,md5:hashes.md5,sha256:hashes.sha256,size:owned.length};
}
