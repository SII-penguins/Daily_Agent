// Generated locally from immutable schema-only source SQL. No user records.
import { sql, desc } from "drizzle-orm";
import { sqliteTable, text, integer, blob, real, primaryKey, unique, index, uniqueIndex, foreignKey, check } from "drizzle-orm/sqlite-core";

export const zotero_report_catalogs = sqliteTable("zotero_report_catalogs", {
  owner_id: text("owner_id").notNull(),
  report_id: text("report_id").notNull(),
  catalog_id: text("catalog_id").notNull(),
  catalog_version: integer("catalog_version").notNull(),
  content_sha256: text("content_sha256").notNull(),
  enabled: integer("enabled").notNull().default(sql.raw("0")),
  created_at: text("created_at").notNull(),
  catalog_blob_json: text("catalog_blob_json"),
  report_sha256: text("report_sha256"),
  report_day: text("report_day"),
  personal_scope: text("personal_scope"),
  source_catalog_sha256: text("source_catalog_sha256"),
  registration_sha256: text("registration_sha256"),
}, (table) => [
  primaryKey({ columns: [table.owner_id, table.report_id, table.catalog_id] }),
  unique("zotero_report_catalogs_owner_id_report_id_catalog_id_catalog_version_unique").on(table.owner_id, table.report_id, table.catalog_id, table.catalog_version),
  check("zotero_report_catalogs_check_0", sql.raw("catalog_version > 0")),
  check("zotero_report_catalogs_check_1", sql.raw("length(content_sha256) = 64")),
  check("zotero_report_catalogs_check_2", sql.raw("enabled IN (0, 1)"))
]);

export const zotero_catalog_papers = sqliteTable("zotero_catalog_papers", {
  owner_id: text("owner_id").notNull(),
  report_id: text("report_id").notNull(),
  catalog_id: text("catalog_id").notNull(),
  paper_id: text("paper_id").notNull(),
  display_title: text("display_title").notNull(),
  metadata_json: text("metadata_json").notNull().default(sql.raw("'{}'")),
}, (table) => [
  primaryKey({ columns: [table.owner_id, table.report_id, table.catalog_id, table.paper_id] }),
  foreignKey({ columns: [table.owner_id, table.report_id, table.catalog_id], foreignColumns: [zotero_report_catalogs.owner_id, zotero_report_catalogs.report_id, zotero_report_catalogs.catalog_id] })
]);

export const zotero_import_intents = sqliteTable("zotero_import_intents", {
  owner_id: text("owner_id").notNull(),
  job_id: text("job_id").notNull(),
  idempotency_key: text("idempotency_key").notNull(),
  report_id: text("report_id").notNull(),
  catalog_id: text("catalog_id").notNull(),
  catalog_version: integer("catalog_version").notNull(),
  catalog_sha256: text("catalog_sha256").notNull(),
  paper_ids_json: text("paper_ids_json").notNull(),
  pdf_intent: text("pdf_intent").notNull(),
  state: text("state").notNull(),
  created_at: text("created_at").notNull(),
}, (table) => [
  primaryKey({ columns: [table.owner_id, table.job_id] }),
  index("zotero_import_intents_owner_created").on(table.owner_id, desc(table.created_at)),
  unique("zotero_import_intents_owner_id_idempotency_key_unique").on(table.owner_id, table.idempotency_key),
  foreignKey({ columns: [table.owner_id, table.report_id, table.catalog_id, table.catalog_version], foreignColumns: [zotero_report_catalogs.owner_id, zotero_report_catalogs.report_id, zotero_report_catalogs.catalog_id, zotero_report_catalogs.catalog_version] }),
  check("zotero_import_intents_check_0", sql.raw("pdf_intent IN ('metadata_only', 'open_access_if_available')")),
  check("zotero_import_intents_check_1", sql.raw("state IN ('blocked_connection', 'preview_only')"))
]);

export const zotero_setup_grants_consumed = sqliteTable("zotero_setup_grants_consumed", {
  owner_id: text("owner_id").notNull(),
  grant_id: text("grant_id").notNull(),
  used_at: text("used_at").notNull(),
}, (table) => [
  primaryKey({ columns: [table.owner_id, table.grant_id] })
]);

export const zotero_encrypted_credentials = sqliteTable("zotero_encrypted_credentials", {
  owner_id: text("owner_id").notNull(),
  library_type: text("library_type").notNull(),
  library_id: text("library_id").notNull(),
  key_id: text("key_id").notNull(),
  grant_id: text("grant_id").notNull(),
  cipher_version: text("cipher_version").notNull(),
  iv_b64: text("iv_b64").notNull(),
  ciphertext_b64: text("ciphertext_b64").notNull(),
  state: text("state").notNull(),
  created_at: text("created_at").notNull(),
}, (table) => [
  primaryKey({ columns: [table.owner_id] }),
  foreignKey({ columns: [table.owner_id, table.grant_id], foreignColumns: [zotero_setup_grants_consumed.owner_id, zotero_setup_grants_consumed.grant_id] }),
  check("zotero_encrypted_credentials_check_0", sql.raw("library_type = 'user'")),
  check("zotero_encrypted_credentials_check_1", sql.raw("cipher_version = 'AES-256-GCM.v1'")),
  check("zotero_encrypted_credentials_check_2", sql.raw("state = 'encrypted_pending_verification'"))
]);

export const zotero_active_connections = sqliteTable("zotero_active_connections", {
  owner_id: text("owner_id"),
  library_type: text("library_type").notNull(),
  library_id: text("library_id").notNull(),
  connection_version: integer("connection_version").notNull(),
  state: text("state").notNull(),
  verified_at: text("verified_at").notNull(),
  credential_key_id: text("credential_key_id").notNull().default(sql.raw("''")),
  grant_id: text("grant_id").notNull().default(sql.raw("''")),
  permissions_json: text("permissions_json").notNull().default(sql.raw("'{}'")),
}, (table) => [
  primaryKey({ columns: [table.owner_id] }),
  check("zotero_active_connections_check_0", sql.raw("library_type = 'user'")),
  check("zotero_active_connections_check_1", sql.raw("connection_version > 0")),
  check("zotero_active_connections_check_2", sql.raw("state IN ('active', 'revoked')"))
]);

export const zotero_execution_jobs = sqliteTable("zotero_execution_jobs", {
  owner_id: text("owner_id").notNull(),
  job_id: text("job_id").notNull(),
  library_type: text("library_type").notNull(),
  library_id: text("library_id").notNull(),
  connection_version: integer("connection_version").notNull(),
  report_id: text("report_id").notNull(),
  catalog_id: text("catalog_id").notNull(),
  catalog_version: integer("catalog_version").notNull(),
  catalog_sha256: text("catalog_sha256").notNull(),
  paper_ids_json: text("paper_ids_json").notNull(),
  pdf_intent: text("pdf_intent").notNull(),
  state: text("state").notNull().default(sql.raw("'queued'")),
  created_at: text("created_at").notNull(),
  updated_at: text("updated_at").notNull(),
}, (table) => [
  primaryKey({ columns: [table.owner_id, table.job_id] }),
  index("zotero_execution_jobs_owner_created").on(table.owner_id, desc(table.created_at)),
  foreignKey({ columns: [table.owner_id, table.report_id, table.catalog_id, table.catalog_version], foreignColumns: [zotero_report_catalogs.owner_id, zotero_report_catalogs.report_id, zotero_report_catalogs.catalog_id, zotero_report_catalogs.catalog_version] }),
  check("zotero_execution_jobs_check_0", sql.raw("library_type = 'user'")),
  check("zotero_execution_jobs_check_1", sql.raw("pdf_intent IN ('metadata_only', 'open_access_if_available')"))
]);

export const zotero_execution_leases = sqliteTable("zotero_execution_leases", {
  library_id: text("library_id"),
  owner_id: text("owner_id").notNull(),
  job_id: text("job_id"),
  token: text("token"),
  generation: integer("generation").notNull().default(sql.raw("0")),
  expires_at: integer("expires_at").notNull().default(sql.raw("0")),
  not_before: integer("not_before").notNull().default(sql.raw("0")),
}, (table) => [
  primaryKey({ columns: [table.library_id] })
]);

export const zotero_execution_items = sqliteTable("zotero_execution_items", {
  owner_id: text("owner_id").notNull(),
  job_id: text("job_id").notNull(),
  paper_id: text("paper_id").notNull(),
  ordinal: integer("ordinal").notNull(),
  reserved_key: text("reserved_key").notNull(),
  attachment_reserved_key: text("attachment_reserved_key").notNull(),
  item_key: text("item_key"),
  attachment_key: text("attachment_key"),
  status: text("status").notNull().default(sql.raw("'queued'")),
  metadata_saved: integer("metadata_saved").notNull().default(sql.raw("0")),
  classification_saved: integer("classification_saved").notNull().default(sql.raw("0")),
  existing_item: integer("existing_item").notNull().default(sql.raw("0")),
  pdf_verified: integer("pdf_verified").notNull().default(sql.raw("0")),
  pdf_proof_json: text("pdf_proof_json"),
  pending_operation: text("pending_operation"),
  pending_key: text("pending_key"),
  uncertain: integer("uncertain").notNull().default(sql.raw("0")),
  error_code: text("error_code"),
  retryable: integer("retryable").notNull().default(sql.raw("0")),
  retry_at: integer("retry_at"),
  updated_at: text("updated_at").notNull(),
}, (table) => [
  primaryKey({ columns: [table.owner_id, table.job_id, table.paper_id] }),
  index("zotero_execution_items_pending").on(table.owner_id, table.job_id, table.ordinal),
  foreignKey({ columns: [table.owner_id, table.job_id], foreignColumns: [zotero_execution_jobs.owner_id, zotero_execution_jobs.job_id] })
]);

export const zotero_execution_collections = sqliteTable("zotero_execution_collections", {
  owner_id: text("owner_id").notNull(),
  job_id: text("job_id").notNull(),
  path_json: text("path_json").notNull(),
  depth: integer("depth").notNull(),
  reserved_key: text("reserved_key").notNull(),
  collection_key: text("collection_key"),
  pending_operation: text("pending_operation"),
  uncertain: integer("uncertain").notNull().default(sql.raw("0")),
}, (table) => [
  primaryKey({ columns: [table.owner_id, table.job_id, table.path_json] }),
  foreignKey({ columns: [table.owner_id, table.job_id], foreignColumns: [zotero_execution_jobs.owner_id, zotero_execution_jobs.job_id] })
]);
