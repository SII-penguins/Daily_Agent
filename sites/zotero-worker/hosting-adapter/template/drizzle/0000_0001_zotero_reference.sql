-- Schema only. Populate immutable catalogs through a separately authorized server pipeline.
PRAGMA foreign_keys = ON;
--> statement-breakpoint
CREATE TABLE zotero_report_catalogs (
  owner_id TEXT NOT NULL,
  report_id TEXT NOT NULL,
  catalog_id TEXT NOT NULL,
  catalog_version INTEGER NOT NULL CHECK (catalog_version > 0),
  content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
  enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
  created_at TEXT NOT NULL,
  PRIMARY KEY (owner_id, report_id, catalog_id),
  UNIQUE (owner_id, report_id, catalog_id, catalog_version)
);
--> statement-breakpoint
CREATE TABLE zotero_catalog_papers (
  owner_id TEXT NOT NULL,
  report_id TEXT NOT NULL,
  catalog_id TEXT NOT NULL,
  paper_id TEXT NOT NULL,
  display_title TEXT NOT NULL,
  -- Server-normalized allowlisted metadata only; never populated from browser input.
  metadata_json TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY (owner_id, report_id, catalog_id, paper_id),
  FOREIGN KEY (owner_id, report_id, catalog_id)
    REFERENCES zotero_report_catalogs(owner_id, report_id, catalog_id)
);
--> statement-breakpoint
CREATE TABLE zotero_import_intents (
  owner_id TEXT NOT NULL,
  job_id TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  report_id TEXT NOT NULL,
  catalog_id TEXT NOT NULL,
  catalog_version INTEGER NOT NULL,
  catalog_sha256 TEXT NOT NULL,
  paper_ids_json TEXT NOT NULL,
  pdf_intent TEXT NOT NULL CHECK (pdf_intent IN ('metadata_only', 'open_access_if_available')),
  state TEXT NOT NULL CHECK (state IN ('blocked_connection', 'preview_only')),
  created_at TEXT NOT NULL,
  PRIMARY KEY (owner_id, job_id),
  UNIQUE (owner_id, idempotency_key),
  FOREIGN KEY (owner_id, report_id, catalog_id, catalog_version)
    REFERENCES zotero_report_catalogs(owner_id, report_id, catalog_id, catalog_version)
);
--> statement-breakpoint
CREATE TABLE zotero_setup_grants_consumed (
  owner_id TEXT NOT NULL,
  grant_id TEXT NOT NULL,
  used_at TEXT NOT NULL,
  PRIMARY KEY (owner_id, grant_id)
);
--> statement-breakpoint
CREATE TABLE zotero_encrypted_credentials (
  owner_id TEXT NOT NULL PRIMARY KEY,
  library_type TEXT NOT NULL CHECK (library_type = 'user'),
  library_id TEXT NOT NULL,
  key_id TEXT NOT NULL,
  grant_id TEXT NOT NULL,
  cipher_version TEXT NOT NULL CHECK (cipher_version = 'AES-256-GCM.v1'),
  iv_b64 TEXT NOT NULL,
  ciphertext_b64 TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state = 'encrypted_pending_verification'),
  created_at TEXT NOT NULL,
  FOREIGN KEY (owner_id, grant_id)
    REFERENCES zotero_setup_grants_consumed(owner_id, grant_id)
);
--> statement-breakpoint
CREATE INDEX zotero_import_intents_owner_created
  ON zotero_import_intents(owner_id, created_at DESC);
