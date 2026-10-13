import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash, randomBytes } from 'node:crypto';
import { mkdtemp, mkdir, writeFile, readFile, stat, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawnSync } from 'node:child_process';
import { md5Bytes, sha256Bytes, createHashBytes, hashBytes, DEFAULT_MAX_PDF_BYTES, HARD_MAX_PDF_BYTES } from '../src/hash-bytes.mjs';
import { canonicalCatalogJson, parseCatalogJson, prepareTrustedCatalog, registerTrustedCatalog,
  readRegisteredCatalog, registrationBinding, privateBlobKey, createPrivateR2BlobStore } from '../src/catalog-registration.mjs';
import { MockD1 } from './mock-d1.mjs';
const utf8 = text => new TextEncoder().encode(text);
const digest = bytes => createHash('sha256').update(bytes).digest('hex');
const md5 = bytes => createHash('md5').update(bytes).digest('hex');
const error = code => e => e.code === code || e.message === code;
const bind = { owner_id: 'synthetic-owner', report_id: 'synthetic-report', catalog_id: 'synthetic-catalog',
  catalog_version: 1, personal_scope: 'personal:12345' };
function fixture() {
  const html = utf8('<!doctype html><title>Synthetic offline catalog fixture</title>');
  const pdfBytes = [utf8('%PDF-1.4\n% SYNTHETIC TEST ONLY alpha\n%%EOF\n'), utf8('%PDF-1.5\n% SYNTHETIC TEST ONLY beta\n%%EOF\n')];
  const pdfs = new Map(pdfBytes.map(b => [digest(b), b]));
  const papers = pdfBytes.map((b, i) => ({ paper_id: `synthetic-paper-${i}`, title: `Synthetic paper ${i}`,
    source_url: `https://publisher.example.test/paper-${i}`, doi: i ? '' : '10.9999/synthetic.alpha', arxiv: '',
    authors: ['Synthetic A. Writer', 'Synthetic B. Writer'], year: '2000', venue: 'Synthetic Journal',
    item_type: 'journalArticle', publication: 'published_verified', publication_evidence_sha256: 'f'.repeat(64),
    topics: ['other-reviewed'], pdf: { blob_id: digest(b), sha256: digest(b), size: b.length },
    review_sha256: 'a'.repeat(64), qualification_receipt_sha256: 'b'.repeat(64), source_pdf_sha256: digest(b) }));
  const report = { schema: 'paper-first-corrected-edition-v1', edition_id: 'synthetic-edition', date: '2000-01-02',
    html_sha256: digest(html), html_bytes: html.length, qualified_papers: papers.length,
    paper_records: papers.map(p => ({ key: p.paper_id, title: p.title, source_url: p.source_url,
      review_sha256: p.review_sha256, qualification_receipt_sha256: p.qualification_receipt_sha256,
      source_pdf_sha256: p.source_pdf_sha256 })) };
  return { html, report, catalog: { schema: 'daily-agent-zotero-catalog-v1', report_sha256: '', papers }, pdfs };
}
function input(f = fixture()) {
  const reportBytes = utf8(JSON.stringify(f.report, null, 2));
  f.catalog.report_sha256 = digest(reportBytes);
  const catalogBytes = utf8(JSON.stringify(f.catalog, null, 2));
  return { binding: { ...bind }, trustedPins: { ...bind, report_sha256: digest(reportBytes), catalog_sha256: digest(catalogBytes),
    html_sha256: digest(f.html), report_edition_id: 'synthetic-edition', allowed_source_hosts: ['publisher.example.test'] },
  reportBytes, htmlBytes: f.html, catalogBytes, readPdfBytes: async key => f.pdfs.get(key) };
}
function localDb() {
  const db = new MockD1();
  const present = new Set(db.sqlite.prepare('PRAGMA table_info(zotero_report_catalogs)').all().map(c => c.name));
  for (const col of ['catalog_blob_json', 'report_sha256', 'report_day', 'personal_scope', 'source_catalog_sha256', 'registration_sha256']) {
    if (!present.has(col)) db.sqlite.exec(`ALTER TABLE zotero_report_catalogs ADD COLUMN ${col} TEXT`);
  }
  return db;
}
function localBlobs() {
  const objects = new Map(), calls = [];
  return { ownerId: bind.owner_id, objects, calls,
    async readBlob(context) { calls.push(['read', { ...context }]); const b = objects.get(await privateBlobKey(context.ownerId, context.sha256)); return b ? new Uint8Array(b) : null; },
    async putBlobIfAbsent(context, content) { calls.push(['put', { ...context }]); const key = await privateBlobKey(context.ownerId, context.sha256);
      if (objects.has(key)) return false; objects.set(key, new Uint8Array(content)); return true; },
  };
}
const register = (prepared, db = localDb(), blobs = localBlobs()) => registerTrustedCatalog({ prepared,
  expectedBinding: { ...bind }, db, blobs, now: () => '2000-01-02T00:00:00.000Z' });
const read = (db, more = {}) => readRegisteredCatalog({ db, ownerId: bind.owner_id, reportId: bind.report_id,
  catalogId: bind.catalog_id, requireEnabled: false, ...more });

const vectors = [
  ['', 'd41d8cd98f00b204e9800998ecf8427e'], ['a', '0cc175b9c0f1b6a831c399e269772661'],
  ['abc', '900150983cd24fb0d6963f7d28e17f72'], ['message digest', 'f96b697d7cb7938d525a2f31aaf161d0'],
  ['abcdefghijklmnopqrstuvwxyz', 'c3fcd3d76192e4007dfb496cca67e13b'],
  ['ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789', 'd174ab98d277d9f5a5611c2c9f419d9f'],
  ['12345678901234567890123456789012345678901234567890123456789012345678901234567890', '57edf4a22be3c955ac49da2e2107b67a'],
];
for (const [value, expected] of vectors) test(`RFC 1321 MD5 vector length ${value.length}`, () => assert.equal(md5Bytes(utf8(value)), expected));
test('MD5/SHA256 independent crypto checks around padding/block boundaries and random sizes', async () => {
  const sizes = new Set([...Array.from({ length: 132 }, (_, i) => i), 255, 256, 257, 511, 512, 513, 1023, 1024,
    1025, 4095, 4096, 4097, 65535, 65536, 65537]);
  for (let i = 0; i < 40; i++) sizes.add(randomBytes(2).readUInt16BE());
  for (const n of sizes) {
    const bytes = randomBytes(n), result = await hashBytes(bytes);
    assert.equal(result.md5, md5(bytes), `MD5 at ${n}`); assert.equal(result.sha256, digest(bytes), `SHA256 at ${n}`);
  }
});
test('subarray offsets, zero length, binary and UTF8 are hashed without including surrounding bytes', async () => {
  const all = new Uint8Array([42, 43, 0, 255, 128, 5, 60]);
  for (const bytes of [all.subarray(2, 6), all.subarray(3, 3), utf8('中文 🧪 café\u0000')]) {
    assert.equal(md5Bytes(bytes), md5(bytes)); assert.equal(await sha256Bytes(bytes), digest(bytes));
  }
});
test('adapter takes one immutable byte snapshot before async digest', async () => {
  const bytes = utf8('original payload'), before = new Uint8Array(bytes);
  const promise = hashBytes(bytes); bytes.fill(0);
  assert.deepEqual(await promise, { md5: md5(before), sha256: digest(before) });
});
test('rejects wrong byte types, shared memory, absent crypto and invalid bounds', async () => {
  for (const value of ['', [], new ArrayBuffer(5), new DataView(new ArrayBuffer(5)), new Int8Array(5)]) {
    assert.throws(() => md5Bytes(value), /HASH_REQUIRES/); await assert.rejects(() => hashBytes(value), /HASH_REQUIRES/);
  }
  if (typeof SharedArrayBuffer !== 'undefined') assert.throws(() => md5Bytes(new Uint8Array(new SharedArrayBuffer(8))), /HASH_REQUIRES/);
  for (const maxBytes of [-1, 0, 7, 8.5, HARD_MAX_PDF_BYTES + 1, NaN]) assert.throws(() => createHashBytes({ maxBytes }), /LIMIT/);
  assert.throws(() => createHashBytes({ subtle: null }), /WEBCRYPTO_REQUIRED/);
  await assert.rejects(() => sha256Bytes(new Uint8Array(), { subtle: null }), /WEBCRYPTO_REQUIRED/);
});
test('PDF maximum is inclusive; maximum+1 fails before digest', async () => {
  const bytes = new Uint8Array(DEFAULT_MAX_PDF_BYTES); bytes.set(utf8('%PDF-1.4'));
  const result = await hashBytes(bytes);
  assert.equal(result.md5, md5(bytes)); assert.equal(result.sha256, digest(bytes));
  await assert.rejects(() => hashBytes(new Uint8Array(DEFAULT_MAX_PDF_BYTES + 1)), /HASH_INPUT_TOO_LARGE/);
  const small = createHashBytes({ maxBytes: 8 }); assert.equal((await small(utf8('%PDF-1.4'))).md5, md5(utf8('%PDF-1.4')));
  await assert.rejects(() => small(new Uint8Array(9)), /HASH_INPUT_TOO_LARGE/);
});
test('canonical JSON preserves exact strings/array order and rejects ambiguous values', () => {
  assert.equal(canonicalCatalogJson({ z: [true, null, '中文'], a: { y: 1, b: 'x' } }), '{"a":{"b":"x","y":1},"z":[true,null,"中文"]}');
  for (const value of [{ x: undefined }, { x: NaN }, { x: 1.1 }, { x: -0 }, { x: '\ud800' }, new Date(), Array(2)]) {
    assert.throws(() => canonicalCatalogJson(value));
  }
  assert.throws(() => parseCatalogJson(utf8('{"a":1,"\\u0061":2}')), error('DUPLICATE_JSON_KEY'));
  assert.throws(() => parseCatalogJson(utf8('{"nested":{"__proto__":1}}')), error('UNSAFE_JSON_KEY'));
  assert.throws(() => parseCatalogJson(new Uint8Array([0xff])), error('INVALID_JSON'));
});
test('valid preparation pins raw source separately from canonical and row hashes', async () => {
  const f = fixture(), args = input(f), prepared = await prepareTrustedCatalog(args);
  assert.equal(prepared.paperRows.length, 2); assert.equal(prepared.blobs.length, 2);
  assert.equal(prepared.catalogRow.enabled, 0);
  assert.equal(prepared.catalogRow.source_catalog_sha256, digest(args.catalogBytes));
  assert.equal(prepared.catalogRow.content_sha256, digest(utf8(prepared.catalogRow.catalog_blob_json)));
  assert.notEqual(prepared.catalogRow.content_sha256, prepared.catalogRow.source_catalog_sha256);
  assert.equal(prepared.catalogRow.registration_sha256, digest(utf8(canonicalCatalogJson(registrationBinding(prepared.catalogRow)))));
  for (const row of prepared.paperRows) assert.equal(row.row_sha256, digest(utf8(row.metadata_json)));
  for (const b of prepared.blobs) assert.equal(b.md5, md5(f.pdfs.get(b.sha256)));
  assert.throws(() => { prepared.catalogRow.owner_id = 'elsewhere'; }, TypeError);
  assert.equal(JSON.stringify(prepared).includes('%PDF-'), false);
});
for (const field of ['reportBytes', 'htmlBytes', 'catalogBytes']) test(`changed ${field} fails before reading PDFs`, async () => {
  const args = input(); let reads = 0; args.readPdfBytes = async () => { reads++; };
  args[field] = new Uint8Array(args[field]); args[field][0] ^= 1;
  await assert.rejects(() => prepareTrustedCatalog(args), error('PINNED_SOURCE_HASH_MISMATCH')); assert.equal(reads, 0);
});
for (const field of Object.keys(bind)) test(`pins require exact ${field} binding`, async () => {
  const args = input(); args.trustedPins[field] = field === 'catalog_version' ? 2 : 'wrong';
  await assert.rejects(() => prepareTrustedCatalog(args), error('TRUSTED_BINDING_MISMATCH'));
});
const mutations = [
  ['unknown catalog field', f => { f.catalog.extra = 'reject'; }],
  ['unknown paper field', f => { f.catalog.papers[0].untrusted_url = 'https://evil.invalid'; }],
  ['unknown PDF field', f => { f.catalog.papers[0].pdf.url = 'https://evil.invalid'; }],
  ['unknown report field', f => { f.report.extra = true; }],
  ['unknown report paper field', f => { f.report.paper_records[0].extra = true; }],
  ['wrong schema', f => { f.catalog.schema = 'another-v1'; }],
  ['wrong edition', f => { f.report.edition_id = 'different'; }],
  ['wrong report day', f => { f.report.date = '2000-02-31'; }],
  ['wrong HTML length', f => { f.report.html_bytes++; }],
  ['duplicate report IDs', f => { f.report.paper_records[1].key = f.report.paper_records[0].key; }],
  ['duplicate catalog IDs', f => { f.catalog.papers[1] = { ...f.catalog.papers[0] }; }],
  ['incomplete report allowlist', f => { f.catalog.papers.pop(); }],
  ['wrong report count', f => { f.report.qualified_papers++; }],
  ['mismatched title', f => { f.catalog.papers[0].title += ' changed'; }],
  ['mismatched review lineage', f => { f.catalog.papers[0].review_sha256 = 'c'.repeat(64); }],
  ['unsafe source host', f => { f.catalog.papers[0].source_url = f.report.paper_records[0].source_url = 'https://evil.invalid/paper'; }],
  ['source credentials', f => { f.catalog.papers[0].source_url = f.report.paper_records[0].source_url = 'https://a:secret@publisher.example.test/paper'; }],
  ['source query', f => { f.catalog.papers[0].source_url = f.report.paper_records[0].source_url += '?s=secret'; }],
  ['invalid DOI', f => { f.catalog.papers[0].doi = '10.bad/invalid'; }],
  ['invalid arxiv', f => { f.catalog.papers[0].arxiv = 'https://evil.invalid/1234.56789'; }],
  ['incomplete fallback identity', f => { f.catalog.papers[1].authors = []; }],
  ['incorrect formal-publication promotion', f => { f.catalog.papers[0].publication = 'unverified'; }],
  ['preprint promotion', f => { f.catalog.papers[0].publication = 'preprint'; }],
  ['unrecognized topic', f => { f.catalog.papers[0].topics = ['invented-topic']; }],
  ['duplicate topics', f => { f.catalog.papers[0].topics.push('other-reviewed'); }],
  ['wrong PDF content address', f => { f.catalog.papers[0].pdf.blob_id = 'e'.repeat(64); }],
  ['oversized PDF metadata', f => { f.catalog.papers[0].pdf.size = DEFAULT_MAX_PDF_BYTES + 1; }],
  ['fractional PDF metadata', f => { f.catalog.papers[0].pdf.size = 8.5; }],
  ['PDF byte length mismatch', f => { f.catalog.papers[0].pdf.size++; }],
  ['PDF bytes changed', f => { const k = f.catalog.papers[0].pdf.sha256; f.pdfs.get(k)[20] ^= 1; }],
  ['PDF signature changed', f => { const k = f.catalog.papers[0].pdf.sha256; f.pdfs.get(k)[0] = 0; }],
  ['missing PDF bytes', f => { f.pdfs.clear(); }],
];
for (const [label, mutate] of mutations) test(`rejects ${label}`, async () => {
  const f = fixture(); mutate(f); await assert.rejects(() => prepareTrustedCatalog(input(f)));
});
test('preprint and explicit metadata-only PDF omission are preserved', async () => {
  const f = fixture(); const p = f.catalog.papers[0]; p.publication = 'preprint'; p.item_type = 'preprint';
  p.publication_evidence_sha256 = ''; p.pdf = null;
  const prepared = await prepareTrustedCatalog(input(f));
  assert.equal(prepared.blobs.length, 1); assert.equal(JSON.parse(prepared.paperRows[0].metadata_json).publication, 'preprint');
});
test('registration creates disabled immutable rows, private PDFs, exact readback and duplicate no-op', async () => {
  const f = fixture(), prepared = await prepareTrustedCatalog(input(f)), db = localDb(), blobs = localBlobs();
  for (const bytes of f.pdfs.values()) bytes.fill(0); // preparation owns snapshots
  const result = await register(prepared, db, blobs);
  assert.equal(result.created, true); assert.equal(result.blobs_written, 2);
  assert.equal(db.rows('zotero_report_catalogs')[0].enabled, 0);
  const loaded = await read(db); assert.equal(loaded.papers.length, 2); assert.equal(loaded.row.content_sha256, prepared.catalogRow.content_sha256);
  assert.equal(await read(db, { requireEnabled: true }), null);
  const puts = blobs.calls.filter(c => c[0] === 'put').length;
  assert.equal((await register(prepared, db, blobs)).created, false);
  assert.equal(blobs.calls.filter(c => c[0] === 'put').length, puts);
  for (const [key, bytes] of blobs.objects) { assert.match(key, /^zotero-pdfs\/[a-f0-9]{64}\/[a-f0-9]{64}$/); assert.equal(digest(bytes), key.split('/').at(-1)); }
  db.sqlite.exec('UPDATE zotero_report_catalogs SET enabled = 1');
  assert.ok(await read(db, { requireEnabled: true }));
  assert.equal((await register(prepared, db, blobs)).created, false); // does not reset activation
  assert.equal(db.rows('zotero_report_catalogs')[0].enabled, 1);
});
test('serialized/forged preparation and wrong expected binding cannot register', async () => {
  const p = await prepareTrustedCatalog(input()), db = localDb(), blobs = localBlobs();
  await assert.rejects(() => register(JSON.parse(JSON.stringify(p)), db, blobs), error('PREPARATION_REQUIRED'));
  await assert.rejects(() => registerTrustedCatalog({ prepared: p, expectedBinding: { ...bind, personal_scope: 'personal:54321' }, db, blobs }), error('REGISTRATION_BINDING_MISMATCH'));
  assert.equal(db.calls.length, 0); assert.equal(blobs.calls.length, 0);
});
test('different catalog version/content/row binding never overwrites existing immutable data', async () => {
  const f = fixture(), p = await prepareTrustedCatalog(input(f)), db = localDb(), blobs = localBlobs(); await register(p, db, blobs);
  const mutations = ["UPDATE zotero_report_catalogs SET catalog_version = 2", "UPDATE zotero_catalog_papers SET metadata_json = '{}' WHERE paper_id = 'synthetic-paper-0'"];
  for (const sql of mutations) {
    const before = blobs.calls.filter(c => c[0] === 'put').length; db.sqlite.exec(sql);
    await assert.rejects(() => register(p, db, blobs)); assert.equal(blobs.calls.filter(c => c[0] === 'put').length, before);
  }
});
test('existing corrupt last PDF causes zero blob/DB mutations', async () => {
  const p = await prepareTrustedCatalog(input()), db = localDb(), blobs = localBlobs();
  const last = p.blobs.at(-1); blobs.objects.set(await privateBlobKey(bind.owner_id, last.sha256), utf8('%PDF-WRONG'));
  await assert.rejects(() => register(p, db, blobs), error('IMMUTABLE_BLOB_CONFLICT'));
  assert.equal(blobs.calls.filter(c => c[0] === 'put').length, 0); assert.equal(db.rows('zotero_report_catalogs').length, 0);
});
test('blob storage failed readback does not publish D1 rows', async () => {
  const p = await prepareTrustedCatalog(input()), db = localDb(), blobs = localBlobs();
  blobs.putBlobIfAbsent = async () => true;
  await assert.rejects(() => register(p, db, blobs), error('BLOB_WRITE_UNVERIFIED'));
  assert.equal(db.rows('zotero_report_catalogs').length, 0);
});
test('failed D1 batch leaves no rows and retry reuses exact private orphaned blobs', async () => {
  const p = await prepareTrustedCatalog(input()), db = localDb(), blobs = localBlobs(), batch = db.batch.bind(db);
  db.batch = async () => { throw Error('synthetic failure'); };
  await assert.rejects(() => register(p, db, blobs), error('DATABASE_WRITE_FAILED'));
  assert.equal(db.rows('zotero_report_catalogs').length, 0); assert.equal(blobs.objects.size, 2);
  db.batch = batch; const result = await register(p, db, blobs); assert.equal(result.created, true); assert.equal(result.blobs_written, 0);
});
test('cross-owner/scope reads and tampered canonical/row bindings fail closed', async () => {
  const p = await prepareTrustedCatalog(input()), db = localDb(), blobs = localBlobs(); await register(p, db, blobs);
  assert.equal(await read(db, { ownerId: 'someone-else' }), null);
  await assert.rejects(() => read(db, { personalScope: 'personal:54321' }), error('REGISTRATION_BINDING_MISMATCH'));
  db.sqlite.exec("UPDATE zotero_report_catalogs SET catalog_blob_json = catalog_blob_json || ' '");
  await assert.rejects(() => read(db), error('INVALID_REGISTERED_CATALOG'));
});
test('R2 adapter hashes owner namespace and uses conditional, checksum-bound PDF puts', async () => {
  const calls = [], objects = new Map();
  const bucket = { async get(key) { const b = objects.get(key); return b ? { size: b.length, body: new Response(b).body } : null; },
    async put(key, bytes, options) { calls.push({ key, bytes, options }); if (objects.has(key)) return null; objects.set(key, bytes); return { key }; } };
  const store = createPrivateR2BlobStore(bucket, { ownerId: bind.owner_id });
  const bytes = utf8('%PDF-1.4 test'), sha256 = digest(bytes);
  const context = { ownerId: bind.owner_id, reportId: bind.report_id, catalogId: bind.catalog_id,
    paperId: 'synthetic-paper-0', sha256, size: bytes.length };
  assert.equal(await store.readBlob(context), null); assert.equal(await store.putBlobIfAbsent(context, bytes), true);
  assert.deepEqual(await store.readBlob(context), bytes); assert.equal(await store.putBlobIfAbsent(context, bytes), false);
  assert.deepEqual(calls[0].options.onlyIf, { etagDoesNotMatch: '*' }); assert.equal(calls[0].options.sha256, sha256);
  assert.notEqual(await privateBlobKey('owner-A', sha256), await privateBlobKey('owner-B', sha256));
  await assert.rejects(() => store.readBlob({ ...context, ownerId: 'someone-else' }), error('INVALID_BLOB_CONTEXT'));
});
test('CLI is validate-only by default, requires private explicit output, writes no PDFs or DB', async t => {
  const dir = await mkdtemp(join(tmpdir(), 'zotero-synthetic-')); t.after(() => rm(dir, { recursive: true, force: true }));
  const f = fixture(), args = input(f); await mkdir(join(dir, 'pdfs'));
  for (const [key, bytes] of f.pdfs) await writeFile(join(dir, 'pdfs', key), bytes);
  for (const [name, value] of [['pins', utf8(JSON.stringify(args.trustedPins))], ['report', args.reportBytes], ['html', args.htmlBytes], ['catalog', args.catalogBytes]]) await writeFile(join(dir, name), value);
  const base = ['scripts/prepare-catalog.mjs', '--pins', join(dir, 'pins'), '--report', join(dir, 'report'),
    '--html', join(dir, 'html'), '--catalog', join(dir, 'catalog'), '--pdf-dir', join(dir, 'pdfs')];
  const run = extras => spawnSync(process.execPath, [...base, ...extras], { cwd: new URL('..', import.meta.url), encoding: 'utf8' });
  const validation = run([]); assert.equal(validation.status, 0, validation.stderr); assert.equal(JSON.parse(validation.stdout).private_artifact_written, false);
  const output = join(dir, 'seed.json'), write = run(['--output', output]); assert.equal(write.status, 0, write.stderr);
  const artifact = JSON.parse(await readFile(output, 'utf8')); assert.equal(artifact.blobs.length, 2);
  assert.equal((await stat(output)).mode & 0o777, 0o600); assert.equal(JSON.stringify(artifact).includes('%PDF-'), false);
  assert.equal(run(['--output', output]).status, 1); // never replace an artifact
  const publicOutput = new URL('../test/forbidden-seed.json', import.meta.url).pathname;
  const rejected = run(['--output', publicOutput]); assert.equal(rejected.status, 1); assert.match(rejected.stderr, /PUBLIC_OUTPUT_FORBIDDEN/);
  assert.equal(run(['--upload', 'yes']).status, 1);
});
test('two concurrent identical registrations create one immutable catalog', async () => {
  const p = await prepareTrustedCatalog(input()), db = localDb(), blobs = localBlobs();
  const receipts = await Promise.all([register(p, db, blobs), register(p, db, blobs)]);
  assert.equal(receipts.filter(r => r.created).length, 1);
  assert.equal(db.rows('zotero_report_catalogs').length, 1); assert.equal(db.rows('zotero_catalog_papers').length, 2);
  assert.equal(blobs.objects.size, 2); assert.ok(await read(db));
});
test('concurrent different catalogs cannot replace one another', async () => {
  const f = fixture(), p = await prepareTrustedCatalog(input(f));
  f.catalog.papers[0].title = f.report.paper_records[0].title = 'Changed synthetic paper';
  const changed = await prepareTrustedCatalog(input(f)), db = localDb(), blobs = localBlobs();
  const results = await Promise.allSettled([register(p, db, blobs), register(changed, db, blobs)]);
  assert.equal(results.filter(r => r.status === 'fulfilled').length, 1);
  assert.equal(results.filter(r => r.status === 'rejected').length, 1);
  assert.equal(db.rows('zotero_report_catalogs').length, 1); assert.ok(await read(db));
});
test('required string IDs, calendar dates, BOM and independent row tampering fail closed', async () => {
  const args = input(); args.binding.report_id = args.trustedPins.report_id = 123;
  await assert.rejects(() => prepareTrustedCatalog(args), error('INVALID_BINDING'));
  const f = fixture(); f.report.date = '0000-01-01';
  await assert.rejects(() => prepareTrustedCatalog(input(f)), error('INVALID_REPORT_DAY'));
  assert.throws(() => parseCatalogJson(new Uint8Array([0xef, 0xbb, 0xbf, ...utf8('{}')])), error('INVALID_JSON'));
  const p = await prepareTrustedCatalog(input()), db = localDb(), blobs = localBlobs(); await register(p, db, blobs);
  db.sqlite.exec("UPDATE zotero_catalog_papers SET display_title = 'Tampered'");
  await assert.rejects(() => read(db), error('IMMUTABLE_PAPER_CONFLICT'));
});
test('missing blob on immutable duplicate catalog fails closed instead of silently repairing', async () => {
  const p = await prepareTrustedCatalog(input()), db = localDb(), blobs = localBlobs(); await register(p, db, blobs);
  blobs.objects.delete([...blobs.objects.keys()][0]);
  const before = blobs.calls.filter(c => c[0] === 'put').length;
  await assert.rejects(() => register(p, db, blobs), error('IMMUTABLE_CATALOG_BLOB_MISSING'));
  assert.equal(blobs.calls.filter(c => c[0] === 'put').length, before);
});
test('prepared canonical catalogs interoperate with the executable import domain', async () => {
  const { loadVerifiedCatalog } = await import('../src/import-domain.mjs');
  const p = await prepareTrustedCatalog(input());
  const loaded = await loadVerifiedCatalog({ ...p.catalogRow, enabled: 1 }, p.paperRows, '12345');
  assert.equal(loaded.papers.length, 2); assert.equal(loaded.sha256, p.catalogRow.content_sha256);
  assert.equal(loaded.day, '2000-01-02');
});

test('SHA-only private registration preserves exact integrity pins and branding; upload MD5 remains default', async () => {
  const f=fixture(),args=input(f),normal=await prepareTrustedCatalog(args),native=await prepareTrustedCatalog({...args,includeProtocolMd5:false});
  assert.deepEqual(native.catalogRow,normal.catalogRow);assert.deepEqual(native.paperRows,normal.paperRows);
  for(const blob of native.blobs){assert.equal(Object.hasOwn(blob,'md5'),false);assert.equal(blob.sha256,digest(f.pdfs.get(blob.sha256)));}
  assert.ok(normal.blobs.every(blob=>blob.md5===md5(f.pdfs.get(blob.sha256))));
  const db=localDb();await register(native,db);assert.equal((await read(db)).papers.length,2);
  const corrupted={...args,readPdfBytes:async()=>utf8('%PDF-corrupt bytes')};await assert.rejects(prepareTrustedCatalog({...corrupted,includeProtocolMd5:false}));
  await assert.rejects(prepareTrustedCatalog({...args,includeProtocolMd5:'false'}),error('INVALID_HASH_MODE'));
});
