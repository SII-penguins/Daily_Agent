/** Local-only private Sites checkout preparation. No network or native Sites calls. */
import { mkdir, readdir, lstat, realpath, readFile, writeFile, copyFile, cp } from 'node:fs/promises';
import { resolve, relative, dirname, sep, isAbsolute } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createHash } from 'node:crypto';
import { spawnSync } from 'node:child_process';

const projectRoot = fileURLToPath(new URL('../', import.meta.url));
const publicSourceRoot = resolve(projectRoot, '../..');
const templateRoot = fileURLToPath(new URL('./template/', import.meta.url));
const inside = (root, child) => { const p = relative(root, child); return p === '' || (!p.startsWith(`..${sep}`) && p !== '..' && !isAbsolute(p)); };
export const digest = bytes => createHash('sha256').update(bytes).digest('hex');

export async function inventory(directory, { archive = false } = {}) {
  const root = await realpath(directory);
  if (!(await lstat(directory)).isDirectory() || (await lstat(directory)).isSymbolicLink()) throw new Error('Input must be a real directory');
  const files = [];
  async function walk(dir, prefix = '') {
    for (const entry of (await readdir(dir, { withFileTypes: true })).sort((a,b) => a.name.localeCompare(b.name, 'en'))) {
      const name = prefix + entry.name;
      if (!entry.isFile() && !entry.isDirectory()) throw new Error('Symlinks and special files are not allowed');
      if (/[\x00-\x1f\\]/.test(name)) throw new Error('Unsafe filename');
      if (archive && (entry.name.startsWith('.') || /^(?:node_modules|server|zotero)$/.test(entry.name) || /(?:^|\/)api\/zotero(?:\/|$)/.test(name))) throw new Error('Archive contains a reserved/private path');
      const path = resolve(dir, entry.name);
      if (!inside(root, await realpath(path))) throw new Error('Input escapes directory');
      if (entry.isDirectory()) await walk(path, name + '/');
      else {
        const bytes = await readFile(path);
        if (archive && bytes.length > 25 * 1024 * 1024) throw new Error('Archive asset exceeds the 25 MiB upstream limit');
        files.push({ path: name, bytes: bytes.length, sha256: digest(bytes) });
      }
    }
  }
  await walk(root);
  if (archive && !files.some(f => f.path === 'index.html')) throw new Error('Archive index.html is required');
  if (archive && files.length > 20000) throw new Error('Archive exceeds the conservative upstream 20,000-asset limit');
  return files;
}

async function syncRuntime(destination) {
  for (const name of ['worker', 'src', 'migrations']) {
    const source = resolve(projectRoot, name);
    await inventory(source); // Reject symlink escapes before copying anything.
    await cp(source, resolve(destination, 'zotero', name), { recursive: true, force: true, dereference: false });
  }
  await writeFile(resolve(destination, 'zotero/package.json'), JSON.stringify({ private: true, type: 'module' }) + '\n');
  for (const name of ['prepare-migrations.mjs', 'verify-archive.mjs']) {
    await copyFile(fileURLToPath(new URL(name, import.meta.url)), resolve(destination, 'scripts', name));
  }
}

async function main() {
  const args = process.argv.slice(2), options = {};
  const names = new Set(['--destination','--archive-dir','--plugin-root','--refresh-runtime']);
  for (let i=0;i<args.length;i++) {
    if (!names.has(args[i]) || Object.hasOwn(options,args[i])) throw new Error('Unsupported or duplicate option');
    if (args[i] === '--refresh-runtime') options[args[i]] = true;
    else { if (!args[i+1] || args[i+1].startsWith('--')) throw new Error('Missing option value'); options[args[i++]] = args[i]; }
  }
  if (!options['--destination'] || !isAbsolute(options['--destination'])) throw new Error('--destination must be an absolute private checkout path');
  const requested = resolve(options['--destination']);
  await mkdir(dirname(requested), { recursive: true });
  const destination = resolve(await realpath(dirname(requested)), requested.split(sep).at(-1));
  let destinationStat;
  try { destinationStat = await lstat(destination); } catch (error) { if (error.code !== 'ENOENT') throw error; }
  if (destinationStat && (!destinationStat.isDirectory() || destinationStat.isSymbolicLink())) throw new Error('Destination must be a real directory, not a symlink');
  if (inside(await realpath(publicSourceRoot), destination)) throw new Error('Private hosting checkout cannot be inside the public source snapshot');
  if (options['--refresh-runtime']) {
    try { await lstat(resolve(destination,'.sites-runtime/archive-refresh-pending.json')); throw new Error('Interrupted archive refresh must be reconciled before source updates'); } catch (error) { if (error.code !== 'ENOENT') throw error; }
    const marker = JSON.parse(await readFile(resolve(destination, '.sites-runtime/zotero-materialization.json'), 'utf8'));
    if (marker.kind !== 'daily-agent-private-vinext-checkout') throw new Error('Unrecognized destination');
    await syncRuntime(destination);
    console.log(JSON.stringify({ status:'runtime_refreshed', destination, installed:false, deployed:false }));
    return;
  }
  const archive = options['--archive-dir'];
  const plugin = options['--plugin-root'];
  if (!archive || !isAbsolute(archive) || !plugin || !isAbsolute(plugin)) throw new Error('Explicit absolute --archive-dir and --plugin-root are required');
  const archiveRoot = await realpath(archive);
  if (inside(archiveRoot, destination) || inside(destination, archiveRoot)) throw new Error('Archive input and checkout must be disjoint');
  const before = await inventory(archive, { archive:true });
  await mkdir(destination, { recursive: true, mode:0o700 });
  if ((await readdir(destination)).length) throw new Error('Destination must be empty; use --refresh-runtime only for an existing materialization');
  const result = spawnSync(process.execPath, [resolve(plugin,'scripts/project-setup.mjs'),'--template-source',templateRoot], { cwd:destination, stdio:'inherit' });
  if (result.error || result.status !== 0) throw result.error || new Error('Official project setup failed');
  await syncRuntime(destination);
  for (const file of before) {
    const target = resolve(destination, 'public', file.path);
    await mkdir(dirname(target), { recursive:true });
    await copyFile(resolve(archiveRoot,file.path), target);
  }
  const copied = await inventory(resolve(destination,'public'), { archive:true });
  const after = await inventory(archiveRoot, { archive:true });
  if (JSON.stringify(before) !== JSON.stringify(copied) || JSON.stringify(before) !== JSON.stringify(after)) throw new Error('Archive changed during preparation; stop before build');
  await mkdir(resolve(destination,'.sites-runtime'), { recursive:true });
  await writeFile(resolve(destination,'.sites-runtime/archive-inventory.json'),JSON.stringify({version:1,files:before},null,2)+'\n');
  await writeFile(resolve(destination,'.sites-runtime/zotero-materialization.json'),JSON.stringify({kind:'daily-agent-private-vinext-checkout',archive_files:before.length,archive_sha256:digest(JSON.stringify(before))},null,2)+'\n');
  console.log(JSON.stringify({status:'private_checkout_materialized',destination,archive_files:before.length,archive_bytes:before.reduce((n,f)=>n+f.bytes,0),archive_unchanged:true,installed:false,built:false,deployed:false}));
}
if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) main().catch(error=>{ console.error(error.message); process.exitCode=1; });
