/** Deterministic owner-upload index derived ONLY from the server-pinned manifest.
 * Offsets start at the first payload byte, after the 12-byte prefix and header.
 * The header deliberately excludes owners, credentials, paper IDs and filenames. */
import { canonicalCatalogJson } from './catalog-registration.mjs';

export const OWNER_BUNDLE_MAGIC = 'DAZB0001';
export const OWNER_BUNDLE_SCHEMA = 'daily-agent-owner-bundle-v1';
export const OWNER_BUNDLE_MAX_HEADER_BYTES = 65536;
const SOURCE_KINDS = ['report', 'html', 'catalog'];
const SOURCE_LIMITS = { report: 1024 * 1024, html: 4 * 1024 * 1024, catalog: 1024 * 1024 };
const BATCH_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$/;
const PAPER_ID = /^[A-Za-z0-9][A-Za-z0-9._:()/;-]{0,299}$/;
const SHA = /^[a-f0-9]{64}$/;
const record = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const fail = () => { const error = new Error('INVALID_OWNER_BUNDLE_CONFIGURATION'); error.code = error.message; throw error; };
const ids = value => Array.isArray(value) && value.length > 0 && value.length <= 100
  && new Set(value).size === value.length && value.every(id => typeof id === 'string' && PAPER_ID.test(id));

/** Accepts either bundleConfig's validated result or the raw pinned manifest.
 * Layout is rebuilt from pins, never accepted from an uploaded file's index. */
export function bundleUploadDescriptor(config) {
  if (!record(config) || config.schema !== 'daily-agent-zotero-private-bundles-v1' || config.approved !== true
      || !ids(config.paper_ids) || !Array.isArray(config.batches) || !config.batches.length || config.batches.length > 16) fail();
  const batches = new Set(), papers = new Set(), memberIds = new Set(), members = [];
  let offset = 0;
  function append(batch, kind, pin) {
    const minimum = kind === 'pdf' ? 8 : 1, maximum = kind === 'pdf' ? 16 * 1024 * 1024 : SOURCE_LIMITS[kind];
    if (!record(pin) || !Number.isSafeInteger(pin.size) || pin.size < minimum || pin.size > maximum
        || typeof pin.sha256 !== 'string' || !SHA.test(pin.sha256)) fail();
    const id = batch.id + '/' + kind + (kind === 'pdf' ? '/' + pin.sha256 : '');
    if (memberIds.has(id) || !Number.isSafeInteger(offset + pin.size)) fail();
    memberIds.add(id);
    members.push({ id, batch_id: batch.id, kind, offset, size: pin.size, sha256: pin.sha256 });
    offset += pin.size;
  }
  for (const batch of config.batches) {
    if (!record(batch) || typeof batch.id !== 'string' || !BATCH_ID.test(batch.id) || batches.has(batch.id)
        || !ids(batch.paper_ids) || !record(batch.sources) || Object.keys(batch.sources).length !== 3
        || !SOURCE_KINDS.every(kind => Object.hasOwn(batch.sources, kind)) || !record(batch.config)
        || !record(batch.config.trustedPins) || !Array.isArray(batch.config.pdfs) || batch.config.pdfs.length > 100) fail();
    batches.add(batch.id);
    for (const id of batch.paper_ids) {
      if (!config.paper_ids.includes(id) || papers.has(id)) fail();
      papers.add(id);
    }
    let sourceSize = 0, pdfSize = 0;
    for (const kind of SOURCE_KINDS) {
      const pin = batch.sources[kind];
      if (!record(pin) || Object.keys(pin).length !== 2 || !Object.hasOwn(pin, 'size') || !Object.hasOwn(pin, 'sha256')
          || pin.sha256 !== batch.config.trustedPins[kind + '_sha256']) fail();
      append(batch, kind, pin); sourceSize += pin.size;
    }
    if (sourceSize > 4 * 1024 * 1024) fail();
    const pdfPapers = new Set();
    for (const pdf of batch.config.pdfs) {
      if (!record(pdf) || Object.keys(pdf).length !== 3 || !['paper_id', 'size', 'sha256'].every(key => Object.hasOwn(pdf, key))
          || !batch.paper_ids.includes(pdf.paper_id) || pdfPapers.has(pdf.paper_id)) fail();
      pdfPapers.add(pdf.paper_id); append(batch, 'pdf', pdf); pdfSize += pdf.size;
    }
    if (pdfSize > 24 * 1024 * 1024) fail();
  }
  if (papers.size !== config.paper_ids.length) fail();
  const header_json = canonicalCatalogJson({ schema: OWNER_BUNDLE_SCHEMA, members });
  const headerSize = new TextEncoder().encode(header_json).length;
  if (headerSize > OWNER_BUNDLE_MAX_HEADER_BYTES) fail();
  return { header_json, total_size: 12 + headerSize + offset };
}
