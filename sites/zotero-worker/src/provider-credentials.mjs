/** Provider-managed D1 credential storage. This is deliberately NOT application
 * encryption: api_key is plaintext-usable by the Worker and database administrators.
 * D1's provider-managed at-rest encryption protects storage media, not DB readers.
 * Never return these records to browsers, logs, exports, or operator diagnostics.
 */
const sql = (db, text, ...args) => db.prepare(text).bind(...args);
export const PROVIDER_STORAGE = 'provider-managed-d1';
export const providerMode = env => env.ZOTERO_CREDENTIAL_STORAGE === PROVIDER_STORAGE;
export const personalLibraryId = value => typeof value === 'string' && /^[1-9]\d{0,15}$/.test(value)
  && Number.isSafeInteger(Number(value));
export const validApiKey = value => typeof value === 'string' && /^[A-Za-z0-9]{16,128}$/.test(value);
export async function providerCredential(db, owner) {
  // Status and binding guards must never select api_key.
  return sql(db, `SELECT owner_id, library_type, library_id, credential_id, key_fingerprint, verified_at
    FROM zotero_provider_credentials WHERE owner_id=?`, owner).first();
}
export function providerBound(owner, row) {
  return row && row.owner_id === owner && row.library_type === 'user' && personalLibraryId(row.library_id)
    && /^[a-f0-9-]{36}$/.test(row.credential_id) && /^[a-f0-9]{64}$/.test(row.key_fingerprint);
}
export function sameProviderBinding(left, right) {
  return right && ['owner_id', 'library_type', 'library_id', 'credential_id', 'key_fingerprint']
    .every(field => left[field] === right[field]);
}
export async function providerSecret(db, owner, row, fail) {
  if (!providerBound(owner, row)) fail(409, 'credential_unavailable');
  const secret = await sql(db, `SELECT api_key FROM zotero_provider_credentials
    WHERE owner_id=? AND library_type='user' AND library_id=? AND credential_id=? AND key_fingerprint=?`,
    owner, row.library_id, row.credential_id, row.key_fingerprint).first();
  try {
    if (!validApiKey(secret?.api_key) || await keyFingerprint(secret.api_key) !== row.key_fingerprint) fail(409, 'credential_unavailable');
    return secret.api_key;
  } finally { if (secret) secret.api_key = ''; }
}
export async function credentialEpoch(db, owner) {
  // This non-secret counter binds no Zotero account. Disconnect advances it even
  // during an in-flight first verification, fencing delayed enrollment completion.
  const row = await sql(db, `INSERT INTO zotero_credential_epochs(owner_id,generation) VALUES(?,0)
    ON CONFLICT(owner_id) DO UPDATE SET generation=generation RETURNING generation`, owner).first();
  if (!Number.isSafeInteger(row?.generation) || row.generation < 0) throw Error('CREDENTIAL_EPOCH_UNAVAILABLE');
  return row.generation;
}
export async function keyFingerprint(key) {
  const bytes = new TextEncoder().encode(key);
  try { return Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)), v => v.toString(16).padStart(2, '0')).join(''); }
  finally { bytes.fill(0); }
}
export async function saveVerifiedProvider({ db, owner, libraryId, apiKey, fingerprint, epoch, existing, permissions, fail }) {
  const credentialId = existing?.credential_id || crypto.randomUUID();
  const now = new Date().toISOString();
  const args = [owner, libraryId, credentialId, fingerprint, apiKey, now, owner, epoch, owner];
  // The INSERT and active binding are atomic. Unique owner/credential IDs prevent
  // concurrent submissions from overwriting a different accepted credential.
  const results = await db.batch([
    sql(db, `INSERT INTO zotero_provider_credentials
      (owner_id,library_type,library_id,credential_id,key_fingerprint,api_key,verified_at)
      SELECT ?,'user',?,?,?,?,? WHERE EXISTS
        (SELECT 1 FROM zotero_credential_epochs WHERE owner_id=? AND generation=?)
      AND NOT EXISTS (SELECT 1 FROM zotero_encrypted_credentials WHERE owner_id=?)
      ON CONFLICT(owner_id) DO UPDATE SET verified_at=excluded.verified_at
      WHERE credential_id=excluded.credential_id AND library_id=excluded.library_id
        AND key_fingerprint=excluded.key_fingerprint`, ...args),
    sql(db, `INSERT INTO zotero_active_connections
      (owner_id,library_type,library_id,connection_version,state,verified_at,credential_key_id,grant_id,permissions_json,storage_mode,credential_id)
      SELECT ?,'user',?,1,'active',?,'','',?,'provider-managed-d1',?
      WHERE EXISTS (SELECT 1 FROM zotero_provider_credentials WHERE owner_id=? AND credential_id=? AND verified_at=?)
        AND EXISTS (SELECT 1 FROM zotero_credential_epochs WHERE owner_id=? AND generation=?)
      ON CONFLICT(owner_id) DO UPDATE SET library_type='user',library_id=excluded.library_id,
        connection_version=CASE WHEN storage_mode='provider-managed-d1' AND credential_id=excluded.credential_id
          AND library_id=excluded.library_id THEN connection_version ELSE connection_version+1 END,
        state='active',verified_at=excluded.verified_at,credential_key_id='',grant_id='',
        permissions_json=excluded.permissions_json,storage_mode='provider-managed-d1',credential_id=excluded.credential_id`,
      owner, libraryId, now, JSON.stringify(permissions), credentialId, owner, credentialId, now, owner, epoch),
  ]);
  if (results.some(result => result.success === false)) fail(503, 'storage_unavailable');
  if (results.some(result => result.meta?.changes !== 1)) fail(409, 'credential_changed');
}
export async function revokeProvider(db, owner, row) {
  await sql(db, `UPDATE zotero_active_connections SET state='revoked'
    WHERE owner_id=? AND storage_mode='provider-managed-d1' AND credential_id=?
    AND EXISTS (SELECT 1 FROM zotero_provider_credentials WHERE owner_id=? AND credential_id=?)`,
    owner, row.credential_id, owner, row.credential_id).run();
}
export async function disconnectCredentials(db, owner, fail) {
  const result = await db.batch([
    sql(db, `INSERT INTO zotero_credential_epochs(owner_id,generation) VALUES(?,1)
      ON CONFLICT(owner_id) DO UPDATE SET generation=generation+1`, owner),
    sql(db, "UPDATE zotero_active_connections SET state='revoked' WHERE owner_id=?", owner),
    sql(db, 'DELETE FROM zotero_provider_credentials WHERE owner_id=?', owner),
    sql(db, 'DELETE FROM zotero_encrypted_credentials WHERE owner_id=?', owner),
  ]);
  if (result.some(row => row.success === false)) fail(503, 'storage_unavailable');
}
