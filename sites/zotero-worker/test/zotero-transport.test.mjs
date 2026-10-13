import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { createZoteroTransport, ZoteroTransportError } from '../src/zotero-transport.mjs';

// All transport traffic is a closed local mock. No native/global fetch is used.
const OWNER = '999999991';
const FAKE_KEY = 'FAKE_TEST_KEY_DO_NOT_USE';
const API = `https://api.zotero.org/users/${OWNER}`;
const STORAGE = 'https://zoterofilestorage.s3.us-east-1.amazonaws.com/mock-only';
const ITEM = 'ABCD2345';
const PARENT = 'PARENT23';
const ATTACHMENT = 'ATTACH23';
const COLLECTION = 'CLLN2345';
const PDF = new TextEncoder().encode('%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF\n');
const hashBytes = async bytes => ({ md5: createHash('md5').update(bytes).digest('hex'), sha256: createHash('sha256').update(bytes).digest('hex') });
const HASHES = await hashBytes(PDF);
const response = (value, status = 200, headers = {}) => new Response(status === 204 ? null : JSON.stringify(value), {
  status, headers: { 'Content-Type': 'application/json', ...headers },
});
const grant = overrides => response({ userID: Number(OWNER), key: FAKE_KEY, username: 'not returned',
  access: { user: { library: true, write: true, files: true, notes: false, ...overrides } } });
const object = (key = ITEM, version = 8, data = {}) => ({ key, version, library: { type: 'user', id: Number(OWNER) },
  data: { key, version, itemType: 'journalArticle', title: 'Mock paper', collections: [], tags: [], relations: {}, ...data } });
const one = (key, version, data, libraryVersion = version) => response([object(key, version, data)], 200,
  { 'Last-Modified-Version': String(libraryVersion), 'Total-Results': '1' });
const page = (values, version = 8, total = values.length) => response(values, 200, {
  'Last-Modified-Version': String(version), 'Total-Results': String(total),
});
function fixture(steps = [], options = {}) {
  const calls = [];
  let time = 100000;
  const sleeps = [];
  const pending = [...steps];
  const transport = createZoteroTransport({ userID: OWNER, apiKey: FAKE_KEY, hashBytes,
    now: () => time, sleep: async ms => { sleeps.push(ms); time += ms; },
    fetchImpl: async (url, init) => {
      calls.push({ url, ...init });
      assert.ok(pending.length, 'Mock response queue must contain every requested response');
      const step = pending.shift();
      return typeof step === 'function' ? step(url, init) : step;
    }, ...options });
  return { transport, calls, sleeps, pending, advance: ms => { time += ms; } };
}
async function authorized(steps = [], options = {}) {
  const f = fixture([grant(), ...steps], options);
  await f.transport.verifyCurrentKey();
  return f;
}
async function rejectsCode(promise, code, extra = {}) {
  await assert.rejects(promise, error => {
    assert.ok(error instanceof ZoteroTransportError);
    assert.equal(error.code, code);
    assert.equal(error.message, code);
    for (const [k, v] of Object.entries(extra)) assert.equal(error[k], v);
    return true;
  });
}
function attachmentData(saved = false) {
  return { itemType: 'attachment', parentItem: PARENT, linkMode: 'imported_file', contentType: 'application/pdf',
    filename: 'mock.pdf', md5: saved ? HASHES.md5 : null, mtime: saved ? 1700000000000 : null };
}
const attachment = saved => one(ATTACHMENT, saved ? 9 : 8, attachmentData(saved));
const authorization = (url = STORAGE) => response({ url, contentType: 'multipart/form-data; boundary=mock-only',
  prefix: 'FAKE-MULTIPART-PREFIX\r\n', suffix: '\r\nFAKE-MULTIPART-SUFFIX', uploadKey: 'FAKE_UPLOAD_TOKEN' });
const uploadInput = () => ({ itemKey: ATTACHMENT, parentItem: PARENT, bytes: PDF, filename: 'mock.pdf', mtime: 1700000000000 });
const redirect = (target = STORAGE) => new Response(null, { status: 302, headers: { Location: target } });
const download = (bytes = PDF, etag = HASHES.md5) => new Response(bytes, { headers: { ETag: `"${etag}"`, 'Content-Length': String(bytes.length) } });
const verifySequence = () => [attachment(true), redirect(), download(), attachment(true)];

test('constructor requires explicit fetch, strict personal ID and credentials without issuing requests', () => {
  assert.throws(() => createZoteroTransport(), { code: 'FETCH_ADAPTER_REQUIRED' });
  assert.throws(() => createZoteroTransport({ fetchImpl() {}, userID: OWNER }), { code: 'MISSING_OR_INVALID_CREDENTIAL' });
  for (const userID of ['../groups/7', '0', '0012', '-7', '1?key=secret', '9007199254740992']) {
    assert.throws(() => createZoteroTransport({ fetchImpl() {}, userID, apiKey: FAKE_KEY }), { code: 'INVALID_USER_ID' });
  }
  const f = fixture();
  assert.equal(f.calls.length, 0);
  assert.equal(JSON.stringify(f.transport), '{}');
});

test('permissions require verified owner and grants; key data is never returned', async () => {
  const f = fixture([grant()]);
  await rejectsCode(f.transport.getItem(ITEM), 'PERMISSIONS_UNVERIFIED');
  const permissions = await f.transport.verifyCurrentKey();
  assert.deepEqual(permissions, { userID: OWNER, library: true, write: true, files: true, notes: false });
  assert.equal(f.calls[0].url, 'https://api.zotero.org/keys/current');
  assert.equal(f.calls[0].headers['Zotero-API-Key'], FAKE_KEY);
  assert.equal(f.calls[0].headers['Zotero-API-Version'], '3');
  assert.equal(f.calls[0].redirect, 'manual');
  assert.equal(f.calls[0].credentials, 'omit');
  assert.equal(JSON.stringify(permissions).includes(FAKE_KEY), false);
  f.advance(300000);
  await rejectsCode(f.transport.getItem(ITEM), 'PERMISSIONS_UNVERIFIED');
});

test('wrong owner and missing required grants fail closed; notes access is not required', async () => {
  const bad = fixture([response({ userID: '1', access: { user: {} } })]);
  await rejectsCode(bad.transport.verifyCurrentKey(), 'KEY_OWNER_MISMATCH');
  for (const field of ['library', 'write', 'files']) {
    const f = fixture([grant({ [field]: false })]);
    await rejectsCode(f.transport.verifyCurrentKey(), 'INSUFFICIENT_PERMISSION');
  }
  const notesOptIn = fixture([grant()]);
  await rejectsCode(notesOptIn.transport.verifyCurrentKey({ requireNotes: true }), 'INSUFFICIENT_PERMISSION');
  for (const notes of [false, undefined]) {
    const minimal = fixture([grant({ notes })]);
    assert.equal((await minimal.transport.verifyCurrentKey()).notes, false);
  }
  const readOnly = fixture([grant({ write: false, files: false, notes: false }), page([]), page([]),
    response({}, 200, { 'Last-Modified-Version': '8' }), response({}, 200, { 'Last-Modified-Version': '8' })]);
  await readOnly.transport.verifyCurrentKey({ requireWrite: false, requireFiles: false, requireNotes: false });
  await rejectsCode(readOnly.transport.createItem({ key: ITEM, data: { itemType: 'book' } }), 'INSUFFICIENT_PERMISSION');
  assert.equal((await readOnly.transport.inventory()).excludesNotes, true);
});

test('filtered item fetch validates strict keys, target owner and object/library versions', async () => {
  const f = await authorized([one(ITEM, 8, {}, 10), page([], 10)]);
  await rejectsCode(f.transport.getItem('../file'), 'INVALID_OBJECT_KEY');
  const item = await f.transport.getItem(ITEM);
  assert.equal(item.key, ITEM);
  assert.equal(f.calls[1].url, `${API}/items?format=json&include=data&itemKey=${ITEM}&includeTrashed=1&itemType=-note%20%7C%7C%20annotation`);
  assert.equal(await f.transport.getItem(ITEM), null);
  for (const bad of [object(ITEM, 8, { key: 'WRNG2345' }), { ...object(), library: { type: 'group', id: 3 } }]) {
    const invalid = await authorized([page([bad])]);
    await rejectsCode(invalid.transport.getItem(ITEM), 'INVALID_OBJECT_RESPONSE');
  }
  for (const invalidResponse of [page([object(ITEM, 9)], 8), page([object(ITEM)], 8, 2),
    page([object(ITEM), object('EFGH6789')]), response(object(ITEM), 200, { 'Last-Modified-Version': '8', 'Total-Results': '1' })]) {
    const invalid = await authorized([invalidResponse]);
    await rejectsCode(invalid.transport.getItem(ITEM), 'INVALID_OBJECT_RESPONSE');
  }
});

test('complete paginated inventory includes trash, collections and independent key/version proof', async () => {
  const second = 'EFGH6789';
  const f = await authorized([
    page([object(ITEM)], 8, 2), page([object(second, 7, { deleted: 1 })], 8, 2),
    page([object(COLLECTION, 6, { name: 'Mock collection', parentCollection: false })], 8),
    response({ [ITEM]: 8, [second]: 7 }, 200, { 'Last-Modified-Version': '8' }),
    response({ [COLLECTION]: 6 }, 200, { 'Last-Modified-Version': '8' }),
  ], { pageSize: 1 });
  const result = await f.transport.inventory();
  assert.equal(result.complete, true);
  assert.equal(result.includesTrashed, true);
  assert.equal(result.excludesNotes, true);
  assert.equal(result.libraryVersion, 8);
  assert.equal(result.items.length, 2);
  assert.equal(result.collections.length, 1);
  assert.ok(f.calls[1].url.includes('includeTrashed=1'));
  assert.ok(f.calls[2].url.includes('start=1'));
  for (const call of f.calls.filter(call => new URL(call.url).pathname === `/users/${OWNER}/items`)) {
    const params = new URL(call.url).searchParams;
    assert.equal(params.get('includeTrashed'), '1');
    assert.deepEqual(params.getAll('itemType'), ['-note || annotation']);
    assert.ok(['json', 'versions'].includes(params.get('format')));
  }
  assert.equal(f.pending.length, 0);
});

test('note/annotation filter violations fail closed for inventory and individual item reads', async () => {
  for (const itemType of ['note', 'annotation']) {
    for (const operation of ['inventory', 'getItem']) {
      const f = fixture([grant({ notes: true }), page([object(ITEM, 8, { itemType, note: 'PRIVATE_NOTE_DO_NOT_EXPOSE' })])]);
      await f.transport.verifyCurrentKey();
      await rejectsCode(f.transport[operation](ITEM), 'EXCLUDED_ITEM_RESPONSE');
      assert.equal(new URL(f.calls[1].url).searchParams.get('itemType'), '-note || annotation');
      assert.equal(f.calls.length, 2);
    }
  }
});

test('legacy embedded attachment notes are discarded at the boundary for both grant modes', async () => {
  for (const notes of [false, true]) {
    const data = { ...attachmentData(true), note: 'PRIVATE_EMBEDDED_NOTE', notes: ['PRIVATE_NESTED_NOTE'],
      annotationComment: 'PRIVATE_COMMENT' };
    const f = fixture([grant({ notes }), one(ATTACHMENT, 9, data), page([object(ATTACHMENT, 9, data)], 9), page([], 9),
      response({ [ATTACHMENT]: 9 }, 200, { 'Last-Modified-Version': '9' }),
      response({}, 200, { 'Last-Modified-Version': '9' })]);
    await f.transport.verifyCurrentKey();
    const item = await f.transport.getItem(ATTACHMENT);
    const inventory = await f.transport.inventory();
    for (const result of [item, inventory]) assert.equal(JSON.stringify(result).includes('PRIVATE_'), false);
    assert.equal(item.data.md5, HASHES.md5);
    assert.equal(item.data.parentItem, PARENT);
    assert.equal(inventory.items[0].data.itemType, 'attachment');
    assert.equal(inventory.excludesNotes, true);
  }
});

test('a filtered absent note-like key never triggers an unfiltered fallback read', async () => {
  const f = fixture([grant({ notes: true }), page([])]);
  await f.transport.verifyCurrentKey();
  assert.equal(await f.transport.getItem(ITEM), null);
  assert.equal(f.calls.length, 2);
  assert.equal(new URL(f.calls[1].url).searchParams.get('itemKey'), ITEM);
  assert.equal(new URL(f.calls[1].url).searchParams.get('itemType'), '-note || annotation');
});

test('bibliographic creation refuses private note fields without issuing a write', async () => {
  const f = await authorized();
  for (const field of ['note', 'notes', 'annotationComment']) {
    await rejectsCode(f.transport.createItem({ key: ITEM, data: { itemType: 'book', [field]: 'private' } }), 'INVALID_ITEM_DATA');
  }
  assert.equal(f.calls.length, 1);
});

test('incomplete inventory never returns complete: short pages, duplicate keys, missing totals and key proof', async () => {
  const variants = [
    [page([object()], 8, 2)],
    [page([object(), object()], 8, 2)],
    [response([object()], 200, { 'Last-Modified-Version': '8' })],
    [page([object()]), page([]), response({}, 200, { 'Last-Modified-Version': '8' })],
  ];
  for (const steps of variants) {
    const f = await authorized(steps);
    await rejectsCode(f.transport.inventory(), 'INCOMPLETE_INVENTORY');
  }
});

test('inventory detects version drift across pages, collections and terminal proof', async () => {
  for (const steps of [
    [page([object()], 8, 2), page([object('EFGH6789')], 9, 2)],
    [page([object()]), page([], 9)],
    [page([object()]), page([]), response({ [ITEM]: 8 }, 200, { 'Last-Modified-Version': '9' })],
  ]) {
    const f = await authorized(steps, { pageSize: 1 });
    await rejectsCode(f.transport.inventory(), 'INVENTORY_CHANGED');
  }
});

test('inventory ignores hostile Link URLs and uses fixed-origin local pagination', async () => {
  const malicious = page([]);
  malicious.headers.set('Link', '<https://attacker.invalid/?key=secret>; rel="next"');
  const f = await authorized([malicious, page([]), response({}, 200, { 'Last-Modified-Version': '8' }), response({}, 200, { 'Last-Modified-Version': '8' })]);
  assert.equal((await f.transport.inventory()).complete, true);
  assert.ok(f.calls.every(call => call.url.startsWith('https://api.zotero.org/')));
});

test('inventory and JSON body bounds stop oversized data', async () => {
  const f = await authorized([page([object(ITEM, 8, { title: 'x'.repeat(2000) })])], { maxInventoryBytes: 1024 });
  await rejectsCode(f.transport.inventory(), 'INVENTORY_TOO_LARGE');
  const oversize = await authorized([new Response('x'.repeat(2048), { headers: { 'Last-Modified-Version': '8', 'Total-Results': '1' } })], { maxJsonBytes: 1024 });
  await rejectsCode(oversize.transport.getItem(ITEM), 'RESPONSE_TOO_LARGE');
});

test('version-zero create uses caller key, checks per-object success, and never sends a write token', async () => {
  const f = await authorized([(_url, init) => {
    const [sent] = JSON.parse(init.body);
    assert.equal(sent.key, ITEM);
    assert.equal(sent.version, 0);
    assert.equal(init.headers['Zotero-Write-Token'], undefined);
    assert.equal(init.headers['If-Unmodified-Since-Version'], undefined);
    return response({ successful: { 0: object(ITEM, 9, { ...sent, version: 9 }) }, failed: {}, unchanged: {} }, 200, { 'Last-Modified-Version': '9' });
  }]);
  const saved = await f.transport.createItem({ key: ITEM, data: { itemType: 'book', title: 'Created with mocks' } });
  assert.equal(saved.version, 9);
  assert.equal(saved.key, ITEM);
});

test('HTTP 200 per-object rejection does not masquerade as success or leak server messages', async () => {
  const f = await authorized([response({ successful: {}, unchanged: {}, failed: { 0: { code: 400,
    message: `${FAKE_KEY} https://secret.invalid/?credential=secret` } } }, 200, { 'Last-Modified-Version': '8' })]);
  let error;
  try { await f.transport.createItem({ key: ITEM, data: { itemType: 'book' } }); } catch (e) { error = e; }
  assert.equal(error.code, 'OBJECT_WRITE_FAILED');
  assert.equal(error.uncertain, false);
  assert.equal(JSON.stringify(error).includes(FAKE_KEY), false);
  assert.equal(error.stack.includes('secret.invalid'), false);
});

test('duplicate, per-object version conflict, malformed success and mismatched keys fail closed', async () => {
  const variants = [
    [response({ successful: {}, unchanged: { 0: ITEM }, failed: {} }, 200, { 'Last-Modified-Version': '8' }), 'CREATE_ALREADY_EXISTS'],
    [response({ successful: {}, unchanged: {}, failed: { 0: { code: 412, message: 'exists' } } }, 200, { 'Last-Modified-Version': '8' }), 'WRITE_CONFLICT'],
    [response({ successful: {}, unchanged: {}, failed: {} }, 200, { 'Last-Modified-Version': '8' }), 'INVALID_WRITE_RESPONSE'],
    [response({ successful: { 0: object('EFGH6789') }, unchanged: {}, failed: {} }, 200, { 'Last-Modified-Version': '8' }), 'INVALID_OBJECT_RESPONSE'],
  ];
  for (const [res, code] of variants) {
    const f = await authorized([res]);
    await rejectsCode(f.transport.createItem({ key: ITEM, data: { itemType: 'book' } }), code);
    assert.equal(f.calls.length, 2);
  }
});

test('writes never auto-retry on network failure, 412, 429 or 503', async () => {
  for (const [step, code, uncertain] of [
    [() => { throw new Error(`${FAKE_KEY} private-url`); }, 'NETWORK_FAILURE', true],
    [response({ raw: FAKE_KEY }, 412), 'WRITE_CONFLICT', false],
    [response({}, 429, { 'Retry-After': '12' }), 'RATE_LIMITED', false],
    [response({ raw: FAKE_KEY }, 503), 'SERVICE_UNAVAILABLE', true],
  ]) {
    const f = await authorized([step]);
    await rejectsCode(f.transport.createItem({ key: ITEM, data: { itemType: 'book' } }), code, { uncertain });
    assert.equal(f.calls.length, 2);
  }
});

test('malformed success and post-write mismatch report uncertain effects', async () => {
  for (const [step, code] of [
    [new Response('not-json', { status: 200, headers: { 'Last-Modified-Version': '9' } }), 'INVALID_JSON_RESPONSE'],
    [response({}, 200), 'INVALID_RESPONSE'],
    [response({}, 200, { Backoff: FAKE_KEY, 'Last-Modified-Version': '9' }), 'INVALID_BACKOFF'],
    [response({ successful: { 0: {} }, unchanged: {}, failed: {} }, 200, { 'Last-Modified-Version': '9' }), 'INVALID_OBJECT_RESPONSE'],
    [response({}, 503, { Backoff: 'malformed' }), 'INVALID_BACKOFF'],
    [() => undefined, 'INVALID_RESPONSE'],
  ]) {
    const f = await authorized([step]);
    await rejectsCode(f.transport.createItem({ key: ITEM, data: { itemType: 'book' } }), code, { uncertain: true });
  }
});

test('collection creation includes caller-persisted key and parent', async () => {
  const f = await authorized([(_url, init) => {
    const [sent] = JSON.parse(init.body);
    assert.deepEqual(sent, { key: COLLECTION, version: 0, name: 'Mock folder', parentCollection: 'PRNT2345' });
    return response({ successful: { 0: object(COLLECTION, 9, { ...sent, version: 9 }) }, failed: {}, unchanged: {} }, 200, { 'Last-Modified-Version': '9' });
  }]);
  assert.equal((await f.transport.createCollection({ key: COLLECTION, name: 'Mock folder', parentCollection: 'PRNT2345' })).key, COLLECTION);
  await rejectsCode(f.transport.createCollection({ key: COLLECTION, name: 'Mock', parentCollection: COLLECTION }), 'INVALID_COLLECTION_PARENT');
});

test('conditional PATCH sends only complete collections/tags and verifies readback', async () => {
  const tags = [{ tag: 'old tag' }, { tag: 'new tag', type: 0 }];
  const collections = ['PRNT2345', COLLECTION];
  const f = await authorized([(_url, init) => {
    assert.equal(init.method, 'PATCH');
    assert.equal(init.headers['If-Unmodified-Since-Version'], '8');
    assert.deepEqual(JSON.parse(init.body), { tags, collections });
    return response(null, 204, { 'Last-Modified-Version': '9' });
  }, one(ITEM, 9, { collections, tags })]);
  assert.equal((await f.transport.patchItemMembership({ itemKey: ITEM, expectedVersion: 8, collections, tags })).version, 9);
});

test('PATCH conflict never falls back to overwriting metadata', async () => {
  const f = await authorized([response({}, 412)]);
  await rejectsCode(f.transport.patchItemMembership({ itemKey: ITEM, expectedVersion: 8, tags: [] }), 'WRITE_CONFLICT');
  assert.equal(f.calls.length, 2);
  const changed = await authorized([response(null, 204, { 'Last-Modified-Version': '9' }), one(ITEM, 10)]);
  await rejectsCode(changed.transport.patchItemMembership({ itemKey: ITEM, expectedVersion: 8, tags: [] }), 'PATCH_READBACK_MISMATCH', { uncertain: true });
});

test('PATCH 204 followed by failed GET retains mutation uncertainty', async () => {
  for (const [step, code] of [
    [() => { throw new Error('mock network failure'); }, 'NETWORK_FAILURE'],
    [new Response('not-json', { headers: { 'Last-Modified-Version': '9', 'Total-Results': '1' } }), 'INVALID_JSON_RESPONSE'],
  ]) {
    const f = await authorized([response(null, 204, { 'Last-Modified-Version': '9' }), step]);
    await rejectsCode(f.transport.patchItemMembership({ itemKey: ITEM, expectedVersion: 8, tags: [] }), code, { uncertain: true });
    assert.equal(f.calls.filter(c => c.method === 'PATCH').length, 1);
  }
});

test('stored attachment is a version-zero imported PDF child with null storage metadata', async () => {
  const f = await authorized([(_url, init) => {
    const [sent] = JSON.parse(init.body);
    assert.equal(sent.key, ATTACHMENT);
    assert.equal(sent.parentItem, PARENT);
    assert.equal(sent.linkMode, 'imported_file');
    assert.equal(sent.contentType, 'application/pdf');
    assert.equal(sent.md5, null);
    assert.equal(sent.mtime, null);
    assert.equal(sent.version, 0);
    return response({ successful: { 0: object(ATTACHMENT, 9, { ...sent, version: 9, note: 'PRIVATE_INCIDENTAL_FIELD' }) }, failed: {}, unchanged: {} }, 200, { 'Last-Modified-Version': '9' });
  }]);
  const result = await f.transport.createStoredAttachment({ key: ATTACHMENT, parentItem: PARENT, title: 'Mock PDF', filename: 'mock.pdf' });
  assert.equal(result.key, ATTACHMENT);
  assert.equal(JSON.stringify(result).includes('PRIVATE_INCIDENTAL_FIELD'), false);
  assert.equal(Object.hasOwn(result.data, 'note'), false);
});

test('full PDF pipeline authorizes, transfers without API key, registers and verifies actual stored bytes', async () => {
  const f = await authorized([attachment(false), (_url, init) => {
    assert.equal(init.headers['If-None-Match'], '*');
    const form = new URLSearchParams(init.body);
    assert.equal(form.get('filesize'), String(PDF.length));
    assert.equal(form.get('md5'), HASHES.md5);
    assert.equal(form.get('mtime'), '1700000000000');
    return authorization();
  }, async (url, init) => {
    assert.equal(url, STORAGE);
    assert.deepEqual(Object.keys(init.headers), ['Content-Type']);
    assert.equal(init.headers['Zotero-API-Key'], undefined);
    assert.equal(init.headers.Authorization, undefined);
    assert.equal(init.headers['Zotero-API-Version'], undefined);
    const body = new Uint8Array(await init.body.arrayBuffer());
    assert.equal(new TextDecoder().decode(body), `FAKE-MULTIPART-PREFIX\r\n${new TextDecoder().decode(PDF)}\r\nFAKE-MULTIPART-SUFFIX`);
    return new Response(null, { status: 201 });
  }, (_url, init) => {
    assert.equal(init.headers['Zotero-API-Key'], FAKE_KEY);
    assert.equal(new URLSearchParams(init.body).get('upload'), 'FAKE_UPLOAD_TOKEN');
    return response(null, 204);
  }, ...verifySequence()]);
  const result = await f.transport.uploadStoredPdf(uploadInput());
  assert.deepEqual(result, { itemKey: ATTACHMENT, parentItem: PARENT, version: 9,
    ...HASHES, size: PDF.length, storedBytesVerified: true });
  for (const call of f.calls.filter(c => c.url === STORAGE)) {
    assert.equal(call.headers['Zotero-API-Key'], undefined);
    assert.equal(call.redirect, 'manual');
  }
  for (const call of f.calls.filter(c => c.method === 'GET' && c.url.startsWith(API) && !c.url.endsWith('/file'))) {
    const url = new URL(call.url);
    assert.equal(url.pathname, `/users/${OWNER}/items`);
    assert.equal(url.searchParams.get('itemKey'), ATTACHMENT);
    assert.equal(url.searchParams.get('itemType'), '-note || annotation');
  }
  assert.equal(f.pending.length, 0);
});

test('already-existing storage still requires actual byte readback, skips transfer and registration', async () => {
  const f = await authorized([attachment(false), response({ exists: 1 }), ...verifySequence()]);
  assert.equal((await f.transport.uploadStoredPdf(uploadInput())).storedBytesVerified, true);
  assert.equal(f.calls.filter(c => c.method === 'POST').length, 1);
});

test('existing-file replacement uses previous MD5 for authorization AND registration', async () => {
  const previousMd5 = 'a'.repeat(32);
  const f = await authorized([
    one(ATTACHMENT, 8, { ...attachmentData(false), md5: previousMd5 }), authorization(),
    new Response(null, { status: 201 }), response(null, 204), ...verifySequence(),
  ]);
  await f.transport.uploadStoredPdf({ ...uploadInput(), previousMd5 });
  const apiPosts = f.calls.filter(c => c.method === 'POST' && c.url.startsWith(API));
  assert.equal(apiPosts.length, 2);
  assert.ok(apiPosts.every(c => c.headers['If-Match'] === previousMd5 && !c.headers['If-None-Match']));
});

test('PDF size/signature/hash adapter failures stop before file writes', async () => {
  const f = await authorized();
  await rejectsCode(f.transport.authorizePdfUpload({ ...uploadInput(), bytes: new TextEncoder().encode('<html>oops</html>') }), 'INVALID_PDF');
  await rejectsCode(f.transport.authorizePdfUpload({ ...uploadInput(), filename: '../mock.pdf' }), 'INVALID_FILENAME');
  const noHash = await authorized([], { hashBytes: undefined });
  await rejectsCode(noHash.transport.authorizePdfUpload(uploadInput()), 'HASH_ADAPTER_REQUIRED');
  const big = await authorized([], { maxPdfBytes: 8 });
  await rejectsCode(big.transport.authorizePdfUpload(uploadInput()), 'PDF_TOO_LARGE');
  const leaked = await authorized([], { hashBytes: () => { throw new Error(FAKE_KEY); } });
  await rejectsCode(leaked.transport.authorizePdfUpload(uploadInput()), 'HASH_ADAPTER_FAILED');
  assert.equal(f.calls.length, 1);
});

test('caller byte mutation cannot alter the owned authorized upload', async () => {
  const original = new Uint8Array(PDF);
  const f = await authorized([attachment(false), authorization(), async (_url, init) => {
    assert.ok(new TextDecoder().decode(await init.body.arrayBuffer()).includes('%PDF-1.4'));
    return new Response(null, { status: 201 });
  }]);
  const a = await f.transport.authorizePdfUpload({ ...uploadInput(), bytes: original });
  original.fill(0);
  assert.deepEqual(Object.keys(a.ticket), ['kind']);
  assert.equal(JSON.stringify(a).includes('FAKE_UPLOAD_TOKEN'), false);
  assert.equal(JSON.stringify(a).includes(STORAGE), false);
  await f.transport.transferPdfUpload(a.ticket);
  await rejectsCode(f.transport.transferPdfUpload(a.ticket), 'INVALID_UPLOAD_STATE');
  await rejectsCode(f.transport.registerPdfUpload({ kind: 'zotero-pdf-upload' }), 'INVALID_UPLOAD_STATE');
});

test('unverified upload destinations are rejected before any storage request', async () => {
  for (const target of ['https://attacker.invalid/', 'http://zoterofilestorage.s3.us-east-1.amazonaws.com/',
    'https://zoterofilestorage.s3.us-east-1.amazonaws.com.attacker.invalid/',
    'https://user@zoterofilestorage.s3.us-east-1.amazonaws.com/',
    'https://zoterofilestorage.s3.us-east-1.amazonaws.com:444/',
    'https://s3.amazonaws.com/other-bucket/', 'https://127.0.0.1/', 'https://api.zotero.org/']) {
    const f = await authorized([attachment(false), authorization(target)]);
    await rejectsCode(f.transport.authorizePdfUpload(uploadInput()), 'UNVERIFIED_STORAGE_TARGET');
    assert.equal(f.calls.length, 3);
  }
});

test('all upload redirects are refused, including to another allowlisted storage URL', async () => {
  const f = await authorized([attachment(false), authorization(), redirect(`${STORAGE}-redirect`)]);
  await rejectsCode(f.transport.uploadStoredPdf(uploadInput()), 'UNSAFE_REDIRECT', { uncertain: true });
  assert.equal(f.calls.length, 4);
});

test('API redirects and adapter-followed redirects fail closed', async () => {
  const redirected = one(ITEM, 8);
  Object.defineProperty(redirected, 'redirected', { value: true });
  for (const step of [redirect('https://attacker.invalid'), redirected]) {
    const f = await authorized([step]);
    await rejectsCode(f.transport.getItem(ITEM), 'UNSAFE_REDIRECT');
    assert.equal(f.calls.length, 2);
  }
});

test('download redirect to unknown host and storage-to-API redirect never forward API auth', async () => {
  const input = { ...uploadInput(), size: PDF.length, ...HASHES };
  const bad = await authorized([attachment(true), redirect('https://attacker.invalid/private')]);
  await rejectsCode(bad.transport.verifyStoredPdf(input), 'UNVERIFIED_STORAGE_TARGET');
  assert.equal(bad.calls.length, 3);
  const bounce = await authorized([attachment(true), redirect(), redirect(`${API}/items/${ITEM}`)]);
  await rejectsCode(bounce.transport.verifyStoredPdf(input), 'UNSAFE_REDIRECT');
  assert.equal(bounce.calls[3].headers['Zotero-API-Key'], undefined);
  assert.equal(bounce.calls.length, 4);
});

test('failed registration cannot be reported as a stored PDF', async () => {
  const f = await authorized([attachment(false), authorization(), new Response(null, { status: 201 }), response({}, 503)]);
  await rejectsCode(f.transport.uploadStoredPdf(uploadInput()), 'SERVICE_UNAVAILABLE', { uncertain: true });
  assert.equal(f.calls.length, 5);
  assert.equal(f.calls.filter(c => c.url.endsWith('/file') && c.method === 'GET').length, 0);
});

test('registered upload followed by failed verification retains mutation uncertainty', async () => {
  const f = await authorized([attachment(false), authorization(), new Response(null, { status: 201 }), response(null, 204),
    () => { throw new Error('mock verification failure'); }]);
  await rejectsCode(f.transport.uploadStoredPdf(uploadInput()), 'NETWORK_FAILURE', { uncertain: true });
  assert.equal(f.calls.filter(c => c.method === 'POST').length, 3);
  const exists = await authorized([attachment(false), response({ exists: 1 }), one(ATTACHMENT, 9, { ...attachmentData(true), filename: 'different.pdf' })]);
  await rejectsCode(exists.transport.uploadStoredPdf(uploadInput()), 'STORED_METADATA_MISMATCH', { uncertain: true });
});

test('metadata, ETag, size and SHA256 verification all independently fail closed', async () => {
  const input = { ...uploadInput(), size: PDF.length, ...HASHES };
  const altered = new Uint8Array(PDF); altered[10] ^= 1;
  const variants = [
    [[one(ATTACHMENT, 9, { ...attachmentData(true), md5: 'b'.repeat(32) })], 'STORED_METADATA_MISMATCH'],
    [[attachment(true), redirect(), download(PDF, 'b'.repeat(32))], 'STORED_ETAG_MISMATCH'],
    [[attachment(true), redirect(), download(PDF.subarray(0, PDF.length - 1))], 'STORED_SIZE_MISMATCH'],
    [[attachment(true), redirect(), download(altered)], 'STORED_BYTES_MISMATCH'],
    [[attachment(true), redirect(), download(), one(ATTACHMENT, 10, attachmentData(true))], 'STORED_METADATA_CHANGED'],
  ];
  for (const [steps, code] of variants) {
    const f = await authorized(steps);
    await rejectsCode(f.transport.verifyStoredPdf(input), code);
  }
});

test('download body remains bounded without Content-Length and rejects truncation', async () => {
  const input = { ...uploadInput(), size: PDF.length, ...HASHES };
  const oversized = new Response(new Uint8Array(PDF.length + 1), { headers: { ETag: HASHES.md5 } });
  const f = await authorized([attachment(true), redirect(), oversized], { maxPdfBytes: PDF.length });
  await rejectsCode(f.transport.verifyStoredPdf(input), 'PDF_TOO_LARGE');
  const truncated = new Response(PDF.subarray(0, -1), { headers: { ETag: HASHES.md5, 'Content-Length': String(PDF.length) } });
  const t = await authorized([attachment(true), redirect(), truncated]);
  await rejectsCode(t.transport.verifyStoredPdf(input), 'TRUNCATED_RESPONSE');
});

test('Backoff on successful reads delays the next request; GET 429/503 retry at most the configured count', async () => {
  const first = one(ITEM, 8); first.headers.set('Backoff', '2');
  const f = await authorized([first, response({}, 429, { 'Retry-After': '3' }), response({}, 503, { 'Retry-After': '4' }), one(ITEM, 8)]);
  await f.transport.getItem(ITEM);
  await f.transport.getItem(ITEM);
  assert.deepEqual(f.sleeps, [2000, 3000, 4000]);
  const rate = await authorized([response({}, 429), response({}, 429), response({}, 429)]);
  await rejectsCode(rate.transport.getItem(ITEM), 'RATE_LIMITED');
  assert.equal(rate.calls.length, 4);
  assert.deepEqual(rate.sleeps, [1000, 2000]);
});

test('long backoff is surfaced rather than capped/ignored and no request starts during it', async () => {
  const first = one(ITEM, 8); first.headers.set('Backoff', '120');
  const f = await authorized([first]);
  await f.transport.getItem(ITEM);
  await rejectsCode(f.transport.getItem(ITEM), 'BACKOFF_ACTIVE', { retryAfterMs: 120000 });
  assert.equal(f.calls.length, 2);
});

test('authorization failure clears previously verified permission and keeps response private', async () => {
  const f = await authorized([response({ message: FAKE_KEY }, 403)]);
  await rejectsCode(f.transport.getItem(ITEM), 'AUTHORIZATION_DENIED');
  await rejectsCode(f.transport.getItem(ITEM), 'PERMISSIONS_UNVERIFIED');
  assert.equal(f.calls.length, 2);
  const malformed = await authorized([response({}, 403, { Backoff: 'malformed' })]);
  await rejectsCode(malformed.transport.getItem(ITEM), 'INVALID_BACKOFF');
  await rejectsCode(malformed.transport.getItem(ITEM), 'PERMISSIONS_UNVERIFIED');
  assert.equal(malformed.calls.length, 2);
});

test('request deadline sanitizes abort errors; never logs credential-bearing adapter errors', async () => {
  const f = await authorized([(_url, init) => new Promise((_resolve, reject) => {
    init.signal.addEventListener('abort', () => reject(new Error(`${FAKE_KEY} https://private.invalid`)), { once: true });
  })], { timeoutMs: 5 });
  await rejectsCode(f.transport.getItem(ITEM), 'REQUEST_TIMEOUT');
});
