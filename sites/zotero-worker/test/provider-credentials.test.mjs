// Dummy keys and in-memory SQLite only. No live credentials or network.
import test from 'node:test';
import assert from 'node:assert/strict';
import { handleZoteroRequest } from '../worker/index.js';
import { MockD1 } from './mock-d1.mjs';
const origin = 'https://daily.example.test', owner = 'dummy-owner', library = '123456';
const key = 'SYNTHETICPROVIDERKEY123456';
const metadata = () => ({ userID: Number(library), username: 'NEVER_EXPOSE_USERNAME', key,
  access: { user: { library: true, write: true, files: true, notes: false }, groups: {} } });
const response = (body, status = 200, headers = {}) => new Response(JSON.stringify(body), { status, headers });
function fixture(db = new MockD1()) {
  const env = { DB: db, BUCKET: { get: async () => null }, SITES_GATEWAY_IDENTITY_TRUSTED: 'platform-managed',
    ZOTERO_OWNER_ID: owner, ZOTERO_SITE_ORIGIN: origin, ZOTERO_CREDENTIAL_STORAGE: 'provider-managed-d1',
    ZOTERO_CREDENTIAL_ENTRY_ENABLED: 'approved' };
  const requests = [];
  let upstream = async () => response(metadata());
  const fetchImpl = async (url, init) => { requests.push({ url, method: init.method });
    assert.equal(url, 'https://api.zotero.org/keys/current'); assert.equal(init.method, 'GET');
    assert.match(init.headers['Zotero-API-Key'], /^SYNTHETIC/); assert.equal(init.body, undefined);
    assert.equal(init.redirect, 'manual'); assert.equal(init.credentials, 'omit');
    return upstream(url, init);
  };
  const call = async (path, { body, headers = {}, identity = owner, method = body ? 'POST' : 'GET' } = {}) => {
    const all = { 'oai-authenticated-user-id': identity, ...(method === 'POST' ? { Origin: origin, 'X-Zotero-Action': '1', 'Content-Type': 'application/json' } : {}), ...headers };
    for (const [k,v] of Object.entries(all)) if (v === null) delete all[k];
    const result = await handleZoteroRequest(new Request(origin + path, { method, headers: all, body: body ? JSON.stringify(body) : undefined }), env, { fetchImpl });
    const text = await result.text();
    assert.equal(text.includes(key), false); assert.equal(text.includes('NEVER_EXPOSE_USERNAME'), false);
    return { status: result.status, text, data: result.headers.get('Content-Type').includes('application/json') ? JSON.parse(text) : null };
  };
  const connect = (changes = {}) => call('/api/zotero/connect', { body: { action: 'connect_personal_library', library_id: library, api_key: key, ...changes } });
  const disconnect = () => call('/api/zotero/disconnect', { body: { action: 'disconnect' } });
  const verify = () => call('/api/zotero/verify-connection', { body: { action: 'verify_connection' } });
  return { db, env, requests, call, connect, disconnect, verify, setUpstream(fn) { upstream = fn; } };
}
test('provider entry verifies before persistence, uses header only, needs no encryption secret or execution gate', async () => {
  const f = fixture();
  f.setUpstream(async () => { assert.equal(f.db.rows('zotero_provider_credentials').length, 0); assert.equal(f.db.rows('zotero_active_connections').length, 0); return response(metadata()); });
  const initial = await f.call('/api/zotero/connection'); assert.equal(initial.data.credential_entry_enabled, true);
  const result = await f.connect(); assert.equal(result.status, 201, result.text);
  assert.equal(result.data.ready, true); assert.equal(result.data.live_ready, false); assert.equal(result.data.status, 'connected');
  assert.equal(result.data.credential_storage, 'provider_managed_d1'); assert.equal(f.requests.length, 1);
  assert.equal(f.db.rows('zotero_provider_credentials')[0].api_key, key);
  assert.equal(f.db.rows('zotero_encrypted_credentials').length, 0);
  assert.equal(f.db.rows('zotero_execution_jobs').length, 0);
});
test('persistent binding survives new application context; status/history/page never read plaintext or call Zotero', async () => {
  const first = fixture(); await first.connect(); const fresh = fixture(first.db); fresh.db.calls.length = 0;
  for (const path of ['/api/zotero/connection', '/api/zotero/executions', '/api/zotero/catalogs', '/zotero']) assert.equal((await fresh.call(path)).status, 200);
  assert.equal((await fresh.call('/api/zotero/connection')).data.ready, true); assert.equal(fresh.requests.length, 0);
  assert.ok(fresh.db.calls.filter(c => /SELECT/i.test(c.sql)).every(c => !/SELECT\s+api_key|SELECT\s+\*/i.test(c.sql)));
  fresh.env.ZOTERO_EXECUTION_ENABLED = 'approved'; assert.equal((await fresh.call('/api/zotero/connection')).data.live_ready, true);
});
test('duplicate verified connect and reverify preserve same binding/version without jobs', async () => {
  const f = fixture(); await f.connect(); const first = f.db.rows('zotero_active_connections')[0];
  assert.equal((await f.connect()).status, 200); assert.equal((await f.verify()).status, 200);
  const after = f.db.rows('zotero_active_connections')[0]; assert.equal(after.connection_version, first.connection_version); assert.equal(after.credential_id, first.credential_id);
  assert.equal(f.db.rows('zotero_execution_jobs').length, 0);
});
test('different key requires disconnect; reconnect advances version; library cannot silently change', async () => {
  const f = fixture(); await f.connect(); const first = f.db.rows('zotero_active_connections')[0];
  assert.equal((await f.connect({ api_key: 'SYNTHETICNEWPROVIDERKEY5678' })).data.error, 'disconnect_required');
  assert.equal((await f.disconnect()).status, 200); assert.equal(f.db.rows('zotero_provider_credentials').length, 0);
  const result = await f.connect({ api_key: 'SYNTHETICNEWPROVIDERKEY5678' }); assert.equal(result.status, 201, result.text);
  const second = f.db.rows('zotero_active_connections')[0]; assert.equal(second.connection_version, first.connection_version + 1); assert.notEqual(second.credential_id, first.credential_id);
  await f.disconnect(); const count = f.requests.length;
  assert.equal((await f.connect({ library_id: '999999' })).data.error, 'library_rebind_required'); assert.equal(f.requests.length, count);
});
test('disconnect during first delayed verification fences its commit; no credential or active binding survives', async () => {
  const f = fixture(); f.setUpstream(async () => { await f.disconnect(); return response(metadata()); });
  const result = await f.connect(); assert.equal(result.status, 409, result.text);
  assert.equal(f.db.rows('zotero_provider_credentials').length, 0); assert.equal(f.db.rows('zotero_active_connections').length, 0);
});
test('disconnect during repeat connect and reverify cannot revive deleted credentials', async () => {
  for (const method of ['connect', 'verify']) {
    const f = fixture(); await f.connect(); f.setUpstream(async () => { await f.disconnect(); return response(metadata()); });
    assert.equal((await f[method]()).status, 409); assert.equal(f.db.rows('zotero_provider_credentials').length, 0);
    assert.equal(f.db.rows('zotero_active_connections')[0].state, 'revoked');
  }
});
test('concurrent enrollment cannot overwrite accepted credential or increment its version twice', async () => {
  const f = fixture(); const outcomes = await Promise.all([f.connect(), f.connect()]);
  assert.equal(outcomes.filter(r => r.status === 201).length, 1); assert.equal(f.db.rows('zotero_provider_credentials').length, 1);
  assert.equal(f.db.rows('zotero_active_connections')[0].connection_version, 1);
});
for (const [name, mutate] of [
  ['wrong owner', m => m.userID = 999], ['missing read', m => m.access.user.library = false],
  ['missing write', m => delete m.access.user.write], ['missing files', m => m.access.user.files = false],
  ['notes true', m => m.access.user.notes = true], ['notes malformed', m => m.access.user.notes = 'false'],
  ['group access', m => m.access.groups = { 123: { library: true, write: true } }],
  ['all groups', m => m.access.groups = { all: { library: true } }],
  ['unknown user scope', m => m.access.user.admin = true], ['unknown top-level scope', m => m.access.everything = true],
]) test('unapproved scope rejected before persistence: ' + name, async () => {
  const f = fixture(); const m = metadata(); mutate(m); f.setUpstream(async () => response(m));
  const result = await f.connect(); assert.equal(result.status, 409, result.text);
  assert.equal(f.db.rows('zotero_provider_credentials').length, 0); assert.equal(f.db.rows('zotero_active_connections').length, 0);
});
test('omitted notes and groups represent no extra grant', async () => {
  const f = fixture(); const m = metadata(); delete m.access.user.notes; delete m.access.groups;
  f.setUpstream(async () => response(m)); assert.equal((await f.connect()).status, 201);
});
test('revoked/expanded key fails closed and deactivates existing ready binding', async () => {
  for (const failure of [() => response({ secret: key }, 403), () => { const m = metadata(); m.access.user.notes = true; return response(m); }]) {
    const f = fixture(); await f.connect(); f.setUpstream(async () => failure());
    const result = await f.verify(); assert.ok([403,409].includes(result.status)); assert.equal((await f.call('/api/zotero/connection')).data.ready, false);
  }
});
test('backoff survives failed verification and repeated actions do not bypass it', async () => {
  const f = fixture(); f.setUpstream(async () => response({ secret: key }, 429, { 'Retry-After': '60' }));
  const first = await f.connect(); assert.equal(first.data.error, 'rate_limited'); assert.equal(first.data.retry_after_ms, 60000);
  assert.equal((await f.connect()).data.error, 'backoff_required'); assert.equal(f.requests.length, 1);
});
test('authorization, origins, content type, action header, and global query denial precede secret processing', async () => {
  const f = fixture(); const body = { action: 'connect_personal_library', library_id: library, api_key: key };
  for (const options of [{identity:null},{identity:'other'},{headers:{Origin:'https://evil.test'}},{headers:{'Sec-Fetch-Site':'cross-site'}},{headers:{'X-Zotero-Action':null}},{headers:{'Content-Type':'text/plain'}}])
    assert.ok((await f.call('/api/zotero/connect',{body,...options})).status >= 400);
  for (const path of ['/zotero?api_key=dummy','/api/zotero/connection?api_key=dummy','/api/zotero/connect?api_key=dummy']) assert.equal((await f.call(path,{body})).status,400);
  assert.equal(f.requests.length,0); assert.equal(f.db.rows('zotero_provider_credentials').length,0);
});
test('entry disabled and invalid library/body never call Zotero or store key', async () => {
  const f = fixture(); f.env.ZOTERO_CREDENTIAL_ENTRY_ENABLED = 'off'; assert.equal((await f.connect()).status,409);
  f.env.ZOTERO_CREDENTIAL_ENTRY_ENABLED = 'approved';
  for(const library_id of ['', 'abc', '01', '0', '9007199254740992', 123456, 'https://zotero.org/users/1']) assert.equal((await f.connect({library_id})).status,400);
  assert.equal((await f.connect({ extra: true })).status,400); assert.equal(f.requests.length,0);
});
test('raw upstream failures/redirects are sanitized; no raw secret, username, URL or HTML escapes', async () => {
  for(const failure of [() => response({secret:key},500), () => {throw Error(key);}, () => new Response(null,{status:302,headers:{Location:'https://evil.test/'+key}})]) {
    const f=fixture(); f.setUpstream(async()=>failure()); const result=await f.connect(); assert.ok(result.status>=400); assert.equal(result.text.includes('evil.test'),false); assert.equal(f.db.rows('zotero_provider_credentials').length,0);
  }
});

test('inconsistent persisted key/fingerprint fails closed before any outbound reverify',async()=>{
 const f=fixture();await f.connect();f.db.sqlite.prepare('UPDATE zotero_provider_credentials SET api_key=?').run('SYNTHETICREPLACEDKEY567890');const before=f.requests.length;
 const result=await f.verify();assert.equal(result.status,409);assert.equal(result.data.error,'credential_unavailable');assert.equal(f.requests.length,before);
});
