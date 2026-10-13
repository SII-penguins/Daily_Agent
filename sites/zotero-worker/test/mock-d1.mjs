// Dependency-free D1 mock backed by Node's in-memory SQLite. No network or disk DB.
import { DatabaseSync } from 'node:sqlite';
import { readFileSync, readdirSync } from 'node:fs';

export class MockD1 {
  constructor() {
    this.sqlite = new DatabaseSync(':memory:');
    this.calls = [];
    for (const filename of readdirSync(new URL('../migrations/', import.meta.url)).filter(name => name.endsWith('.sql')).sort()) {
      this.sqlite.exec(readFileSync(new URL('../migrations/' + filename, import.meta.url), 'utf8'));
    }
  }
  prepare(sql) {
    const db = this;
    function make(args) {
      return {
        bind(...values) { return make(values); },
        async first() {
          db.calls.push({ operation: 'first', sql, args: [...args] });
          return db.sqlite.prepare(sql).get(...args) || null;
        },
        async all() {
          db.calls.push({ operation: 'all', sql, args: [...args] });
          return { success: true, results: db.sqlite.prepare(sql).all(...args) };
        },
        _run() {
          db.calls.push({ operation: 'run', sql, args: [...args] });
          const result = db.sqlite.prepare(sql).run(...args);
          return { success: true, meta: { changes: Number(result.changes) } };
        },
        async run() { return this._run(); },
      };
    }
    return make([]);
  }
  async batch(statements) {
    this.sqlite.exec('BEGIN');
    try {
      const result = statements.map(statement => statement._run());
      this.sqlite.exec('COMMIT');
      return result;
    } catch (error) {
      this.sqlite.exec('ROLLBACK');
      throw error;
    }
  }
  rows(table) {
    if (!/^[a-z_]+$/.test(table)) throw new Error('Invalid fixture table');
    return this.sqlite.prepare(`SELECT * FROM ${table}`).all();
  }
  seedCatalog({ owner = 'owner-1', report = 'report-20261010', catalog = 'catalog-v1',
    version = 1, hash = 'a'.repeat(64), enabled = 1, papers = ['paper-a', 'paper-b'] } = {}) {
    this.sqlite.prepare(`INSERT INTO zotero_report_catalogs
      (owner_id, report_id, catalog_id, catalog_version, content_sha256, enabled, created_at)
      VALUES (?, ?, ?, ?, ?, ?, ?)`).run(owner, report, catalog, version, hash, enabled, '2026-10-10T00:00:00.000Z');
    for (const paper of papers) {
      this.sqlite.prepare(`INSERT INTO zotero_catalog_papers
        (owner_id, report_id, catalog_id, paper_id, display_title, metadata_json)
        VALUES (?, ?, ?, ?, ?, ?)`).run(owner, report, catalog, paper, `Title of ${paper}`, '{"server_only":true}');
    }
  }
}

export function fixture() {
  const db = new MockD1();
  db.seedCatalog();
  db.seedCatalog({ owner: 'owner-2', report: 'foreign-report', catalog: 'foreign-catalog', papers: ['foreign-paper'] });
  return { db, env: {
    DB: db,
    SITES_GATEWAY_IDENTITY_TRUSTED: 'platform-managed',
    ZOTERO_OWNER_ID: 'owner-1',
    ZOTERO_SITE_ORIGIN: 'https://daily.example.test',
    ZOTERO_INTENT_MODE: 'blocked_connection',
  } };
}
