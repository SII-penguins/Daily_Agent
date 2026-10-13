#!/usr/bin/env node
/** Offline operator packaging only. No service access, tokens, credentials,
 * runtime configuration changes, registration, activation, or library writes. */
import { open, realpath, lstat, mkdir } from 'node:fs/promises';
import { constants } from 'node:fs';
import { dirname, resolve, relative, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { prepareTrustedCatalog, parseCatalogJson, canonicalCatalogJson } from '../src/catalog-registration.mjs';
import { sha256Bytes } from '../src/hash-bytes.mjs';
import { REGISTRATION_LIMITS, registrationConfig } from '../src/registration-routes.mjs';
const BINDING = ['owner_id','report_id','catalog_id','catalog_version','personal_scope'];
const text = new TextEncoder();
const fail = code => { const e = new Error(code); e.code = code; throw e; };
async function readBounded(path, maximum) {
  const before = await lstat(path);
  if (!before.isFile() || before.isSymbolicLink() || before.size > maximum) fail('INVALID_OR_OVERSIZED_LOCAL_FILE');
  const f = await open(path, constants.O_RDONLY | (constants.O_NOFOLLOW || 0));
  try {
    const info = await f.stat();
    if (!info.isFile() || info.size !== before.size || info.dev !== before.dev || info.ino !== before.ino) fail('LOCAL_FILE_CHANGED');
    const bytes = new Uint8Array(info.size); let offset = 0;
    while (offset < bytes.length) { const r = await f.read(bytes, offset, bytes.length-offset, offset); if (!r.bytesRead) fail('LOCAL_FILE_CHANGED'); offset += r.bytesRead; }
    if ((await f.stat()).size !== info.size) fail('LOCAL_FILE_CHANGED');
    return bytes;
  } finally { await f.close(); }
}
async function privateDestination(path) {
  const requested = resolve(path), parent = await realpath(dirname(requested));
  const destination = resolve(parent, requested.slice(dirname(requested).length + 1));
  const source = await realpath(resolve(dirname(fileURLToPath(import.meta.url)), '../../..'));
  const rel = relative(source, destination);
  if (rel === '' || (!rel.startsWith('..'+sep) && rel !== '..' && !rel.startsWith(sep))
      || destination.split(sep).some(p=>['public','dist','node_modules','.git'].includes(p))) fail('PUBLIC_OUTPUT_FORBIDDEN');
  return destination;
}
async function writeExclusive(path, bytes) {
  const f = await open(path, 'wx', 0o600);
  try { await f.writeFile(bytes); await f.sync(); } finally { await f.close(); }
}
export async function main(argv = process.argv.slice(2)) {
  if (argv.length === 1 && ['--help','-h'].includes(argv[0])) {
    process.stdout.write('Offline private registration bundle\nUse --pins FILE --report FILE --html FILE --catalog FILE --pdf-dir DIR\nOptional: --output-dir NEW_PRIVATE_DIR --enable-after-registration\nDefault is validate only. No service or configuration writes.\n'); return;
  }
  const args = new Map(); let enable = false;
  const allowed = new Set(['--pins','--report','--html','--catalog','--pdf-dir','--output-dir']);
  for (let i=0;i<argv.length;i++) {
    if (argv[i]==='--enable-after-registration') { if (enable) fail('INVALID_ARGUMENTS'); enable=true; continue; }
    if (!allowed.has(argv[i]) || args.has(argv[i]) || !argv[i+1] || argv[i+1].startsWith('--')) fail('INVALID_ARGUMENTS');
    args.set(argv[i],argv[++i]);
  }
  for (const flag of ['--pins','--report','--html','--catalog','--pdf-dir']) if (!args.has(flag)) fail('REQUIRED_ARGUMENT_MISSING');
  const output = args.has('--output-dir') ? await privateDestination(args.get('--output-dir')) : null;
  const trustedPins = parseCatalogJson(await readBounded(resolve(args.get('--pins')), REGISTRATION_LIMITS.config));
  const binding = Object.fromEntries(BINDING.map(k=>[k,trustedPins[k]]));
  const report = await readBounded(resolve(args.get('--report')), REGISTRATION_LIMITS.report);
  const html = await readBounded(resolve(args.get('--html')), REGISTRATION_LIMITS.html);
  const catalog = await readBounded(resolve(args.get('--catalog')), REGISTRATION_LIMITS.catalog);
  if (report.length + html.length + catalog.length > REGISTRATION_LIMITS.totalSources) fail('REGISTRATION_SOURCES_TOO_LARGE');
  const pdfDir = await realpath(resolve(args.get('--pdf-dir'))), pdfBytes = new Map(); let totalPdf = 0;
  const prepared = await prepareTrustedCatalog({ binding, trustedPins, reportBytes:report, htmlBytes:html, catalogBytes:catalog,
    readPdfBytes: async digest => {
      if (!/^[a-f0-9]{64}$/.test(digest)) fail('INVALID_PDF_REFERENCE');
      const bytes = await readBounded(resolve(pdfDir,digest),REGISTRATION_LIMITS.pdf);
      totalPdf += bytes.length; if (totalPdf > REGISTRATION_LIMITS.totalPdf) fail('REGISTRATION_PDFS_TOO_LARGE');
      pdfBytes.set(digest,bytes); return bytes;
    } });
  const filenames = {report:'corrected-manifest.json',html:'report-reviewed.html',catalog:'verified-catalog.json'};
  const pdfs = prepared.paperRows.map(row=>JSON.parse(row.metadata_json)).filter(p=>p.pdf)
    .map(p=>({sha256:p.pdf.sha256,size:p.pdf.size,paper_id:p.paper_id}));
  const cfg = {schema:'daily-agent-zotero-registration-config-v1',approved:true,binding,trustedPins,pdfs,filenames,enable_after_registration:enable};
  const configText = canonicalCatalogJson(cfg), configBytes = text.encode(configText);
  if (configBytes.length > REGISTRATION_LIMITS.config) fail('REGISTRATION_CONFIG_TOO_LARGE');
  // Validate against the same route validator without invoking a mock or real IO.
  const db = {}, env = {DB:db,BUCKET:{get(){},put(){}},SITES_GATEWAY_IDENTITY_TRUSTED:'platform-managed',
    ZOTERO_OWNER_ID:binding.owner_id,ZOTERO_SITE_ORIGIN:'https://offline.invalid',
    ZOTERO_VERIFIED_PERSONAL_LIBRARY_ID:binding.personal_scope.slice(9),ZOTERO_CATALOG_REGISTRATION_JSON:configText};
  if (!registrationConfig(env,{db,owner:binding.owner_id,origin:env.ZOTERO_SITE_ORIGIN})) fail('INVALID_REGISTRATION_CONFIGURATION');
  if (output) {
    // Exclusive directory creation occurs only AFTER every source byte validates.
    // A failed disk write can leave a private partial folder; the manifest is
    // written last, and no existing folder/file is ever replaced or deleted.
    await mkdir(output,{mode:0o700}); await mkdir(resolve(output,'pdfs'),{mode:0o700});
    const files = [[filenames.report,report],[filenames.html,html],[filenames.catalog,catalog],
      ...[...pdfBytes].map(([hash,bytes])=>[`pdfs/${hash}`,bytes]),['registration-config.json',configBytes]];
    const manifest = [];
    for (const [name,bytes] of files) { await writeExclusive(resolve(output,name),bytes); manifest.push({path:name,size:bytes.length,sha256:await sha256Bytes(bytes)}); }
    await writeExclusive(resolve(output,'operator-manifest.json'),text.encode(JSON.stringify({schema:'daily-agent-zotero-operator-bundle-v1',
      private:true,complete:true,config_filename:'registration-config.json',config_bytes:configBytes.length,
      enable_after_explicit_registration:enable,files:manifest,
      next_step:'Review and explicitly configure ZOTERO_CATALOG_REGISTRATION_JSON through the authorized operator environment. Then authenticated owner chooses this private folder on /zotero/register and explicitly clicks Register. This bundle does not authorize or execute a Zotero import.'},null,2)+'\n'));
  }
  process.stdout.write(JSON.stringify({valid:true,paper_count:prepared.paperRows.length,pdf_count:pdfs.length,
    total_pdf_bytes:totalPdf,source_bytes:report.length+html.length+catalog.length,config_bytes:configBytes.length,
    private_bundle_written:Boolean(output),enable_after_explicit_registration:enable,
    network_calls:0,service_writes:0,configuration_writes:0,library_writes:0})+'\n');
}
if(process.argv[1]&&resolve(process.argv[1])===fileURLToPath(import.meta.url))main().catch(e=>{
  process.stderr.write(JSON.stringify({valid:false,error:/^[A-Z_]+$/.test(e.code||'')?e.code:'LOCAL_BUNDLE_FAILED'})+'\n');process.exitCode=1;
});
