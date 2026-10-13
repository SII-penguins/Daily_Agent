import test, { before, after } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import worker, { handleZoteroRequest } from '../worker/index.js';
import { fixture } from './mock-d1.mjs';

const origin = 'https://daily.example.test';
const savePath = '/api/zotero/import-intents';
const connectPath = '/api/zotero/connect';
const catalogPath = '/api/zotero/reports/report-20261010/catalogs/catalog-v1';
const selection = () => ({ action: 'save_to_zotero', report_id: 'report-20261010',
  catalog_id: 'catalog-v1', catalog_version: 1, paper_ids: ['paper-b', 'paper-a'], pdf_intent: 'metadata_only' });
function request(path, { method = 'GET', body, headers = {}, owner = 'owner-1', urlOrigin = origin } = {}) {
  const all = { 'oai-authenticated-user-id': owner, ...headers };
  if (method === 'POST') Object.assign(all, { Origin: origin, 'Content-Type': 'application/json', 'X-Zotero-Action': '1', ...headers });
  for (const [key, value] of Object.entries(all)) if (value === null) delete all[key];
  return new Request(urlOrigin + path, { method, headers: all, body: body === undefined ? undefined : typeof body === 'string' ? body : JSON.stringify(body) });
}
async function call(env, path, options) {
  const response = await worker.fetch(request(path, options), env);
  return { response, data: await response.json() };
}
function grant(env, overrides = {}) {
  const now = Date.now();
  const approval = { grant_id: 'test-only-grant', owner_id: 'owner-1', library_id: '123456',
    purpose: 'zotero_credential_staging', issued_at: new Date(now - 1000).toISOString(),
    expires_at: new Date(now + 60_000).toISOString(), security_action_approved: true,
    secure_user_entry_approved: true, ...overrides };
  return { ...env, ZOTERO_VERIFIED_PERSONAL_LIBRARY_ID: '123456', ZOTERO_ENCRYPTION_KEY_ID: 'test-key-v1',
    ZOTERO_CREDENTIAL_ENCRYPTION_KEY_B64: Buffer.alloc(32, 7).toString('base64'),
    ZOTERO_SETUP_GRANT_JSON: JSON.stringify(approval) };
}
let savedFetch;
before(() => {
  savedFetch = globalThis.fetch;
  globalThis.fetch = () => { throw new Error('An outbound network call is forbidden in the Worker reference'); };
});
after(() => { globalThis.fetch = savedFetch; });

test('schema-only migration creates empty, constrained, owner-scoped tables', () => {
  const source = readFileSync(new URL('../migrations/0001_zotero_reference.sql', import.meta.url), 'utf8');
  assert.doesNotMatch(source, /\bINSERT\b|\bUPDATE\b|\bDELETE\b/i);
  const { db } = fixture();
  assert.equal(db.rows('zotero_import_intents').length, 0);
  assert.equal(db.rows('zotero_encrypted_credentials').length, 0);
  assert.throws(() => db.sqlite.prepare('INSERT INTO zotero_import_intents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)')
    .run('owner-1', 'bad', 'bad', 'missing', 'missing', 1, 'a'.repeat(64), '[]', 'metadata_only', 'complete', 'now'));
});

test('connection is explicitly disconnected and panel credential entry is disabled by default', async () => {
  const { env } = fixture();
  const { data } = await call(env, '/api/zotero/connection');
  assert.deepEqual(data, { status: 'not_connected', ready: false, live_ready: false,
    credential_storage: 'not_staged', credential_entry_enabled: false, reason: 'setup_required', live_gate_enabled: false,
    verification_enabled: false, credential_entry_mode: 'aead', disconnect_enabled: false, connection_version: null, last_verified_at: null });
  const response = await worker.fetch(request('/zotero'), env);
  const html = await response.text();
  assert.match(html, /id="credential-fields" disabled/);
  assert.match(html, /勾选本身不会触发导入/);
  assert.match(response.headers.get('Content-Security-Policy'), /default-src 'none'/);
  assert.equal(response.headers.get('Access-Control-Allow-Origin'), null);
  assert.equal(response.headers.get('Cache-Control'), 'no-store, private');
});

test('all routes reject anonymous and other owners, ignoring client ownership claims', async () => {
  for (const path of ['/zotero', '/api/zotero/connection', catalogPath, savePath, connectPath]) {
    const { env, db } = fixture();
    for (const owner of [null, 'owner-2']) {
      const response = await worker.fetch(request(path, { owner, headers: { 'X-Owner': 'owner-1', 'oai-authenticated-user-email': 'owner@example.test' } }), env);
      assert.equal(response.status, owner === null ? 401 : 403);
      assert.equal(db.calls.length, 0);
    }
  }
});

test('missing or invalid server binding fails closed before DB/body access', async () => {
  for (const [key, value] of [
    ['DB', undefined], ['SITES_GATEWAY_IDENTITY_TRUSTED', undefined],
    ['SITES_GATEWAY_IDENTITY_TRUSTED', 'true'], ['ZOTERO_OWNER_ID', undefined],
    ['ZOTERO_OWNER_ID', 'owner 1'], ['ZOTERO_SITE_ORIGIN', undefined],
    ['ZOTERO_SITE_ORIGIN', 'http://daily.example.test'], ['ZOTERO_SITE_ORIGIN', origin + '/'],
  ]) {
    const { env, db } = fixture();
    env[key] = value;
    const req = request(connectPath, { method: 'POST', body: { api_key: 'never-store-this' } });
    const response = await worker.fetch(req, env);
    assert.equal(response.status, 503, key);
    assert.equal(req.bodyUsed, false);
    assert.equal(db.calls.length, 0);
  }
});

test('rejects methods, OPTIONS, mismatched or missing origins, unsafe content types, and cross-site signals', async () => {
  const cases = [
    ['PUT', {}, 405], ['DELETE', {}, 405], ['OPTIONS', {}, 405],
    ['POST', { Origin: null }, 403], ['POST', { Origin: 'null' }, 403],
    ['POST', { Origin: 'https://evil.example' }, 403],
    ['POST', { Origin: origin + '/' }, 403],
    ['POST', { 'Sec-Fetch-Site': 'cross-site' }, 403],
    ['POST', { 'Sec-Fetch-Site': 'same-site' }, 403],
    ['POST', { 'X-Zotero-Action': null }, 403],
    ['POST', { 'Content-Type': 'text/plain' }, 415],
    ['POST', { 'Content-Type': 'application/x-www-form-urlencoded' }, 415],
    ['POST', { 'Content-Type': 'application/json; charset=iso-8859-1' }, 415],
  ];
  for (const [method, headers, expected] of cases) {
    const { env, db } = fixture();
    const { response } = await call(env, savePath, { method, headers, body: selection() });
    assert.equal(response.status, expected, `${method} ${JSON.stringify(headers)}`);
    assert.equal(response.headers.get('Access-Control-Allow-Origin'), null);
    assert.equal(db.rows('zotero_import_intents').length, 0);
  }
  const { env } = fixture();
  assert.equal((await call(env, savePath, { method: 'POST', body: selection(), urlOrigin: 'https://evil.example' })).response.status, 403);
  assert.equal((await call(env, savePath + '?api_key=no', { method: 'POST', body: selection() })).response.status, 400);
  assert.equal((await call(env, '/api/zotero/connection', { method: 'POST', body: {} })).response.status, 405);
});

test('catalog read is owner-scoped, sanitized, and selection alone creates no job', async () => {
  const { env, db } = fixture();
  const { data } = await call(env, catalogPath);
  assert.equal(data.papers.length, 2);
  assert.equal(data.papers[0].paper_id, 'paper-a');
  assert.equal('metadata_json' in data.papers[0], false);
  assert.equal(db.rows('zotero_import_intents').length, 0);
  assert.equal((await call(env, '/api/zotero/reports/foreign-report/catalogs/foreign-catalog')).response.status, 404);
});

test('explicit Save persists deterministic blocked intents and handles duplicate/reordered concurrent clicks', async () => {
  const { env, db } = fixture();
  const requests = Array.from({ length: 8 }, (_, index) => ({ ...selection(),
    paper_ids: index % 2 ? ['paper-a', 'paper-b', 'paper-a'] : ['paper-b', 'paper-a'] }));
  const results = await Promise.all(requests.map(body => call(env, savePath, { method: 'POST', body })));
  assert.equal(results.filter(result => result.response.status === 201).length, 1);
  assert.equal(results.filter(result => result.response.status === 200).length, 7);
  assert.equal(new Set(results.map(result => result.data.job_id)).size, 1);
  const row = db.rows('zotero_import_intents')[0];
  assert.equal(db.rows('zotero_import_intents').length, 1);
  assert.equal(row.owner_id, 'owner-1');
  assert.equal(row.report_id, 'report-20261010');
  assert.equal(row.catalog_id, 'catalog-v1');
  assert.equal(row.catalog_version, 1);
  assert.equal(row.catalog_sha256, 'a'.repeat(64));
  assert.equal(row.paper_ids_json, '["paper-a","paper-b"]');
  assert.equal(row.pdf_intent, 'metadata_only');
  assert.equal(row.state, 'blocked_connection');
  for (const { data } of results) {
    assert.equal(data.executed, false);
    assert.equal(data.live_ready, false);
    assert.equal(data.zotero_requests, 0);
    assert.doesNotMatch(JSON.stringify(data), /metadata_saved|"complete"/);
  }
});

test('PDF intent and selection affect identity; preview mode never executes', async () => {
  const { env, db } = fixture();
  env.ZOTERO_INTENT_MODE = 'preview_only';
  const first = await call(env, savePath, { method: 'POST', body: selection() });
  const second = await call(env, savePath, { method: 'POST', body: { ...selection(), pdf_intent: 'open_access_if_available' } });
  const third = await call(env, savePath, { method: 'POST', body: { ...selection(), paper_ids: ['paper-a'] } });
  assert.equal(new Set([first.data.job_id, second.data.job_id, third.data.job_id]).size, 3);
  assert.equal(db.rows('zotero_import_intents').length, 3);
  assert.ok(db.rows('zotero_import_intents').every(row => row.state === 'preview_only'));
});

test('jobs are readable only by their owner, including another configured owner on the same DB', async () => {
  const { env, db } = fixture();
  const saved = await call(env, savePath, { method: 'POST', body: selection() });
  const jobPath = savePath + '/' + saved.data.job_id;
  assert.equal((await call(env, jobPath)).response.status, 200);
  assert.equal((await call(env, savePath)).data.intents.length, 1);
  const otherEnv = { ...env, ZOTERO_OWNER_ID: 'owner-2' };
  assert.equal((await call(otherEnv, jobPath, { owner: 'owner-2' })).response.status, 404);
  assert.equal((await call(otherEnv, savePath, { owner: 'owner-2' })).data.intents.length, 0);
  assert.equal((await call(otherEnv, catalogPath, { owner: 'owner-2' })).response.status, 404);
  assert.equal(db.rows('zotero_import_intents').length, 1);
});

test('rejects stale or tampered catalog, selections, auto-import actions, and all extra fields', async () => {
  const modifications = [
    { action: 'select' }, { action: 'auto_import' }, { catalog_version: 999 },
    { report_id: 'foreign-report', catalog_id: 'foreign-catalog', paper_ids: ['foreign-paper'] },
    { paper_ids: ['foreign-paper'] }, { paper_ids: ['https://example.test/paper'] },
    { paper_ids: [] }, { paper_ids: Array(101).fill('paper-a') },
    { pdf_intent: 'download_any_url' }, { catalog_id: '../foreign-catalog' },
    { url: 'https://evil.example' }, { metadata: { title: 'injected' } },
    { library_id: '999' }, { library_type: 'group' }, { api_key: 'do-not-store' },
    { credential: 'do-not-store' }, { owner_id: 'owner-2' }, { idempotency_key: 'browser-chosen' },
  ];
  for (const changed of modifications) {
    const { env, db } = fixture();
    const result = await call(env, savePath, { method: 'POST', body: { ...selection(), ...changed } });
    assert.ok([400, 404, 409].includes(result.response.status), JSON.stringify(changed));
    assert.equal(db.rows('zotero_import_intents').length, 0);
    assert.equal(db.rows('zotero_encrypted_credentials').length, 0);
    assert.doesNotMatch(JSON.stringify(db.calls), /do-not-store|evil.example/);
  }
});

test('revoked catalog, oversized and malformed bodies, and invalid intent mode cannot create intents', async () => {
  const { env, db } = fixture();
  for (const body of ['{', '[]', 'null', ' '.repeat(16_385)]) {
    const result = await call(env, savePath, { method: 'POST', body });
    assert.ok([400, 413].includes(result.response.status));
  }
  env.ZOTERO_INTENT_MODE = 'live';
  assert.equal((await call(env, savePath, { method: 'POST', body: selection() })).response.status, 503);
  env.ZOTERO_INTENT_MODE = 'blocked_connection';
  db.sqlite.exec('UPDATE zotero_report_catalogs SET enabled = 0');
  assert.equal((await call(env, savePath, { method: 'POST', body: selection() })).response.status, 404);
  assert.equal(db.rows('zotero_import_intents').length, 0);
});

test('default connect rejects credential payload without reading, persisting, reflecting, or logging it', async () => {
  const { env, db } = fixture();
  const fake = 'UNSTAGEDFAKEKEYONLY123456789012345';
  const req = request(connectPath, { method: 'POST', body: { action: 'stage_credentials', api_key: fake } });
  const response = await worker.fetch(req, env);
  assert.equal(response.status, 409);
  assert.equal(req.bodyUsed, false);
  assert.equal((await response.json()).error, 'setup_required');
  assert.equal(db.rows('zotero_encrypted_credentials').length, 0);
  assert.equal(db.rows('zotero_setup_grants_consumed').length, 0);
  assert.doesNotMatch(JSON.stringify(db.calls), new RegExp(fake));
});

test('staging grant must be approved, scoped, unexpired, short-lived, and backed by a verified personal library and AES key', async () => {
  const cases = [
    { owner_id: 'owner-2' }, { library_id: '999' }, { purpose: 'connect_now' },
    { security_action_approved: false }, { secure_user_entry_approved: false },
    { expires_at: '2000-01-01T00:00:00Z' }, { issued_at: '2099-01-01T00:00:00Z' },
    { issued_at: '2000-01-01T00:00:00Z' }, { extra: true },
  ];
  for (const override of cases) {
    const { env, db } = fixture();
    const result = await call(grant(env, override), connectPath, { method: 'POST', body: { action: 'stage_credentials', api_key: 'A'.repeat(32) } });
    assert.equal(result.response.status, 409);
    assert.equal(db.rows('zotero_encrypted_credentials').length, 0);
  }
  for (const [key, value] of [['ZOTERO_CREDENTIAL_ENCRYPTION_KEY_B64', undefined],
    ['ZOTERO_CREDENTIAL_ENCRYPTION_KEY_B64', Buffer.alloc(16).toString('base64')],
    ['ZOTERO_VERIFIED_PERSONAL_LIBRARY_ID', 'group:123456'], ['ZOTERO_ENCRYPTION_KEY_ID', undefined]]) {
    const { env } = fixture();
    const ready = grant(env); ready[key] = value;
    assert.equal((await call(ready, '/api/zotero/connection')).data.credential_entry_enabled, false);
    assert.equal((await call(ready, connectPath, { method: 'POST', body: { action: 'stage_credentials', api_key: 'A'.repeat(32) } })).response.status, 409);
  }
});

test('explicit approved staging persists only owner-bound AEAD ciphertext; connection remains unready', async () => {
  const { env, db } = fixture();
  const enabled = grant(env);
  const fake = 'TESTONLYFAKEZOTEROKEY1234567890123';
  const logged = [];
  const originalLog = console.log;
  const originalError = console.error;
  console.log = (...args) => logged.push(args);
  console.error = (...args) => logged.push(args);
  let result;
  try {
    assert.equal((await call(enabled, '/api/zotero/connection')).data.credential_entry_enabled, true);
    result = await call(enabled, connectPath, { method: 'POST', body: { action: 'stage_credentials', api_key: fake } });
  } finally { console.log = originalLog; console.error = originalError; }
  assert.equal(result.response.status, 201);
  assert.equal(result.data.status, 'not_connected');
  assert.equal(result.data.ready, false);
  const [row] = db.rows('zotero_encrypted_credentials');
  assert.equal(row.state, 'encrypted_pending_verification');
  assert.equal(row.library_type, 'user');
  assert.equal(row.library_id, '123456');
  assert.equal(Buffer.from(row.iv_b64, 'base64').length, 12);
  assert.equal(db.rows('zotero_setup_grants_consumed').length, 1);
  assert.equal(JSON.stringify([db.calls, row, result.data, logged]).includes(fake), false);
  assert.equal(logged.length, 0);
  const key = await crypto.subtle.importKey('raw', Buffer.alloc(32, 7), { name: 'AES-GCM' }, false, ['decrypt']);
  const aad = { version: 'AES-256-GCM.v1', owner_id: 'owner-1', library_type: 'user',
    library_id: '123456', key_id: 'test-key-v1', grant_id: 'test-only-grant' };
  const plaintext = await crypto.subtle.decrypt({ name: 'AES-GCM', iv: Buffer.from(row.iv_b64, 'base64'),
    additionalData: new TextEncoder().encode(JSON.stringify(aad)), tagLength: 128 }, key, Buffer.from(row.ciphertext_b64, 'base64'));
  assert.equal(new TextDecoder().decode(plaintext), fake);
  await assert.rejects(() => crypto.subtle.decrypt({ name: 'AES-GCM', iv: Buffer.from(row.iv_b64, 'base64'),
    additionalData: new TextEncoder().encode(JSON.stringify({ ...aad, owner_id: 'owner-2' })), tagLength: 128 }, key, Buffer.from(row.ciphertext_b64, 'base64')));
  const status = (await call(enabled, '/api/zotero/connection')).data;
  assert.equal(status.credential_entry_enabled, false);
  assert.equal(status.status, 'not_connected');
  assert.equal(status.ready, false);
  assert.equal((await call(enabled, connectPath, { method: 'POST', body: { action: 'stage_credentials', api_key: fake } })).response.status, 409);
  const saved = await call(enabled, savePath, { method: 'POST', body: selection() });
  assert.equal(saved.data.state, 'blocked_connection');
  assert.equal(saved.data.executed, false);
});

test('credential payload cannot choose owner, library, URL, metadata, or a replacement credential', async () => {
  for (const extra of [{ owner_id: 'owner-2' }, { library_id: '999' }, { library_type: 'group' },
    { url: 'https://evil.example' }, { metadata: {} }, { replace: true }]) {
    const { env, db } = fixture();
    const result = await call(grant(env), connectPath, { method: 'POST', body: { action: 'stage_credentials', api_key: 'A'.repeat(32), ...extra } });
    assert.equal(result.response.status, 400);
    assert.equal(db.rows('zotero_encrypted_credentials').length, 0);
    assert.equal(db.rows('zotero_setup_grants_consumed').length, 0);
  }
});

test('credential batch failure rolls back grant consumption and never surfaces DB or request details', async () => {
  const { env, db } = fixture();
  db.sqlite.exec(`CREATE TRIGGER reject_test_credential BEFORE INSERT ON zotero_encrypted_credentials
    BEGIN SELECT RAISE(ABORT, 'private database error'); END;`);
  const result = await call(grant(env), connectPath, { method: 'POST', body: { action: 'stage_credentials', api_key: 'A'.repeat(32) } });
  assert.equal(result.response.status, 503);
  assert.equal(db.rows('zotero_setup_grants_consumed').length, 0);
  assert.equal(db.rows('zotero_encrypted_credentials').length, 0);
  assert.doesNotMatch(JSON.stringify(result.data), /private database error|AAAAA/);
});

test('default-disabled manager has no secret-read route or automatic legacy-intent execution hooks', async () => {
  const source = readFileSync(new URL('../worker/index.js', import.meta.url), 'utf8');
  const serverOnly = source.slice(0, source.indexOf('function panel')) + source.slice(source.indexOf('export function isZoteroRoute'));
  assert.doesNotMatch(serverOnly, /(?<![.A-Za-z])fetch\s*\((?!request, env)/);
  assert.doesNotMatch(source, /\bimport\s+.*transport|\bscheduled\s*\(|\bqueue\s*\(|console\./);
  const { env } = fixture();
  assert.equal((await call(env, '/api/zotero/credentials')).response.status, 404);
  assert.equal((await call(env, '/signin-with-chatgpt')).response.status, 503);
});


test('host router and existing static/archive assets are preserved without exposing Zotero routes', async () => {
  const { env } = fixture();
  assert.equal(await handleZoteroRequest(request('/reports/2026-10-09.html'), env), null);
  let assetsCalls = 0;
  const withAssets = { ...env, ASSETS: { fetch: async req => {
    assetsCalls++; return new Response('Existing archive: ' + new URL(req.url).pathname);
  } } };
  const archive = await worker.fetch(request('/reports/2026-10-09.html'), withAssets);
  assert.equal(await archive.text(), 'Existing archive: /reports/2026-10-09.html');
  assert.equal(assetsCalls, 1);
  assert.equal((await worker.fetch(request('/api/zotero/credentials'), withAssets)).status, 404);
  assert.equal((await worker.fetch(request('/zotero', { owner: null }), withAssets)).status, 401);
  assert.equal(assetsCalls, 1);
});


test('opaque DOI-shaped paper IDs with slash work only when present in the server catalog', async () => {
  const { env, db } = fixture();
  const paper = 'doi:10.55555/example';
  db.seedCatalog({ report: 'doi-report', catalog: 'doi-catalog', papers: [paper] });
  const body = { ...selection(), report_id: 'doi-report', catalog_id: 'doi-catalog', paper_ids: [paper] };
  const result = await call(env, savePath, { method: 'POST', body });
  assert.equal(result.response.status, 201);
  assert.deepEqual(result.data.paper_ids, [paper]);
  for (const invalid of ['doi:10.55555/other', 'doi:10.55555/../secret', 'https://example.test/paper', 'file:///tmp/paper', 'data:text/plain']) {
    assert.equal((await call(env, savePath, { method: 'POST', body: { ...body, paper_ids: [invalid] } })).response.status, 400);
  }
  assert.equal(db.rows('zotero_import_intents').length, 1);
});

test('a setup grant expiring during streamed credential entry cannot commit any state', async () => {
  const { env, db } = fixture();
  const enabled = grant(env, { expires_at: new Date(Date.now() + 100).toISOString() });
  const body = JSON.stringify({ action: 'stage_credentials', api_key: 'A'.repeat(32) });
  const stream = new ReadableStream({ start(controller) {
    setTimeout(() => { controller.enqueue(new TextEncoder().encode(body)); controller.close(); }, 160);
  } });
  const req = new Request(origin + connectPath, { method: 'POST', headers: request(connectPath, { method: 'POST' }).headers,
    body: stream, duplex: 'half' });
  const response = await worker.fetch(req, enabled);
  assert.equal(response.status, 409);
  assert.equal(req.bodyUsed, true);
  assert.equal((await response.json()).error, 'setup_required');
  assert.equal(db.rows('zotero_encrypted_credentials').length, 0);
  assert.equal(db.rows('zotero_setup_grants_consumed').length, 0);
});

test('temporary owner identity display needs verified private audience and gateway; never enrolls or touches DB', async () => {
  const { env, db } = fixture(); delete env.ZOTERO_OWNER_ID; delete env.DB;
  let response = await worker.fetch(request('/zotero/identity'), env);
  assert.equal(response.status, 503);
  env.ZOTERO_OWNER_BOOTSTRAP_ENABLED = 'approved'; env.SITES_OWNER_PRIVATE_AUDIENCE_VERIFIED = 'owner-private';
  response = await worker.fetch(request('/zotero/identity', { owner: 'site-scoped-current-visitor' }), env);
  assert.equal(response.status, 200); const body = await response.text();
  assert.match(body, /site-scoped-current-visitor/); assert.match(body, /只读/);
  assert.equal(response.headers.get('Content-Type'), 'text/plain; charset=utf-8');
  assert.equal(response.headers.get('Cache-Control'), 'no-store, private');
  assert.equal(response.headers.get('Access-Control-Allow-Origin'), null);
  assert.equal(env.ZOTERO_OWNER_ID, undefined); assert.equal(db.calls.length, 0);
  assert.equal((await worker.fetch(request('/zotero'), env)).status, 503);
});

test('owner identity display rejects missing/spoofed context, cross-site access, mutations and disabled audience assertion', async () => {
  const { env } = fixture(); Object.assign(env, { ZOTERO_OWNER_BOOTSTRAP_ENABLED:'approved', SITES_OWNER_PRIVATE_AUDIENCE_VERIFIED:'owner-private' });
  for (const options of [{owner:null}, {owner:'owner-2'}, {headers:{'Sec-Fetch-Site':'cross-site'}}, {headers:{Origin:'https://attacker.example'}}, {method:'POST',body:{owner_id:'new-owner'}}]) {
    const response=await worker.fetch(request('/zotero/identity',options),env);assert.ok([401,403,405].includes(response.status));
  }
  for(const [key,value] of [['SITES_OWNER_PRIVATE_AUDIENCE_VERIFIED','public'],['SITES_GATEWAY_IDENTITY_TRUSTED',undefined],['ZOTERO_OWNER_BOOTSTRAP_ENABLED',undefined]]) {
    const response=await worker.fetch(request('/zotero/identity'),{...env,[key]:value});assert.equal(response.status,503);
  }
  assert.equal((await worker.fetch(request('/zotero/identity?owner_id=new'),env)).status,403);
});
