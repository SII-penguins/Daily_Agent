/** Server-only trusted artifact preparation. NEVER mount this as a public upsert
 * route. Pins and bindings come from an authenticated, reviewed server registry,
 * not from the catalog, browser, caller's request body, or a neighboring checksum. */
import { createHashBytes, sha256Bytes, DEFAULT_MAX_PDF_BYTES, HARD_MAX_PDF_BYTES } from './hash-bytes.mjs';
const encoder = new TextEncoder();
const decoder = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true });
const SHA = /^[a-f0-9]{64}$/;
const ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const validId = value => typeof value === 'string' && ID.test(value);
const PAPER_ID = /^[A-Za-z0-9][A-Za-z0-9._:()/;-]{0,299}$/;
const MAX_JSON_BYTES = 4 * 1024 * 1024;
const MAX_HTML_BYTES = 16 * 1024 * 1024;
const MAX_TOTAL_PDF_BYTES = 256 * 1024 * 1024;
const TOPICS = new Set(['quantum-error-correction', 'neural-quantum-states', 'quantum-optimization',
  'quantum-machine-learning', 'ai-for-science', 'agents', 'world-models', 'embodied-ai', 'other-reviewed']);
const BINDING_KEYS = ['owner_id', 'report_id', 'catalog_id', 'catalog_version', 'personal_scope'];
const PAPER_KEYS = ['paper_id', 'title', 'source_url', 'doi', 'arxiv', 'authors', 'year', 'venue',
  'item_type', 'publication', 'publication_evidence_sha256', 'topics', 'pdf',
  'review_sha256', 'qualification_receipt_sha256', 'source_pdf_sha256'];
const BOUND_FIELDS = ['title', 'source_url', 'review_sha256', 'qualification_receipt_sha256', 'source_pdf_sha256'];
const REPORT_KEYS = ['schema', 'edition_id', 'date', 'title', 'sealed_at', 'html_sha256', 'html_bytes',
  'source_article_html_sha256', 'source_manifest_sha256', 'presentation_proof_sha256', 'approved_css_sha256',
  'qualified_papers', 'qualified_repositories', 'new_fullread_papers', 'reused_reviewed_papers',
  'reused_reviewed_repositories', 'original_figure_count', 'actual_model_jobs', 'bounded_repair_papers',
  'paper_records', 'legacy_production_seal', 'publication', 'delivery', 'browser_visual_qa', 'static_checks'];
const REPORT_PAPER_KEYS = ['key', 'title', 'source_date', 'source_url', 'source_pdf_sha256',
  'qualification_receipt_sha256', 'draft_sha256', 'review_sha256', 'source_component',
  'source_article_sha256', 'article_sha256', 'figure_count', 'reuse'];
const preparedBytes = new WeakMap();
export class CatalogRegistrationError extends Error {
  constructor(code) { super(code); this.name = 'CatalogRegistrationError'; this.code = code; }
}
const fail = code => { throw new CatalogRegistrationError(code); };
const record = v => v !== null && typeof v === 'object' && !Array.isArray(v)
  && [Object.prototype, null].includes(Object.getPrototypeOf(v));
function keys(value, required, allowed = required) {
  if (!record(value) || required.some(k => !Object.hasOwn(value, k)) || Object.keys(value).some(k => !allowed.includes(k))) fail('UNEXPECTED_FIELDS');
}
function text(value, max = 1000, empty = false) {
  if (typeof value !== 'string' || value.length > max || (!empty && !value.trim()) || value !== value.trim()
      || /[\u0000-\u001f\u007f]/u.test(value)) fail('INVALID_TEXT');
  return value;
}
function digest(value) { if (typeof value !== 'string' || !SHA.test(value)) fail('INVALID_DIGEST'); return value; }
function bytesOf(value, max, code = 'INVALID_SOURCE_BYTES') {
  if (!(value instanceof Uint8Array) || value.byteLength > max ||
      (typeof SharedArrayBuffer !== 'undefined' && value.buffer instanceof SharedArrayBuffer)) fail(code);
  return new Uint8Array(value);
}
function scalarString(value) {
  for (let i = 0; i < value.length; i++) {
    const code = value.charCodeAt(i);
    if (code >= 0xd800 && code <= 0xdbff) {
      const next = value.charCodeAt(++i);
      if (!(next >= 0xdc00 && next <= 0xdfff)) fail('NONSCALAR_UNICODE');
    } else if (code >= 0xdc00 && code <= 0xdfff) fail('NONSCALAR_UNICODE');
  }
  return value;
}
function compareKeys(a, b) {
  const x = Array.from(a, c => c.codePointAt(0)), y = Array.from(b, c => c.codePointAt(0));
  for (let i = 0; i < Math.min(x.length, y.length); i++) if (x[i] !== y[i]) return x[i] - y[i];
  return x.length - y.length;
}
/** Fixed JSON domain, recursively sorted keys, no undefined/NaN/floats/surrogates.
 * With the ASCII schema keys this matches Python ensure_ascii=False, sort_keys=True,
 * separators=(',', ':'). Array order and exact Unicode spelling are preserved. */
export function canonicalCatalogJson(value) {
  const ancestors = new Set();
  const encode = (v, depth = 0) => {
    if (depth > 40) fail('JSON_TOO_DEEP');
    if (v === null || typeof v === 'boolean') return JSON.stringify(v);
    if (typeof v === 'string') return JSON.stringify(scalarString(v));
    if (typeof v === 'number') { if (!Number.isSafeInteger(v) || Object.is(v, -0)) fail('INVALID_JSON_NUMBER'); return String(v); }
    if ((!record(v) && !Array.isArray(v)) || ancestors.has(v)) fail('INVALID_JSON_VALUE');
    ancestors.add(v);
    let out;
    if (Array.isArray(v)) {
      if (Object.keys(v).length !== v.length) fail('INVALID_JSON_ARRAY');
      out = '[' + v.map(item => encode(item, depth + 1)).join(',') + ']';
    } else {
      out = '{' + Object.keys(v).sort(compareKeys).map(k => {
        if (['__proto__', 'constructor', 'prototype'].includes(k)) fail('UNSAFE_JSON_KEY');
        return JSON.stringify(scalarString(k)) + ':' + encode(v[k], depth + 1);
      }).join(',') + '}';
    }
    ancestors.delete(v);
    return out;
  };
  return encode(value);
}

/** Parse UTF-8 JSON while rejecting duplicate keys, including escaped equivalents. */
export function parseCatalogJson(bytes) {
  let source, result;
  try { source = decoder.decode(bytesOf(bytes, MAX_JSON_BYTES)); result = JSON.parse(source); }
  catch (e) { if (e instanceof CatalogRegistrationError) throw e; fail('INVALID_JSON'); }
  let i = 0;
  const ws = () => { while (/\s/u.test(source[i] || '') && i < source.length) i++; };
  const string = () => {
    const start = i++;
    while (i < source.length) {
      const c = source[i++];
      if (c === '\\') i++;
      else if (c === '"') return JSON.parse(source.slice(start, i));
    }
    fail('INVALID_JSON');
  };
  const scan = (depth = 0) => {
    if (depth > 40) fail('JSON_TOO_DEEP');
    ws();
    if (source[i] === '"') { scalarString(string()); return; }
    if (source[i] === '{') {
      i++; ws(); const seen = new Set();
      if (source[i] === '}') { i++; return; }
      for (;;) {
        const key = string();
        if (seen.has(key)) fail('DUPLICATE_JSON_KEY');
        if (['__proto__', 'constructor', 'prototype'].includes(key)) fail('UNSAFE_JSON_KEY');
        seen.add(key); ws(); i++; scan(depth + 1); ws();
        if (source[i++] === '}') return;
        ws();
      }
    }
    if (source[i] === '[') {
      i++; ws(); if (source[i] === ']') { i++; return; }
      for (;;) { scan(depth + 1); ws(); if (source[i++] === ']') return; }
    }
    while (i < source.length && !/[\s,\]}]/u.test(source[i])) i++;
  };
  scan();
  // Includes number and Unicode validation throughout auxiliary manifest data.
  canonicalCatalogJson(result);
  return result;
}
function validateBinding(binding) {
  keys(binding, BINDING_KEYS);
  text(binding.owner_id, 256);
  if (/\s/u.test(binding.owner_id) || !validId(binding.report_id) || !validId(binding.catalog_id)
      || !Number.isSafeInteger(binding.catalog_version) || binding.catalog_version < 1
      || !/^personal:[1-9]\d{0,15}$/.test(binding.personal_scope)
      || !Number.isSafeInteger(Number(binding.personal_scope.slice(9)))) fail('INVALID_BINDING');
  return { ...binding };
}
function sourceURL(value, allowedHosts) {
  text(value, 2000);
  let url;
  try { url = new URL(value); } catch { fail('INVALID_SOURCE_URL'); }
  if (url.protocol !== 'https:' || !allowedHosts.has(url.hostname) || url.username || url.password
      || url.port || url.search || url.hash || /[\\\s]/u.test(value) || value !== url.href
      || !/^[a-z0-9.-]+$/.test(url.hostname) || /^\d+(?:\.\d+)*$/.test(url.hostname)
      || /(?:^|\.)(?:localhost|local|internal)$/.test(url.hostname)) fail('INVALID_SOURCE_URL');
}
function doi(value) {
  text(value, 300, true);
  if (!value) return;
  let raw = value.replace(/^doi:\s*/i, '');
  if (/^https?:\/\//i.test(raw)) {
    let u; try { u = new URL(raw); } catch { fail('INVALID_DOI'); }
    if (!['doi.org', 'dx.doi.org'].includes(u.hostname) || u.username || u.password || u.port || u.search || u.hash) fail('INVALID_DOI');
    try { raw = decodeURIComponent(u.pathname.slice(1)); } catch { fail('INVALID_DOI'); }
  }
  if (!/^10\.\d{4,9}\/[^\s]+$/iu.test(raw) || /[\u0000-\u001f\u007f]/u.test(raw)) fail('INVALID_DOI');
}
function arxiv(value) {
  text(value, 200, true);
  if (!value) return;
  let raw = value.replace(/^arxiv:\s*/i, '');
  if (/^https?:\/\//i.test(raw)) {
    let u; try { u = new URL(raw); } catch { fail('INVALID_ARXIV'); }
    if (!['arxiv.org', 'www.arxiv.org'].includes(u.hostname) || u.username || u.password || u.port || u.search || u.hash
        || !/^\/(?:abs|pdf)\//.test(u.pathname)) fail('INVALID_ARXIV');
    raw = u.pathname.replace(/^\/(?:abs|pdf)\//, '').replace(/\.pdf$/i, '');
  }
  raw = raw.replace(/v[1-9]\d*$/i, '').toLowerCase();
  if (!/^(?:\d{4}\.\d{4,5}|[a-z][a-z.\-]*\/\d{7})$/.test(raw)) fail('INVALID_ARXIV');
}
function paper(row, source, hosts, maxPdfBytes) {
  keys(row, PAPER_KEYS);
  text(row.paper_id, 300);
  if (!PAPER_ID.test(row.paper_id) || /^(?:https?|ftp|file|data|javascript|ws|wss):/i.test(row.paper_id)
      || row.paper_id.split('/').some(p => ['.', '..'].includes(p))) fail('INVALID_PAPER_ID');
  for (const field of BOUND_FIELDS) if (row[field] !== source[field]) fail('REPORT_CATALOG_MISMATCH');
  text(row.title); sourceURL(row.source_url, hosts);
  for (const field of BOUND_FIELDS.slice(2)) digest(row[field]);
  doi(row.doi); arxiv(row.arxiv);
  if (!Array.isArray(row.authors) || row.authors.length > 200) fail('INVALID_AUTHORS');
  for (const author of row.authors) text(author, 200);
  if (typeof row.year !== 'string' || (row.year && !/^\d{4}$/.test(row.year))) fail('INVALID_YEAR');
  text(row.venue, 1000, true);
  if (!row.doi && !row.arxiv && (!row.authors.length || !row.year || !row.venue)) fail('INSUFFICIENT_STABLE_IDENTITY');
  if (!['published_verified', 'preprint', 'unverified'].includes(row.publication)
      || !['journalArticle', 'conferencePaper', 'preprint', 'document'].includes(row.item_type)
      || (row.publication !== 'published_verified' && ['journalArticle', 'conferencePaper'].includes(row.item_type))
      || (row.publication === 'preprint' && row.item_type !== 'preprint')) fail('INVALID_PUBLICATION');
  if (row.publication === 'published_verified' || row.publication_evidence_sha256) digest(row.publication_evidence_sha256);
  else if (row.publication_evidence_sha256 !== '') fail('INVALID_PUBLICATION_EVIDENCE');
  if (!Array.isArray(row.topics) || !row.topics.length || row.topics.some(t => !TOPICS.has(t))
      || new Set(row.topics).size !== row.topics.length) fail('INVALID_TOPICS');
  if (row.pdf !== null) {
    keys(row.pdf, ['blob_id', 'sha256', 'size']);
    digest(row.pdf.sha256);
    if (row.pdf.blob_id !== row.pdf.sha256 || row.pdf.sha256 !== row.source_pdf_sha256
        || !Number.isSafeInteger(row.pdf.size) || row.pdf.size < 8 || row.pdf.size > maxPdfBytes) fail('INVALID_PDF_REFERENCE');
  }
}
function freeze(value) {
  if (value && typeof value === 'object') { for (const v of Object.values(value)) freeze(v); Object.freeze(value); }
  return value;
}
/** Recompute this exact payload in the executor before trusting stored row metadata. */
export function registrationBinding(row) {
  return { schema: 'daily-agent-zotero-registration-v1', owner_id: row.owner_id, report_id: row.report_id,
    catalog_id: row.catalog_id, catalog_version: row.catalog_version, personal_scope: row.personal_scope,
    report_sha256: row.report_sha256, report_day: row.report_day,
    source_catalog_sha256: row.source_catalog_sha256, content_sha256: row.content_sha256 };
}

/** Read-only/offline preparation. It validates ALL source bytes before a prepared
 * capability can be used to register. Bytes stay in a private WeakMap, never JSON. */
export async function prepareTrustedCatalog({ binding, trustedPins, reportBytes, htmlBytes, catalogBytes,
  readPdfBytes, maxPdfBytes = DEFAULT_MAX_PDF_BYTES, includeProtocolMd5 = true } = {}) {
  if (typeof includeProtocolMd5 !== 'boolean') fail('INVALID_HASH_MODE');
  binding = validateBinding(binding);
  keys(trustedPins, [...BINDING_KEYS, 'report_sha256', 'catalog_sha256', 'html_sha256', 'report_edition_id', 'allowed_source_hosts']);
  for (const key of BINDING_KEYS) if (trustedPins[key] !== binding[key]) fail('TRUSTED_BINDING_MISMATCH');
  for (const key of ['report_sha256', 'catalog_sha256', 'html_sha256']) digest(trustedPins[key]);
  if (!validId(trustedPins.report_edition_id)) fail('INVALID_EDITION_ID');
  if (!Array.isArray(trustedPins.allowed_source_hosts) || !trustedPins.allowed_source_hosts.length
      || trustedPins.allowed_source_hosts.length > 100 || trustedPins.allowed_source_hosts.some(h =>
        typeof h !== 'string' || !/^[a-z0-9]+(?:[.-][a-z0-9]+)+$/.test(h))) fail('INVALID_SOURCE_ALLOWLIST');
  const pins = JSON.parse(canonicalCatalogJson(trustedPins));
  const hosts = new Set(pins.allowed_source_hosts);
  const hash = createHashBytes({ maxBytes: maxPdfBytes });
  const reportRaw = bytesOf(reportBytes, MAX_JSON_BYTES), catalogRaw = bytesOf(catalogBytes, MAX_JSON_BYTES);
  const htmlRaw = bytesOf(htmlBytes, MAX_HTML_BYTES);
  if (await sha256Bytes(reportRaw) !== pins.report_sha256 || await sha256Bytes(catalogRaw) !== pins.catalog_sha256
      || await sha256Bytes(htmlRaw) !== pins.html_sha256) fail('PINNED_SOURCE_HASH_MISMATCH');
  const report = parseCatalogJson(reportRaw), catalog = parseCatalogJson(catalogRaw);
  keys(report, ['schema', 'edition_id', 'date', 'html_sha256', 'html_bytes', 'qualified_papers', 'paper_records'], REPORT_KEYS);
  keys(catalog, ['schema', 'report_sha256', 'papers']);
  if (report.schema !== 'paper-first-corrected-edition-v1' || catalog.schema !== 'daily-agent-zotero-catalog-v1') fail('UNSUPPORTED_SCHEMA');
  if (report.edition_id !== pins.report_edition_id || catalog.report_sha256 !== pins.report_sha256
      || report.html_sha256 !== pins.html_sha256 || report.html_bytes !== htmlRaw.byteLength) fail('REPORT_SEAL_MISMATCH');
  if (typeof report.date !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(report.date)
      || report.date.startsWith('0000-') || !Number.isFinite(Date.parse(report.date)) || new Date(report.date).toISOString().slice(0, 10) !== report.date) fail('INVALID_REPORT_DAY');
  if (!Array.isArray(report.paper_records) || !report.paper_records.length || report.paper_records.length > 1000
      || report.qualified_papers !== report.paper_records.length || !Array.isArray(catalog.papers)
      || catalog.papers.length !== report.paper_records.length) fail('INVALID_REPORT_ALLOWLIST');
  const sourceRows = new Map();
  for (const r of report.paper_records) {
    keys(r, ['key', ...BOUND_FIELDS], REPORT_PAPER_KEYS);
    text(r.key, 300);
    if (sourceRows.has(r.key)) fail('DUPLICATE_REPORT_PAPER');
    sourceRows.set(r.key, r);
  }
  const seen = new Set(), pdfs = new Map();
  let total = 0;
  for (const row of catalog.papers) {
    if (!record(row) || !sourceRows.has(row.paper_id) || seen.has(row.paper_id)) fail('CATALOG_ALLOWLIST_MISMATCH');
    seen.add(row.paper_id);
    paper(row, sourceRows.get(row.paper_id), hosts, maxPdfBytes);
    if (row.pdf) {
      if (pdfs.has(row.pdf.blob_id)) {
        if (pdfs.get(row.pdf.blob_id).size !== row.pdf.size) fail('PDF_SIZE_MISMATCH');
        continue;
      }
      total += row.pdf.size;
      if (total > MAX_TOTAL_PDF_BYTES) fail('PDF_PACKAGE_TOO_LARGE');
      if (typeof readPdfBytes !== 'function') fail('PDF_READER_REQUIRED');
      const content = bytesOf(await readPdfBytes(row.pdf.blob_id), maxPdfBytes, 'INVALID_PDF_BYTES');
      if (content.byteLength !== row.pdf.size || content[0] !== 37 || content[1] !== 80
          || content[2] !== 68 || content[3] !== 70 || content[4] !== 45) fail('PDF_SIZE_OR_SIGNATURE_MISMATCH');
      // MD5 is a Zotero upload-protocol field, never a catalog integrity pin.
      // Bundled registration uses native SHA-256; actual uploads still hash both.
      const hashes = includeProtocolMd5 ? await hash(content) : { sha256: await sha256Bytes(content) };
      if (hashes.sha256 !== row.pdf.sha256) fail('PDF_HASH_MISMATCH');
      pdfs.set(row.pdf.blob_id, { content, size: content.byteLength, ...hashes });
    }
  }
  const catalogJson = canonicalCatalogJson(catalog);
  const catalogRow = { ...binding, content_sha256: await sha256Bytes(encoder.encode(catalogJson)), enabled: 0,
    catalog_blob_json: catalogJson, report_sha256: pins.report_sha256, report_day: report.date,
    source_catalog_sha256: pins.catalog_sha256 };
  catalogRow.registration_sha256 = await sha256Bytes(encoder.encode(canonicalCatalogJson(registrationBinding(catalogRow))));
  const paperRows = await Promise.all(catalog.papers.map(async p => {
    const metadata_json = canonicalCatalogJson(p);
    return { owner_id: binding.owner_id, report_id: binding.report_id, catalog_id: binding.catalog_id,
      paper_id: p.paper_id, display_title: p.title, metadata_json,
      row_sha256: await sha256Bytes(encoder.encode(metadata_json)) };
  }));
  const result = freeze({ schema: 'daily-agent-zotero-prepared-v1', catalogRow, paperRows,
    blobs: [...pdfs.entries()].map(([key, p]) => ({ key, size: p.size, sha256: p.sha256, ...(p.md5 ? { md5: p.md5 } : {}) })),
    sourcePins: pins });
  preparedBytes.set(result, pdfs);
  return result;
}

export const CATALOG_SQL = Object.freeze({
  catalog: 'SELECT * FROM zotero_report_catalogs WHERE owner_id = ? AND report_id = ? AND catalog_id = ?',
  papers: 'SELECT * FROM zotero_catalog_papers WHERE owner_id = ? AND report_id = ? AND catalog_id = ? ORDER BY paper_id',
  insertCatalog: `INSERT INTO zotero_report_catalogs
    (owner_id, report_id, catalog_id, catalog_version, personal_scope, content_sha256, enabled, created_at,
     catalog_blob_json, report_sha256, report_day, source_catalog_sha256, registration_sha256)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
  insertPaper: `INSERT INTO zotero_catalog_papers
    (owner_id, report_id, catalog_id, paper_id, display_title, metadata_json) VALUES (?, ?, ?, ?, ?, ?)`,
});
const statement = (db, sql, ...values) => db.prepare(sql).bind(...values);
function sameRow(actual, expected, fields = Object.keys(expected)) {
  return actual && fields.every(k => actual[k] === expected[k]);
}
async function r2Bytes(object, max) {
  if (!object) return null;
  if (!Number.isSafeInteger(object.size) || object.size < 0 || object.size > max
      || typeof object.body?.getReader !== 'function') fail('INVALID_EXISTING_BLOB');
  const reader = object.body.getReader(), chunks = [];
  let length = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      if (!(value instanceof Uint8Array) || (length += value.byteLength) > max) {
        await reader.cancel(); fail('INVALID_EXISTING_BLOB');
      }
      chunks.push(value);
    }
  } finally { reader.releaseLock(); }
  if (length !== object.size) fail('INVALID_EXISTING_BLOB');
  const result = new Uint8Array(length); let offset = 0;
  for (const c of chunks) { result.set(c, offset); offset += c.byteLength; }
  return result;
}
/** Optional explicit adapter around an injected PRIVATE R2 binding. This module
 * never acquires bindings, URLs, credentials, public routes, or network access. */
export async function privateBlobKey(ownerId, sha256) {
  text(ownerId, 256); digest(sha256);
  if (/\s/u.test(ownerId)) fail('INVALID_BLOB_OWNER');
  return `zotero-pdfs/${await sha256Bytes(encoder.encode(ownerId))}/${sha256}`;
}
export function createPrivateR2BlobStore(bucket, { ownerId, maxPdfBytes = DEFAULT_MAX_PDF_BYTES } = {}) {
  text(ownerId, 256);
  if (/\s/u.test(ownerId)) fail('INVALID_BLOB_OWNER');
  if (typeof bucket?.get !== 'function' || typeof bucket?.put !== 'function') fail('PRIVATE_BLOB_BINDING_REQUIRED');
  if (!Number.isSafeInteger(maxPdfBytes) || maxPdfBytes < 8 || maxPdfBytes > HARD_MAX_PDF_BYTES) fail('INVALID_PDF_LIMIT');
  const keyOf = async context => {
    keys(context, ['ownerId', 'reportId', 'catalogId', 'paperId', 'sha256', 'size']);
    if (context.ownerId !== ownerId || !validId(context.reportId) || !validId(context.catalogId)
        || typeof context.paperId !== 'string' || !PAPER_ID.test(context.paperId) || !Number.isSafeInteger(context.size)
        || context.size < 8 || context.size > maxPdfBytes) fail('INVALID_BLOB_CONTEXT');
    return privateBlobKey(ownerId, context.sha256);
  };
  return Object.freeze({
    ownerId,
    async readBlob(context) { return r2Bytes(await bucket.get(await keyOf(context)), maxPdfBytes); },
    async putBlobIfAbsent(context, bytes) {
      const key = await keyOf(context);
      const copy = bytesOf(bytes, maxPdfBytes);
      if (copy.byteLength !== context.size || await sha256Bytes(copy) !== context.sha256) fail('PDF_HASH_OR_SIZE_MISMATCH');
      // Conditional write: existing immutable objects are NEVER overwritten.
      const result = await bucket.put(key, copy, { onlyIf: { etagDoesNotMatch: '*' },
        sha256: context.sha256, httpMetadata: { contentType: 'application/pdf' } });
      return result !== null;
    },
  });
}

/** Explicit server operation only. Default artifacts remain disabled. A branded
 * prepared object, exact expected binding, atomic D1 batch, and conditional blob
 * writes are required. No INSERT OR REPLACE/UPDATE/DELETE exists in this path. */
export async function registerTrustedCatalog({ prepared, expectedBinding, db, blobs, now = () => new Date().toISOString() } = {}) {
  if (!preparedBytes.has(prepared)) fail('PREPARATION_REQUIRED');
  expectedBinding = validateBinding(expectedBinding);
  const wanted = prepared.catalogRow;
  for (const k of BINDING_KEYS) if (wanted[k] !== expectedBinding[k]) fail('REGISTRATION_BINDING_MISMATCH');
  if (typeof db?.prepare !== 'function' || typeof db?.batch !== 'function') fail('DATABASE_REQUIRED');
  if (prepared.blobs.length && (typeof blobs?.readBlob !== 'function' || typeof blobs?.putBlobIfAbsent !== 'function' || blobs.ownerId !== wanted.owner_id)) fail('PRIVATE_BLOB_STORE_REQUIRED');
  const values = BINDING_KEYS.slice(0, 3).map(k => wanted[k]);
  const existing = await statement(db, CATALOG_SQL.catalog, ...values).first();
  const existingPapers = await statement(db, CATALOG_SQL.papers, ...values).all();
  if (!Array.isArray(existingPapers?.results)) fail('DATABASE_READ_FAILED');
  const checkExisting = (row, rows) => {
    // Activation may independently change enabled; never undo it during a retry.
    if (!sameRow(row, wanted, Object.keys(wanted).filter(k => k !== 'enabled')) || rows.length !== prepared.paperRows.length) fail('IMMUTABLE_CATALOG_CONFLICT');
    const index = new Map(rows.map(p => [p.paper_id, p]));
    for (const p of prepared.paperRows) if (!sameRow(index.get(p.paper_id), p, Object.keys(p).filter(k => k !== 'row_sha256'))) fail('IMMUTABLE_PAPER_CONFLICT');
  };
  if (existing) checkExisting(existing, existingPapers.results);
  else if (existingPapers.results.length) fail('ORPHAN_PAPER_ROWS');
  const contextOf = blob => ({ ownerId: wanted.owner_id, reportId: wanted.report_id, catalogId: wanted.catalog_id,
    paperId: prepared.paperRows.find(p => JSON.parse(p.metadata_json).pdf?.sha256 === blob.sha256).paper_id,
    sha256: blob.sha256, size: blob.size });
  const missing = [];
  // Complete destination preflight BEFORE any blob or DB mutation.
  for (const blob of prepared.blobs) {
    const prior = await blobs.readBlob(contextOf(blob));
    if (prior === null) { missing.push(blob); continue; }
    const content = bytesOf(prior, HARD_MAX_PDF_BYTES, 'INVALID_EXISTING_BLOB');
    if (content.byteLength !== blob.size || await sha256Bytes(content) !== blob.sha256) fail('IMMUTABLE_BLOB_CONFLICT');
  }
  if (existing && missing.length) fail('IMMUTABLE_CATALOG_BLOB_MISSING');
  if (existing) return Object.freeze({ created: false, catalog_sha256: wanted.content_sha256, papers: prepared.paperRows.length, blobs_written: 0 });
  const createdAt = now();
  if (typeof createdAt !== 'string' || !Number.isFinite(Date.parse(createdAt)) || new Date(createdAt).toISOString() !== createdAt) fail('INVALID_CREATED_AT');
  let written = 0;
  for (const blob of missing) {
    const content = preparedBytes.get(prepared).get(blob.key).content;
    if (await blobs.putBlobIfAbsent(contextOf(blob), new Uint8Array(content))) written++;
    // Verify races and adapter/storage behavior before publishing the catalog.
    const stored = bytesOf(await blobs.readBlob(contextOf(blob)), HARD_MAX_PDF_BYTES, 'BLOB_WRITE_UNVERIFIED');
    if (stored.byteLength !== blob.size || await sha256Bytes(stored) !== blob.sha256) fail('BLOB_WRITE_UNVERIFIED');
  }
  const batch = [statement(db, CATALOG_SQL.insertCatalog,
    wanted.owner_id, wanted.report_id, wanted.catalog_id, wanted.catalog_version, wanted.personal_scope,
    wanted.content_sha256, 0, createdAt, wanted.catalog_blob_json, wanted.report_sha256,
    wanted.report_day, wanted.source_catalog_sha256, wanted.registration_sha256),
  ...prepared.paperRows.map(p => statement(db, CATALOG_SQL.insertPaper,
    p.owner_id, p.report_id, p.catalog_id, p.paper_id, p.display_title, p.metadata_json))];
  try {
    const results = await db.batch(batch);
    if (!Array.isArray(results) || results.length !== batch.length || results.some(r => r?.success !== true)) fail('DATABASE_WRITE_UNVERIFIED');
  } catch {
    // A concurrent identical registration is idempotent; differing content still
    // fails closed. R2 and D1 have no cross-service transaction: orphaned private
    // content-addressed blobs can remain after a failed batch, never live rows.
    const concurrent = await statement(db, CATALOG_SQL.catalog, ...values).first();
    const concurrentPapers = await statement(db, CATALOG_SQL.papers, ...values).all();
    if (!concurrent || !Array.isArray(concurrentPapers?.results)) fail('DATABASE_WRITE_FAILED');
    checkExisting(concurrent, concurrentPapers.results);
    return Object.freeze({ created: false, catalog_sha256: wanted.content_sha256, papers: prepared.paperRows.length, blobs_written: written });
  }
  const readback = await statement(db, CATALOG_SQL.catalog, ...values).first();
  const rows = await statement(db, CATALOG_SQL.papers, ...values).all();
  if (!Array.isArray(rows?.results)) fail('DATABASE_WRITE_UNVERIFIED');
  checkExisting(readback, rows.results);
  return Object.freeze({ created: true, catalog_sha256: wanted.content_sha256, papers: prepared.paperRows.length, blobs_written: written });
}

/** Shared read path for authenticated application routes and the job executor.
 * Caller authentication remains the application's responsibility. The helper
 * checks exact stored bytes/rows and returns only this owner's requested catalog. */
export async function readRegisteredCatalog({ db, ownerId, reportId, catalogId, personalScope,
  requireEnabled = true } = {}) {
  if (typeof db?.prepare !== 'function') fail('DATABASE_REQUIRED');
  text(ownerId, 256);
  if (/\s/u.test(ownerId) || !validId(reportId) || !validId(catalogId)) fail('INVALID_BINDING');
  const row = await statement(db, CATALOG_SQL.catalog, ownerId, reportId, catalogId).first();
  if (!row || (requireEnabled && row.enabled !== 1)) return null;
  const binding = validateBinding(Object.fromEntries(BINDING_KEYS.map(k => [k, row[k]])));
  if (binding.owner_id !== ownerId || binding.report_id !== reportId || binding.catalog_id !== catalogId
      || (personalScope !== undefined && binding.personal_scope !== personalScope)) fail('REGISTRATION_BINDING_MISMATCH');
  for (const key of ['content_sha256', 'report_sha256', 'source_catalog_sha256', 'registration_sha256']) digest(row[key]);
  if (typeof row.report_day !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(row.report_day)
      || row.report_day.startsWith('0000-') || !Number.isFinite(Date.parse(row.report_day)) || new Date(row.report_day).toISOString().slice(0, 10) !== row.report_day) fail('INVALID_REPORT_DAY');
  if (typeof row.catalog_blob_json !== 'string') fail('INVALID_REGISTERED_CATALOG');
  const raw = encoder.encode(row.catalog_blob_json);
  const catalog = parseCatalogJson(raw);
  keys(catalog, ['schema', 'report_sha256', 'papers']);
  if (catalog.schema !== 'daily-agent-zotero-catalog-v1' || catalog.report_sha256 !== row.report_sha256
      || canonicalCatalogJson(catalog) !== row.catalog_blob_json || await sha256Bytes(raw) !== row.content_sha256
      || await sha256Bytes(encoder.encode(canonicalCatalogJson(registrationBinding(row)))) !== row.registration_sha256
      || !Array.isArray(catalog.papers) || !catalog.papers.length || catalog.papers.length > 1000) fail('INVALID_REGISTERED_CATALOG');
  const result = await statement(db, CATALOG_SQL.papers, ownerId, reportId, catalogId).all();
  if (!Array.isArray(result?.results) || result.results.length !== catalog.papers.length) fail('IMMUTABLE_PAPER_CONFLICT');
  const index = new Map(result.results.map(p => [p.paper_id, p]));
  const seen = new Set();
  for (const p of catalog.papers) {
    if (!record(p) || seen.has(p.paper_id)) fail('CATALOG_ALLOWLIST_MISMATCH');
    // The pinned source URL host is already registered; structural checking here
    // prevents corrupted rows from introducing unsafe metadata/fetch targets.
    let host; try { host = new URL(p.source_url).hostname; } catch { fail('INVALID_SOURCE_URL'); }
    paper(p, p, new Set([host]), HARD_MAX_PDF_BYTES); seen.add(p.paper_id);
    const stored = index.get(p.paper_id);
    if (!stored || stored.owner_id !== ownerId || stored.report_id !== reportId || stored.catalog_id !== catalogId
        || stored.display_title !== p.title || stored.metadata_json !== canonicalCatalogJson(p)) fail('IMMUTABLE_PAPER_CONFLICT');
  }
  return freeze({ row: { ...row }, papers: catalog.papers });
}
