/** Owner-authorized application wiring. Disabled unless approved runtime gates exist. */
import { createZoteroTransport, ZoteroTransportError } from './zotero-transport.mjs';
import { hashBytes } from './hash-bytes.mjs';
import { createExecutionJob, advanceExecutionJob, readExecutionJob, ExecutionError } from './job-runner.mjs';
import { readRegisteredCatalog, privateBlobKey } from './catalog-registration.mjs';
import { renderManagerPage } from './manager-ui.mjs';
import { handleRegistrationRoute } from './registration-routes.mjs';
import { renderRegistrationPage } from './registration-ui.mjs';
import { renderPrivateBundlePage } from './private-bundle-ui.mjs';
import { deploymentBundleManifest, deploymentBundleContainment, bundleConfig, bundledCatalogsReady, bundleAllowsCatalog, bundleConnectionMatches, handlePrivateBundleRoute } from './private-bundles.mjs';
import { providerMode, providerCredential, providerBound, sameProviderBinding, providerSecret,
  credentialEpoch, keyFingerprint, saveVerifiedProvider, revokeProvider, disconnectCredentials,
  personalLibraryId, validApiKey } from './provider-credentials.mjs';

const MAX_PDF_BYTES = 16 * 1024 * 1024;
const ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const JOB_ID = /^ze_[a-f0-9]{64}$/;
const SHA = /^[a-f0-9]{64}$/;
const encoder = new TextEncoder();
const sql = (db, statement, ...args) => db.prepare(statement).bind(...args);
const exact = (body, keys) => body && typeof body === 'object' && !Array.isArray(body)
  && Object.keys(body).length === keys.length && keys.every(key => Object.hasOwn(body, key));
const jsonObject = text => { try { const value = JSON.parse(text); return value && typeof value === 'object' && !Array.isArray(value) ? value : {}; } catch { return {}; } };
function canonicalBase64(value, maxBytes) {
  if (typeof value !== 'string' || value.length > Math.ceil(maxBytes / 3) * 4 || !/^[A-Za-z0-9+/]*={0,2}$/.test(value)) return null;
  try {
    const raw = atob(value);
    if (btoa(raw) !== value || raw.length > maxBytes) return null;
    return Uint8Array.from(raw, char => char.charCodeAt(0));
  } catch { return null; }
}
function runtimeReady(env) { return env.ZOTERO_EXECUTION_ENABLED === 'approved' && connectionRuntimeReady(env); }
function connectionRuntimeReady(env) {
  return (providerMode(env) || (typeof env.ZOTERO_VERIFIED_PERSONAL_LIBRARY_ID === 'string'
    && /^[1-9]\d{0,15}$/.test(env.ZOTERO_VERIFIED_PERSONAL_LIBRARY_ID)
    && Number.isSafeInteger(Number(env.ZOTERO_VERIFIED_PERSONAL_LIBRARY_ID))
    && typeof env.ZOTERO_ENCRYPTION_KEY_ID === 'string' && ID.test(env.ZOTERO_ENCRYPTION_KEY_ID)
    && typeof env.ZOTERO_CREDENTIAL_ENCRYPTION_KEY_B64 === 'string'
    && /^[A-Za-z0-9+/]{43}=$/.test(env.ZOTERO_CREDENTIAL_ENCRYPTION_KEY_B64)))
    && env.BUCKET && typeof env.BUCKET.get === 'function';
}
async function credentialRow(env, db, ownerId) {
  if (providerMode(env)) return providerCredential(db, ownerId);
  return sql(db, `SELECT owner_id, library_type, library_id, key_id, grant_id, cipher_version,
    iv_b64, ciphertext_b64, state FROM zotero_encrypted_credentials WHERE owner_id=?`, ownerId).first();
}
async function activeRow(db, ownerId) {
  return sql(db, `SELECT owner_id, library_type, library_id, connection_version, state, verified_at,
    credential_key_id, grant_id, permissions_json, storage_mode, credential_id FROM zotero_active_connections WHERE owner_id=?`, ownerId).first();
}
function boundCredential(env, ownerId, row) {
  if (providerMode(env)) return providerBound(ownerId, row);
  return row && row.owner_id === ownerId && row.library_type === 'user'
    && row.library_id === env.ZOTERO_VERIFIED_PERSONAL_LIBRARY_ID
    && row.key_id === env.ZOTERO_ENCRYPTION_KEY_ID && row.cipher_version === 'AES-256-GCM.v1'
    && row.state === 'encrypted_pending_verification' && ID.test(row.grant_id);
}
function eligibleConnection(env, ownerId, active, credential, requireExecution = true) {
  if (!(requireExecution ? runtimeReady(env) : connectionRuntimeReady(env)) || !boundCredential(env, ownerId, credential) || !active
      || active.owner_id !== ownerId || active.library_type !== 'user'
      || active.library_id !== credential.library_id || active.state !== 'active'
      || !Number.isSafeInteger(active.connection_version) || active.connection_version < 1
      || (providerMode(env) ? active.storage_mode !== 'provider-managed-d1' || active.credential_id !== credential.credential_id
        : active.storage_mode !== 'aead' || active.credential_key_id !== credential.key_id || active.grant_id !== credential.grant_id)) return false;
  const p = jsonObject(active.permissions_json);
  return p.library === true && p.write === true && p.files === true && (!providerMode(env) || p.notes === false);
}
export async function applicationConnectionStatus(env, auth, legacy) {
  const base = providerMode(env) ? { credential_storage: 'not_stored', credential_entry_enabled: false, reason: 'setup_required' } : await legacy(env, auth);
  const [credential, active] = await Promise.all([credentialRow(env, auth.db, auth.owner), activeRow(auth.db, auth.owner)]);
  const ready = eligibleConnection(env, auth.owner, active, credential, !providerMode(env));
  const catalogReady = await bundledCatalogsReady(env, auth);
  let bundleMatch = true;
  try { const cfg=bundleConfig(env,auth); if(cfg)bundleMatch=bundleConnectionMatches(cfg,active); } catch { bundleMatch=false; }
  const live = ready && runtimeReady(env) && catalogReady && bundleMatch;
  return { ...base, status: ready ? 'connected' : 'not_connected', ready, live_ready: live,
    live_gate_enabled: Boolean(runtimeReady(env)) && !env.__privateBundleContainment,
    ...(env.__privateBundleContainment ? {catalog_preparation_unavailable:true} : {}), ...((env.__privateBundleManifest || env.ZOTERO_PRIVATE_BUNDLES_REQUIRED==='approved') ? {catalog_ready:catalogReady} : {}),
    verification_enabled: Boolean((providerMode(env) ? connectionRuntimeReady(env) : runtimeReady(env)) && boundCredential(env, auth.owner, credential)),
    credential_storage: providerMode(env) ? credential ? 'provider_managed_d1' : 'not_stored' : ready ? 'encrypted_verified' : base.credential_storage,
    credential_entry_mode: providerMode(env) ? 'provider-managed-d1' : 'aead',
    credential_entry_enabled: providerMode(env) ? Boolean(connectionRuntimeReady(env) && env.ZOTERO_CREDENTIAL_ENTRY_ENABLED === 'approved' && !credential
      && !await sql(auth.db, 'SELECT owner_id FROM zotero_encrypted_credentials WHERE owner_id=?', auth.owner).first()) : base.credential_entry_enabled,
    disconnect_enabled: Boolean(credential || active),
    connection_version: ready ? active.connection_version : null,
    last_verified_at: active?.verified_at || null,
    reason: ready ? null : credential ? 'connection_verification_required' : base.reason };
}
async function currentConnection(env, auth, fail) {
  const [credential, connection] = await Promise.all([credentialRow(env, auth.db, auth.owner), activeRow(auth.db, auth.owner)]);
  if (!eligibleConnection(env, auth.owner, connection, credential)) fail(409, 'connection_required');
  if (!await executionReady(env,auth)) fail(409,'private_catalogs_required');
  return { connection, credential };
}
async function bundleConnection(env,auth) {
  const [credential,connection]=await Promise.all([credentialRow(env,auth.db,auth.owner),activeRow(auth.db,auth.owner)]);
  return eligibleConnection(env,auth.owner,connection,credential,false) ? connection : null;
}
async function executionReady(env,auth) {
  if(env.__privateBundleContainment)return false;
  if(!runtimeReady(env))return false;
  if(!env.__privateBundleManifest)return env.ZOTERO_PRIVATE_BUNDLES_REQUIRED!=='approved';
  try{const cfg=bundleConfig(env,auth);return bundleConnectionMatches(cfg,await bundleConnection(env,auth)) && await bundledCatalogsReady(env,auth);}catch{return false;}
}
async function decryptCredential(env, auth, row, fail) {
  if (!connectionRuntimeReady(env) || !boundCredential(env, auth.owner, row)) fail(409, 'connection_required');
  if (providerMode(env)) return providerSecret(auth.db, auth.owner, row, fail);
  const keyBytes = canonicalBase64(env.ZOTERO_CREDENTIAL_ENCRYPTION_KEY_B64, 32);
  const iv = canonicalBase64(row.iv_b64, 12);
  const ciphertext = canonicalBase64(row.ciphertext_b64, 4096);
  let plaintext;
  try {
    if (!keyBytes || keyBytes.length !== 32 || !iv || iv.length !== 12 || !ciphertext || ciphertext.length < 17) fail(409, 'credential_unavailable');
    const key = await crypto.subtle.importKey('raw', keyBytes, { name: 'AES-GCM' }, false, ['decrypt']);
    const aad = encoder.encode(JSON.stringify({ version: 'AES-256-GCM.v1', owner_id: auth.owner,
      library_type: 'user', library_id: row.library_id, key_id: row.key_id, grant_id: row.grant_id }));
    plaintext = new Uint8Array(await crypto.subtle.decrypt({ name: 'AES-GCM', iv, additionalData: aad, tagLength: 128 }, key, ciphertext));
    const apiKey = new TextDecoder('utf-8', { fatal: true }).decode(plaintext);
    if (!/^[A-Za-z0-9]{16,128}$/.test(apiKey)) fail(409, 'credential_unavailable');
    return apiKey; // Only a short-lived server closure receives this string.
  } catch { fail(409, 'credential_unavailable'); }
  finally { keyBytes?.fill(0); iv?.fill(0); ciphertext?.fill(0); plaintext?.fill(0); }
}
function bindingMatches(left, right) {
  if (left?.credential_id) return sameProviderBinding(left, right);
  return right && ['owner_id','library_type','library_id','key_id','grant_id','cipher_version','iv_b64','ciphertext_b64','state']
    .every(field => left[field] === right[field]);
}
async function revokeCredentialBinding(auth, credential) {
  if (credential?.credential_id) return revokeProvider(auth.db, auth.owner, credential);
  await sql(auth.db, `UPDATE zotero_active_connections SET state='revoked'
    WHERE owner_id=? AND library_type='user' AND library_id=? AND credential_key_id=? AND grant_id=?
      AND EXISTS (SELECT 1 FROM zotero_encrypted_credentials c WHERE c.owner_id=?
        AND c.library_id=? AND c.key_id=? AND c.grant_id=? AND c.iv_b64=? AND c.ciphertext_b64=?)`,
    auth.owner, credential.library_id, credential.key_id, credential.grant_id,
    auth.owner, credential.library_id, credential.key_id, credential.grant_id,
    credential.iv_b64, credential.ciphertext_b64).run();
}
async function transportFor(env, auth, credential, fail, fetchImpl, guardRequest = async () => {}, noteBackoff = async () => {}) {
  let apiKey = await decryptCredential(env, auth, credential, fail);
  try {
    const realFetch = fetchImpl || globalThis.fetch;
    if (typeof realFetch !== 'function') fail(503, 'transport_unavailable');
    const transport = createZoteroTransport({
      fetchImpl: async (url, options) => {
        if (!connectionRuntimeReady(env)) fail(409, 'connection_required');
        await guardRequest();
        const current = await credentialRow(env, auth.db, auth.owner);
        if (!bindingMatches(credential, current)) fail(409, 'credential_changed');
        const response = await realFetch(url, options);
        if (new URL(url).origin === 'https://api.zotero.org' && [401, 403].includes(response.status))
          await revokeCredentialBinding(auth, credential);
        let delayMs = 0;
        for (const header of ['Backoff', 'Retry-After']) {
          const value = response.headers.get(header);
          if (value && /^\d+(?:\.\d+)?$/.test(value.trim())) delayMs = Math.max(delayMs, Number(value) * 1000);
          else if (header === 'Retry-After' && value) {
            const date = Date.parse(value);
            if (Number.isFinite(date)) delayMs = Math.max(delayMs, date - Date.now());
          }
        }
        if (delayMs > 0 && Number.isFinite(delayMs)) await noteBackoff(Math.ceil(delayMs));
        return response;
      }, userID: credential.library_id, apiKey, hashBytes, maxPdfBytes: MAX_PDF_BYTES,
      // No long sleeps within a Worker invocation; the runner persists backoff.
      maxReadRetries: 0, sleep: async ms => { throw new ZoteroTransportError('BACKOFF_REQUIRED', { retryAfterMs: ms }); },
    });
    return Object.freeze({ ...transport, async verifyCurrentKey(requirements) {
      try { return await transport.verifyCurrentKey(providerMode(env)
        ? { requireWrite: true, requireFiles: true, requireNotes: false, exactPersonalScope: true } : requirements); }
      catch (error) {
        if (error instanceof ZoteroTransportError && ['AUTHORIZATION_DENIED','KEY_OWNER_MISMATCH','INSUFFICIENT_PERMISSION','EXCESS_PERMISSION'].includes(error.code))
          await revokeCredentialBinding(auth, credential);
        throw error;
      }
    } });
  } finally { apiKey = ''; }
}
async function verificationThrottle(env, auth, libraryId) {
  return {
    async guardRequest() {
      const row = await sql(auth.db, 'SELECT not_before FROM zotero_execution_leases WHERE library_id=?', libraryId).first();
      if (row?.not_before > Date.now()) throw new ZoteroTransportError('BACKOFF_REQUIRED', { retryAfterMs: row.not_before - Date.now() });
    },
    async noteBackoff(milliseconds) {
      const until = Date.now() + milliseconds;
      if (!Number.isSafeInteger(until) || milliseconds < 0) throw new ZoteroTransportError('INVALID_BACKOFF');
      const result = await sql(auth.db, `INSERT INTO zotero_execution_leases (library_id,owner_id,not_before)
        VALUES (?,?,?) ON CONFLICT(library_id) DO UPDATE SET not_before=MAX(not_before,excluded.not_before)`,
        libraryId, auth.owner, until).run();
      if (result.success === false) throw new ZoteroTransportError('STORAGE_UNAVAILABLE');
    },
  };
}
async function verifyConnection(request, env, auth, helpers, options) {
  const { requireAction, readJson, fail, json } = helpers;
  requireAction(request, auth.origin);
  if (!(providerMode(env) ? connectionRuntimeReady(env) : runtimeReady(env))) fail(409, 'setup_required');
  const body = await readJson(request);
  if (!exact(body, ['action']) || body.action !== 'verify_connection') fail(400, 'invalid_verification_request');
  const credential = await credentialRow(env, auth.db, auth.owner);
  if (!boundCredential(env, auth.owner, credential)) fail(409, 'credential_unavailable');
  const epoch = providerMode(env) ? await credentialEpoch(auth.db, auth.owner) : null;
  const throttle = await verificationThrottle(env, auth, credential.library_id);
  await throttle.guardRequest();
  const transport = await transportFor(env, auth, credential, fail, options.fetchImpl, throttle.guardRequest, throttle.noteBackoff);
  try {
    const permissions = await transport.verifyCurrentKey({ requireWrite: true, requireFiles: true, requireNotes: false });
    if (!connectionRuntimeReady(env) || !bindingMatches(credential, await credentialRow(env, auth.db, auth.owner))) fail(409, 'credential_changed');
    if (providerMode(env)) {
      const saved = await sql(auth.db, `UPDATE zotero_active_connections SET state='active', verified_at=?
        WHERE owner_id=? AND storage_mode='provider-managed-d1' AND credential_id=?
        AND EXISTS (SELECT 1 FROM zotero_provider_credentials WHERE owner_id=? AND credential_id=?)
        AND EXISTS (SELECT 1 FROM zotero_credential_epochs WHERE owner_id=? AND generation=?)`,
        new Date().toISOString(), auth.owner, credential.credential_id, auth.owner, credential.credential_id, auth.owner, epoch).run();
      if (saved.success === false) fail(503, 'storage_unavailable');
      if (saved.meta?.changes !== 1) fail(409, 'credential_changed');
      return json(await applicationConnectionStatus(env, auth, helpers.legacyConnectionStatus));
    }
    const existing = await activeRow(auth.db, auth.owner);
    const same = existing && existing.library_id === credential.library_id && existing.credential_key_id === credential.key_id
      && existing.grant_id === credential.grant_id && existing.state === 'active';
    const version = same ? existing.connection_version : (existing?.connection_version || 0) + 1;
    if (!Number.isSafeInteger(version)) fail(409, 'connection_version_invalid');
    const safePermissions = { library: permissions.library === true, write: permissions.write === true,
      files: permissions.files === true, notes: permissions.notes === true };
    const saved = await sql(auth.db, `INSERT INTO zotero_active_connections
      (owner_id, library_type, library_id, connection_version, state, verified_at, credential_key_id, grant_id, permissions_json)
      VALUES (?, 'user', ?, ?, 'active', ?, ?, ?, ?)
      ON CONFLICT(owner_id) DO UPDATE SET library_type='user', library_id=excluded.library_id,
        connection_version=excluded.connection_version, state='active', verified_at=excluded.verified_at,
        credential_key_id=excluded.credential_key_id, grant_id=excluded.grant_id, permissions_json=excluded.permissions_json,
        storage_mode='aead',credential_id=''`,
      auth.owner, credential.library_id, version, new Date().toISOString(), credential.key_id,
      credential.grant_id, JSON.stringify(safePermissions)).run();
    if (saved.success === false) fail(503, 'storage_unavailable');
    return json(await applicationConnectionStatus(env, auth, helpers.legacyConnectionStatus));
  } catch (error) {
    // A rejected key must not leave a previously active connection enabled.
    if (error instanceof ZoteroTransportError && ['AUTHORIZATION_DENIED','KEY_OWNER_MISMATCH','INSUFFICIENT_PERMISSION','EXCESS_PERMISSION'].includes(error.code)) {
      await revokeCredentialBinding(auth, credential);
    }
    throw error;
  }
}
async function connectProvider(request, env, auth, helpers, options) {
  const { requireAction, readJson, fail, json } = helpers;
  requireAction(request, auth.origin);
  if (!connectionRuntimeReady(env) || env.ZOTERO_CREDENTIAL_ENTRY_ENABLED !== 'approved') fail(409, 'setup_required');
  const epoch = await credentialEpoch(auth.db, auth.owner);
  let body;
  try {
    body = await readJson(request, 4096);
    if (!exact(body, ['action', 'library_id', 'api_key']) || body.action !== 'connect_personal_library'
        || !personalLibraryId(body.library_id) || !validApiKey(body.api_key)) fail(400, 'invalid_credential_request');
    const [existing, active, legacy] = await Promise.all([providerCredential(auth.db, auth.owner), activeRow(auth.db, auth.owner),
      sql(auth.db, 'SELECT owner_id FROM zotero_encrypted_credentials WHERE owner_id=?', auth.owner).first()]);
    // Disconnect leaves the non-secret library binding. Switching personal libraries
    // needs a separately reviewed rebind; old uncertain jobs cannot silently move.
    if (active && active.library_id !== body.library_id) fail(409, 'library_rebind_required');
    if (legacy) fail(409, 'disconnect_required');
    const fingerprint = await keyFingerprint(body.api_key);
    if (existing && (existing.library_id !== body.library_id || existing.key_fingerprint !== fingerprint)) fail(409, 'disconnect_required');
    const throttle = await verificationThrottle(env, auth, body.library_id);
    await throttle.guardRequest();
    const realFetch = options.fetchImpl || globalThis.fetch;
    if (typeof realFetch !== 'function') fail(503, 'transport_unavailable');
    const transport = createZoteroTransport({ userID: body.library_id, apiKey: body.api_key, hashBytes,
      maxReadRetries: 0, sleep: async ms => { throw new ZoteroTransportError('BACKOFF_REQUIRED', { retryAfterMs: ms }); },
      fetchImpl: async (url, init) => {
        await throttle.guardRequest();
        if (url !== 'https://api.zotero.org/keys/current' || init.method !== 'GET') fail(409, 'invalid_verification_request');
        const response = await realFetch(url, init);
        const delays = ['Backoff', 'Retry-After'].map(name => response.headers.get(name));
        const seconds = Math.max(0, ...delays.filter(value => value && /^\d+$/.test(value)).map(Number));
        if (seconds > 0 && Number.isSafeInteger(seconds * 1000)) await throttle.noteBackoff(seconds * 1000);
        return response;
      },
    });
    let permissions;
    try { permissions = await transport.verifyCurrentKey({ requireWrite: true, requireFiles: true, requireNotes: false, exactPersonalScope: true }); }
    catch (error) {
      if (existing && error instanceof ZoteroTransportError && ['AUTHORIZATION_DENIED','KEY_OWNER_MISMATCH','INSUFFICIENT_PERMISSION','EXCESS_PERMISSION'].includes(error.code))
        await revokeProvider(auth.db, auth.owner, existing);
      throw error;
    }
    if (!connectionRuntimeReady(env) || env.ZOTERO_CREDENTIAL_ENTRY_ENABLED !== 'approved') fail(409, 'setup_required');
    await saveVerifiedProvider({ db: auth.db, owner: auth.owner, libraryId: body.library_id, apiKey: body.api_key,
      fingerprint, epoch, existing, permissions: { library: permissions.library, write: permissions.write, files: permissions.files, notes: false }, fail });
    body.api_key = '';
    return json(await applicationConnectionStatus(env, auth, helpers.legacyConnectionStatus), existing ? 200 : 201);
  } finally { if (body && typeof body === 'object') body.api_key = ''; }
}
async function registeredCatalog(auth, reportId, catalogId, fail) {
  if (!ID.test(reportId) || !ID.test(catalogId)) fail(400, 'invalid_catalog_id');
  try {
    const verified = await readRegisteredCatalog({ db: auth.db, ownerId: auth.owner, reportId, catalogId });
    if (!verified) fail(404, 'catalog_unavailable');
    return verified;
  }
  catch { fail(404, 'catalog_unavailable'); }
}
function displayCatalog(verified) {
  return { report_id: verified.row.report_id, catalog_id: verified.row.catalog_id,
    catalog_version: verified.row.catalog_version, catalog_sha256: verified.row.content_sha256,
    report_date: verified.row.report_day,
    papers: verified.papers.map(paper => ({ paper_id: paper.paper_id, title: paper.title,
      authors: paper.authors, publication: paper.publication, publication_type: paper.item_type,
      venue: paper.venue, date: paper.year, topics: paper.topics, doi: paper.doi || '', pdf_available: Boolean(paper.pdf) })) };
}
async function listCatalogs(auth, env) {
  if (!await bundledCatalogsReady(env,auth)) return [];
  const bundle=bundleConfig(env,auth);
  const result = await sql(auth.db, `SELECT report_id, catalog_id, catalog_version, report_day
    FROM zotero_report_catalogs WHERE owner_id=? AND enabled=1 ORDER BY report_day DESC, report_id DESC LIMIT 100`, auth.owner).all();
  const catalogs = [];
  for (const row of result.results || []) {
    if (!bundleAllowsCatalog(bundle,row.report_id,row.catalog_id)) continue;
    try {
      const verified = await readRegisteredCatalog({ db: auth.db, ownerId: auth.owner, reportId: row.report_id, catalogId: row.catalog_id });
      catalogs.push({ report_id: row.report_id, catalog_id: row.catalog_id, catalog_version: row.catalog_version,
        report_date: verified.row.report_day, title: `${verified.row.report_day} 研究日报${bundle ? ' · 第'+(bundle.batches.findIndex(b=>b.config.binding.report_id===row.report_id && b.config.binding.catalog_id===row.catalog_id)+1)+'/'+bundle.batches.length+'批' : ''}`, paper_count: verified.papers.length });
    } catch { /* Unverified/incomplete rows cannot become selectable merely by listing. */ }
  }
  return catalogs;
}
async function boundedBlob(bucket, key, expectedSize) {
  const object = await bucket.get(key);
  if (!object) return null;
  if (object.size !== undefined && object.size !== expectedSize) throw new Error('BLOB_SIZE_MISMATCH');
  if (object.body?.getReader) {
    const reader = object.body.getReader(); const chunks = []; let size = 0;
    try {
      while (true) {
        const { done, value } = await reader.read(); if (done) break;
        size += value.byteLength;
        if (size > expectedSize || size > MAX_PDF_BYTES) { await reader.cancel(); throw new Error('BLOB_SIZE_MISMATCH'); }
        chunks.push(value);
      }
      if (size !== expectedSize) throw new Error('BLOB_SIZE_MISMATCH');
      const result = new Uint8Array(size); let offset = 0;
      for (const chunk of chunks) { result.set(chunk, offset); offset += chunk.length; }
      return result;
    } finally { reader.releaseLock(); }
  }
  // R2 exposes body and arrayBuffer; this fallback supports bounded test adapters.
  if (object.size !== expectedSize || typeof object.arrayBuffer !== 'function') throw new Error('BLOB_UNAVAILABLE');
  const bytes = new Uint8Array(await object.arrayBuffer());
  if (bytes.length !== expectedSize) throw new Error('BLOB_SIZE_MISMATCH');
  return bytes;
}
function blobReader(env, auth, fail) {
  return { async readBlob({ ownerId, reportId, catalogId, paperId, sha256, size }) {
    if (ownerId !== auth.owner || !SHA.test(sha256) || !Number.isSafeInteger(size) || size < 8 || size > MAX_PDF_BYTES) fail(400, 'invalid_blob_reference');
    const verified = await registeredCatalog(auth, reportId, catalogId, fail);
    const paper = verified.papers.find(value => value.paper_id === paperId);
    if (!paper?.pdf || paper.pdf.sha256 !== sha256 || paper.pdf.blob_id !== sha256 || paper.pdf.size !== size) fail(409, 'blob_not_in_catalog');
    return boundedBlob(env.BUCKET, await privateBlobKey(auth.owner, sha256), size);
  } };
}
async function executionView(env, auth, jobId) {
  const [connection, credential] = await Promise.all([activeRow(auth.db, auth.owner), credentialRow(env, auth.db, auth.owner)]);
  return readExecutionJob({ db: auth.db, ownerId: auth.owner, jobId, connection,
    liveGate: eligibleConnection(env, auth.owner, connection, credential) && await executionReady(env,auth) });
}
async function advance(env, auth, jobId, helpers, options) {
  const { connection, credential } = await currentConnection(env, auth, helpers.fail);
  if(env.__privateBundleManifest){const job=await sql(auth.db,'SELECT report_id,catalog_id FROM zotero_execution_jobs WHERE owner_id=? AND job_id=?',auth.owner,jobId).first();
    if(!job || !bundleAllowsCatalog(bundleConfig(env,auth),job.report_id,job.catalog_id))helpers.fail(409,'catalog_not_allowed');}
  return advanceExecutionJob({ db: auth.db, ownerId: auth.owner, jobId, connection, liveGate: () => executionReady(env,auth),
    transportFactory: ({ guardRequest, noteBackoff }) =>
      transportFor(env, auth, credential, helpers.fail, options.fetchImpl, guardRequest, noteBackoff), blobs: blobReader(env, auth, helpers.fail), hashBytes, maxSteps: 1 });
}

export async function handleApplicationRoute(request, env, auth, helpers, options = {}) {
  const { json, requireGet, requireAction, readJson, fail } = helpers;
  // Only compiled server code or trusted test injection supplies these pins.
  env={...env,__privateBundleManifest:options.bundleManifest === undefined ? deploymentBundleManifest() : options.bundleManifest,
    __privateBundleContainment:options.bundleContainment === undefined ? deploymentBundleContainment() : options.bundleContainment};
  const url = new URL(request.url);
  try {
    const bundles=await handlePrivateBundleRoute(request,env,auth,{...helpers,bundleConnection:()=>bundleConnection(env,auth)});
    if(bundles)return bundles;
    if((env.__privateBundleContainment || env.__privateBundleManifest || env.ZOTERO_PRIVATE_BUNDLES_REQUIRED==='approved') && url.pathname.startsWith('/api/zotero/catalog-registration')) fail(404,'not_found');
    const registration = await handleRegistrationRoute(request, env, auth, helpers);
    if (registration) return registration;
    if (url.pathname === '/zotero/register') { requireGet(request); return env.__privateBundleManifest ? renderPrivateBundlePage({contained:env.__privateBundleContainment}) : renderRegistrationPage(); }
    if (url.pathname === '/zotero' || url.pathname === '/zotero/') {
      requireGet(request);
      return renderManagerPage({ connection: await applicationConnectionStatus(env, auth, helpers.legacyConnectionStatus) });
    }
    if (url.pathname === '/api/zotero/connection') {
      requireGet(request); return json(await applicationConnectionStatus(env, auth, helpers.legacyConnectionStatus));
    }
    if (url.pathname === '/api/zotero/connect' && providerMode(env)) return await connectProvider(request, env, auth, helpers, options);
    if (url.pathname === '/api/zotero/disconnect') {
      requireAction(request, auth.origin);
      const body = await readJson(request);
      if (!exact(body, ['action']) || body.action !== 'disconnect') fail(400, 'invalid_disconnect_request');
      await disconnectCredentials(auth.db, auth.owner, fail);
      return json({ ...await applicationConnectionStatus(env, auth, helpers.legacyConnectionStatus),
        message: 'Active credential deleted. Revoke the key at Zotero to invalidate retained backups. Requests already in flight may finish.' });
    }
    if (url.pathname === '/api/zotero/verify-connection') return await verifyConnection(request, env, auth, helpers, options);
    if (url.pathname === '/api/zotero/catalogs') {
      requireGet(request); return json({ catalogs: await listCatalogs(auth,env), live_ready: (await applicationConnectionStatus(env, auth, helpers.legacyConnectionStatus)).live_ready });
    }
    const catalogMatch = url.pathname.match(/^\/api\/zotero\/reports\/([^/]+)\/catalogs\/([^/]+)$/);
    if (catalogMatch) {
      requireGet(request);
      if(!bundleAllowsCatalog(bundleConfig(env,auth),catalogMatch[1],catalogMatch[2]) || !await bundledCatalogsReady(env,auth))fail(404,'catalog_not_found');
      // Legacy read-only catalogs stay inspectable but are never offered by the
      // executable manager and cannot pass createExecutionJob's canonical checks.
      const legacy = await sql(auth.db, `SELECT catalog_blob_json FROM zotero_report_catalogs
        WHERE owner_id=? AND report_id=? AND catalog_id=?`, auth.owner, catalogMatch[1], catalogMatch[2]).first();
      if (legacy && !legacy.catalog_blob_json) return null;
      const verified = await registeredCatalog(auth, catalogMatch[1], catalogMatch[2], fail);
      return json({ ...displayCatalog(verified), catalog_verified: true, live_ready: (await applicationConnectionStatus(env, auth, helpers.legacyConnectionStatus)).live_ready });
    }
    if (url.pathname === '/api/zotero/executions') {
      if (request.method === 'GET') {
        const rows = await sql(auth.db, `SELECT job_id FROM zotero_execution_jobs WHERE owner_id=? ORDER BY created_at DESC LIMIT 50`, auth.owner).all();
        const jobs = [];
        for (const row of rows.results || []) jobs.push(await executionView(env, auth, row.job_id));
        return json({ jobs });
      }
      requireAction(request, auth.origin);
      const { connection } = await currentConnection(env, auth, fail);
      const body = await readJson(request);
      if(!bundleAllowsCatalog(bundleConfig(env,auth),body?.report_id,body?.catalog_id))fail(409,'catalog_not_allowed');
      // Runner independently verifies exact shape, selected allowlist and pinned catalog.
      const job = await createExecutionJob({ db: auth.db, ownerId: auth.owner, request: body, connection, liveGate: () => executionReady(env,auth) });
      return json(job, 201);
    }
    const jobMatch = url.pathname.match(/^\/api\/zotero\/executions\/([^/]+)(\/advance)?$/);
    if (jobMatch) {
      if (!JOB_ID.test(jobMatch[1])) fail(404, 'execution_not_found');
      if (!jobMatch[2]) { requireGet(request); return json(await executionView(env, auth, jobMatch[1])); }
      requireAction(request, auth.origin);
      const body = await readJson(request);
      if (!exact(body, ['action']) || body.action !== 'continue_import') fail(400, 'invalid_continue_request');
      return json(await advance(env, auth, jobMatch[1], helpers, options));
    }
    return null;
  } catch (error) {
    if (error instanceof ExecutionError) return json({ error: error.code.toLowerCase(), retry_after_ms: error.retryAfterMs ?? null, live_ready: false }, error.status || 409);
    if (error instanceof ZoteroTransportError) return json({ error: error.code.toLowerCase(), retry_after_ms: error.retryAfterMs ?? null,
      uncertain: error.uncertain, live_ready: false }, error.code === 'AUTHORIZATION_DENIED' ? 403 : 409);
    throw error; // Existing outer handler sanitizes all unknown exceptions.
  }
}
