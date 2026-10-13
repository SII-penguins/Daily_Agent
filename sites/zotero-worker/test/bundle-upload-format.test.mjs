import test from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { watch, writeFileSync } from 'node:fs';
import { mkdtemp, mkdir, writeFile, readFile, readdir, rm, stat, symlink } from 'node:fs/promises';
import { join, dirname } from 'node:path';
import { tmpdir } from 'node:os';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { bundleUploadDescriptor, OWNER_BUNDLE_MAGIC, OWNER_BUNDLE_SCHEMA } from '../src/bundle-upload-format.mjs';
import { bundleConfig } from '../src/private-bundles.mjs';
import { canonicalCatalogJson } from '../src/catalog-registration.mjs';
import { createOwnerBundle, main } from '../scripts/create-owner-bundle.mjs';

const sha = bytes => createHash('sha256').update(bytes).digest('hex');
const encode = value => Buffer.from(typeof value === 'string' ? value : JSON.stringify(value));
// Synthetic artifacts only. These carry no real owner, account or paper data.
function synthetic(pdfCounts = [1, 1]) {
  const owner = 'synthetic-private-owner', scope = 'personal:123456', files = new Map();
  const manifest = { schema: 'daily-agent-zotero-private-bundles-v1', approved: true, owner_id: owner,
    personal_scope: scope, connection_version: 1, paper_ids: [], batches: [] };
  for (let n = 1; n <= pdfCounts.length; n++) {
    const id = 'batch-' + n, html = encode('<title>Synthetic batch ' + n + '</title>'), papers = [];
    for (let p = 1; p <= pdfCounts[n - 1]; p++) {
      const paper_id = 'paper-' + n + '-' + p, pdf = encode('%PDF-1.4\nSYNTHETIC ' + paper_id + '\n%%EOF\n');
      const digest = sha(pdf);
      papers.push({ paper_id, title: 'Synthetic paper ' + paper_id, source_url: 'https://publisher.example.test/' + paper_id,
        doi: '10.9999/' + paper_id, arxiv: '', authors: ['Synthetic Author'], year: '2000', venue: 'Synthetic Journal',
        item_type: 'journalArticle', publication: 'published_verified', publication_evidence_sha256: 'f'.repeat(64),
        topics: ['other-reviewed'], pdf: { blob_id: digest, sha256: digest, size: pdf.length },
        review_sha256: 'a'.repeat(64), qualification_receipt_sha256: 'b'.repeat(64), source_pdf_sha256: digest });
      files.set(id + '/pdfs/' + digest, pdf);
    }
    const report = encode({ schema: 'paper-first-corrected-edition-v1', edition_id: 'edition-' + n, date: '2000-01-0' + n,
      html_sha256: sha(html), html_bytes: html.length, qualified_papers: papers.length,
      paper_records: papers.map(p => ({ key: p.paper_id, ...Object.fromEntries(['title', 'source_url', 'review_sha256',
        'qualification_receipt_sha256', 'source_pdf_sha256'].map(key => [key, p[key]])) })) });
    const catalog = encode({ schema: 'daily-agent-zotero-catalog-v1', report_sha256: sha(report), papers });
    const binding = { owner_id: owner, report_id: 'report-' + n, catalog_id: 'catalog-' + n, catalog_version: 1, personal_scope: scope };
    const config = { schema: 'daily-agent-zotero-registration-config-v1', approved: true, binding,
      trustedPins: { ...binding, report_sha256: sha(report), html_sha256: sha(html), catalog_sha256: sha(catalog),
        report_edition_id: 'edition-' + n, allowed_source_hosts: ['publisher.example.test'] },
      pdfs: papers.map(p => ({ sha256: p.pdf.sha256, size: p.pdf.size, paper_id: p.paper_id })),
      filenames: { report: 'report.json', html: 'report.html', catalog: 'catalog.json' }, enable_after_registration: false };
    const sources = { report, html, catalog }, paper_ids = papers.map(p => p.paper_id);
    manifest.paper_ids.push(...paper_ids);
    manifest.batches.push({ id, config, paper_ids,
      sources: Object.fromEntries(Object.entries(sources).map(([kind, bytes]) => [kind, { size: bytes.length, sha256: sha(bytes) }])) });
    for (const [kind, bytes] of Object.entries(sources)) files.set(id + '/' + config.filenames[kind], bytes);
  }
  return { manifest, files };
}
async function fixture(t, counts) {
  const base = await mkdtemp(join(tmpdir(), 'synthetic-owner-upload-'));
  t.after(() => rm(base, { recursive: true, force: true }));
  const root = join(base, 'checkout'), bundleDir = join(root, 'zotero/bundles'), output = join(base, 'owner.dazb');
  const f = synthetic(counts); await mkdir(bundleDir, { recursive: true });
  await writeFile(join(bundleDir, 'manifest.json'), JSON.stringify(f.manifest));
  for (const [path, bytes] of f.files) { const target = join(bundleDir, path); await mkdir(dirname(target), { recursive: true }); await writeFile(target, bytes); }
  return { base, root, bundleDir, output, ...f };
}

test('descriptor has canonical fixed order, contiguous offsets and no private identifiers', () => {
  const { manifest } = synthetic(), original = structuredClone(manifest);
  const result = bundleUploadDescriptor(manifest), header = JSON.parse(result.header_json);
  assert.equal(header.schema, OWNER_BUNDLE_SCHEMA); assert.equal(header.members.length, 8);
  assert.equal(result.header_json, canonicalCatalogJson(header));
  assert.ok(result.header_json.startsWith('{"members":[{"batch_id":"batch-1","id":"batch-1/report","kind":"report","offset":0,"sha256":'));
  assert.deepEqual(header.members.map(m => [m.batch_id, m.kind]), [
    ['batch-1', 'report'], ['batch-1', 'html'], ['batch-1', 'catalog'], ['batch-1', 'pdf'],
    ['batch-2', 'report'], ['batch-2', 'html'], ['batch-2', 'catalog'], ['batch-2', 'pdf']]);
  let offset = 0;
  for (const member of header.members) {
    assert.deepEqual(Object.keys(member), ['batch_id', 'id', 'kind', 'offset', 'sha256', 'size']);
    assert.equal(member.offset, offset); offset += member.size;
    assert.equal(member.id, member.batch_id + '/' + member.kind + (member.kind === 'pdf' ? '/' + member.sha256 : ''));
  }
  assert.equal(result.total_size, 12 + Buffer.byteLength(result.header_json) + offset);
  for (const forbidden of [manifest.owner_id, manifest.personal_scope, 'owner_id', 'connection_version', 'paper_id', 'filenames', 'credential', 'report.json']) assert.equal(result.header_json.includes(forbidden), false);
  assert.deepEqual(manifest, original);
});

test('validated bundleConfig and raw pinned manifest produce the exact same descriptor', () => {
  const { manifest } = synthetic(), db = {}, origin = 'https://synthetic.example.test';
  const env = { __privateBundleManifest: manifest, DB: db, BUCKET: { get() {}, put() {} }, ASSETS: { fetch() {} },
    SITES_GATEWAY_IDENTITY_TRUSTED: 'platform-managed', ZOTERO_OWNER_ID: manifest.owner_id, ZOTERO_SITE_ORIGIN: origin };
  const validated = bundleConfig(env, { owner: manifest.owner_id, db, origin });
  assert.deepEqual(bundleUploadDescriptor(validated), bundleUploadDescriptor(manifest));
});

test('manifest order controls layout and supplied member indexes cannot replace pins', () => {
  const { manifest } = synthetic([2, 1]); manifest.batches.reverse(); manifest.batches[1].config.pdfs.reverse();
  const expected = bundleUploadDescriptor(manifest);
  manifest.members = [{ id: 'evil', offset: 0, size: 0, sha256: '0'.repeat(64) }];
  assert.deepEqual(bundleUploadDescriptor(manifest), expected);
  const members = JSON.parse(expected.header_json).members;
  assert.equal(members[0].batch_id, 'batch-2');
  assert.equal(members[7].sha256, manifest.batches[1].config.pdfs[0].sha256);
});

for (const [name, mutate] of [
  ['unsafe batch identifier', m => { m.batches[0].id = '../escape'; }],
  ['duplicate batch', m => { m.batches[1].id = m.batches[0].id; }],
  ['duplicate paper', m => { m.paper_ids.push(m.paper_ids[0]); }],
  ['unassigned paper', m => { m.paper_ids.push('unassigned'); }],
  ['duplicate PDF member', m => { m.batches[0].config.pdfs.push({ ...m.batches[0].config.pdfs[0] }); }],
  ['malformed hash', m => { m.batches[0].config.pdfs[0].sha256 = 'z'.repeat(64); }],
  ['mismatched source pin', m => { m.batches[0].sources.report.sha256 = '0'.repeat(64); }],
  ['zero source size', m => { m.batches[0].sources.report.size = 0; }],
  ['fractional size', m => { m.batches[0].sources.report.size = 1.5; }],
  ['oversized PDF', m => { m.batches[0].config.pdfs[0].size = 16 * 1024 * 1024 + 1; }],
  ['oversized report', m => { m.batches[0].sources.report.size = 1024 * 1024 + 1; }],
  ['combined source limit', m => { m.batches[0].sources.html.size = 4 * 1024 * 1024; }],
  ['injected source offset', m => { m.batches[0].sources.report.offset = 1; }],
  ['injected PDF offset', m => { m.batches[0].config.pdfs[0].offset = 0; }],
  ['too many papers', m => { m.paper_ids = Array.from({ length: 101 }, (_, i) => 'p-' + i); }],
  ['too many batches', m => { m.batches = Array.from({ length: 17 }, (_, i) => ({ ...m.batches[0], id: 'batch-' + i })); }],
]) test('descriptor rejects ' + name, () => {
  const { manifest } = synthetic(); mutate(manifest);
  assert.throws(() => bundleUploadDescriptor(manifest), /INVALID_OWNER_BUNDLE_CONFIGURATION/);
});

test('descriptor enforces the per-batch 24 MiB PDF cap', () => {
  const { manifest } = synthetic([2]);
  for (const pdf of manifest.batches[0].config.pdfs) pdf.size = 12 * 1024 * 1024;
  assert.ok(bundleUploadDescriptor(manifest).total_size > 24 * 1024 * 1024);
  manifest.batches[0].config.pdfs[1].size++;
  assert.throws(() => bundleUploadDescriptor(manifest), /INVALID_OWNER_BUNDLE_CONFIGURATION/);
});

for (const [counts, expectedCount] of [[[1, 1], 8], [[4, 4], 14]]) {
  test('generator writes an exact ' + expectedCount + '-member synthetic bundle, mode 0600, without changing input', async t => {
    const f = await fixture(t, counts), before = await readFile(join(f.bundleDir, 'manifest.json'));
    const receipt = await createOwnerBundle({ privateRoot: f.root, output: f.output }), bytes = await readFile(f.output);
    const descriptor = bundleUploadDescriptor(f.manifest), length = bytes.readUInt32BE(8), header = bytes.subarray(12, 12 + length).toString('utf8');
    assert.equal(bytes.subarray(0, 8).toString('ascii'), OWNER_BUNDLE_MAGIC);
    assert.equal(header, descriptor.header_json); assert.equal(bytes.length, descriptor.total_size);
    assert.equal(length, Buffer.byteLength(header)); assert.ok(length <= 65536);
    assert.equal(receipt.member_count, expectedCount); assert.equal(receipt.size, bytes.length); assert.equal(receipt.sha256, sha(bytes));
    assert.equal((await stat(f.output)).mode & 0o777, 0o600);
    let end = 0;
    for (const member of JSON.parse(header).members) {
      const actual = bytes.subarray(12 + length + member.offset, 12 + length + member.offset + member.size);
      const batch = f.manifest.batches.find(b => b.id === member.batch_id);
      const path = batch.id + '/' + (member.kind === 'pdf' ? 'pdfs/' + member.sha256 : batch.config.filenames[member.kind]);
      assert.deepEqual(actual, f.files.get(path)); assert.equal(sha(actual), member.sha256); end = member.offset + member.size;
    }
    assert.equal(bytes.length, 12 + length + end);
    assert.deepEqual(await readFile(join(f.bundleDir, 'manifest.json')), before);
    for (const [path, original] of f.files) assert.deepEqual(await readFile(join(f.bundleDir, path)), original);
    assert.deepEqual((await readdir(f.base)).sort(), ['checkout', 'owner.dazb']);
  });
}

test('generator rejects extra, changed or symlinked inputs before creating output', async t => {
  for (const mode of ['extra', 'changed', 'symlink']) {
    const f = await fixture(t);
    if (mode === 'extra') await writeFile(join(f.bundleDir, 'unlisted.txt'), 'synthetic extra');
    if (mode === 'changed') await writeFile(join(f.bundleDir, 'batch-1/report.html'), 'changed');
    if (mode === 'symlink') { await rm(join(f.bundleDir, 'batch-1/report.html')); await symlink('catalog.json', join(f.bundleDir, 'batch-1/report.html')); }
    await assert.rejects(createOwnerBundle({ privateRoot: f.root, output: f.output }));
    await assert.rejects(stat(f.output), { code: 'ENOENT' }); assert.deepEqual(await readdir(f.base), ['checkout']);
  }
});

test('generator refuses public/dist/input/reusable paths, existing outputs and relative arguments', async t => {
  const f = await fixture(t);
  const publicDir = join(f.base, 'public'), distDir = join(f.base, 'dist');
  await mkdir(publicDir); await mkdir(distDir);
  const sourceRoot = fileURLToPath(new URL('../', import.meta.url));
  for (const output of [join(f.bundleDir, 'upload.dazb'), join(f.root, 'upload.dazb'), join(publicDir, 'upload.dazb'),
    join(distDir, 'upload.dazb'), join(sourceRoot, 'upload.dazb')]) {
    await assert.rejects(createOwnerBundle({ privateRoot: f.root, output }), /PUBLIC_OR_SOURCE_OUTPUT_FORBIDDEN/);
  }
  await symlink(publicDir, join(f.base, 'alias'));
  await assert.rejects(createOwnerBundle({ privateRoot: f.root, output: join(f.base, 'alias/upload.dazb') }), /PUBLIC_OR_SOURCE_OUTPUT_FORBIDDEN/);
  await writeFile(f.output, 'preserve existing file');
  await assert.rejects(createOwnerBundle({ privateRoot: f.root, output: f.output }), /OWNER_BUNDLE_OUTPUT_EXISTS/);
  assert.equal(await readFile(f.output, 'utf8'), 'preserve existing file');
  await assert.rejects(createOwnerBundle({ privateRoot: '.', output: f.output }), /ABSOLUTE_PRIVATE_ROOT_REQUIRED/);
  await assert.rejects(createOwnerBundle({ privateRoot: f.root, output: 'relative.dazb' }), /ABSOLUTE_PRIVATE_OUTPUT_REQUIRED/);
});

test('CLI returns only a size/hash/count receipt and rejects unknown or duplicate options', async t => {
  const f = await fixture(t), script = fileURLToPath(new URL('../scripts/create-owner-bundle.mjs', import.meta.url));
  const result = spawnSync(process.execPath, [script, '--private-root', f.root, '--output', f.output], { encoding: 'utf8' });
  assert.equal(result.status, 0, result.stderr);
  const receipt = JSON.parse(result.stdout); assert.equal(receipt.member_count, 8); assert.equal(receipt.network_calls, 0); assert.equal(receipt.service_writes, 0);
  for (const forbidden of [f.root, f.output, f.manifest.owner_id, f.manifest.personal_scope, 'paper-', 'batch-']) assert.equal(result.stdout.includes(forbidden), false);
  for (const argv of [[], ['--unknown', 'x'], ['--private-root', f.root, '--private-root', f.root], ['--private-root', f.root, '--output']]) await assert.rejects(main(argv));
});

test('atomic publication never replaces an output created during the write', async t => {
  const f = await fixture(t, [4, 4]); let raced = false, watcherError;
  const watcher = watch(f.base, (_event, filename) => {
    if (!raced && filename?.startsWith('.owner-bundle-')) {
      raced = true;
      try { writeFileSync(f.output, 'preserve concurrent output', { flag: 'wx' }); } catch (error) { watcherError = error; }
    }
  });
  t.after(() => watcher.close());
  await assert.rejects(createOwnerBundle({ privateRoot: f.root, output: f.output }), /OWNER_BUNDLE_OUTPUT_EXISTS/);
  assert.equal(raced, true); assert.equal(watcherError, undefined);
  assert.equal(await readFile(f.output, 'utf8'), 'preserve concurrent output');
  assert.deepEqual((await readdir(f.base)).sort(), ['checkout', 'owner.dazb']);
});
