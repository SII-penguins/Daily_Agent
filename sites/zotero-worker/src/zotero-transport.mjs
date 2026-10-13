/** Source-only Zotero Web API v3 adapter. No global fetch, setup, or application wiring. */
const API_ORIGIN = 'https://api.zotero.org';
const STORAGE_HOST = 'zoterofilestorage.s3.us-east-1.amazonaws.com';
const KEY = /^[23456789ABCDEFGHIJKLMNPQRSTUVWXYZ]{8}$/;
const MD5 = /^[a-f0-9]{32}$/;
const SHA256 = /^[a-f0-9]{64}$/;
// Zotero negates the whole OR set. Apply the identical scope to data and version proofs.
const ITEM_SCOPE = `&includeTrashed=1&itemType=${encodeURIComponent('-note || annotation')}`;
const privateItemField = field => field === 'note' || field === 'notes' || field.startsWith('annotation');
const encoder = new TextEncoder();
const decoder = new TextDecoder('utf-8', { fatal: true });

export class ZoteroTransportError extends Error {
  constructor(code, { status, retryAfterMs, uncertain = false } = {}) {
    super(code);
    this.name = 'ZoteroTransportError';
    this.code = code;
    if (Number.isInteger(status) && status >= 100 && status <= 599) this.status = status;
    if (Number.isSafeInteger(retryAfterMs) && retryAfterMs >= 0) this.retryAfterMs = retryAfterMs;
    this.uncertain = uncertain;
  }
  toJSON() {
    return { code: this.code, ...(this.status ? { status: this.status } : {}),
      ...(this.retryAfterMs !== undefined ? { retryAfterMs: this.retryAfterMs } : {}),
      uncertain: this.uncertain };
  }
}
const fail = (code, details) => { throw new ZoteroTransportError(code, details); };
const record = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const own = (value, key) => Object.prototype.hasOwnProperty.call(value, key);
const integer = value => Number.isSafeInteger(value) && value >= 0;
const validKey = value => typeof value === 'string' && KEY.test(value);
function requireKey(value) { if (!validKey(value)) fail('INVALID_OBJECT_KEY'); return value; }
function requireVersion(value) { if (!integer(value)) fail('INVALID_VERSION'); return value; }
function safeText(value, max, allowEmpty = false) {
  if (typeof value !== 'string' || value.length > max || (!allowEmpty && !value.trim()) || /[\u0000-\u0008\u000b\u000c\u000e-\u001f]/u.test(value)) fail('INVALID_INPUT');
  return value;
}
function filenameOf(value) {
  safeText(value, 240);
  if (!/\.pdf$/i.test(value) || /[\\/:\r\n]/.test(value) || value === '.' || value === '..') fail('INVALID_FILENAME');
  return value;
}
function collectionsOf(value) {
  if (!Array.isArray(value) || value.length > 10000) fail('INVALID_COLLECTIONS');
  const keys = value.map(requireKey);
  if (new Set(keys).size !== keys.length) fail('INVALID_COLLECTIONS');
  return keys;
}
function tagsOf(value) {
  if (!Array.isArray(value) || value.length > 1000) fail('INVALID_TAGS');
  return value.map(tag => {
    if (!record(tag) || Object.keys(tag).some(k => !['tag', 'type'].includes(k))) fail('INVALID_TAGS');
    const result = { tag: safeText(tag.tag, 1000) };
    if (own(tag, 'type')) { if (![0, 1].includes(tag.type)) fail('INVALID_TAGS'); result.type = tag.type; }
    return result;
  });
}
function cloneJSON(value, maxBytes = 256 * 1024) {
  let text;
  try { text = JSON.stringify(value); } catch { fail('INVALID_INPUT'); }
  if (!text || encoder.encode(text).length > maxBytes) fail('INPUT_TOO_LARGE');
  try {
    return JSON.parse(text, (key, v) => {
      if (['__proto__', 'constructor', 'prototype'].includes(key)) fail('INVALID_INPUT');
      return v;
    });
  } catch { fail('INVALID_INPUT'); }
}
function parseIntegerHeader(response, name, code = 'INVALID_RESPONSE') {
  const value = response.headers.get(name);
  if (value === null || !/^(0|[1-9]\d*)$/.test(value) || !integer(Number(value))) fail(code);
  return Number(value);
}
function httpCode(status, storage = false) {
  if (status === 401 || status === 403) return storage ? 'STORAGE_FORBIDDEN' : 'AUTHORIZATION_DENIED';
  if (status === 404) return 'NOT_FOUND';
  if (status === 409) return 'LIBRARY_LOCKED';
  if (status === 412) return 'WRITE_CONFLICT';
  if (status === 413) return storage ? 'STORAGE_REJECTED' : 'QUOTA_OR_SIZE_EXCEEDED';
  if (status === 428) return 'PRECONDITION_REQUIRED';
  if (status === 429) return 'RATE_LIMITED';
  if (status >= 500) return storage ? 'STORAGE_UNAVAILABLE' : 'SERVICE_UNAVAILABLE';
  return storage ? 'STORAGE_REQUEST_REJECTED' : 'REQUEST_REJECTED';
}
function storageURL(value) {
  let url;
  try { url = new URL(value); } catch { fail('UNVERIFIED_STORAGE_TARGET'); }
  if (typeof value !== 'string' || value.length > 16384 || url.protocol !== 'https:' ||
      url.hostname !== STORAGE_HOST || url.port || url.username || url.password || url.hash ||
      /[\r\n\\]/.test(value)) fail('UNVERIFIED_STORAGE_TARGET');
  return url.href;
}
function equalJSON(a, b) {
  if (a === b) return true;
  if (Array.isArray(a) && Array.isArray(b)) return a.length === b.length && a.every((v, i) => equalJSON(v, b[i]));
  if (!record(a) || !record(b)) return false;
  const keys = Object.keys(a);
  return keys.length === Object.keys(b).length && keys.every(k => own(b, k) && equalJSON(a[k], b[k]));
}

/**
 * fetchImpl is REQUIRED; importing/constructing never issues a request.
 * hashBytes is a trusted local async adapter: Uint8Array -> {md5, sha256}.
 * It must compute both hashes over the supplied bytes; WebCrypto has NO MD5.
 */
export function createZoteroTransport({
  fetchImpl, userID, apiKey, hashBytes,
  now = () => Date.now(), sleep = ms => new Promise(resolve => setTimeout(resolve, ms)),
  maxPdfBytes = 16 * 1024 * 1024, maxJsonBytes = 4 * 1024 * 1024,
  maxInventoryObjects = 50000, maxInventoryBytes = 32 * 1024 * 1024, pageSize = 100, timeoutMs = 30000,
  permissionTtlMs = 300000, maxReadRetries = 2,
} = {}) {
  if (typeof fetchImpl !== 'function') fail('FETCH_ADAPTER_REQUIRED');
  if (!/^[1-9]\d{0,15}$/.test(String(userID)) || !Number.isSafeInteger(Number(userID))) fail('INVALID_USER_ID');
  if (typeof apiKey !== 'string' || !/^[A-Za-z0-9_-]{8,128}$/.test(apiKey)) fail('MISSING_OR_INVALID_CREDENTIAL');
  if (typeof now !== 'function' || typeof sleep !== 'function') fail('INVALID_CONFIGURATION');
  for (const [v, min, max] of [[maxPdfBytes, 8, 32 * 1024 * 1024], [maxJsonBytes, 1024, 16 * 1024 * 1024],
    [maxInventoryObjects, 1, 100000], [maxInventoryBytes, 1024, 64 * 1024 * 1024], [pageSize, 1, 100], [timeoutMs, 1, 120000],
    [permissionTtlMs, 1, 300000], [maxReadRetries, 0, 5]]) {
    if (!Number.isSafeInteger(v) || v < min || v > max) fail('INVALID_CONFIGURATION');
  }
  const owner = String(userID);
  const prefix = `/users/${owner}`;
  let permission = null;
  let nextRequestAt = 0;
  let queue = Promise.resolve();
  const uploads = new WeakMap();

  const safe = fn => async (...args) => {
    try { return await fn(...args); }
    catch (error) {
      if (error instanceof ZoteroTransportError) throw error;
      // No adapter errors, raw response text, signed URLs, causes, or secrets escape.
      fail('TRANSPORT_FAILURE');
    }
  };
  function requirePermission({ write = false, files = false, notes = false } = {}) {
    if (!permission || now() >= permission.expiresAt) fail('PERMISSIONS_UNVERIFIED');
    if (!permission.library || (write && !permission.write) || (files && !permission.files) ||
        (notes && !permission.notes)) fail('INSUFFICIENT_PERMISSION');
  }
  function parseDelay(raw) {
    if (raw === null) return 0;
    if (!/^\d+$/.test(raw) || !Number.isSafeInteger(Number(raw) * 1000)) fail('INVALID_BACKOFF');
    return Number(raw) * 1000;
  }
  function noteBackoff(response, attempt) {
    const backoff = parseDelay(response.headers.get('Backoff'));
    const retry = parseDelay(response.headers.get('Retry-After'));
    const ms = Math.max(backoff, retry, response.status === 429 ? 1000 * (2 ** attempt) : 0);
    if (ms) nextRequestAt = Math.max(nextRequestAt, now() + ms);
    return ms;
  }
  async function readBytes(response, max, oversizeCode = 'RESPONSE_TOO_LARGE') {
    const length = response.headers.get('Content-Length');
    if (length !== null && (!/^\d+$/.test(length) || Number(length) > max)) {
      try { await response.body?.cancel(); } catch { /* deliberately discard */ }
      fail(oversizeCode);
    }
    if (!response.body || typeof response.body.getReader !== 'function') fail('INVALID_RESPONSE_STREAM');
    const reader = response.body.getReader();
    const chunks = [];
    let size = 0;
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        if (!(value instanceof Uint8Array)) fail('INVALID_RESPONSE_STREAM');
        size += value.byteLength;
        if (size > max) { await reader.cancel().catch(() => {}); fail(oversizeCode); }
        chunks.push(value);
      }
    } finally { reader.releaseLock(); }
    if (length !== null && Number(length) !== size) fail('TRUNCATED_RESPONSE');
    const bytes = new Uint8Array(size);
    let offset = 0;
    for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
    return bytes;
  }
  async function json(response) {
    const bytes = await readBytes(response, maxJsonBytes);
    try { return JSON.parse(decoder.decode(bytes)); } catch { fail('INVALID_JSON_RESPONSE'); }
  }
  async function request(url, { method = 'GET', body, headers = {}, expected = [200],
    storage = false, allowFileRedirect = false, consume = json } = {}) {
    // Internal callers only supply literal API paths or a validated storage URL.
    if (storage) storageURL(url);
    else if (!url.startsWith(`${API_ORIGIN}/`) || new URL(url).origin !== API_ORIGIN) fail('INVALID_API_TARGET');
    const run = async () => {
      for (let attempt = 0; ; attempt++) {
        const delay = nextRequestAt - now();
        // Long server delays are surfaced for durable scheduling, never shortened.
        if (delay > 30000) fail('BACKOFF_ACTIVE', { retryAfterMs: Math.ceil(delay) });
        if (delay > 0) {
          await sleep(delay);
          if (now() < nextRequestAt) fail('BACKOFF_ACTIVE', { retryAfterMs: Math.ceil(nextRequestAt - now()) });
        }
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), timeoutMs);
        let response;
        try {
          try {
            response = await fetchImpl(url, {
              method, body, headers: storage ? { ...headers } : {
                'Zotero-API-Key': apiKey, 'Zotero-API-Version': '3', 'Accept': 'application/json', ...headers,
              },
              redirect: 'manual', credentials: 'omit', cache: 'no-store', referrerPolicy: 'no-referrer',
              signal: controller.signal,
            });
          } catch {
            fail(controller.signal.aborted ? 'REQUEST_TIMEOUT' : 'NETWORK_FAILURE', { uncertain: method !== 'GET' });
          }
          if (!response || !Number.isInteger(response.status) || !response.headers?.get) fail('INVALID_RESPONSE');
          if (!storage && [401, 403].includes(response.status)) permission = null;
          if (response.redirected || response.type === 'opaqueredirect' || (response.url && response.url !== url)) fail('UNSAFE_REDIRECT', { uncertain: method !== 'GET' });
          const waitMs = noteBackoff(response, attempt);
          if (response.status >= 300 && response.status < 400 && !(allowFileRedirect && [301, 302, 303, 307, 308].includes(response.status))) {
            fail('UNSAFE_REDIRECT', { status: response.status, uncertain: method !== 'GET' });
          }
          if (!expected.includes(response.status)) {
            // Only explicit GET 429/503 responses retry. Never automatically replay writes.
            try { await response.body?.cancel(); } catch { /* deliberately discard */ }
            if (method === 'GET' && [429, 503].includes(response.status) && attempt < maxReadRetries) {
              if (!waitMs) nextRequestAt = Math.max(nextRequestAt, now() + 1000 * (2 ** attempt));
              continue;
            }
            fail(httpCode(response.status, storage), { status: response.status, retryAfterMs: waitMs || undefined,
              uncertain: method !== 'GET' && response.status >= 500 });
          }
          return await consume(response);
        } catch (error) {
          if (error instanceof ZoteroTransportError) {
            if (method !== 'GET' && (!response || !Number.isInteger(response.status) || response.status >= 500 ||
                (expected.includes(response.status) &&
                  !['OBJECT_WRITE_FAILED', 'WRITE_CONFLICT', 'CREATE_ALREADY_EXISTS'].includes(error.code)))) error.uncertain = true;
            throw error;
          }
          fail(controller.signal.aborted ? 'REQUEST_TIMEOUT' : 'RESPONSE_READ_FAILED', { uncertain: method !== 'GET' });
        } finally { clearTimeout(timer); }
      }
    };
    const pending = queue.then(run, run);
    queue = pending.catch(() => {});
    return pending;
  }
  const api = (path, options) => request(`${API_ORIGIN}${path}`, options);
  function objectOf(value, expectedKey, type = 'items') {
    if (!record(value) || !validKey(value.key) || (expectedKey && value.key !== expectedKey) ||
        !integer(value.version) || !record(value.data) || value.data.key !== value.key ||
        value.data.version !== value.version || !record(value.library) || value.library.type !== 'user' ||
        String(value.library.id) !== owner) fail('INVALID_OBJECT_RESPONSE');
    if (type === 'items' && typeof value.data.itemType !== 'string') fail('INVALID_OBJECT_RESPONSE');
    if (type === 'collections' && typeof value.data.name !== 'string') fail('INVALID_OBJECT_RESPONSE');
    if (type === 'items' && ['note', 'annotation'].includes(value.data.itemType)) fail('EXCLUDED_ITEM_RESPONSE');
    // Attachment JSON can contain a legacy embedded note even with notes:false.
    // Drop private note fields at this boundary; never return, persist, log, or PATCH them.
    const data = type === 'items' ? Object.fromEntries(Object.keys(value.data)
      .filter(field => !privateItemField(field)).map(field => [field, value.data[field]])) : value.data;
    return { key: value.key, version: value.version, data };
  }
  async function getObject(type, key) {
    requirePermission(); requireKey(key);
    if (type === 'items') {
      // Single-item URLs do not apply itemType filtering. A stale or colliding key
      // must not cause an intentional note/annotation read, even with a broad key.
      return api(`${prefix}/items?format=json&include=data&itemKey=${key}${ITEM_SCOPE}`, {
        consume: async response => {
          const version = parseIntegerHeader(response, 'Last-Modified-Version');
          const total = parseIntegerHeader(response, 'Total-Results');
          const values = await json(response);
          if (!Array.isArray(values) || total > 1 || values.length !== total) fail('INVALID_OBJECT_RESPONSE');
          if (!total) return null;
          const result = objectOf(values[0], key);
          // Multi-item requests carry the library version, not the object's version.
          if (result.version > version) fail('INVALID_OBJECT_RESPONSE');
          return result;
        },
      });
    }
    return api(`${prefix}/${type}/${key}`, { expected: [200, 404], consume: async response => {
      if (response.status === 404) return null;
      const result = objectOf(await json(response), key, type);
      if (parseIntegerHeader(response, 'Last-Modified-Version') !== result.version) fail('INVALID_OBJECT_RESPONSE');
      return result;
    } });
  }
  async function createObject(type, key, data) {
    requirePermission({ write: true }); requireKey(key);
    const payload = cloneJSON({ ...data, key, version: 0 });
    return api(`${prefix}/${type}`, {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify([payload]),
      consume: async response => {
        const version = parseIntegerHeader(response, 'Last-Modified-Version');
        const result = await json(response);
        if (!record(result) || !record(result.failed) || !record(result.unchanged)) fail('INVALID_WRITE_RESPONSE', { uncertain: true });
        const successful = result.successful;
        if (!record(successful)) fail('INVALID_WRITE_RESPONSE', { uncertain: true });
        for (const entries of [successful, result.failed, result.unchanged]) {
          if (Object.keys(entries).some(index => index !== '0')) fail('INVALID_WRITE_RESPONSE', { uncertain: true });
        }
        if ([successful, result.failed, result.unchanged].filter(entries => own(entries, '0')).length !== 1) fail('INVALID_WRITE_RESPONSE', { uncertain: true });
        if (own(result.failed, '0')) {
          const status = result.failed['0']?.code;
          fail(status === 412 ? 'WRITE_CONFLICT' : 'OBJECT_WRITE_FAILED', { status });
        }
        if (own(result.unchanged, '0')) fail('CREATE_ALREADY_EXISTS');
        const saved = objectOf(successful['0'], key, type);
        if (saved.version !== version || saved.version === 0) fail('INVALID_WRITE_RESPONSE', { uncertain: true });
        // Key/version proof plus explicit saved-field proof. No generated replacement key on failure.
        for (const [field, value] of Object.entries(data)) {
          if (!equalJSON(saved.data[field], value)) fail('WRITE_RESULT_MISMATCH', { uncertain: true });
        }
        return saved;
      },
    });
  }
  async function digest(bytes) {
    if (typeof hashBytes !== 'function') fail('HASH_ADAPTER_REQUIRED');
    let hashes;
    try { hashes = await hashBytes(bytes); } catch { fail('HASH_ADAPTER_FAILED'); }
    if (!record(hashes) || !MD5.test(hashes.md5) || !SHA256.test(hashes.sha256)) fail('INVALID_HASH_RESULT');
    return { md5: hashes.md5, sha256: hashes.sha256 };
  }
  function pdfBytes(value) {
    if (!(value instanceof Uint8Array) || value.byteLength < 8) fail('INVALID_PDF');
    if (value.byteLength > maxPdfBytes) fail('PDF_TOO_LARGE');
    if (!['%', 'P', 'D', 'F', '-'].every((v, i) => value[i] === v.charCodeAt(0))) fail('INVALID_PDF');
    return new Uint8Array(value); // Own the bytes; caller mutation cannot change an authorized upload.
  }
  function attachmentOf(item, parentItem) {
    if (!item || item.data.itemType !== 'attachment' || item.data.linkMode !== 'imported_file' ||
        item.data.contentType !== 'application/pdf' || item.data.parentItem !== parentItem) fail('ATTACHMENT_MISMATCH');
    return item;
  }
  function ticketState(ticket, expected) {
    const state = record(ticket) && uploads.get(ticket);
    if (!state || !expected.includes(state.stage)) fail('INVALID_UPLOAD_STATE');
    requirePermission({ write: true, files: true });
    return state;
  }
  async function verifyStored(state) {
    requirePermission({ files: true });
    const item = attachmentOf(await getObject('items', state.itemKey), state.parentItem);
    if (item.data.md5 !== state.md5 || Number(item.data.mtime) !== state.mtime || item.data.filename !== state.filename) fail('STORED_METADATA_MISMATCH');
    const redirect = await api(`${prefix}/items/${state.itemKey}/file`, {
      expected: [200, 301, 302, 303, 307, 308], allowFileRedirect: true,
      consume: async response => {
        const etag = response.headers.get('ETag');
        if (etag && etag.replace(/^"|"$/g, '') !== state.md5) fail('STORED_ETAG_MISMATCH');
        if (response.status === 200) return { bytes: await readBytes(response, maxPdfBytes, 'PDF_TOO_LARGE'), etag };
        const target = storageURL(response.headers.get('Location'));
        try { await response.body?.cancel(); } catch { /* deliberately discard */ }
        return { target };
      },
    });
    const downloaded = redirect.bytes ? redirect : await request(redirect.target, {
      storage: true, headers: { Accept: 'application/pdf' },
      consume: async response => ({ bytes: await readBytes(response, maxPdfBytes, 'PDF_TOO_LARGE'), etag: response.headers.get('ETag') }),
    });
    if (!downloaded.etag || downloaded.etag.replace(/^"|"$/g, '') !== state.md5) fail('STORED_ETAG_MISMATCH');
    if (downloaded.bytes.length !== state.size) fail('STORED_SIZE_MISMATCH');
    const hashes = await digest(downloaded.bytes);
    if (hashes.md5 !== state.md5 || hashes.sha256 !== state.sha256) fail('STORED_BYTES_MISMATCH');
    const final = attachmentOf(await getObject('items', state.itemKey), state.parentItem);
    if (final.version !== item.version || final.data.md5 !== state.md5 || final.data.filename !== state.filename || Number(final.data.mtime) !== state.mtime) fail('STORED_METADATA_CHANGED');
    return { itemKey: state.itemKey, parentItem: state.parentItem, version: final.version,
      md5: state.md5, sha256: state.sha256, size: state.size, storedBytesVerified: true };
  }

  const methods = {
    verifyCurrentKey: safe(async ({ requireWrite = true, requireFiles = true, requireNotes = false, exactPersonalScope = false } = {}) => {
      permission = null;
      const result = await api('/keys/current');
      if (!record(result) || String(result.userID) !== owner || !record(result.access?.user)) fail('KEY_OWNER_MISMATCH');
      const grants = result.access.user;
      const verified = { library: grants.library === true, write: grants.write === true,
        files: grants.files === true, notes: grants.notes === true };
      if (!verified.library || (requireWrite && !verified.write) || (requireFiles && !verified.files) || (requireNotes && !verified.notes)) fail('INSUFFICIENT_PERMISSION');
      if (exactPersonalScope) {
        // Missing notes is no grant; all present grants must be explicitly known.
        // Empty groups is accepted, any group/all-groups entry is rejected.
        if (Object.keys(result.access).some(key => !['user', 'groups'].includes(key))
            || (own(result.access, 'groups') && (!record(result.access.groups) || Object.keys(result.access.groups).length))
            || Object.keys(grants).some(key => !['library', 'write', 'files', 'notes'].includes(key))
            || (own(grants, 'notes') && grants.notes !== false)
            || !verified.library || !verified.write || !verified.files) fail('EXCESS_PERMISSION');
      }
      permission = { ...verified, expiresAt: now() + permissionTtlMs };
      return { userID: owner, ...verified }; // Deliberately omit raw key info/username/group grants.
    }),
    getItem: safe(key => getObject('items', key)),
    getCollection: safe(key => getObject('collections', key)),
    inventory: safe(async () => {
      requirePermission();
      let libraryVersion;
      let inventoryBytes = 0;
      const results = {};
      for (const type of ['items', 'collections']) {
        const objects = [];
        const seen = new Set();
        let total;
        for (let start = 0; ; start += pageSize) {
          const query = `?format=json&include=data&limit=${pageSize}&start=${start}${type === 'items' ? ITEM_SCOPE : ''}`;
          const page = await api(`${prefix}/${type}${query}`, { consume: async response => ({
            version: parseIntegerHeader(response, 'Last-Modified-Version', 'INCOMPLETE_INVENTORY'),
            total: parseIntegerHeader(response, 'Total-Results', 'INCOMPLETE_INVENTORY'), data: await json(response),
          }) });
          libraryVersion ??= page.version;
          total ??= page.total;
          if (page.version !== libraryVersion) fail('INVENTORY_CHANGED');
          if (total !== page.total || total > maxInventoryObjects || !Array.isArray(page.data) ||
              page.data.length !== Math.min(pageSize, total - start)) fail('INCOMPLETE_INVENTORY');
          for (const entry of page.data) {
            const object = objectOf(entry, undefined, type);
            inventoryBytes += encoder.encode(JSON.stringify(object)).length;
            if (inventoryBytes > maxInventoryBytes) fail('INVENTORY_TOO_LARGE');
            if (seen.has(object.key) || object.version > libraryVersion) fail('INCOMPLETE_INVENTORY');
            seen.add(object.key); objects.push(object);
          }
          if (objects.length === total) break;
        }
        results[type] = objects;
      }
      // Complete maps within the same non-note scope prove no skipped keys and version drift.
      for (const type of ['items', 'collections']) {
        const proof = await api(`${prefix}/${type}?format=versions${type === 'items' ? ITEM_SCOPE : ''}`, {
          consume: async response => ({ version: parseIntegerHeader(response, 'Last-Modified-Version', 'INCOMPLETE_INVENTORY'), map: await json(response) }),
        });
        if (proof.version !== libraryVersion) fail('INVENTORY_CHANGED');
        if (!record(proof.map) || Object.keys(proof.map).length !== results[type].length ||
            results[type].some(item => proof.map[item.key] !== item.version)) fail('INCOMPLETE_INVENTORY');
      }
      return { libraryVersion, complete: true, includesTrashed: true, excludesNotes: true, ...results };
    }),
    createItem: safe(async ({ key, data } = {}) => {
      if (!record(data) || own(data, 'key') || own(data, 'version') || !/^[a-zA-Z]{2,64}$/.test(data.itemType) ||
          ['attachment', 'note', 'annotation'].includes(data.itemType) || Object.keys(data).some(privateItemField)) fail('INVALID_ITEM_DATA');
      const payload = cloneJSON(data);
      payload.collections = collectionsOf(payload.collections ?? []);
      payload.tags = tagsOf(payload.tags ?? []);
      payload.relations ??= {};
      if (!record(payload.relations)) fail('INVALID_ITEM_DATA');
      return createObject('items', key, payload);
    }),
    createCollection: safe(async ({ key, name, parentCollection = false } = {}) => {
      requireKey(key);
      if (parentCollection !== false) requireKey(parentCollection);
      if (key === parentCollection) fail('INVALID_COLLECTION_PARENT');
      return createObject('collections', key, { name: safeText(name, 1000), parentCollection });
    }),
    patchItemMembership: safe(async ({ itemKey, expectedVersion, collections, tags } = {}) => {
      requirePermission({ write: true }); requireKey(itemKey); requireVersion(expectedVersion);
      const patch = {};
      if (collections !== undefined) patch.collections = collectionsOf(collections);
      if (tags !== undefined) patch.tags = tagsOf(tags);
      if (!Object.keys(patch).length) fail('EMPTY_PATCH');
      const version = await api(`${prefix}/items/${itemKey}`, { method: 'PATCH', expected: [204],
        headers: { 'Content-Type': 'application/json', 'If-Unmodified-Since-Version': String(expectedVersion) },
        body: JSON.stringify(patch), consume: response => parseIntegerHeader(response, 'Last-Modified-Version'),
      });
      try {
        const saved = await getObject('items', itemKey);
        if (!saved || saved.version !== version || Object.keys(patch).some(k => !equalJSON(saved.data[k], patch[k]))) fail('PATCH_READBACK_MISMATCH', { uncertain: true });
        return saved;
      } catch (error) {
        if (error instanceof ZoteroTransportError) { error.uncertain = true; throw error; }
        fail('PATCH_READBACK_FAILED', { uncertain: true });
      }
    }),
    createStoredAttachment: safe(async ({ key, parentItem, title, filename, tags = [] } = {}) => {
      requirePermission({ write: true, files: true }); requireKey(parentItem);
      if (key === parentItem) fail('INVALID_ATTACHMENT_PARENT');
      return createObject('items', key, { itemType: 'attachment', parentItem, linkMode: 'imported_file',
        title: safeText(title, 10000), contentType: 'application/pdf', filename: filenameOf(filename),
        tags: tagsOf(tags), relations: {}, md5: null, mtime: null });
    }),
    authorizePdfUpload: safe(async ({ itemKey, parentItem, bytes, filename, mtime, previousMd5 = null } = {}) => {
      requirePermission({ write: true, files: true }); requireKey(itemKey); requireKey(parentItem);
      filenameOf(filename);
      if (!integer(mtime) || (previousMd5 !== null && !MD5.test(previousMd5))) fail('INVALID_UPLOAD_METADATA');
      const owned = pdfBytes(bytes);
      const hashes = await digest(owned);
      const item = attachmentOf(await getObject('items', itemKey), parentItem);
      if ((item.data.md5 || null) !== previousMd5) fail('WRITE_CONFLICT');
      const condition = previousMd5 === null ? { 'If-None-Match': '*' } : { 'If-Match': previousMd5 };
      const state = { itemKey, parentItem, filename, mtime, previousMd5, ...hashes, bytes: owned,
        size: owned.length, condition, stage: 'authorizing' };
      const authorization = await api(`${prefix}/items/${itemKey}/file`, { method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded', ...condition },
        body: new URLSearchParams({ md5: hashes.md5, filename, filesize: String(owned.length), mtime: String(mtime) }).toString(),
      });
      if (!record(authorization)) fail('INVALID_UPLOAD_AUTHORIZATION', { uncertain: true });
      if (authorization.exists === 1) {
        state.stage = 'registered'; state.bytes = null;
      } else {
        const target = storageURL(authorization.url);
        if (typeof authorization.contentType !== 'string' || authorization.contentType.length > 512 ||
            !/^multipart\/form-data;\s*boundary=[A-Za-z0-9'()+_,.\/:=? -]+$/.test(authorization.contentType) ||
            typeof authorization.prefix !== 'string' || typeof authorization.suffix !== 'string' ||
            encoder.encode(authorization.prefix).length + encoder.encode(authorization.suffix).length > 128 * 1024 ||
            typeof authorization.uploadKey !== 'string' || !/^[A-Za-z0-9_-]{1,256}$/.test(authorization.uploadKey)) fail('INVALID_UPLOAD_AUTHORIZATION', { uncertain: true });
        Object.assign(state, { target, contentType: authorization.contentType, prefix: authorization.prefix,
          suffix: authorization.suffix, uploadKey: authorization.uploadKey, stage: 'authorized' });
      }
      const ticket = Object.freeze({ kind: 'zotero-pdf-upload' });
      uploads.set(ticket, state);
      return { ticket, itemKey, exists: authorization.exists === 1, size: state.size, ...hashes };
    }),
    transferPdfUpload: safe(async ticket => {
      const state = ticketState(ticket, ['authorized']);
      state.stage = 'transferring'; // Single-use: an ambiguous transfer must be reconciled, never blindly replayed.
      try {
        await request(state.target, { method: 'POST', storage: true, expected: [201],
          headers: { 'Content-Type': state.contentType },
          body: new Blob([state.prefix, state.bytes, state.suffix]), consume: () => undefined });
        state.stage = 'transferred'; state.bytes = null;
        return { transferred: true, registered: false, storedBytesVerified: false };
      } catch (error) { state.stage = 'transfer_failed'; throw error; }
    }),
    registerPdfUpload: safe(async ticket => {
      const state = ticketState(ticket, ['transferred']);
      state.stage = 'registering';
      try {
        await api(`${prefix}/items/${state.itemKey}/file`, { method: 'POST', expected: [204],
          headers: { 'Content-Type': 'application/x-www-form-urlencoded', ...state.condition },
          body: new URLSearchParams({ upload: state.uploadKey }).toString(), consume: () => undefined });
        state.stage = 'registered';
        delete state.uploadKey; delete state.target; delete state.prefix; delete state.suffix;
        return { registered: true, storedBytesVerified: false };
      } catch (error) { state.stage = 'registration_failed'; throw error; }
    }),
    verifyPdfUpload: safe(async ticket => {
      const known = record(ticket) && uploads.get(ticket);
      try {
        const state = ticketState(ticket, ['registered', 'verified']);
        const proof = await verifyStored(state);
        state.stage = 'verified';
        return proof;
      } catch (error) {
        if (error instanceof ZoteroTransportError && known && ['registered', 'verified'].includes(known.stage)) error.uncertain = true;
        throw error;
      }
    }),
    verifyStoredPdf: safe(async ({ itemKey, parentItem, filename, mtime, size, md5, sha256 } = {}) => {
      requireKey(itemKey); requireKey(parentItem); filenameOf(filename);
      if (!integer(mtime) || !integer(size) || size < 8 || size > maxPdfBytes || !MD5.test(md5) || !SHA256.test(sha256)) fail('INVALID_UPLOAD_METADATA');
      return verifyStored({ itemKey, parentItem, filename, mtime, size, md5, sha256 });
    }),
  };
  methods.uploadStoredPdf = safe(async input => {
    const authorization = await methods.authorizePdfUpload(input);
    try {
      if (!authorization.exists) {
        await methods.transferPdfUpload(authorization.ticket);
        await methods.registerPdfUpload(authorization.ticket);
      }
      return await methods.verifyPdfUpload(authorization.ticket);
    } catch (error) {
      if (error instanceof ZoteroTransportError) error.uncertain = true;
      throw error;
    }
  });
  return Object.freeze(methods);
}
