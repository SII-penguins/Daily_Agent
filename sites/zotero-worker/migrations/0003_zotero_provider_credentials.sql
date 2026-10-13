-- Explicit provider-managed mode; existing AEAD credentials stay unchanged.
-- api_key is plaintext-usable. Cloudflare at-rest encryption is not application
-- encryption: administrators with D1 read access can read this column.
CREATE TABLE zotero_provider_credentials (
  owner_id TEXT NOT NULL PRIMARY KEY,
  library_type TEXT NOT NULL CHECK (library_type = 'user'),
  library_id TEXT NOT NULL,
  credential_id TEXT NOT NULL UNIQUE,
  key_fingerprint TEXT NOT NULL CHECK (length(key_fingerprint) = 64),
  api_key TEXT NOT NULL CHECK (length(api_key) BETWEEN 16 AND 128),
  verified_at TEXT NOT NULL
);
CREATE TABLE zotero_credential_epochs (
  owner_id TEXT NOT NULL PRIMARY KEY,
  generation INTEGER NOT NULL DEFAULT 0 CHECK (generation >= 0)
);
ALTER TABLE zotero_active_connections ADD COLUMN storage_mode TEXT NOT NULL DEFAULT 'aead';
ALTER TABLE zotero_active_connections ADD COLUMN credential_id TEXT NOT NULL DEFAULT '';
