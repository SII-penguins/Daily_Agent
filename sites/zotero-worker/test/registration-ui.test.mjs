import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { createHash } from 'node:crypto';
import { renderRegistrationPage } from '../src/registration-ui.mjs';

// Executes the actual shipped inline script; no browser/layout claims.
class Element {
  constructor(tag) { this.tagName = tag.toUpperCase(); this.children = []; this.listeners = {}; this.disabled = false; this.value = ''; this.files = []; this._text = ''; }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  set innerHTML(_) { throw new Error('HTML insertion forbidden'); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this._text = ''; this.children = children; }
  addEventListener(type, callback) { (this.listeners[type] ||= []).push(callback); }
  async emit(type) { const event = { defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } }; await Promise.all((this.listeners[type] || []).map(callback => callback(event))); return event; }
}
const bytes = text => new TextEncoder().encode(text);
const sha = bytes => createHash('sha256').update(bytes).digest('hex');
const source = { report: bytes('{"synthetic":"report"}'), html: bytes('<!doctype html><title>Synthetic reviewed report</title>'), catalog: bytes('{"synthetic":"catalog"}') };
const pdf = bytes('%PDF-1.4\nsynthetic-test-only\n%%EOF\n');
const pdfSha = sha(pdf);
const manifest = { enabled: true, registered: false, catalog_enabled: false, enable_after_registration: true,
  state: 'ready', report_id: 'synthetic-report', catalog_id: 'synthetic-catalog', catalog_version: 1,
  filenames: { report: 'corrected-manifest.json', html: 'report-reviewed.html', catalog: 'verified-catalog.json' },
  source_pins: Object.fromEntries(Object.entries(source).map(([kind, value]) => [kind + '_sha256', sha(value)])),
  limits: { report: 1024 * 1024, catalog: 1024 * 1024, html: 4 * 1024 * 1024, pdf: 16 * 1024 * 1024, totalPdf: 24 * 1024 * 1024, totalSources: 4 * 1024 * 1024 },
  pdfs: [{ sha256: pdfSha, size: pdf.length, staged: false }] };
const files = () => [...Object.entries(source).map(([kind, value]) => new File([value], manifest.filenames[kind])), new File([pdf], pdfSha), new File(['DO NOT UPLOAD'], 'unrelated-private.txt')];
const response = (data, status = 200) => ({ ok: status >= 200 && status < 300, json: async () => data });
const completed = enabled => ({ registered: true, enabled, report_id: manifest.report_id, catalog_id: manifest.catalog_id, catalog_version: 1, paper_count: 1, pdf_count: 1 });
async function harness({ data = structuredClone(manifest), handler } = {}) {
  const rendered = renderRegistrationPage(); const html = await rendered.text(); const nodes = new Map(); const calls = []; const listeners = {};
  let current = data; let reloads = 0;
  for (const match of html.matchAll(/<([a-z][a-z0-9-]*)\b([^>]*\bid="([^"]+)"[^>]*)>/gi)) {
    const element = new Element(match[1]); element.disabled = /\bdisabled(?:\s|$)/.test(match[2]); nodes.set(match[3], element);
  }
  const get = id => nodes.get(id);
  const platform = { crypto: globalThis.crypto, AbortController, btoa,
    document: { getElementById: id => { assert.ok(nodes.has(id), id); return get(id); }, createElement: tag => new Element(tag) },
    addEventListener(type, listener) { (listeners[type] ||= []).push(listener); }, location: { reload() { reloads++; } },
    fetch: async (path, options) => {
      assert.match(path, /^\/api\/zotero\/catalog-registration(?:\/|$)/);
      assert.equal(options.credentials, 'same-origin'); assert.equal(options.redirect, 'error');
      const isRaw = options.headers['Content-Type'] === 'application/pdf';
      const call = { path, method: options.method, headers: options.headers, signal: options.signal,
        body: options.body === undefined ? undefined : isRaw ? Uint8Array.from(options.body) : JSON.parse(options.body) };
      calls.push(call);
      if (options.method === 'POST') assert.equal(options.headers['X-Zotero-Action'], '1');
      const custom = await handler?.(call); if (custom) return custom;
      if (path === '/api/zotero/catalog-registration') return response(current);
      if (path.endsWith('/pdfs/' + pdfSha)) { current = { ...current, pdfs: current.pdfs.map(pdf => ({ ...pdf, staged: true })) }; return response({ staged: true, sha256: pdfSha, size: pdf.length }); }
      if (path.endsWith('/finalize')) return response(completed(current.enable_after_registration));
      throw new Error('Unexpected request');
    },
  };
  const script = html.match(/<script nonce="[^"]+">([\s\S]*?)<\/script>/)[1];
  const client = vm.runInNewContext(script, platform, { timeout: 1000 }); await client.ready;
  return { html, rendered, get, calls, client,
    setManifest(value) { current = value; },
    async choose(chosen = files()) { get('registration-files').files = chosen; await get('registration-files').emit('change'); },
    async submit() { return get('registration-form').emit('submit'); },
    emitWindow(type, event = {}) { for (const listener of listeners[type] || []) listener(event); },
    get reloads() { return reloads; },
  };
}

test('registration page is no-store, nonce-protected, no-JS disabled and has no credential input', async () => {
  const h = await harness();
  assert.match(h.html, /lang="zh-CN"/); assert.match(h.html, /webkitdirectory multiple disabled/);
  assert.match(h.html, /id="registration-submit"[^>]* disabled/); assert.match(h.html, /<noscript>/);
  assert.doesNotMatch(h.html, /type="password"|localStorage|sessionStorage|console\.|<script[^>]*src=/);
  assert.equal(h.rendered.headers.get('Cache-Control'), 'no-store, private');
  assert.match(h.rendered.headers.get('Content-Security-Policy'), /form-action 'none'/);
  assert.equal(h.calls.filter(call => call.method === 'POST').length, 0);
});

test('disabled server setup blocks directory processing and keyboard submit', async () => {
  const h = await harness({ data: { enabled: false, registered: false, state: 'setup_required', pdfs: [] } });
  assert.equal(h.get('registration-files').disabled, true); await h.choose();
  assert.equal((await h.submit()).defaultPrevented, true); assert.equal(h.calls.filter(call => call.method === 'POST').length, 0);
  assert.match(h.get('registration-status').textContent, /尚未开放/);
});

test('directory choice hashes locally, displays exact byte counts and sends no files', async () => {
  const h = await harness(); await h.choose();
  assert.equal(h.get('registration-submit').disabled, false);
  assert.match(h.get('registration-counts').textContent, /3 份来源文件.*1 份 PDF.*其他 1 个文件不会上传/);
  assert.equal(h.calls.length, 1); assert.doesNotMatch(h.get('registration-details').textContent, /DO NOT UPLOAD/);
  await h.get('registration-reset').emit('click'); assert.equal(h.get('registration-submit').disabled, true);
  assert.equal(h.get('registration-files').value, ''); assert.equal(h.calls.filter(call => call.method === 'POST').length, 0);
});

test('missing, ambiguous and mismatched source/PDF bytes never reach a network write', async () => {
  for (const chosen of [files().slice(1), [...files(), files()[0]],
    files().map(file => file.name === manifest.filenames.catalog ? new File(['PRIVATE WRONG CATALOG'], file.name) : file),
    files().map(file => file.name === pdfSha ? new File([new Uint8Array(pdf.length)], file.name) : file)]) {
    const h = await harness(); await h.choose(chosen); await h.submit();
    assert.equal(h.get('registration-submit').disabled, true); assert.equal(h.calls.filter(call => call.method === 'POST').length, 0);
    assert.doesNotMatch(h.get('registration-status').textContent, /PRIVATE WRONG CATALOG/);
  }
});

test('explicit register rechecks pins, posts exact PDF bytes, verifies staging, then finalizes exact sources', async () => {
  const h = await harness(); await h.choose(); await h.submit();
  assert.deepEqual(h.calls.map(call => call.method), ['GET', 'GET', 'POST', 'GET', 'POST']);
  const posts = h.calls.filter(call => call.method === 'POST');
  assert.equal(posts[0].headers['Content-Type'], 'application/pdf'); assert.deepEqual(posts[0].body, pdf);
  assert.equal(posts[1].headers['Content-Type'], 'application/json');
  assert.deepEqual(posts[1].body, { action: 'register_catalog', report_b64: Buffer.from(source.report).toString('base64'),
    html_b64: Buffer.from(source.html).toString('base64'), catalog_b64: Buffer.from(source.catalog).toString('base64') });
  assert.match(h.get('registration-status').textContent, /服务器已确认.*目录已就绪/);
  assert.equal(h.get('registration-submit').disabled, true); assert.equal(h.get('registration-files').value, '');
  assert.equal(h.calls.some(call => /connection|executions|import-intents/.test(call.path)), false);
});

test('registered but disabled is not ready, and approved explicit activation remains possible', async () => {
  const disabled = await harness({ data: { ...structuredClone(manifest), enable_after_registration: false } });
  await disabled.choose(); await disabled.submit();
  assert.match(disabled.get('registration-status').textContent, /登记，但尚未启用/);
  assert.doesNotMatch(disabled.get('registration-status').textContent, /目录已就绪/);
  const resumable = await harness({ data: { ...structuredClone(manifest), registered: true, catalog_enabled: false, enable_after_registration: true } });
  assert.equal(resumable.get('registration-files').disabled, false); await resumable.choose(); await resumable.submit();
  assert.match(resumable.get('registration-status').textContent, /目录已就绪/);
});

test('configuration drift after selection prevents all uploads', async () => {
  const h = await harness(); await h.choose();
  h.setManifest({ ...structuredClone(manifest), source_pins: { ...manifest.source_pins, catalog_sha256: 'b'.repeat(64) } });
  await h.submit(); assert.equal(h.calls.filter(call => call.method === 'POST').length, 0);
  assert.match(h.get('registration-status').textContent, /尚未确认/);
});

test('PDF stage response and server staged readback must agree before finalize', async () => {
  for (const mode of ['bad_response', 'missing_readback']) {
    const h = await harness({ handler(call) {
      if (call.path.includes('/pdfs/')) return response(mode === 'bad_response' ? { staged: true, sha256: 'b'.repeat(64), size: pdf.length } : { staged: true, sha256: pdfSha, size: pdf.length });
    } });
    await h.choose(); await h.submit();
    assert.equal(h.calls.some(call => call.path.endsWith('/finalize')), false, mode);
    assert.match(h.get('registration-status').textContent, /部分 PDF 可能已私有暂存/);
  }
});

test('double submit is locked, pagehide aborts in-flight work and prevents later finalize', async () => {
  let release;
  const h = await harness({ handler(call) { if (call.path.includes('/pdfs/')) return new Promise(resolve => { release = resolve; }); } });
  await h.choose(); const first = h.submit();
  while (!release) await new Promise(resolve => setImmediate(resolve));
  await h.submit(); assert.equal(h.calls.filter(call => call.method === 'POST').length, 1);
  assert.equal(h.get('registration-files').disabled, true);
  h.emitWindow('pagehide'); assert.equal(h.calls.at(-1).signal.aborted, true);
  release(response({ staged: true, sha256: pdfSha, size: pdf.length })); await first;
  assert.equal(h.calls.some(call => call.path.endsWith('/finalize')), false);
  h.emitWindow('pageshow', { persisted: true }); assert.equal(h.reloads, 1);
});

test('HTTP200 without the exact final registration identity never claims readiness', async () => {
  const h = await harness({ handler(call) { if (call.path.endsWith('/finalize')) return response({ ...completed(true), catalog_id: 'different-catalog' }); } });
  await h.choose(); await h.submit(); assert.doesNotMatch(h.get('registration-status').textContent, /目录已就绪/);
  assert.match(h.get('registration-status').textContent, /尚未确认/);
});

test('server limits and total source bound are enforced before file upload', async () => {
  const oversizedConfig = await harness({ data: { ...structuredClone(manifest), limits: { ...manifest.limits, totalPdf: 256 * 1024 * 1024 } } });
  assert.equal(oversizedConfig.get('registration-files').disabled, true);
  const tinyBundle = await harness({ data: { ...structuredClone(manifest), limits: { ...manifest.limits, totalSources: 2 } } });
  await tinyBundle.choose(); await tinyBundle.submit(); assert.equal(tinyBundle.calls.filter(call => call.method === 'POST').length, 0);
});

test('overlapping manifest refreshes are locked and cannot overwrite an intervening directory selection', async () => {
  let hold = false; let release; let held = 0;
  const h = await harness({ handler(call) {
    if (hold && call.method === 'GET') { held++; return new Promise(resolve => { release = resolve; }); }
  } });
  hold = true;
  const first = h.get('registration-refresh').emit('click');
  await h.get('registration-refresh').emit('click');
  assert.equal(held, 1); assert.equal(h.get('registration-files').disabled, true);
  await h.choose(); assert.equal(h.get('registration-submit').disabled, true);
  release(response(structuredClone(manifest))); await first;
  hold = false; await h.choose(); assert.equal(h.get('registration-submit').disabled, false);
  assert.equal(h.calls.filter(call => call.method === 'POST').length, 0);
});

test('new directory choice cancels older asynchronous hashing without mixed source buffers', async () => {
  let release;
  const slow = files();
  slow[0] = { name: manifest.filenames.report, size: source.report.length,
    arrayBuffer: () => new Promise(resolve => { release = () => resolve(source.report.slice().buffer); }) };
  const h = await harness();
  const first = h.choose(slow);
  while (!release) await new Promise(resolve => setImmediate(resolve));
  await h.choose(files()); assert.equal(h.get('registration-submit').disabled, false);
  release(); await first; assert.equal(h.get('registration-submit').disabled, false);
  await h.submit(); assert.match(h.get('registration-status').textContent, /目录已就绪/);
  const final = h.calls.find(call => call.path.endsWith('/finalize'));
  assert.equal(final.body.report_b64, Buffer.from(source.report).toString('base64'));
});
