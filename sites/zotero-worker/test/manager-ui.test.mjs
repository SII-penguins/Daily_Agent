import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import { renderManagerPage } from '../src/manager-ui.mjs';

// Dependency-free DOM/fetch/timer harness executes the exact inline JavaScript
// shipped by renderManagerPage. It is not a layout, browser or screenshot test.
class Element {
  constructor(tag = 'div') { this.tagName = tag.toUpperCase(); this.children = []; this.listeners = new Map(); this.disabled = false; this.checked = false; this.value = ''; this._text = ''; this.className = ''; }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map(child => child.textContent).join(''); }
  set innerHTML(_) { throw new Error('Untrusted HTML insertion is forbidden'); }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this._text = ''; this.children = children; }
  addEventListener(type, listener) { const listeners = this.listeners.get(type) || []; listeners.push(listener); this.listeners.set(type, listeners); }
  async emit(type, properties = {}) {
    const event = { type, target: this, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, ...properties };
    await Promise.all((this.listeners.get(type) || []).map(listener => listener(event)));
    return event;
  }
  checkValidity() { return this.validate ? this.validate() : true; }
}
const idle = () => new Promise(resolve => setImmediate(resolve));
const connected = { ready: true, live_ready: true, credential_entry_enabled: false, credential_storage: 'encrypted_verified', verification_enabled: false };
const disconnected = { ready: false, live_ready: false, credential_entry_enabled: false, credential_storage: 'not_staged', verification_enabled: false };
const staged = { ...disconnected, credential_storage: 'encrypted_pending_verification', verification_enabled: true };
const jobId = 'ze_' + 'a'.repeat(64);
const catalog = { report_id: 'report-20261010', catalog_id: 'catalog-v1', catalog_version: 4,
  title: '真实日报', report_date: '2026-10-10', papers: [
    { paper_id: 'doi:10.55555/example', title: '<img src=x onerror=alert(1)> 真实论文 A', authors: ['Alice'], topics: ['机器学习', '量子计算'], publication_type: '正式发表', publication: 'Example Journal', pdf_available: true },
    { paper_id: 'paper-b', title: '真实论文 B', authors: [{ firstName: 'Bob', lastName: 'Chen' }], topics: ['机器学习'], publication_type: '预印本', pdf_available: false },
  ] };
const job = (state = 'queued', items = [{ paper_id: catalog.papers[0].paper_id, state: 'queued' }], extra = {}) => ({ job_id: jobId, state, items, retry_at: null, ...extra });
function result(data, status = 200) { return { ok: status >= 200 && status < 300, status, json: async () => data }; }
async function harness({ connection = disconnected, catalogs = [catalog], handler, start = 1_800_000_000_000 } = {}) {
  const response = renderManagerPage({ connection });
  const html = await response.text();
  const elements = new Map();
  for (const match of html.matchAll(/<([a-z][a-z0-9-]*)\b([^>]*\bid="([^"]+)"[^>]*)>/gi)) {
    const element = new Element(match[1]); element.id = match[3]; element.disabled = /\bdisabled(?:\s|>|$)/.test(match[2]);
    element.type = match[2].match(/\btype="([^"]+)"/)?.[1]; element.value = match[2].match(/\bvalue="([^"]*)"/)?.[1] || '';
    elements.set(element.id, element);
  }
  const calls = []; const timers = new Map(); const events = new Map(); let timerId = 0; let now = start; let reloads = 0;
  let currentConnection = connection; let historyJobs = [];
  const FakeDate = class extends Date { static now() { return now; } };
  const platform = {
    document: { getElementById: id => { assert.ok(elements.has(id), 'Missing real HTML node ' + id); return elements.get(id); }, createElement: tag => new Element(tag) },
    fetch: async (path, options) => {
      assert.match(path, /^\/api\/zotero\//, 'Every request must be same-origin Zotero API');
      assert.equal(options.credentials, 'same-origin'); assert.equal(options.redirect, 'error');
      const call = { path, method: options.method, body: options.body ? JSON.parse(options.body) : undefined, headers: options.headers, signal: options.signal };
      calls.push(call);
      if (options.method === 'POST') { assert.equal(options.headers['Content-Type'], 'application/json'); assert.equal(options.headers['X-Zotero-Action'], '1'); }
      if (path === '/api/zotero/executions' && options.method === 'GET') return result({ jobs: historyJobs });
      const handled = await handler?.(call);
      if (handled) return handled;
      if (path === '/api/zotero/connection') return result(currentConnection);
      if (path === '/api/zotero/catalogs') return result({ catalogs });
      const found = catalogs.find(item => path === '/api/zotero/reports/' + encodeURIComponent(item.report_id) + '/catalogs/' + encodeURIComponent(item.catalog_id));
      if (found) return result(found);
      throw new Error('Unmocked request ' + options.method + ' ' + path);
    },
    setTimeout: (callback, delay) => { const id = ++timerId; timers.set(id, { callback, delay, at: now + delay }); return id; },
    clearTimeout: id => timers.delete(id), AbortController, Date: FakeDate,
    addEventListener: (type, listener) => { const listeners = events.get(type) || []; listeners.push(listener); events.set(type, listeners); },
    location: { reload() { reloads++; } },
  };
  const script = html.match(/<script nonce="[^"]+">([\s\S]*?)<\/script>/)[1];
  const client = vm.runInNewContext(script, platform, { timeout: 1000 });
  elements.get('credential-form').validate = () => elements.get('api-key').value.length >= 16 && elements.get('api-key').value.length <= 256;
  await client.ready;
  const get = id => elements.get(id);
  const inputs = (node = get('paper-cards')) => node.children.flatMap(child => child.tagName === 'INPUT' ? [child] : inputs(child));
  return { html, response, client, get, inputs, calls, timers,
    setConnection(value) { currentConnection = value; },
    setJobs(value) { historyJobs = value; },
    async select(index = 0) { const input = inputs()[index]; assert.ok(input); input.checked = true; await input.emit('change'); },
    async pdf(id = 'pdf-metadata') { get(id).checked = true; await get(id).emit('change'); },
    async save() { return get('save-form').emit('submit'); },
    async runTimer() { assert.ok(timers.size, 'Expected a scheduled job check'); const [id, timer] = [...timers].sort((a, b) => a[1].at - b[1].at)[0]; timers.delete(id); now = Math.max(now, timer.at); timer.callback(); await idle(); await idle(); return timer; },
    emitWindow(type, properties = {}) { for (const listener of events.get(type) || []) listener(properties); },
    get reloads() { return reloads; },
  };
}

test('rendered page is Chinese, private, nonce protected, no-JS safe and never serializes secrets', async () => {
  const response = renderManagerPage({ connection: { ...connected, credential_entry_enabled: true,
    api_key: 'NEVER-RENDER-THIS-SECRET', error: '</script><script>alert(1)</script>' } });
  const html = await response.text();
  assert.match(html, /lang="zh-CN"/); assert.match(html, /挑选值得留存的论文/);
  assert.match(html, /id="credential-fields" disabled/); assert.match(html, /id="save-to-zotero"[^>]+ disabled/);
  assert.match(html, /<noscript>/); assert.match(html, /name="pdf-choice"/);
  assert.doesNotMatch(html, /NEVER-RENDER|alert\(1\)|localStorage|sessionStorage|<script[^>]+src=|<link[^>]+href=/);
  assert.doesNotMatch(html, /id="api-key"[^>]+name=/);
  const csp = response.headers.get('Content-Security-Policy');
  assert.match(csp, /default-src 'none'/); assert.match(csp, /connect-src 'self'/); assert.match(csp, /form-action 'none'/);
  const nonce = html.match(/<script nonce="([^"]+)"/)[1]; assert.ok(csp.includes("script-src 'nonce-" + nonce + "'"));
  assert.equal(response.headers.get('Cache-Control'), 'no-store, private'); assert.equal(response.headers.get('X-Frame-Options'), 'DENY');
});

test('real server cards begin unchecked, text is literal, selection and filters perform no writes', async () => {
  const h = await harness();
  assert.equal(h.inputs().length, 2); assert.ok(h.inputs().every(input => !input.checked));
  assert.match(h.get('paper-cards').textContent, /<img src=x onerror=alert\(1\)> 真实论文 A/);
  assert.equal(h.get('connection-english').textContent, 'NOT CONNECTED');
  assert.equal(h.get('credential-fields').disabled, true); assert.equal(h.get('verify-connection').disabled, true);
  await h.select(); await h.pdf(); await h.save();
  assert.equal(h.get('selected-count').textContent, '1'); assert.equal(h.get('save-to-zotero').disabled, true);
  assert.match(h.get('date-collections').textContent, /2026-10-10 · 1 篇/);
  assert.match(h.get('topic-collections').textContent, /量子计算 · 1 篇/);
  assert.match(h.get('publication-collections').textContent, /正式发表 · 1 篇/);
  h.get('publication-filter').value = '预印本'; await h.get('publication-filter').emit('change');
  assert.equal(h.inputs().length, 1); assert.equal(h.get('selected-count').textContent, '1');
  assert.match(h.get('selected-papers').textContent, /真实论文 A/);
  assert.equal(h.calls.filter(call => call.method === 'POST').length, 0);
});

test('empty catalog and empty selection cannot submit even by keyboard form submit', async () => {
  const h = await harness({ connection: connected, catalogs: [] });
  assert.match(h.get('catalog-status').textContent, /尚无可用/); assert.equal(h.inputs().length, 0);
  assert.equal((await h.save()).defaultPrevented, true); assert.equal(h.calls.filter(call => call.method === 'POST').length, 0);
  const withCatalog = await harness({ connection: connected });
  await withCatalog.pdf(); await withCatalog.save(); assert.equal(withCatalog.calls.filter(call => call.method === 'POST').length, 0);
});

test('PDF choice is explicit and reset clears papers and PDF without submitting', async () => {
  const h = await harness({ connection: connected });
  await h.select(); assert.equal(h.get('save-to-zotero').disabled, true); await h.save();
  assert.match(h.get('save-help').textContent, /明确选择/);
  await h.pdf('pdf-open-access'); assert.equal(h.get('save-to-zotero').disabled, false);
  await h.get('save-form').emit('reset');
  assert.equal(h.get('selected-count').textContent, '0'); assert.ok(h.inputs().every(input => !input.checked));
  assert.equal(h.get('pdf-open-access').checked, false); assert.equal(h.get('save-to-zotero').disabled, true);
  assert.equal(h.calls.filter(call => call.method === 'POST').length, 0);
});

test('fresh explicit Save submits only allowlisted IDs and locks double-clicks', async () => {
  let release;
  const h = await harness({ connection: connected, handler: call => call.path === '/api/zotero/executions'
    ? new Promise(resolve => { release = resolve; }) : undefined });
  await h.select(); await h.pdf('pdf-open-access');
  const first = h.save(); const second = h.save(); await second;
  assert.equal(h.get('save-to-zotero').disabled, true); assert.ok(h.inputs().every(input => input.disabled));
  const posts = h.calls.filter(call => call.method === 'POST'); assert.equal(posts.length, 1);
  assert.deepEqual(posts[0].body, { action: 'save_selected', report_id: catalog.report_id, catalog_id: catalog.catalog_id,
    catalog_version: 4, paper_ids: [catalog.papers[0].paper_id], pdf_intent: 'open_access_if_available' });
  release(result(job('complete', [{ paper_id: catalog.papers[0].paper_id, state: 'complete', metadata_saved: true, pdf_verified: true }])));
  await first; assert.match(h.get('selection-status').textContent, /此选择已提交/); assert.match(h.get('job-status').textContent, /已完成/); assert.match(h.get('job-items').textContent, /PDF 已核验/);
  assert.equal(h.timers.size, 0);
});

test('only current explicitly saved job advances serially, reading progress before each bounded action', async () => {
  let advances = 0;
  const h = await harness({ connection: connected, handler(call) {
    if (call.path === '/api/zotero/executions') return result(job());
    if (call.path === '/api/zotero/executions/' + jobId) return result(job(advances ? 'running' : 'queued'));
    if (call.path.endsWith('/advance')) { advances++; return result(job(advances === 1 ? 'running' : 'complete',
      [{ paper_id: catalog.papers[0].paper_id, state: advances === 1 ? 'metadata_saved' : 'complete', metadata_saved: true }])); }
  } });
  assert.equal(h.timers.size, 0); assert.equal(h.calls.some(call => call.path.includes('intent') || (call.path.includes('executions') && call.method === 'POST')), false);
  await h.select(); await h.pdf('pdf-open-access'); await h.save();
  await h.runTimer(); assert.equal(advances, 1); assert.match(h.get('job-items').textContent, /元数据已保存.*PDF 尚未确认完成/);
  await h.runTimer(); assert.equal(advances, 2); assert.equal(h.timers.size, 0);
  assert.deepEqual(h.calls.filter(call => call.path.endsWith('/advance')).map(call => call.body), [{ action: 'continue_import' }, { action: 'continue_import' }]);
  const progress = h.calls.filter(call => call.path.includes('/executions/')).map(call => call.method);
  assert.deepEqual(progress, ['GET', 'POST', 'GET', 'POST']);
});

test('server retry_at defers further processing until the returned timestamp', async () => {
  const retryAt = new Date(1_800_000_020_000).toISOString();
  const h = await harness({ connection: connected, handler(call) {
    if (call.path === '/api/zotero/executions') return result(job('retry_wait', [{ paper_id: 'paper-b', state: 'failure', retryable: true, retry_at: retryAt }], { retry_at: retryAt }));
    if (call.path === '/api/zotero/executions/' + jobId) return result(job('retry_wait', [], { retry_at: retryAt }));
    if (call.path.endsWith('/advance')) return result(job('complete'));
  } });
  await h.select(); await h.pdf(); await h.save();
  assert.equal([...h.timers.values()][0].delay, 20_000); assert.equal(h.calls.some(call => call.path.endsWith('/advance')), false);
  await h.runTimer(); assert.equal(h.calls.filter(call => call.path.endsWith('/advance')).length, 1); assert.equal(h.timers.size, 0);
});

test('terminal outcomes and nonretryable PDF pending never auto-advance', async () => {
  for (const terminal of ['complete', 'already_exists', 'failure', 'partial_failure', 'connection_required', 'blocked_connection', 'preview_only', 'pdf_pending']) {
    const h = await harness({ connection: connected, handler: call => call.path === '/api/zotero/executions'
      ? result(job(terminal, [{ paper_id: catalog.papers[0].paper_id, state: terminal === 'pdf_pending' ? 'pdf_pending' : terminal,
        metadata_saved: true, pdf_verified: false, retryable: false }])) : undefined });
    await h.select(); await h.pdf('pdf-open-access'); await h.save();
    assert.equal(h.timers.size, 0, terminal); assert.equal(h.calls.filter(call => call.path.endsWith('/advance')).length, 0, terminal);
    assert.doesNotMatch(h.get('job-status').textContent, /Saved/);
    if (terminal === 'pdf_pending') { assert.match(h.get('job-items').textContent, /PDF 待处理.*元数据已保存/); assert.match(h.get('job-status').textContent, /已停止自动继续/); }
    if (terminal === 'connection_required') assert.equal(h.get('save-to-zotero').disabled, true);
  }
});

test('staging clears password, does not verify or import, and connection later needs a fresh Save', async () => {
  let h;
  h = await harness({ connection: { ...disconnected, credential_entry_enabled: true }, handler(call) {
    if (call.path === '/api/zotero/connect') {
      assert.equal(h.get('api-key').value, '');
      assert.deepEqual(call.body, { action: 'stage_credentials', api_key: 'synthetic-test-key-only' });
      h.setConnection(staged); return result({ status: 'not_connected', credential_storage: 'encrypted_pending_verification' }, 201);
    }
    if (call.path === '/api/zotero/verify-connection') { h.setConnection(connected); return result(connected); }
    if (call.path === '/api/zotero/executions') return result(job('complete'));
  } });
  await h.select(); await h.pdf();
  h.get('api-key').value = 'synthetic-test-key-only'; await h.get('credential-form').emit('submit');
  assert.equal(h.get('api-key').value, ''); assert.equal(h.get('credential-fields').disabled, true); assert.equal(h.get('verify-connection').disabled, false);
  assert.equal(h.get('connection-english').textContent, 'NOT CONNECTED'); assert.equal(h.get('save-to-zotero').disabled, true);
  assert.deepEqual(h.calls.filter(call => call.method === 'POST').map(call => call.path), ['/api/zotero/connect']);
  assert.doesNotMatch(h.get('credential-result').textContent, /synthetic-test-key-only/);
  await h.get('verify-connection').emit('click');
  assert.equal(h.get('connection-english').textContent, 'CONNECTED'); assert.equal(h.get('save-to-zotero').disabled, false);
  assert.equal(h.calls.some(call => call.path === '/api/zotero/executions' && call.method === 'POST'), false);
  await h.save(); assert.equal(h.calls.filter(call => call.path === '/api/zotero/executions' && call.method === 'POST').length, 1);
});

test('verify requires both staged credentials and an explicit server gate; GET never verifies', async () => {
  for (const connection of [disconnected, { ...disconnected, verification_enabled: true }, { ...staged, verification_enabled: false }]) {
    const h = await harness({ connection });
    await h.get('verify-connection').emit('click'); assert.equal(h.calls.filter(call => call.method === 'POST').length, 0);
  }
});

test('credential transport failure is generic, clears entry and requires status refresh', async () => {
  const h = await harness({ connection: { ...disconnected, credential_entry_enabled: true }, handler(call) {
    if (call.path === '/api/zotero/connect') throw new Error('synthetic-test-key-only');
  } });
  h.get('api-key').value = 'synthetic-test-key-only'; await h.get('credential-form').emit('submit');
  assert.equal(h.get('api-key').value, ''); assert.equal(h.get('credential-fields').disabled, true);
  assert.doesNotMatch(h.get('credential-result').textContent, /synthetic-test-key-only/);
  assert.match(h.get('credential-result').textContent, /尚未确认/);
});

test('ambiguous Save is not automatically replayed or marked saved', async () => {
  const h = await harness({ connection: connected, handler(call) { if (call.path === '/api/zotero/executions') throw new Error('connection lost'); } });
  await h.select(); await h.pdf(); await h.save();
  assert.equal(h.calls.filter(call => call.method === 'POST').length, 1); assert.equal(h.timers.size, 0);
  assert.match(h.get('job-status').textContent, /尚未确认，未自动重发/);
});

test('newer catalog navigation ignores an older response and resets all choices', async () => {
  const second = { ...catalog, catalog_id: 'catalog-second', papers: [{ paper_id: 'paper-new', title: '新目录论文', topics: ['新主题'] }] };
  let postpone = false; let resolveOld;
  const h = await harness({ connection: connected, catalogs: [catalog, second], handler(call) {
    if (postpone && call.path.endsWith('/catalogs/' + catalog.catalog_id)) return new Promise(resolve => { resolveOld = resolve; });
  } });
  await h.select(); await h.pdf(); postpone = true;
  h.get('catalog-select').value = '0'; const old = h.get('catalog-select').emit('change'); await idle();
  h.get('catalog-select').value = '1'; await h.get('catalog-select').emit('change'); await idle();
  resolveOld(result(catalog)); await old; await idle();
  assert.match(h.get('paper-cards').textContent, /新目录论文/); assert.doesNotMatch(h.get('paper-cards').textContent, /真实论文 A/);
  assert.equal(h.get('selected-count').textContent, '0'); assert.equal(h.get('pdf-metadata').checked, false);
  assert.equal(h.calls.filter(call => call.method === 'POST').length, 0);
});

test('pagehide cancels polling, aborts requests and BFCache restore requires a new page', async () => {
  const h = await harness({ connection: connected, handler: call => call.path === '/api/zotero/executions' ? result(job()) : undefined });
  await h.select(); await h.pdf(); await h.save(); assert.equal(h.timers.size, 1);
  h.get('api-key').value = 'synthetic-unsent-key'; h.emitWindow('pagehide');
  assert.equal(h.timers.size, 0); assert.equal(h.get('api-key').value, ''); assert.equal(h.get('save-to-zotero').disabled, true);
  h.emitWindow('pageshow', { persisted: true }); assert.equal(h.reloads, 1);
  assert.equal(h.calls.filter(call => call.path.endsWith('/advance')).length, 0);
});

test('actual execution history resumes only after an explicit click, without creating another selection', async () => {
  const h = await harness({ connection: connected, handler(call) {
    if (call.path === '/api/zotero/executions/' + jobId) return result(job('running', [{ paper_id: 'paper-b', state: 'metadata_saved', metadata_saved: true }], { pdf_intent: 'metadata_only' }));
    if (call.path.endsWith('/advance')) return result(job('complete', [{ paper_id: 'paper-b', state: 'complete' }], { pdf_intent: 'metadata_only' }));
  } });
  h.setJobs([job('running', [{ paper_id: 'paper-b', state: 'metadata_saved', metadata_saved: true }], { pdf_intent: 'metadata_only' })]);
  await h.get('refresh-jobs').emit('click'); await idle();
  assert.equal(h.timers.size, 0); assert.equal(h.calls.filter(call => call.method === 'POST').length, 0);
  const resume = h.get('job-history').children[0].children.find(child => child.textContent === '继续此任务');
  assert.equal(resume.textContent, '继续此任务'); assert.equal(resume.disabled, false);
  await resume.emit('click'); await resume.emit('click'); assert.equal(h.timers.size, 1);
  await h.runTimer();
  assert.equal(h.calls.filter(call => call.path === '/api/zotero/executions' && call.method === 'POST').length, 0);
  assert.equal(h.calls.filter(call => call.path.endsWith('/advance')).length, 1);
  assert.equal(h.timers.size, 0); assert.match(h.get('job-status').textContent, /已完成/);
});

test('history excludes old intents and offers no resume for terminal or unresolved PDF outcomes', async () => {
  const h = await harness({ connection: disconnected });
  const states = ['blocked_connection', 'preview_only', 'complete', 'failure', 'pdf_pending', 'running'];
  h.setJobs(states.map((state, index) => job(state, [{ paper_id: 'paper-b', state, retryable: false }], { job_id: 'ze_' + String(index).repeat(64) })));
  await h.get('refresh-jobs').emit('click'); await idle();
  assert.equal(h.get('job-history').children.length, 4);
  const buttons = h.get('job-history').children.flatMap(row => row.children.filter(child => child.textContent === '继续此任务'));
  assert.equal(buttons.length, 1); assert.equal(buttons[0].disabled, true);
  await buttons[0].emit('click'); assert.equal(h.calls.filter(call => call.method === 'POST').length, 0);
  h.setConnection(connected); await h.get('refresh-connection').emit('click');
  assert.equal(h.timers.size, 0); assert.equal(h.calls.filter(call => call.method === 'POST').length, 0);
});

test('connection loss stops current job before further mutation, while job readback is harmless', async () => {
  const h = await harness({ connection: connected, handler(call) {
    if (call.path === '/api/zotero/executions') return result(job());
    if (call.path === '/api/zotero/executions/' + jobId) return result(job('running'));
  } });
  await h.select(); await h.pdf(); await h.save();
  h.setConnection(disconnected); await h.get('refresh-connection').emit('click');
  await h.runTimer(); assert.equal(h.timers.size, 0); assert.equal(h.calls.some(call => call.path.endsWith('/advance')), false);
  assert.match(h.get('job-status').textContent, /任务已暂停/);
});

test('a stale or unknown job response cannot authorize continuing a different execution', async () => {
  const h = await harness({ connection: connected, handler(call) {
    if (call.path === '/api/zotero/executions') return result(job());
    if (call.path === '/api/zotero/executions/' + jobId) return result(job('running', [], { job_id: 'ze_' + 'b'.repeat(64) }));
  } });
  await h.select(); await h.pdf(); await h.save(); await h.runTimer();
  assert.equal(h.calls.some(call => call.path.endsWith('/advance')), false); assert.equal(h.timers.size, 0);
  assert.match(h.get('job-status').textContent, /响应无法确认/);
});

test('actual catalog status and topic enums become Chinese classifications, while venue stays separate', async () => {
  const realShape = { ...catalog, papers: [{ ...catalog.papers[0], publication: 'published_verified',
    publication_type: 'journalArticle', venue: 'Nature Example', topics: ['quantum-error-correction', 'agents'] }] };
  const h = await harness({ catalogs: [realShape] });
  await h.select();
  assert.match(h.get('paper-cards').textContent, /正式发表 · 已核验/);
  assert.match(h.get('paper-cards').textContent, /Nature Example/);
  assert.match(h.get('topic-collections').textContent, /量子纠错 · 1 篇/);
  assert.match(h.get('topic-collections').textContent, /智能体 · 1 篇/);
  assert.doesNotMatch(h.get('paper-cards').textContent, /published_verified|journalArticle|quantum-error-correction/);
});

test('history detail view shows partial metadata success and PDF failure without claiming completion', async () => {
  const h = await harness();
  h.setJobs([job('partial_failure', [{ paper_id: catalog.papers[0].paper_id, state: 'failure', metadata_saved: true,
    classification_saved: true, pdf_verified: false, error_code: 'PDF_READBACK_MISMATCH', uncertain: true }], { pdf_intent: 'open_access_if_available' })]);
  await h.get('refresh-jobs').emit('click'); await idle();
  const view = h.get('job-history').children[0].children.find(child => child.textContent === '查看结果');
  await view.emit('click');
  assert.match(h.get('job-items').textContent, /失败.*元数据已保存.*PDF 未完成.*PDF 回读校验失败.*操作结果仍待核验/);
  assert.equal(h.calls.filter(call => call.method === 'POST').length, 0); assert.equal(h.timers.size, 0);
});

test('explicit retry guidance on a rejected advance is honored before another mutation', async () => {
  let advances = 0;
  const h = await harness({ connection: connected, handler(call) {
    if (call.path === '/api/zotero/executions') return result(job());
    if (call.path === '/api/zotero/executions/' + jobId) return result(job('running'));
    if (call.path.endsWith('/advance')) { advances++; return advances === 1
      ? result({ error: 'backoff_required', retry_after_ms: 30_000 }, 409) : result(job('complete')); }
  } });
  await h.select(); await h.pdf(); await h.save(); await h.runTimer();
  assert.equal(advances, 1); assert.equal([...h.timers.values()][0].delay, 30_000);
  await h.runTimer(); assert.equal(advances, 2); assert.equal(h.timers.size, 0);
});

const providerEntry = { ...disconnected, credential_entry_mode: 'provider-managed-d1', credential_entry_enabled: true, credential_storage: 'not_stored' };
const providerConnected = { ...connected, credential_entry_mode: 'provider-managed-d1', credential_storage: 'provider_managed_d1', disconnect_enabled: true, verification_enabled: true };
test('provider UI discloses administrator/backups risk, requires numeric ID, clears key and never imports on connect', async () => {
  let h;
  h = await harness({ connection: providerEntry, handler(call) {
    if (call.path === '/api/zotero/connect') {
      assert.deepEqual(call.body, { action: 'connect_personal_library', library_id: '123456', api_key: 'SYNTHETICPROVIDERKEY123456' });
      assert.equal(h.get('api-key').value, '');
      h.setConnection({ ...providerConnected, live_ready: false }); return result({ ready: true, live_ready: false }, 201);
    }
  } });
  assert.match(h.get('setup-note').textContent, /数据库管理员可读取/); assert.match(h.get('setup-note').textContent, /Cloudflare/);
  assert.match(h.html, /备份可能仍保留密钥/); assert.equal(h.get('library-id').required, true); assert.equal(h.get('library-id').hidden, false);
  assert.match(h.get('credential-submit').textContent, /核验并连接/);
  h.get('library-id').value = '123456'; h.get('api-key').value = 'SYNTHETICPROVIDERKEY123456'; await h.get('credential-form').emit('submit');
  assert.equal(h.get('api-key').value, ''); assert.equal(h.get('connection-label').textContent, '已连接');
  assert.match(h.get('connection-note').textContent, /执行尚未开放/); assert.equal(h.get('save-to-zotero').disabled, true);
  assert.deepEqual(h.calls.filter(c => c.method === 'POST').map(c => c.path), ['/api/zotero/connect']); assert.equal(h.timers.size, 0);
});
test('provider connected with no catalog explains why Save is unavailable', async () => {
  const h = await harness({ connection: providerConnected, catalogs: [] });
  assert.equal(h.get('connection-label').textContent, '已连接'); assert.equal(h.get('save-to-zotero').disabled, true);
  assert.match(h.get('save-help').textContent, /尚无可用的已核验目录/); assert.equal(h.calls.filter(c => c.method === 'POST').length, 0);
});
test('provider reverify without execution remains truthfully connected, no import scheduled', async () => {
  const h = await harness({ connection: { ...providerConnected, live_ready: false }, handler: c => c.path === '/api/zotero/verify-connection' ? result({ ready: true, live_ready: false }) : undefined });
  await h.get('verify-connection').emit('click'); assert.match(h.get('credential-result').textContent, /连接已核验/); assert.match(h.get('credential-result').textContent, /执行尚未开放/); assert.equal(h.timers.size, 0);
});
test('explicit disconnect sends only its action, stops saved-job loop and discloses Zotero revocation', async () => {
  let h;
  h = await harness({ connection: providerConnected, handler(call) {
    if (call.path === '/api/zotero/executions') return result(job('queued'));
    if (call.path === '/api/zotero/disconnect') { assert.deepEqual(call.body, { action: 'disconnect' }); h.setConnection(providerEntry); return result({ ready: false, live_ready: false }); }
  } });
  await h.select(); await h.pdf(); await h.save(); assert.equal(h.timers.size, 1);
  await h.get('disconnect-connection').emit('click'); assert.equal(h.timers.size, 0); assert.equal(h.get('connection-label').textContent, '未连接');
  assert.match(h.get('credential-result').textContent, /已删除活动凭据/); assert.match(h.get('credential-result').textContent, /备份/); assert.match(h.get('credential-result').textContent, /撤销/);
  assert.equal(h.calls.filter(c => c.path.endsWith('/advance')).length, 0);
});
