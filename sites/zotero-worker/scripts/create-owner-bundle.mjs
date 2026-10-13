#!/usr/bin/env node
/** Local-only private packaging. This does not publish, upload, configure,
 * register or activate anything, and never copies payloads into static assets. */
import { open, realpath, lstat, link, unlink } from 'node:fs/promises';
import { constants } from 'node:fs';
import { resolve, dirname, basename, relative, isAbsolute, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createHash, randomUUID } from 'node:crypto';
import { loadPrivateBundles } from '../hosting-adapter/private-bundle-assets.mjs';
import { bundleUploadDescriptor, OWNER_BUNDLE_MAGIC, OWNER_BUNDLE_SCHEMA } from '../src/bundle-upload-format.mjs';

const fail = code => { const error = new Error(code); error.code = code; throw error; };
const inside = (root, path) => { const r = relative(root, path); return r === '' || (!isAbsolute(r) && r !== '..' && !r.startsWith('..' + sep)); };
async function realDirectory(path) {
  const info = await lstat(path);
  if (!info.isDirectory() || info.isSymbolicLink()) fail('UNSAFE_OWNER_BUNDLE_DIRECTORY');
}
async function absent(path) {
  try { await lstat(path); } catch (error) { if (error.code === 'ENOENT') return; throw error; }
  fail('OWNER_BUNDLE_OUTPUT_EXISTS');
}
async function destination(privateRoot, output) {
  if (typeof output !== 'string' || !isAbsolute(output)) fail('ABSOLUTE_PRIVATE_OUTPUT_REQUIRED');
  const requested = resolve(output), parent = await realpath(dirname(requested));
  const target = resolve(parent, basename(requested));
  // Keep artifacts outside both the private input checkout and reusable source.
  const reusableRoot = await realpath(fileURLToPath(new URL('../../../', import.meta.url)));
  if (inside(privateRoot, target) || inside(reusableRoot, target)
      || target.split(sep).some(part => ['public', 'dist', 'node_modules', '.git'].includes(part))
      || requested.split(sep).some(part => ['public', 'dist', 'node_modules', '.git'].includes(part))) fail('PUBLIC_OR_SOURCE_OUTPUT_FORBIDDEN');
  await absent(target);
  return target;
}
async function pinnedBytes(root, parts, pin) {
  let parent = root;
  for (const part of parts.slice(0, -1)) { parent = resolve(parent, part); await realDirectory(parent); }
  const path = resolve(parent, parts.at(-1)), before = await lstat(path);
  if (!inside(root, path) || !before.isFile() || before.isSymbolicLink() || before.size !== pin.size) fail('OWNER_BUNDLE_SOURCE_CHANGED');
  const handle = await open(path, constants.O_RDONLY | (constants.O_NOFOLLOW || 0));
  try {
    const info = await handle.stat();
    if (!info.isFile() || info.size !== before.size || info.dev !== before.dev || info.ino !== before.ino) fail('OWNER_BUNDLE_SOURCE_CHANGED');
    const bytes = Buffer.alloc(pin.size); let offset = 0;
    while (offset < bytes.length) {
      const { bytesRead } = await handle.read(bytes, offset, bytes.length - offset, offset);
      if (!bytesRead) fail('OWNER_BUNDLE_SOURCE_CHANGED');
      offset += bytesRead;
    }
    if ((await handle.stat()).size !== info.size || createHash('sha256').update(bytes).digest('hex') !== pin.sha256) fail('OWNER_BUNDLE_SOURCE_CHANGED');
    return bytes;
  } finally { await handle.close(); }
}

export async function createOwnerBundle({ privateRoot, output } = {}) {
  if (typeof privateRoot !== 'string' || !isAbsolute(privateRoot)) fail('ABSOLUTE_PRIVATE_ROOT_REQUIRED');
  const root = await realpath(privateRoot);
  await realDirectory(root); await realDirectory(resolve(root, 'zotero'));
  const target = await destination(root, output);
  // The existing validator checks the approved manifest, catalog provenance,
  // all source/PDF hashes, the complete allowlist and absence of extra files.
  const manifest = await loadPrivateBundles(root);
  if (!manifest) fail('PRIVATE_BUNDLE_INPUT_MISSING');
  const descriptor = bundleUploadDescriptor(manifest), header = Buffer.from(descriptor.header_json, 'utf8');
  const prefix = Buffer.alloc(12); prefix.write(OWNER_BUNDLE_MAGIC, 0, 'ascii'); prefix.writeUInt32BE(header.length, 8);
  const temporary = resolve(dirname(target), '.owner-bundle-' + randomUUID() + '.tmp');
  const handle = await open(temporary, 'wx', 0o600), hash = createHash('sha256');
  let written = 0, closed = false;
  async function append(bytes) { await handle.writeFile(bytes); hash.update(bytes); written += bytes.length; }
  try {
    await handle.chmod(0o600); await append(prefix); await append(header);
    const sourceRoot = resolve(root, 'zotero/bundles');
    for (const batch of manifest.batches) {
      for (const kind of ['report', 'html', 'catalog']) {
        await append(await pinnedBytes(sourceRoot, [batch.id, batch.config.filenames[kind]], batch.sources[kind]));
      }
      for (const pdf of batch.config.pdfs) await append(await pinnedBytes(sourceRoot, [batch.id, 'pdfs', pdf.sha256], pdf));
    }
    if (written !== descriptor.total_size) fail('OWNER_BUNDLE_SIZE_MISMATCH');
    await handle.sync(); await handle.close(); closed = true;
    // Atomic no-replace publication: rename() would overwrite a file created
    // after an absence check. A same-directory hard link fails if it exists.
    try { await link(temporary, target); }
    catch (error) { if (error.code === 'EEXIST') fail('OWNER_BUNDLE_OUTPUT_EXISTS'); throw error; }
    return { written: true, schema: OWNER_BUNDLE_SCHEMA, size: written, sha256: hash.digest('hex'),
      member_count: JSON.parse(descriptor.header_json).members.length, network_calls: 0, service_writes: 0 };
  } finally {
    if (!closed) await handle.close();
    await unlink(temporary).catch(error => { if (error.code !== 'ENOENT') throw error; });
  }
}

export async function main(argv = process.argv.slice(2)) {
  if (argv.length === 1 && ['--help', '-h'].includes(argv[0])) {
    process.stdout.write('Create a private owner-upload bundle locally.\nUsage: --private-root ABSOLUTE_SITES_CHECKOUT --output ABSOLUTE_PRIVATE_FILE\nOutput must be outside the input checkout, reusable source, public and dist directories.\nNo network or service writes.\n'); return;
  }
  const args = new Map(), allowed = new Set(['--private-root', '--output']);
  for (let i = 0; i < argv.length; i++) {
    if (!allowed.has(argv[i]) || args.has(argv[i]) || !argv[i + 1] || argv[i + 1].startsWith('--')) fail('INVALID_ARGUMENTS');
    args.set(argv[i], argv[++i]);
  }
  if (args.size !== 2) fail('REQUIRED_ARGUMENT_MISSING');
  const receipt = await createOwnerBundle({ privateRoot: args.get('--private-root'), output: args.get('--output') });
  process.stdout.write(JSON.stringify(receipt) + '\n');
}
if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) main().catch(error => {
  process.stderr.write(JSON.stringify({ written: false, error: /^[A-Z_]+$/.test(error.code || '') ? error.code : 'OWNER_BUNDLE_FAILED' }) + '\n');
  process.exitCode = 1;
});
