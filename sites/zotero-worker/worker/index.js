import { denyPrivateBundleRequest, privateBundleDeniedResponse } from '../src/private-bundle-paths.mjs';
import { handleApplicationRoute } from '../src/application.mjs';
/**
 * Owner-only Zotero application. Outbound transport is disabled until approved setup.
 * The Sites gateway, not this file, authenticates and sanitizes its identity headers.
 * Never enable the gateway assertion on a directly reachable/untrusted Worker.
 */
export const SQL = Object.freeze({
  catalog: `SELECT owner_id, report_id, catalog_id, catalog_version, content_sha256, enabled
    FROM zotero_report_catalogs WHERE owner_id = ? AND report_id = ? AND catalog_id = ?`,
  papers: `SELECT paper_id, display_title FROM zotero_catalog_papers
    WHERE owner_id = ? AND report_id = ? AND catalog_id = ? ORDER BY paper_id`,
  insertIntent: `INSERT INTO zotero_import_intents
    (owner_id, job_id, idempotency_key, report_id, catalog_id, catalog_version,
     catalog_sha256, paper_ids_json, pdf_intent, state, created_at)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(owner_id, idempotency_key) DO NOTHING`,
  intent: `SELECT owner_id, job_id, report_id, catalog_id, catalog_version, catalog_sha256,
    paper_ids_json, pdf_intent, state, created_at FROM zotero_import_intents
    WHERE owner_id = ? AND job_id = ?`,
  intents: `SELECT owner_id, job_id, report_id, catalog_id, catalog_version, catalog_sha256,
    paper_ids_json, pdf_intent, state, created_at FROM zotero_import_intents
    WHERE owner_id = ? ORDER BY created_at DESC, job_id LIMIT 50`,
  credentialState: `SELECT state FROM zotero_encrypted_credentials WHERE owner_id = ?`,
  grantUsed: `SELECT grant_id FROM zotero_setup_grants_consumed WHERE owner_id = ? AND grant_id = ?`,
  consumeGrant: `INSERT INTO zotero_setup_grants_consumed (owner_id, grant_id, used_at) VALUES (?, ?, ?)`,
  insertCredential: `INSERT INTO zotero_encrypted_credentials
    (owner_id, library_type, library_id, key_id, grant_id, cipher_version, iv_b64,
     ciphertext_b64, state, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
});

const ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const JOB_ID = /^zj_[a-f0-9]{64}$/;
const MAX_JSON_BYTES = 16_384;
const SECURITY_HEADERS = Object.freeze({
  'Cache-Control': 'no-store, private',
  'Pragma': 'no-cache',
  'X-Content-Type-Options': 'nosniff',
  'Referrer-Policy': 'no-referrer',
  'Cross-Origin-Resource-Policy': 'same-origin',
  'X-Frame-Options': 'DENY',
});

class HttpError extends Error {
  constructor(status, code) { super(code); this.status = status; this.code = code; }
}
const fail = (status, code) => { throw new HttpError(status, code); };
const json = (body, status = 200, extra = {}) => new Response(JSON.stringify(body), {
  status, headers: { ...SECURITY_HEADERS, 'Content-Type': 'application/json; charset=utf-8', ...extra },
});
const sameKeys = (value, keys) => value !== null && typeof value === 'object'
  && !Array.isArray(value) && Object.keys(value).length === keys.length
  && keys.every(key => Object.hasOwn(value, key));
const validId = value => typeof value === 'string' && ID.test(value);
// Paper IDs are opaque JSON values, never URL route segments or fetch targets.
// DOI-shaped IDs may include '/', but must still match the server catalog exactly.
const validPaperId = value => typeof value === 'string'
  && /^[A-Za-z0-9][A-Za-z0-9._:()/;-]{0,511}$/.test(value)
  && !/^(?:https?|ftp|file|data|javascript|ws|wss):/i.test(value)
  && !value.includes('://') && !value.split('/').some(part => part === '.' || part === '..');
const asStatement = (db, sql, ...args) => db.prepare(sql).bind(...args);

function ownerIdentityDisplay(request, env) {
  requireGet(request);
  // Explicit, temporary operator assertions after checking the existing Site's
  // owner-private audience and gateway-only ingress. Never enroll a visitor.
  if (env?.ZOTERO_OWNER_BOOTSTRAP_ENABLED !== 'approved'
      || env.SITES_OWNER_PRIVATE_AUDIENCE_VERIFIED !== 'owner-private'
      || env.SITES_GATEWAY_IDENTITY_TRUSTED !== 'platform-managed') fail(503, 'owner_bootstrap_unavailable');
  let origin;
  try { origin = new URL(env.ZOTERO_SITE_ORIGIN); } catch { fail(503, 'setup_required'); }
  if (origin.protocol !== 'https:' || origin.origin !== env.ZOTERO_SITE_ORIGIN || origin.username || origin.password)
    fail(503, 'setup_required');
  const url = new URL(request.url);
  if (url.origin !== origin.origin || url.search) fail(403, 'origin_denied');
  const fetchSite = request.headers.get('Sec-Fetch-Site');
  if (fetchSite && !['same-origin', 'none'].includes(fetchSite)) fail(403, 'origin_denied');
  if (request.headers.has('Origin') && request.headers.get('Origin') !== origin.origin) fail(403, 'origin_denied');
  const identity = request.headers.get('oai-authenticated-user-id');
  if (!identity) fail(401, 'authentication_required');
  if (identity.length > 256 || /[\x00-\x20\x7f]/.test(identity)) fail(401, 'authentication_required');
  if (env.ZOTERO_OWNER_ID && env.ZOTERO_OWNER_ID !== identity) fail(403, 'owner_required');
  return new Response(`Daily Agent · 当前登录身份\n\n${identity}\n\n此标识仅属于当前 Site 的登录会话。请通过已确认的部署流程绑定拥有者，然后关闭此临时页面。\n此页面只读，不会创建拥有者、连接 Zotero 或授权访问。\n`, {
    headers: { ...SECURITY_HEADERS, 'Content-Type': 'text/plain; charset=utf-8',
      'Content-Security-Policy': "default-src 'none'; frame-ancestors 'none'; form-action 'none'" },
  });
}

function authorize(request, env) {
  // This is a deployment-boundary assertion, NOT an authentication mechanism.
  // The platform gateway must be the sole ingress and strip client identity headers.
  if (env?.SITES_GATEWAY_IDENTITY_TRUSTED !== 'platform-managed'
      || typeof env.ZOTERO_OWNER_ID !== 'string' || !env.ZOTERO_OWNER_ID
      || /[\x00-\x20\x7f]/.test(env.ZOTERO_OWNER_ID) || env.ZOTERO_OWNER_ID.length > 256
      || typeof env.ZOTERO_SITE_ORIGIN !== 'string'
      || !env.DB || typeof env.DB.prepare !== 'function' || typeof env.DB.batch !== 'function') {
    fail(503, 'setup_required');
  }
  let origin;
  try { origin = new URL(env.ZOTERO_SITE_ORIGIN); } catch { fail(503, 'setup_required'); }
  if (origin.protocol !== 'https:' || origin.origin !== env.ZOTERO_SITE_ORIGIN
      || origin.username || origin.password) fail(503, 'setup_required');
  if (new URL(request.url).origin !== origin.origin) fail(403, 'origin_denied');
  const owner = request.headers.get('oai-authenticated-user-id');
  if (!owner) fail(401, 'authentication_required');
  if (owner !== env.ZOTERO_OWNER_ID) fail(403, 'owner_required');
  // Display names, email addresses, cookies, X-Owner, and body fields never authorize.
  return { owner, origin: origin.origin, db: env.DB };
}

function requireGet(request) {
  if (request.method !== 'GET') fail(405, 'method_not_allowed');
}

function requireAction(request, origin, contentType = 'application/json') {
  if (request.method !== 'POST') fail(405, 'method_not_allowed');
  if (request.headers.get('Origin') !== origin) fail(403, 'origin_denied');
  const fetchSite = request.headers.get('Sec-Fetch-Site');
  if (fetchSite !== null && fetchSite !== 'same-origin') fail(403, 'origin_denied');
  if (request.headers.get('X-Zotero-Action') !== '1') fail(403, 'action_header_required');
  const suppliedType = request.headers.get('Content-Type') || '';
  const validType = contentType === 'application/json'
    ? /^application\/json(?:\s*;\s*charset=utf-8)?$/i.test(suppliedType)
    : contentType === 'application/pdf' && /^application\/pdf$/i.test(suppliedType);
  if (!validType) fail(415, contentType === 'application/pdf' ? 'pdf_required' : 'json_required');
  if (new URL(request.url).search) fail(400, 'query_not_allowed');
}

async function readJson(request, limit = MAX_JSON_BYTES) {
  const length = request.headers.get('Content-Length');
  if (length !== null && (!/^\d+$/.test(length) || Number(length) > limit)) fail(413, 'body_too_large');
  if (!request.body) fail(400, 'invalid_json');
  const reader = request.body.getReader();
  const chunks = [];
  let count = 0;
  let timer;
  const deadline = new Promise((_, reject) => {
    timer = setTimeout(() => {
      reject(new HttpError(408, 'body_timeout'));
      void reader.cancel().catch(() => {});
    }, 10_000);
  });
  try {
    while (true) {
      const { value, done } = await Promise.race([reader.read(), deadline]);
      if (done) break;
      count += value.byteLength;
      if (count > limit) { await reader.cancel(); fail(413, 'body_too_large'); }
      chunks.push(value);
    }
    const bytes = new Uint8Array(count);
    let offset = 0;
    for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
    try { return JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes)); }
    catch { fail(400, 'invalid_json'); }
    finally { bytes.fill(0); }
  } finally {
    clearTimeout(timer);
    for (const chunk of chunks) chunk.fill(0);
    reader.releaseLock();
  }
}

async function getCatalog(auth, reportId, catalogId) {
  if (!validId(reportId) || !validId(catalogId)) fail(400, 'invalid_catalog_id');
  const row = await asStatement(auth.db, SQL.catalog, auth.owner, reportId, catalogId).first();
  if (!row || row.enabled !== 1) fail(404, 'catalog_not_found');
  if (!Number.isSafeInteger(row.catalog_version) || row.catalog_version < 1
      || !/^[a-f0-9]{64}$/.test(row.content_sha256)) fail(503, 'catalog_unavailable');
  const result = await asStatement(auth.db, SQL.papers, auth.owner, reportId, catalogId).all();
  if (!Array.isArray(result.results)) fail(503, 'catalog_unavailable');
  return { ...row, papers: result.results };
}

const hex = bytes => Array.from(bytes, value => value.toString(16).padStart(2, '0')).join('');
const sha256 = async value => hex(new Uint8Array(await crypto.subtle.digest(
  'SHA-256', new TextEncoder().encode(value),
)));

function publicIntent(row) {
  // Explicit output allowlist: never expose stored credentials or claim a live save.
  return {
    job_id: row.job_id, report_id: row.report_id, catalog_id: row.catalog_id,
    catalog_version: row.catalog_version, catalog_sha256: row.catalog_sha256,
    paper_ids: JSON.parse(row.paper_ids_json), pdf_intent: row.pdf_intent,
    state: row.state, created_at: row.created_at,
    executed: false, live_ready: false, zotero_requests: 0,
  };
}

async function saveIntent(request, env, auth) {
  requireAction(request, auth.origin);
  const body = await readJson(request);
  if (!sameKeys(body, ['action', 'report_id', 'catalog_id', 'catalog_version', 'paper_ids', 'pdf_intent'])
      || body.action !== 'save_to_zotero') fail(400, 'invalid_save_request');
  if (!validId(body.report_id) || !validId(body.catalog_id)
      || !Number.isSafeInteger(body.catalog_version) || body.catalog_version < 1
      || !Array.isArray(body.paper_ids) || body.paper_ids.length < 1 || body.paper_ids.length > 100
      || !body.paper_ids.every(validPaperId)
      || !['metadata_only', 'open_access_if_available'].includes(body.pdf_intent)) {
    fail(400, 'invalid_save_request');
  }
  const paperIds = [...new Set(body.paper_ids)].sort();
  const catalog = await getCatalog(auth, body.report_id, body.catalog_id);
  if (catalog.catalog_version !== body.catalog_version) fail(409, 'catalog_version_mismatch');
  const allowed = new Set(catalog.papers.map(paper => paper.paper_id));
  if (!paperIds.every(id => allowed.has(id))) fail(400, 'selection_not_in_catalog');
  const canonical = JSON.stringify({
    version: 1, owner_id: auth.owner, report_id: body.report_id, catalog_id: body.catalog_id,
    catalog_version: catalog.catalog_version, catalog_sha256: catalog.content_sha256,
    paper_ids: paperIds, pdf_intent: body.pdf_intent,
  });
  const idempotencyKey = await sha256(canonical);
  const jobId = `zj_${idempotencyKey}`;
  const mode = env.ZOTERO_INTENT_MODE || 'blocked_connection';
  if (!['blocked_connection', 'preview_only'].includes(mode)) fail(503, 'setup_required');
  const write = await asStatement(auth.db, SQL.insertIntent,
    auth.owner, jobId, idempotencyKey, body.report_id, body.catalog_id, catalog.catalog_version,
    catalog.content_sha256, JSON.stringify(paperIds), body.pdf_intent, mode, new Date().toISOString(),
  ).run();
  if (write.success === false) fail(503, 'storage_unavailable');
  const row = await asStatement(auth.db, SQL.intent, auth.owner, jobId).first();
  if (!row) fail(503, 'storage_unavailable');
  const created = write.meta?.changes === 1;
  return json({ ...publicIntent(row), created, message: 'Intent recorded only. Zotero is not connected.' }, created ? 201 : 200);
}

function bytesToBase64(bytes) {
  let binary = '';
  for (const value of bytes) binary += String.fromCharCode(value);
  return btoa(binary);
}
function encryptionBytes(encoded) {
  if (typeof encoded !== 'string' || !/^[A-Za-z0-9+/]{43}=$/.test(encoded)) return null;
  try {
    const raw = atob(encoded);
    const bytes = Uint8Array.from(raw, char => char.charCodeAt(0));
    if (bytes.length !== 32 || bytesToBase64(bytes) !== encoded) { bytes.fill(0); return null; }
    return bytes;
  } catch { return null; }
}

function getStagingSetup(env, owner, now = Date.now()) {
  // No connection path exists unless EACH separately approved server binding is present.
  // This code cannot obtain approval, mint grants, generate keys, or verify a library.
  let grant;
  try { grant = JSON.parse(env.ZOTERO_SETUP_GRANT_JSON); } catch { return null; }
  if (!sameKeys(grant, ['grant_id', 'owner_id', 'library_id', 'purpose', 'issued_at', 'expires_at',
    'security_action_approved', 'secure_user_entry_approved'])
    || !validId(grant.grant_id) || grant.owner_id !== owner
    || grant.purpose !== 'zotero_credential_staging'
    || grant.security_action_approved !== true || grant.secure_user_entry_approved !== true
    || typeof env.ZOTERO_VERIFIED_PERSONAL_LIBRARY_ID !== 'string'
    || !/^[1-9]\d{0,15}$/.test(env.ZOTERO_VERIFIED_PERSONAL_LIBRARY_ID)
    || !Number.isSafeInteger(Number(env.ZOTERO_VERIFIED_PERSONAL_LIBRARY_ID))
    || grant.library_id !== env.ZOTERO_VERIFIED_PERSONAL_LIBRARY_ID
    || !validId(env.ZOTERO_ENCRYPTION_KEY_ID)) return null;
  const issued = Date.parse(grant.issued_at);
  const expires = Date.parse(grant.expires_at);
  if (!Number.isFinite(issued) || !Number.isFinite(expires) || issued > now || expires <= now
      || expires <= issued || expires - issued > 3_600_000) return null;
  const keyBytes = encryptionBytes(env.ZOTERO_CREDENTIAL_ENCRYPTION_KEY_B64);
  if (!keyBytes) return null;
  return { grant, keyBytes, libraryId: grant.library_id, keyId: env.ZOTERO_ENCRYPTION_KEY_ID };
}

async function connectionStatus(env, auth) {
  const credential = await asStatement(auth.db, SQL.credentialState, auth.owner).first();
  const setup = getStagingSetup(env, auth.owner);
  let canStage = false;
  if (setup) {
    try {
      const consumed = await asStatement(auth.db, SQL.grantUsed, auth.owner, setup.grant.grant_id).first();
      canStage = !consumed && !credential;
    } finally { setup.keyBytes.fill(0); }
  }
  return {
    status: 'not_connected', ready: false, live_ready: false,
    credential_storage: credential ? 'encrypted_pending_verification' : 'not_staged',
    credential_entry_enabled: canStage,
    reason: credential ? 'live_verification_and_transport_not_implemented' : 'setup_required',
  };
}

async function stageCredential(request, env, auth) {
  requireAction(request, auth.origin);
  const setup = getStagingSetup(env, auth.owner);
  // Do not even parse credential-bearing bodies while the secure setup gate is closed.
  if (!setup) fail(409, 'setup_required');
  let plaintextBytes;
  let body;
  try {
    const consumed = await asStatement(auth.db, SQL.grantUsed, auth.owner, setup.grant.grant_id).first();
    const existing = await asStatement(auth.db, SQL.credentialState, auth.owner).first();
    if (consumed || existing) fail(409, 'credential_staging_unavailable');
    body = await readJson(request, 4096);
    if (!sameKeys(body, ['action', 'api_key']) || body.action !== 'stage_credentials'
        || typeof body.api_key !== 'string' || !/^[A-Za-z0-9]{16,128}$/.test(body.api_key)) {
      fail(400, 'invalid_credential_request');
    }
    plaintextBytes = new TextEncoder().encode(body.api_key);
    body.api_key = ''; // Strings cannot be reliably zeroized; minimize their lifetime.
    const iv = crypto.getRandomValues(new Uint8Array(12));
    const aad = new TextEncoder().encode(JSON.stringify({
      version: 'AES-256-GCM.v1', owner_id: auth.owner, library_type: 'user',
      library_id: setup.libraryId, key_id: setup.keyId, grant_id: setup.grant.grant_id,
    }));
    const key = await crypto.subtle.importKey('raw', setup.keyBytes, { name: 'AES-GCM' }, false, ['encrypt']);
    const ciphertext = new Uint8Array(await crypto.subtle.encrypt({ name: 'AES-GCM', iv, additionalData: aad, tagLength: 128 }, key, plaintextBytes));
    // Body streaming and encryption may outlast the short-lived approval.
    // Recheck immediately before starting the atomic write, not only on entry.
    if (Date.now() >= Date.parse(setup.grant.expires_at)) fail(409, 'setup_required');
    const now = new Date().toISOString();
    // D1 batch is transactional: replay protection and ciphertext are committed together.
    // A concurrent submission loses the unique constraint race without partial writes.
    const results = await auth.db.batch([
      asStatement(auth.db, SQL.consumeGrant, auth.owner, setup.grant.grant_id, now),
      asStatement(auth.db, SQL.insertCredential, auth.owner, 'user', setup.libraryId, setup.keyId,
        setup.grant.grant_id, 'AES-256-GCM.v1', bytesToBase64(iv), bytesToBase64(ciphertext),
        'encrypted_pending_verification', now),
    ]);
    if (results.some(result => result.success === false)) fail(503, 'storage_unavailable');
    return json({ status: 'not_connected', ready: false, credential_storage: 'encrypted_pending_verification',
      message: 'Credential encrypted and staged. No Zotero request has been made.' }, 201);
  } finally {
    if (body && typeof body === 'object' && Object.hasOwn(body, 'api_key')) body.api_key = '';
    plaintextBytes?.fill(0);
    setup.keyBytes.fill(0);
  }
}

function panel(status) {
  const nonce = bytesToBase64(crypto.getRandomValues(new Uint8Array(18)));
  const disabled = status.credential_entry_enabled ? '' : ' disabled';
  const storage = status.credential_storage === 'not_staged'
    ? '尚未保存任何凭据。' : '凭据已加密暂存，仍待核验；这不代表账号已连接。';
  const html = `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Zotero 连接设置 · 未连接 · Daily Agent</title>
<style nonce="${nonce}">
:root{color-scheme:light dark;--bg:#f6f4ed;--card:#fffef9;--ink:#172c29;--muted:#5d6c66;--line:#ccd3c8;--accent:#14634c;--soft:#e9eee4;--warm:#f2ecd9;--focus:#925025}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.8 Inter,-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}.page{max-width:960px;margin:auto;padding:0 40px}a{color:var(--accent);text-underline-offset:5px}.masthead{display:flex;justify-content:space-between;align-items:center;gap:20px;border-bottom:1px solid var(--ink);padding:26px 0 20px}.brand{font:700 30px Georgia,serif;color:var(--ink);text-decoration:none}.edition,.eyebrow{font-size:12px;letter-spacing:1.6px;color:var(--muted)}.hero{display:flex;align-items:center;justify-content:space-between;gap:24px;padding:34px 0 26px}h1{font-size:clamp(28px,4vw,38px);line-height:1.4;margin:8px 0 10px}h2{font-size:18px;margin:0 0 12px}p{margin:8px 0}.intro,.note,small{color:var(--muted)}.status{border:1px solid var(--line);border-radius:4px;background:var(--card);padding:10px 16px;white-space:nowrap;font-weight:600}.status small{display:block;font-size:12px;letter-spacing:1px;font-weight:400}.notice{background:var(--warm);border-left:3px solid var(--focus);padding:14px 18px;font-size:14px}.panel{background:var(--card);border:1px solid var(--line);border-radius:4px;padding:24px;margin:24px 0}.panel p{font-size:14px}.step{color:var(--accent);font-size:12px;letter-spacing:1.8px;margin-bottom:8px}label{display:block;margin-top:18px;font-size:14px;font-weight:600}input,button{font:inherit;border-radius:4px}input{width:100%;padding:10px 12px;border:1px solid var(--line);background:var(--bg);color:var(--ink);margin:8px 0}button{background:var(--accent);color:var(--card);border:1px solid var(--accent);padding:10px 18px;min-height:48px;margin-top:18px;cursor:pointer}button:disabled{background:var(--soft);color:var(--muted);border-color:var(--line);cursor:not-allowed}fieldset{border:0;padding:0;margin:0}small{display:block;font-size:12px}a:focus-visible,input:focus-visible,button:focus-visible{outline:3px solid var(--focus);outline-offset:4px}#result{white-space:pre-wrap;color:var(--accent)}ul{padding-left:22px;font-size:14px;overflow-wrap:anywhere}.footer{border-top:1px solid var(--line);padding:18px 0 26px;font-size:12px;color:var(--muted)}
@media(prefers-color-scheme:dark){:root{--bg:#142320;--card:#1c2e29;--ink:#e6ece0;--muted:#adbdb3;--line:#41564b;--accent:#a7d3ae;--soft:#263c32;--warm:#36382b;--focus:#eab98a}}
@media(max-width:620px){.page{padding:0 20px}.hero{flex-wrap:wrap;padding:26px 0 22px;gap:14px}.brand{font-size:26px}.edition{display:none}.panel{padding:20px}.status{display:flex;align-items:center;gap:12px}.status small{display:inline}}
</style></head><body><div class="page"><header class="masthead"><a class="brand" href="/">Daily Agent</a><span class="edition">研究日报 / ZOTERO</span></header>
<main><section class="hero"><div><p class="eyebrow">YOUR LIBRARY, YOUR CHOICE</p><h1>Zotero 连接设置</h1><p class="intro">先确认授权，再由你决定哪些论文值得留存。</p></div><span class="status">未连接<small>NOT CONNECTED</small></span></section>
<div class="notice"><strong>当前不能导入</strong><p>勾选本身不会触发导入。只有明确点击 Save to Zotero 才会记录待处理意向；目前所有意向都保持「等待连接」或「仅预览」。</p></div>
<section class="panel"><p class="step">01 / CONNECTION</p><h2>连接状态</h2><p>${storage}</p><p class="note">此设置页独立于合成选文预览，不修改已有日报或归档。真实导入仍需完成文库权限核验、任务执行与附件回读验收。</p></section>
<section class="panel"><p class="step">02 / SECURE SETUP</p><h2>安全录入凭据</h2><p id="setup-note">${status.credential_entry_enabled
    ? '一次性安全授权已生效。提交后只会将密钥加密暂存，并绑定已核验的个人文库；不会立即连接或导入。'
    : '录入暂未开放。需先完成一次性安全批准、个人文库确认、限定范围的服务器授权及运行时加密密钥配置。'}</p>
<form id="credential-form" autocomplete="off"><fieldset id="credential-fields"${disabled}>
<label for="api-key">个人文库 Zotero API 密钥</label><input id="api-key" type="password" autocomplete="off" spellcheck="false" autocapitalize="none" minlength="16" maxlength="256" required aria-describedby="setup-note">
<small>仅在批准配置后于此处安全录入；请勿将密钥粘贴到聊天、日报或链接中。</small>
<button type="submit">加密并暂存凭据</button></fieldset></form><p id="result" role="status" aria-live="polite"></p></section>
<section class="panel"><p class="step">03 / SAVED INTENTS</p><h2>待处理的保存意向</h2><p class="note">意向保存在仅你可访问的数据库中。目前没有向 Zotero 保存任何元数据或 PDF。</p><ul id="intents"><li>正在读取…</li></ul></section></main>
<footer class="footer">Daily Agent / Zotero · 尚未连接 · <a href="/">返回研究日报</a></footer></div>
<script nonce="${nonce}">
const form=document.getElementById('credential-form');
const fields=document.getElementById('credential-fields');
const key=document.getElementById('api-key');
const result=document.getElementById('result');
form.addEventListener('submit',async event=>{event.preventDefault();if(fields.disabled)return;fields.disabled=true;
let payload=JSON.stringify({action:'stage_credentials',api_key:key.value});key.value='';
try{const response=await fetch('/api/zotero/connect',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-Zotero-Action':'1'},body:payload});payload='';const data=await response.json();result.textContent=response.ok?'已加密暂存，Zotero 仍未连接。':'录入未完成，请检查配置或重新申请一次性安全授权。';if(!response.ok)fields.disabled=false;}catch{result.textContent='暂存结果尚未确认，请刷新检查状态后再操作。';}finally{key.value='';payload='';}});
fetch('/api/zotero/import-intents',{credentials:'same-origin'}).then(response=>{if(!response.ok)throw Error();return response.json();}).then(data=>{const list=document.getElementById('intents');list.replaceChildren();for(const item of data.intents){const li=document.createElement('li');const state=item.state==='preview_only'?'仅预览':'等待连接';li.textContent=item.paper_ids.length+' 篇 · '+state+' · '+item.report_id;list.append(li);}if(!data.intents.length){const li=document.createElement('li');li.textContent='尚无保存意向';list.append(li);}}).catch(()=>{document.getElementById('intents').textContent='暂时无法读取待处理意向';});
</script></body></html>`;
  return new Response(html, { headers: { ...SECURITY_HEADERS,
    'Content-Type': 'text/html; charset=utf-8',
    'Content-Security-Policy': `default-src 'none'; script-src 'nonce-${nonce}'; style-src 'nonce-${nonce}'; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'`,
    'Permissions-Policy': 'camera=(), microphone=(), geolocation=()',
  } });
}

export function isZoteroRoute(pathname) {
  return pathname === '/zotero' || pathname.startsWith('/zotero/')
    || pathname === '/api/zotero' || pathname.startsWith('/api/zotero/');
}

// Host integration: return null for unrelated routes, leaving their original router intact.
export async function handleZoteroRequest(request, env, options = {}) {
    if (denyPrivateBundleRequest(request)) return privateBundleDeniedResponse();
    const url = new URL(request.url);
    if (!isZoteroRoute(url.pathname)) return null;
    try {
      if (url.pathname === '/zotero/identity') return ownerIdentityDisplay(request, env);
      if (url.search) fail(400, 'query_not_allowed');
      const auth = authorize(request, env);
      const application = await handleApplicationRoute(request, env, auth, {
        json, requireGet, requireAction, readJson, fail, legacyConnectionStatus: connectionStatus,
      }, options);
      if (application) return application;
      // No permissive CORS, OPTIONS handler, public diagnostics, callbacks, or proxy endpoint.
      if (url.pathname === '/zotero' || url.pathname === '/zotero/') {
        requireGet(request);
        return panel(await connectionStatus(env, auth));
      }
      if (url.pathname === '/api/zotero/connection') {
        requireGet(request);
        return json(await connectionStatus(env, auth));
      }
      if (url.pathname === '/api/zotero/connect') return await stageCredential(request, env, auth);
      if (url.pathname === '/api/zotero/import-intents') {
        if (request.method === 'POST') return await saveIntent(request, env, auth);
        requireGet(request);
        const rows = await asStatement(auth.db, SQL.intents, auth.owner).all();
        return json({ intents: rows.results.map(publicIntent), live_ready: false });
      }
      const jobMatch = url.pathname.match(/^\/api\/zotero\/import-intents\/([^/]+)$/);
      if (jobMatch) {
        requireGet(request);
        if (!JOB_ID.test(jobMatch[1])) fail(404, 'intent_not_found');
        const row = await asStatement(auth.db, SQL.intent, auth.owner, jobMatch[1]).first();
        if (!row) fail(404, 'intent_not_found');
        return json(publicIntent(row));
      }
      const catalogMatch = url.pathname.match(/^\/api\/zotero\/reports\/([^/]+)\/catalogs\/([^/]+)$/);
      if (catalogMatch) {
        requireGet(request);
        const catalog = await getCatalog(auth, catalogMatch[1], catalogMatch[2]);
        return json({ report_id: catalog.report_id, catalog_id: catalog.catalog_id,
          catalog_version: catalog.catalog_version, catalog_sha256: catalog.content_sha256,
          papers: catalog.papers.map(paper => ({ paper_id: paper.paper_id, title: paper.display_title })),
          catalog_verified: false, live_ready: false });
      }
      fail(404, 'not_found');
    } catch (error) {
      // Never log request bodies, bindings, database exceptions, credentials, or ciphertext.
      if (error instanceof HttpError) return json({ error: error.code, live_ready: false }, error.status);
      return json({ error: 'service_unavailable', live_ready: false }, 503);
    }
}

export default {
  async fetch(request, env) {
    const zotero = await handleZoteroRequest(request, env);
    if (zotero) return zotero;
    // Only an existing supported static binding may handle unrelated routes. This is
    // not an outbound HTTP fetch. Otherwise the host must compose its original router.
    if (env?.ASSETS && typeof env.ASSETS.fetch === 'function') return env.ASSETS.fetch(request);
    return json({ error: 'host_router_required', live_ready: false }, 503);
  },
};
