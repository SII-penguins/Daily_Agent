-- Runnable execution stays disabled until a separately verified connection and live gate exist.
-- Legacy blocked/preview intents are deliberately not migrated into execution jobs.
ALTER TABLE zotero_report_catalogs ADD COLUMN catalog_blob_json TEXT;
ALTER TABLE zotero_report_catalogs ADD COLUMN report_sha256 TEXT;
ALTER TABLE zotero_report_catalogs ADD COLUMN report_day TEXT;
ALTER TABLE zotero_report_catalogs ADD COLUMN personal_scope TEXT;
ALTER TABLE zotero_report_catalogs ADD COLUMN source_catalog_sha256 TEXT;
ALTER TABLE zotero_report_catalogs ADD COLUMN registration_sha256 TEXT;

-- Contains only non-secret verification state. Credential storage remains separate.
CREATE TABLE zotero_active_connections (
  owner_id TEXT PRIMARY KEY,
  library_type TEXT NOT NULL CHECK (library_type = 'user'),
  library_id TEXT NOT NULL,
  connection_version INTEGER NOT NULL CHECK (connection_version > 0),
  state TEXT NOT NULL CHECK (state IN ('active', 'revoked')),
  verified_at TEXT NOT NULL,
  credential_key_id TEXT NOT NULL DEFAULT '',
  grant_id TEXT NOT NULL DEFAULT '',
  permissions_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE zotero_execution_jobs (
  owner_id TEXT NOT NULL,
  job_id TEXT NOT NULL,
  library_type TEXT NOT NULL CHECK (library_type = 'user'),
  library_id TEXT NOT NULL,
  connection_version INTEGER NOT NULL,
  report_id TEXT NOT NULL,
  catalog_id TEXT NOT NULL,
  catalog_version INTEGER NOT NULL,
  catalog_sha256 TEXT NOT NULL,
  paper_ids_json TEXT NOT NULL,
  pdf_intent TEXT NOT NULL CHECK (pdf_intent IN ('metadata_only', 'open_access_if_available')),
  state TEXT NOT NULL DEFAULT 'queued',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (owner_id, job_id),
  FOREIGN KEY (owner_id, report_id, catalog_id, catalog_version)
    REFERENCES zotero_report_catalogs(owner_id, report_id, catalog_id, catalog_version)
);

-- One lease for the entire personal library, including distinct selection jobs.
-- Fencing generations prevent an expired worker from committing after a takeover.
CREATE TABLE zotero_execution_leases (
  library_id TEXT PRIMARY KEY,
  owner_id TEXT NOT NULL,
  job_id TEXT,
  token TEXT,
  generation INTEGER NOT NULL DEFAULT 0,
  expires_at INTEGER NOT NULL DEFAULT 0,
  not_before INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE zotero_execution_items (
  owner_id TEXT NOT NULL,
  job_id TEXT NOT NULL,
  paper_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL,
  reserved_key TEXT NOT NULL,
  attachment_reserved_key TEXT NOT NULL,
  item_key TEXT,
  attachment_key TEXT,
  status TEXT NOT NULL DEFAULT 'queued',
  metadata_saved INTEGER NOT NULL DEFAULT 0,
  classification_saved INTEGER NOT NULL DEFAULT 0,
  existing_item INTEGER NOT NULL DEFAULT 0,
  pdf_verified INTEGER NOT NULL DEFAULT 0,
  pdf_proof_json TEXT,
  pending_operation TEXT,
  pending_key TEXT,
  uncertain INTEGER NOT NULL DEFAULT 0,
  error_code TEXT,
  retryable INTEGER NOT NULL DEFAULT 0,
  retry_at INTEGER,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (owner_id, job_id, paper_id),
  FOREIGN KEY (owner_id, job_id) REFERENCES zotero_execution_jobs(owner_id, job_id)
);

CREATE TABLE zotero_execution_collections (
  owner_id TEXT NOT NULL,
  job_id TEXT NOT NULL,
  path_json TEXT NOT NULL,
  depth INTEGER NOT NULL,
  reserved_key TEXT NOT NULL,
  collection_key TEXT,
  pending_operation TEXT,
  uncertain INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (owner_id, job_id, path_json),
  FOREIGN KEY (owner_id, job_id) REFERENCES zotero_execution_jobs(owner_id, job_id)
);

CREATE INDEX zotero_execution_jobs_owner_created ON zotero_execution_jobs(owner_id, created_at DESC);
CREATE INDEX zotero_execution_items_pending ON zotero_execution_items(owner_id, job_id, ordinal);
