#!/usr/bin/env node
/** OFFLINE ONLY. No fetch, credentials, database, Worker deployment, or Site API.
 * Default: validate and print counts. --output explicitly writes a PRIVATE seed
 * descriptor; PDFs remain in the selected private directory and are not copied. */
import { stat, lstat, realpath, open } from 'node:fs/promises';
import { dirname, resolve, relative, sep, extname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { prepareTrustedCatalog, parseCatalogJson, privateBlobKey } from '../src/catalog-registration.mjs';
import { DEFAULT_MAX_PDF_BYTES } from '../src/hash-bytes.mjs';

const HELP = `Offline trusted Zotero catalog preflight\n\nnode scripts/prepare-catalog.mjs --pins /private/reviewed-pins.json \\\n  --report /private/report.json --html /private/report.html \\\n  --catalog /private/catalog.json --pdf-dir /private/pdfs \\\n  [--output /private/prepared-seed.json]\n\nWithout --output, validation only; no file or service mutations.\nPins must already be independently reviewed. A checksum shipped beside untrusted\ninput is not a trust anchor. No output is permitted inside the public source tree.\n`;
const FLAGS = new Set(['--pins', '--report', '--html', '--catalog', '--pdf-dir', '--output']);
const BINDING_KEYS = ['owner_id', 'report_id', 'catalog_id', 'catalog_version', 'personal_scope'];
function fail(code) { const e = new Error(code); e.code = code; throw e; }
async function boundedFile(path, maximum) {
  const info = await lstat(path);
  if (!info.isFile() || info.isSymbolicLink() || info.size > maximum) fail('INVALID_OR_OVERSIZED_LOCAL_FILE');
  // Open once, then verify size again; read only the bounded snapshot.
  const handle = await open(path, 'r');
  try {
    const opened = await handle.stat();
    if (!opened.isFile() || opened.size !== info.size || opened.size > maximum) fail('LOCAL_FILE_CHANGED');
    const result = new Uint8Array(opened.size);
    let offset = 0;
    while (offset < result.byteLength) {
      const { bytesRead } = await handle.read(result, offset, result.byteLength - offset, offset);
      if (!bytesRead) fail('LOCAL_FILE_CHANGED');
      offset += bytesRead;
    }
    if ((await handle.stat()).size !== opened.size) fail('LOCAL_FILE_CHANGED');
    return result;
  } finally { await handle.close(); }
}
function within(child, root) { const path = relative(root, child); return path === '' || (!path.startsWith('..' + sep) && path !== '..' && !path.startsWith(sep)); }
async function outputPath(path) {
  if (!path || extname(path) !== '.json') fail('EXPLICIT_PRIVATE_JSON_OUTPUT_REQUIRED');
  const target = resolve(path);
  const parent = await realpath(dirname(target));
  const actual = resolve(parent, target.slice(dirname(target).length + 1));
  const sourceRoot = await realpath(resolve(dirname(fileURLToPath(import.meta.url)), '../../..'));
  if (within(actual, sourceRoot) || actual.split(sep).some(segment => ['public', 'dist', 'node_modules', '.git'].includes(segment))) fail('PUBLIC_OUTPUT_FORBIDDEN');
  if (!(await stat(parent)).isDirectory()) fail('OUTPUT_PARENT_REQUIRED');
  return actual;
}
export async function main(argv = process.argv.slice(2)) {
  if (argv.length === 1 && ['--help', '-h'].includes(argv[0])) { process.stdout.write(HELP); return; }
  const args = new Map();
  for (let i = 0; i < argv.length; i += 2) {
    if (!FLAGS.has(argv[i]) || args.has(argv[i]) || !argv[i + 1] || argv[i + 1].startsWith('--')) fail('INVALID_ARGUMENTS');
    args.set(argv[i], argv[i + 1]);
  }
  for (const flag of ['--pins', '--report', '--html', '--catalog', '--pdf-dir']) if (!args.has(flag)) fail('REQUIRED_ARGUMENT_MISSING');
  // Validate the destination before reading any private source bytes.
  const destination = args.has('--output') ? await outputPath(args.get('--output')) : null;
  const pins = parseCatalogJson(await boundedFile(resolve(args.get('--pins')), 64 * 1024));
  const binding = Object.fromEntries(BINDING_KEYS.map(key => [key, pins[key]]));
  const pdfRoot = await realpath(resolve(args.get('--pdf-dir')));
  const prepared = await prepareTrustedCatalog({ binding, trustedPins: pins,
    reportBytes: await boundedFile(resolve(args.get('--report')), 4 * 1024 * 1024),
    htmlBytes: await boundedFile(resolve(args.get('--html')), 16 * 1024 * 1024),
    catalogBytes: await boundedFile(resolve(args.get('--catalog')), 4 * 1024 * 1024),
    readPdfBytes: async sha => {
      if (!/^[a-f0-9]{64}$/.test(sha)) fail('INVALID_BLOB_NAME');
      return boundedFile(resolve(pdfRoot, sha), DEFAULT_MAX_PDF_BYTES);
    },
  });
  if (destination) {
    const artifact = { ...prepared, usage: 'PRIVATE OFFLINE REVIEW ONLY. Re-prepare from pinned source bytes before registration. Not an executable SQL script or a registration capability.',
      sources: { report: await realpath(resolve(args.get('--report'))), html: await realpath(resolve(args.get('--html'))),
        catalog: await realpath(resolve(args.get('--catalog'))) },
      blobs: await Promise.all(prepared.blobs.map(async blob => ({ ...blob,
        private_storage_key: await privateBlobKey(binding.owner_id, blob.sha256), local_source: resolve(pdfRoot, blob.sha256) }))) };
    const file = await open(destination, 'wx', 0o600);
    try { await file.writeFile(JSON.stringify(artifact, null, 2) + '\n', 'utf8'); await file.sync(); }
    finally { await file.close(); }
  }
  // No titles, paper IDs, account IDs, paths, or PDF contents enter logs.
  process.stdout.write(JSON.stringify({ valid: true, paper_count: prepared.paperRows.length,
    pdf_count: prepared.blobs.length, total_pdf_bytes: prepared.blobs.reduce((n, b) => n + b.size, 0),
    private_artifact_written: Boolean(destination), network_calls: 0, database_writes: 0,
    library_writes: 0, site_changes: 0 }) + '\n');
}
if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch(error => {
    process.stderr.write(JSON.stringify({ valid: false, error: /^[A-Z_]+$/.test(error.code || '') ? error.code : 'LOCAL_PREFLIGHT_FAILED' }) + '\n');
    process.exitCode = 1;
  });
}
