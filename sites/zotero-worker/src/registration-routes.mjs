/** Explicit owner-gateway registration routes. No authentication tokens, grants,
 * credentials, public catalog upsert, URL fetch, or Zotero calls are created here. */
import { sha256Bytes, DEFAULT_MAX_PDF_BYTES } from './hash-bytes.mjs';
import { parseCatalogJson, prepareTrustedCatalog, registerTrustedCatalog, readRegisteredCatalog,
  createPrivateR2BlobStore, CatalogRegistrationError } from './catalog-registration.mjs';
const encoder = new TextEncoder();
const BINDING = ['owner_id', 'report_id', 'catalog_id', 'catalog_version', 'personal_scope'];
const PIN_KEYS = [...BINDING, 'report_sha256', 'catalog_sha256', 'html_sha256', 'report_edition_id', 'allowed_source_hosts'];
const ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const SHA = /^[a-f0-9]{64}$/;
const PAPER_ID = /^[A-Za-z0-9][A-Za-z0-9._:()/;-]{0,299}$/;
const SCHEMA = 'daily-agent-zotero-registration-config-v1';
export const REGISTRATION_LIMITS = Object.freeze({ config: 5000, report: 1024 * 1024,
  catalog: 1024 * 1024, html: 4 * 1024 * 1024, finalBody: 9 * 1024 * 1024,
  pdf: DEFAULT_MAX_PDF_BYTES, totalPdf: 24 * 1024 * 1024, totalSources: 4 * 1024 * 1024 });
const exact = (v, fields) => v !== null && typeof v === 'object' && !Array.isArray(v)
  && Object.keys(v).length === fields.length && fields.every(k => Object.hasOwn(v, k));
const validId = v => typeof v === 'string' && ID.test(v);
const fail = code => { throw new CatalogRegistrationError(code); };
const sql = (db, statement, ...args) => db.prepare(statement).bind(...args);

/** The setting is ordinary private operator configuration, NOT a bearer secret.
 * It only authorizes these pinned artifacts for the existing authenticated owner. */
export function registrationConfig(env, auth) {
  try {
    if (typeof env.ZOTERO_CATALOG_REGISTRATION_JSON !== 'string'
        || encoder.encode(env.ZOTERO_CATALOG_REGISTRATION_JSON).length > REGISTRATION_LIMITS.config
        || env.SITES_GATEWAY_IDENTITY_TRUSTED !== 'platform-managed' || auth.owner !== env.ZOTERO_OWNER_ID
        || typeof auth.owner !== 'string' || !auth.owner || auth.owner.length > 256 || /\s/u.test(auth.owner)
        || auth.origin !== env.ZOTERO_SITE_ORIGIN || !env.DB || auth.db !== env.DB
        || typeof env.BUCKET?.get !== 'function' || typeof env.BUCKET?.put !== 'function'
        || typeof env.ZOTERO_VERIFIED_PERSONAL_LIBRARY_ID !== 'string'
        || !/^[1-9]\d{0,15}$/.test(env.ZOTERO_VERIFIED_PERSONAL_LIBRARY_ID)
        || !Number.isSafeInteger(Number(env.ZOTERO_VERIFIED_PERSONAL_LIBRARY_ID))) return null;
    const raw = env.ZOTERO_CATALOG_REGISTRATION_JSON;
    const value = parseCatalogJson(encoder.encode(raw));
    if (!exact(value, ['schema', 'approved', 'binding', 'trustedPins', 'pdfs', 'filenames', 'enable_after_registration'])
        || value.schema !== SCHEMA || value.approved !== true || typeof value.enable_after_registration !== 'boolean'
        || !exact(value.binding, BINDING) || !exact(value.trustedPins, PIN_KEYS)
        || value.binding.owner_id !== auth.owner || !validId(value.binding.report_id) || !validId(value.binding.catalog_id)
        || !Number.isSafeInteger(value.binding.catalog_version) || value.binding.catalog_version < 1
        || value.binding.personal_scope !== `personal:${env.ZOTERO_VERIFIED_PERSONAL_LIBRARY_ID}`
        || BINDING.some(k => value.binding[k] !== value.trustedPins[k])
        || ['report_sha256', 'catalog_sha256', 'html_sha256'].some(k => typeof value.trustedPins[k] !== 'string' || !SHA.test(value.trustedPins[k]))
        || !validId(value.trustedPins.report_edition_id)
        || !Array.isArray(value.trustedPins.allowed_source_hosts) || !value.trustedPins.allowed_source_hosts.length
        || value.trustedPins.allowed_source_hosts.length > 100
        || value.trustedPins.allowed_source_hosts.some(h => typeof h !== 'string' || !/^[a-z0-9]+(?:[.-][a-z0-9]+)+$/.test(h))
        || !exact(value.filenames, ['report', 'html', 'catalog'])
        || Object.values(value.filenames).some(n => typeof n !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(n))
        || new Set(Object.values(value.filenames)).size !== 3
        || !Array.isArray(value.pdfs) || value.pdfs.length > 100) return null;
    const seen = new Set(), paperIds = new Set(); let total = 0;
    for (const p of value.pdfs) {
      if (!exact(p, ['sha256', 'size', 'paper_id']) || typeof p.sha256 !== 'string' || !SHA.test(p.sha256)
          || !Number.isSafeInteger(p.size) || p.size < 8 || p.size > REGISTRATION_LIMITS.pdf
          || typeof p.paper_id !== 'string' || !PAPER_ID.test(p.paper_id) || seen.has(p.sha256) || paperIds.has(p.paper_id)) return null;
      seen.add(p.sha256); paperIds.add(p.paper_id); total += p.size;
    }
    if (total > REGISTRATION_LIMITS.totalPdf) return null;
    return { raw, ...value };
  } catch { return null; }
}
function currentConfig(env, auth, previous) {
  const current = registrationConfig(env, auth);
  if (!current || current.raw !== previous.raw) fail('REGISTRATION_CONFIGURATION_CHANGED');
  return current;
}
function context(auth, cfg, pdf) {
  return { ownerId: auth.owner, reportId: cfg.binding.report_id, catalogId: cfg.binding.catalog_id,
    paperId: pdf.paper_id, sha256: pdf.sha256, size: pdf.size };
}
function storeFor(env, auth, cfg) {
  return createPrivateR2BlobStore({
    get: key => env.BUCKET.get(key),
    put: (key, bytes, options) => { currentConfig(env, auth, cfg); return env.BUCKET.put(key, bytes, options); },
  }, { ownerId: auth.owner });
}
async function registered(auth, cfg) {
  const result = await readRegisteredCatalog({ db: auth.db, ownerId: auth.owner,
    reportId: cfg.binding.report_id, catalogId: cfg.binding.catalog_id,
    personalScope: cfg.binding.personal_scope, requireEnabled: false });
  if (result && (result.row.catalog_version !== cfg.binding.catalog_version
      || result.row.report_sha256 !== cfg.trustedPins.report_sha256
      || result.row.source_catalog_sha256 !== cfg.trustedPins.catalog_sha256)) fail('IMMUTABLE_CATALOG_CONFLICT');
  return result;
}
async function pdfPresent(store, auth, cfg, pdf) {
  const bytes = await store.readBlob(context(auth, cfg, pdf));
  if (bytes === null) return false;
  if (bytes.length !== pdf.size || await sha256Bytes(bytes) !== pdf.sha256) fail('IMMUTABLE_BLOB_CONFLICT');
  return true;
}
export async function boundedBody(request, maximum, expected) {
  const length = request.headers.get('Content-Length');
  if (length !== null && (!/^(?:0|[1-9]\d*)$/.test(length) || !Number.isSafeInteger(Number(length))
      || Number(length) > maximum || (expected !== undefined && Number(length) !== expected))) fail('REGISTRATION_BODY_SIZE_MISMATCH');
  if (!request.body?.getReader) fail('REGISTRATION_BODY_REQUIRED');
  const reader = request.body.getReader(), chunks = []; let size = 0, timer;
  const deadline = new Promise((_, reject) => { timer = setTimeout(() => {
    reject(new CatalogRegistrationError('REGISTRATION_BODY_TIMEOUT')); void reader.cancel().catch(() => {});
  }, 10_000); });
  try {
    for (;;) {
      const { done, value } = await Promise.race([reader.read(), deadline]); if (done) break;
      if (!(value instanceof Uint8Array) || (size += value.length) > maximum) { await reader.cancel(); fail('REGISTRATION_BODY_TOO_LARGE'); }
      chunks.push(value);
    }
    if ((length !== null && Number(length) !== size) || (expected !== undefined && size !== expected)) fail('REGISTRATION_BODY_SIZE_MISMATCH');
    const result = new Uint8Array(size); let offset = 0;
    for (const chunk of chunks) { result.set(chunk, offset); offset += chunk.length; }
    return result;
  } finally { clearTimeout(timer); reader.releaseLock(); }
}
function base64Bytes(value, maximum) {
  if (typeof value !== 'string' || value.length > Math.ceil(maximum / 3) * 4 || !/^[A-Za-z0-9+/]*={0,2}$/.test(value)) fail('INVALID_REGISTRATION_BASE64');
  try {
    const binary = atob(value);
    if (!binary.length || binary.length > maximum || btoa(binary) !== value) fail('INVALID_REGISTRATION_BASE64');
    return Uint8Array.from(binary, c => c.charCodeAt(0));
  } catch { fail('INVALID_REGISTRATION_BASE64'); }
}
export function finalSources(bytes) {
  // Final request's fields are base64 ASCII. parseCatalogJson has a stricter 4MiB
  // artifact cap, so use a duplicate-key-safe envelope check with canonical field
  // count and a small structural tokenizer for this flat, four-string shape.
  let body;
  try { body = JSON.parse(new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(bytes)); }
  catch { fail('INVALID_REGISTRATION_BODY'); }
  if (!exact(body, ['action', 'report_b64', 'html_b64', 'catalog_b64']) || body.action !== 'register_catalog'
      || Object.values(body).some(v => typeof v !== 'string')) fail('INVALID_REGISTRATION_BODY');
  // Duplicate keys cannot hide a different instruction or source. Base64 values
  // never contain quotes/backslashes; action and field spelling must be literal.
  const source = new TextDecoder().decode(bytes);
  const envelope = /^\s*\{\s*"([a-z0-9_]+)"\s*:\s*"([A-Za-z0-9+/=_]*)"(?:\s*,\s*"([a-z0-9_]+)"\s*:\s*"([A-Za-z0-9+/=_]*)"){3}\s*\}\s*$/;
  if (!envelope.test(source) || [...source.matchAll(/"([a-z0-9_]+)"\s*:/g)].length !== 4
      || new Set([...source.matchAll(/"([a-z0-9_]+)"\s*:/g)].map(m => m[1])).size !== 4) fail('INVALID_REGISTRATION_BODY');
  const reportBytes = base64Bytes(body.report_b64, REGISTRATION_LIMITS.report);
  const htmlBytes = base64Bytes(body.html_b64, REGISTRATION_LIMITS.html);
  const catalogBytes = base64Bytes(body.catalog_b64, REGISTRATION_LIMITS.catalog);
  if (reportBytes.length + htmlBytes.length + catalogBytes.length > REGISTRATION_LIMITS.totalSources) fail('REGISTRATION_SOURCES_TOO_LARGE');
  return { reportBytes, htmlBytes, catalogBytes };
}
async function finalize(request, env, auth, cfg) {
  const { reportBytes, htmlBytes, catalogBytes } = finalSources(await boundedBody(request, REGISTRATION_LIMITS.finalBody));
  currentConfig(env, auth, cfg);
  const store = storeFor(env, auth, cfg), pdfs = new Map(cfg.pdfs.map(p => [p.sha256, p]));
  const prepared = await prepareTrustedCatalog({ binding: cfg.binding, trustedPins: cfg.trustedPins,
    reportBytes, htmlBytes, catalogBytes, readPdfBytes: async sha => {
      const pdf = pdfs.get(sha); if (!pdf) fail('PDF_NOT_IN_REGISTRATION_MANIFEST');
      return store.readBlob(context(auth, cfg, pdf));
    } });
  const actual = prepared.paperRows.map(p => JSON.parse(p.metadata_json)).filter(p => p.pdf);
  if (actual.length !== cfg.pdfs.length || actual.some(p => {
    const approved = pdfs.get(p.pdf.sha256);
    return !approved || approved.size !== p.pdf.size || approved.paper_id !== p.paper_id;
  })) fail('PDF_REGISTRATION_MANIFEST_MISMATCH');
  currentConfig(env, auth, cfg);
  const guardedDb = { prepare: query => auth.db.prepare(query), batch: statements => {
    currentConfig(env, auth, cfg); return auth.db.batch(statements);
  } };
  await registerTrustedCatalog({ prepared, expectedBinding: cfg.binding, db: guardedDb, blobs: store });
  currentConfig(env, auth, cfg);
  if (cfg.enable_after_registration) {
    const row = prepared.catalogRow;
    const result = await sql(auth.db, `UPDATE zotero_report_catalogs SET enabled=1
      WHERE owner_id=? AND report_id=? AND catalog_id=? AND catalog_version=? AND personal_scope=?
      AND source_catalog_sha256=? AND report_sha256=? AND content_sha256=? AND registration_sha256=? AND enabled=0`,
    auth.owner, row.report_id, row.catalog_id, row.catalog_version, row.personal_scope,
    row.source_catalog_sha256, row.report_sha256, row.content_sha256, row.registration_sha256).run();
    if (result.success !== true) fail('REGISTRATION_ENABLE_FAILED');
  }
  const verified = await registered(auth, cfg);
  if (!verified || (cfg.enable_after_registration && verified.row.enabled !== 1)) fail('REGISTRATION_READBACK_FAILED');
  return { registered: true, enabled: verified.row.enabled === 1, report_id: cfg.binding.report_id,
    catalog_id: cfg.binding.catalog_id, catalog_version: cfg.binding.catalog_version,
    paper_count: prepared.paperRows.length, pdf_count: prepared.blobs.length };
}

export async function handleRegistrationRoute(request, env, auth, helpers) {
  const { json, requireGet, requireAction } = helpers;
  const url = new URL(request.url);
  const root = '/api/zotero/catalog-registration';
  if (url.pathname !== root && url.pathname !== `${root}/finalize` && !url.pathname.startsWith(`${root}/pdfs/`)) return null;
  try {
    if (url.origin !== auth.origin || url.search) fail('INVALID_REGISTRATION_TARGET');
    if (url.pathname === root) requireGet(request);
    else requireAction(request, auth.origin, url.pathname.startsWith(`${root}/pdfs/`) ? 'application/pdf' : 'application/json');
    const cfg = registrationConfig(env, auth);
    if (!cfg) {
      if (url.pathname === root) return json({ enabled: false, registered: false, state: 'setup_required', pdfs: [] });
      fail('CATALOG_REGISTRATION_NOT_CONFIGURED');
    }
    const existing = await registered(auth, cfg);
    const store = storeFor(env, auth, cfg);
    if (url.pathname === root) {
      const pdfs = [];
      for (const p of cfg.pdfs) pdfs.push({ ...p, staged: await pdfPresent(store, auth, cfg, p) });
      currentConfig(env, auth, cfg);
      return json({ enabled: true, registered: Boolean(existing), state: existing ? 'registered' : 'ready',
        report_id: cfg.binding.report_id, catalog_id: cfg.binding.catalog_id, catalog_version: cfg.binding.catalog_version,
        catalog_enabled: existing?.row.enabled === 1, enable_after_registration: cfg.enable_after_registration,
        filenames: cfg.filenames, limits: REGISTRATION_LIMITS, source_pins: { report_sha256: cfg.trustedPins.report_sha256,
          html_sha256: cfg.trustedPins.html_sha256, catalog_sha256: cfg.trustedPins.catalog_sha256 }, pdfs });
    }
    if (url.pathname === `${root}/finalize`) return json(await finalize(request, env, auth, cfg));
    const sha256 = url.pathname.slice(`${root}/pdfs/`.length);
    if (!SHA.test(sha256)) fail('INVALID_REGISTRATION_PDF');
    const pdf = cfg.pdfs.find(p => p.sha256 === sha256);
    if (!pdf) fail('PDF_NOT_IN_REGISTRATION_MANIFEST');
    const bytes = await boundedBody(request, pdf.size, pdf.size);
    if (bytes[0] !== 37 || bytes[1] !== 80 || bytes[2] !== 68 || bytes[3] !== 70 || bytes[4] !== 45
        || await sha256Bytes(bytes) !== sha256) fail('REGISTRATION_PDF_HASH_MISMATCH');
    currentConfig(env, auth, cfg);
    const exists = await pdfPresent(store, auth, cfg, pdf);
    const created = exists ? false : await store.putBlobIfAbsent(context(auth, cfg, pdf), bytes);
    if (!await pdfPresent(store, auth, cfg, pdf)) fail('REGISTRATION_PDF_READBACK_FAILED');
    return json({ staged: true, sha256, size: pdf.size, created });
  } catch (error) {
    if (error instanceof CatalogRegistrationError) return json({ error: error.code.toLowerCase(), registered: false },
      ['REGISTRATION_BODY_TOO_LARGE', 'REGISTRATION_BODY_SIZE_MISMATCH'].includes(error.code) ? 413 : 409);
    throw error; // Existing owner gateway handler sanitizes service/auth exceptions.
  }
}
