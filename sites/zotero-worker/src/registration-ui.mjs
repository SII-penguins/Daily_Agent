/** Owner-authenticated private registration page; host authorizes before rendering.
 * Directory contents never establish trust: only server-pinned filenames/hashes do.
 */
export function registrationClient(platform = globalThis) {
  const { document, fetch, crypto, AbortController } = platform;
  const ids = ['registration-status', 'registration-files', 'registration-counts', 'registration-form',
    'registration-submit', 'registration-reset', 'registration-refresh', 'registration-details'];
  const nodes = Object.fromEntries(ids.map(id => [id, document.getElementById(id)]));
  const state = { manifest: null, files: null, sources: null, epoch: 0, busy: false, checking: false, loading: false, alive: true, requests: new Set() };
  const SHA = /^[a-f0-9]{64}$/;
  const limits = { report: 1024 * 1024, html: 4 * 1024 * 1024, catalog: 1024 * 1024, pdf: 16 * 1024 * 1024, totalSources: 4 * 1024 * 1024, totalPdf: 24 * 1024 * 1024 };
  const text = value => typeof value === 'string' ? value.slice(0, 160) : '';
  const create = (tag, value) => { const element = document.createElement(tag); element.textContent = value; return element; };
  const canRegister = () => state.manifest?.enabled === true && state.manifest?.catalog_enabled !== true &&
    (state.manifest?.registered !== true || state.manifest?.enable_after_registration === true);
  const update = () => {
    nodes['registration-files'].disabled = state.busy || state.loading || !canRegister();
    nodes['registration-submit'].disabled = state.busy || state.checking || !canRegister() || !state.sources || !state.files;
    nodes['registration-reset'].disabled = state.busy || (!state.files && !state.checking);
    nodes['registration-refresh'].disabled = state.busy || state.checking || state.loading;
  };
  const wipeSources = sources => { if (sources) Object.values(sources).forEach(bytes => bytes.fill(0)); };
  function reset(message) {
    state.epoch++; wipeSources(state.sources); state.sources = null; state.files = null; state.checking = false;
    nodes['registration-files'].value = ''; nodes['registration-counts'].textContent = '尚未选择文件夹';
    if (message) nodes['registration-status'].textContent = message;
    update();
  }
  async function request(path, { body, contentType = 'application/json' } = {}) {
    const controller = new AbortController(); state.requests.add(controller);
    try {
      const response = await fetch(path, { method: body === undefined ? 'GET' : 'POST', credentials: 'same-origin',
        cache: 'no-store', redirect: 'error', signal: controller.signal,
        headers: body === undefined ? { Accept: 'application/json' }
          : { Accept: 'application/json', 'Content-Type': contentType, 'X-Zotero-Action': '1' },
        ...(body === undefined ? {} : { body }) });
      let data;
      try { data = await response.json(); } catch { throw new Error('invalid_response'); }
      return { ok: response.ok, data };
    } finally { state.requests.delete(controller); }
  }
  function validManifest(data) {
    if (!data || data.enabled !== true || !Number.isSafeInteger(data.catalog_version) || data.catalog_version < 1) return false;
    for (const kind of Object.keys(limits)) if (!Number.isSafeInteger(data.limits?.[kind]) || data.limits[kind] < 1 || data.limits[kind] > limits[kind]) return false;
    for (const kind of ['report', 'html', 'catalog']) {
      const filename = data.filenames?.[kind];
      if (typeof filename !== 'string' || !filename || filename.length > 160 || /[/\\\x00-\x1f\x7f]/.test(filename) || ['.', '..'].includes(filename) || !SHA.test(data.source_pins?.[kind + '_sha256'])) return false;
    }
    if (Object.keys(data.filenames).length !== 3 || new Set(Object.values(data.filenames)).size !== 3 || !Array.isArray(data.pdfs) || data.pdfs.length > 100) return false;
    const ids = new Set(); let total = 0;
    for (const pdf of data.pdfs) {
      if (!SHA.test(pdf?.sha256) || ids.has(pdf.sha256) || !Number.isSafeInteger(pdf.size) || pdf.size < 8 || pdf.size > data.limits.pdf) return false;
      ids.add(pdf.sha256); total += pdf.size;
    }
    return total <= data.limits.totalPdf && new Set([...Object.values(data.filenames), ...data.pdfs.map(pdf => pdf.sha256)]).size === data.pdfs.length + 3;
  }
  async function loadManifest() {
    if (state.busy || state.checking || state.loading) return;
    reset(); const epoch = state.epoch; state.loading = true; state.manifest = null; update();
    nodes['registration-status'].textContent = '正在读取服务器登记配置…';
    try {
      const result = await request('/api/zotero/catalog-registration');
      if (!state.alive || epoch !== state.epoch) return;
      if (!result.ok || result.data?.enabled !== true) {
        nodes['registration-status'].textContent = '登记尚未开放。需要已批准的私有存储和服务器可信清单配置；此页不会创建授权或连接。'; return;
      }
      if (!validManifest(result.data)) throw new Error('invalid_manifest');
      state.manifest = result.data;
      nodes['registration-details'].replaceChildren();
      for (const kind of ['report', 'html', 'catalog']) nodes['registration-details'].append(create('li', text(result.data.filenames[kind])));
      const pdfBytes = result.data.pdfs.reduce((sum, pdf) => sum + pdf.size, 0);
      nodes['registration-details'].append(create('li', result.data.pdfs.length + ' 份固定 SHA-256 文件名的 PDF · 合计 ' + pdfBytes + ' 字节'));
      nodes['registration-status'].textContent = result.data.registered === true
        ? result.data.catalog_enabled === true ? '服务器已确认目录已登记并启用。可返回论文管理页查看；此操作没有连接 Zotero 或导入论文。'
          : '服务器已登记此目录，但尚未启用。只有服务器另行批准启用后，才能在论文管理页选择。'
        : '配置已就绪。请选择包含指定报告、目录及 PDF 的文件夹，先在本机校验，再明确点击登记。';
    } catch {
      if (state.alive && epoch === state.epoch) nodes['registration-status'].textContent = '无法确认登记配置，请刷新状态。没有上传任何文件。';
    } finally { if (state.alive && epoch === state.epoch) { state.loading = false; update(); } }
  }
  async function readChecked(file, sha256, maxBytes, expectedSize) {
    if (!file || !Number.isSafeInteger(file.size) || file.size < 1 || file.size > maxBytes || (expectedSize !== undefined && file.size !== expectedSize)) throw new Error('file_size_mismatch');
    const bytes = new Uint8Array(await file.arrayBuffer());
    try {
      if (bytes.byteLength !== file.size) throw new Error('file_changed');
      const digest = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)), value => value.toString(16).padStart(2, '0')).join('');
      if (digest !== sha256) throw new Error('file_hash_mismatch');
      return bytes;
    } catch (error) { bytes.fill(0); throw error; }
  }
  async function chooseDirectory() {
    if (state.busy || state.loading || !canRegister()) return;
    const manifest = state.manifest;
    const chosen = Array.from(nodes['registration-files'].files || []);
    const epoch = ++state.epoch; wipeSources(state.sources); state.sources = null; state.files = null;
    state.checking = true; update(); nodes['registration-counts'].textContent = '正在本机核对文件名、字节数与 SHA-256…';
    let sources = {};
    try {
      if (!chosen.length || chosen.length > 10_000) throw new Error('invalid_directory');
      const expectedNames = new Set([...Object.values(manifest.filenames), ...manifest.pdfs.map(pdf => pdf.sha256)]);
      const matches = new Map();
      for (const file of chosen) if (expectedNames.has(file.name)) {
        if (matches.has(file.name)) throw new Error('ambiguous_filename'); matches.set(file.name, file);
      }
      if (matches.size !== expectedNames.size) throw new Error('missing_files');
      if (['report', 'html', 'catalog'].reduce((sum, kind) => sum + matches.get(manifest.filenames[kind]).size, 0) > manifest.limits.totalSources) throw new Error('source_bundle_too_large');
      for (const kind of ['report', 'html', 'catalog']) {
        sources[kind] = await readChecked(matches.get(manifest.filenames[kind]), manifest.source_pins[kind + '_sha256'], manifest.limits[kind]);
        if (!state.alive || epoch !== state.epoch) { wipeSources(sources); return; }
      }
      for (const pdf of manifest.pdfs) {
        const bytes = await readChecked(matches.get(pdf.sha256), pdf.sha256, manifest.limits.pdf, pdf.size);
        const signature = bytes[0] === 37 && bytes[1] === 80 && bytes[2] === 68 && bytes[3] === 70 && bytes[4] === 45;
        bytes.fill(0); if (!signature) throw new Error('invalid_pdf');
        if (!state.alive || epoch !== state.epoch) { wipeSources(sources); return; }
      }
      state.files = matches; state.sources = sources; sources = null;
      const sourceBytes = Object.values(state.sources).reduce((sum, bytes) => sum + bytes.length, 0);
      const pdfBytes = manifest.pdfs.reduce((sum, pdf) => sum + pdf.size, 0);
      nodes['registration-counts'].textContent = '已匹配并核验 3 份来源文件（' + sourceBytes + ' 字节）及 ' + manifest.pdfs.length + ' 份 PDF（' + pdfBytes + ' 字节）。其他 ' + (chosen.length - matches.size) + ' 个文件不会上传。';
      nodes['registration-status'].textContent = '本机校验通过，尚未上传。点击「登记私有目录」后才会传到此站点的私有存储。';
    } catch {
      wipeSources(sources);
      if (state.alive && epoch === state.epoch) {
        state.files = null; state.sources = null;
        nodes['registration-counts'].textContent = '未通过校验：文件缺失、同名冲突、大小或 SHA-256 与服务器可信清单不符。';
        nodes['registration-status'].textContent = '没有上传文件。请使用原始已核验文件夹重新选择。';
      }
    } finally { if (state.alive && epoch === state.epoch) { state.checking = false; update(); } }
  }
  const fingerprint = data => JSON.stringify([data.report_id, data.catalog_id, data.catalog_version,
    data.enable_after_registration, ['report', 'html', 'catalog'].map(kind => [data.filenames[kind], data.source_pins[kind + '_sha256']]),
    data.pdfs.map(pdf => [pdf.sha256, pdf.size]), Object.keys(limits).map(kind => [kind, data.limits[kind]])]);
  async function recheckManifest(requireStaged) {
    const result = await request('/api/zotero/catalog-registration');
    if (!result.ok || !validManifest(result.data) || fingerprint(result.data) !== fingerprint(state.manifest) ||
      (requireStaged && !result.data.pdfs.every(pdf => pdf.staged === true))) throw new Error('manifest_changed_or_unstaged');
  }
  function base64(bytes) {
    // Chunked conversion avoids spreading multi-megabyte arrays on the stack.
    let binary = '';
    for (let offset = 0; offset < bytes.length; offset += 0x8000) binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
    return platform.btoa(binary);
  }
  nodes['registration-form'].addEventListener('submit', async event => {
    event.preventDefault();
    if (nodes['registration-submit'].disabled || state.busy || state.checking || !canRegister() || !state.sources || !state.files) return;
    const epoch = state.epoch; state.busy = true; update();
    let payload = '';
    try {
      await recheckManifest(false);
      if (!state.alive || epoch !== state.epoch) return;
      let done = 0;
      for (const pdf of state.manifest.pdfs) {
        if (!state.alive || epoch !== state.epoch) return;
        nodes['registration-status'].textContent = '正在私有暂存 PDF：' + done + ' / ' + state.manifest.pdfs.length + '。尚未登记目录。';
        const bytes = await readChecked(state.files.get(pdf.sha256), pdf.sha256, state.manifest.limits.pdf, pdf.size);
        try {
          if (!state.alive || epoch !== state.epoch) return;
          const result = await request('/api/zotero/catalog-registration/pdfs/' + pdf.sha256, { body: bytes, contentType: 'application/pdf' });
          if (!result.ok || result.data?.staged !== true || result.data.sha256 !== pdf.sha256 || result.data.size !== pdf.size) throw new Error('pdf_stage_unconfirmed');
        } finally { bytes.fill(0); }
        done++;
      }
      if (!state.alive || epoch !== state.epoch) return;
      await recheckManifest(true);
      if (!state.alive || epoch !== state.epoch) return;
      nodes['registration-status'].textContent = 'PDF 暂存完成，正在请求服务器核验并登记目录…';
      payload = JSON.stringify({ action: 'register_catalog', report_b64: base64(state.sources.report),
        html_b64: base64(state.sources.html), catalog_b64: base64(state.sources.catalog) });
      const result = await request('/api/zotero/catalog-registration/finalize', { body: payload }); payload = '';
      if (!state.alive || epoch !== state.epoch) return;
      if (!result.ok || result.data?.registered !== true || typeof result.data?.enabled !== 'boolean' ||
        result.data.report_id !== state.manifest.report_id || result.data.catalog_id !== state.manifest.catalog_id ||
        result.data.catalog_version !== state.manifest.catalog_version) throw new Error('registration_unconfirmed');
      state.manifest = { ...state.manifest, registered: true, catalog_enabled: result.data.enabled };
      reset(result.data.enabled ? '服务器已确认：私有论文目录已就绪。请返回论文管理页查看；Zotero 连接与保存仍需你单独操作。'
        : '服务器已确认目录登记，但尚未启用。没有连接 Zotero 或导入论文，请等待单独的启用批准。');
    } catch {
      if (state.alive && epoch === state.epoch) {
        reset('登记结果尚未确认。部分 PDF 可能已私有暂存；未自动重试。请刷新服务器状态后再操作。');
      }
    } finally { payload = ''; state.busy = false; if (state.alive) update(); }
  });
  nodes['registration-files'].addEventListener('change', chooseDirectory);
  nodes['registration-reset'].addEventListener('click', () => { if (!state.busy) reset('已清空本页文件选择，没有提交登记。'); });
  nodes['registration-refresh'].addEventListener('click', loadManifest);
  const dispose = () => {
    state.alive = false; state.epoch++; state.requests.forEach(controller => controller.abort()); state.requests.clear();
    wipeSources(state.sources); state.sources = null; state.files = null; nodes['registration-files'].value = '';
    nodes['registration-submit'].disabled = true; nodes['registration-files'].disabled = true;
  };
  platform.addEventListener('pagehide', dispose);
  platform.addEventListener('pageshow', event => { if (event.persisted) platform.location.reload(); });
  const ready = loadManifest();
  return { ready, dispose };
}

export function renderRegistrationPage() {
  const nonce = btoa(String.fromCharCode(...crypto.getRandomValues(new Uint8Array(18))));
  const html = `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>私有目录登记 · Daily Agent</title>
<style nonce="${nonce}">:root{color-scheme:light dark;--bg:#f6f4ed;--card:#fffef9;--ink:#172c29;--muted:#5d6c66;--line:#ccd3c8;--accent:#14634c;--soft:#e9eee4;--warm:#f2ecd9;--focus:#925025}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.8 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}.page{max-width:960px;margin:auto;padding:0 32px}header{display:flex;justify-content:space-between;gap:18px;align-items:center;padding:26px 0 20px;border-bottom:1px solid var(--ink)}a{color:var(--accent);text-underline-offset:5px}.brand{font:700 30px Georgia,serif;color:var(--ink);text-decoration:none}.eyebrow,header span{font-size:12px;letter-spacing:1.5px;color:var(--muted)}h1{font-size:clamp(28px,4vw,38px);line-height:1.4;margin:10px 0}h2{font-size:18px;margin:0 0 12px}.hero{padding:32px 0 24px}.hero p{margin:8px 0}.note{color:var(--muted);font-size:14px}.notice{background:var(--warm);border-left:3px solid var(--focus);padding:14px 18px;font-size:14px}.panel{background:var(--card);border:1px solid var(--line);padding:24px;border-radius:4px;margin:24px 0}.panel p,.panel ul{font-size:14px}label{display:block;font-weight:600;margin:12px 0}input{font:inherit;display:block;width:100%;max-width:100%;margin:12px 0;min-height:44px;color:var(--ink)}button{font:inherit;border:1px solid var(--accent);border-radius:4px;padding:10px 16px;min-height:48px;background:var(--accent);color:var(--card);cursor:pointer;margin:12px 10px 0 0}.secondary{background:transparent;color:var(--accent);border-color:var(--line)}button:disabled,input:disabled{opacity:.7;cursor:not-allowed}button:disabled{background:var(--soft);color:var(--muted);border-color:var(--line)}:focus-visible{outline:3px solid var(--focus);outline-offset:4px}#registration-details{overflow-wrap:anywhere;padding-left:20px}footer{border-top:1px solid var(--line);padding:18px 0 26px;font-size:12px;color:var(--muted)}@media(prefers-color-scheme:dark){:root{--bg:#142320;--card:#1c2e29;--ink:#e6ece0;--muted:#adbdb3;--line:#41564b;--accent:#a7d3ae;--soft:#263c32;--warm:#36382b;--focus:#eab98a}}@media(max-width:600px){.page{padding:0 20px}.panel{padding:18px}header span{display:none}.brand{font-size:26px}button{width:100%;margin-right:0}}</style></head><body><div class="page"><header><a class="brand" href="/">Daily Agent</a><span>PRIVATE CATALOG / OWNER ONLY</span></header><main><section class="hero"><p class="eyebrow">REVIEWED FILES, EXPLICIT REGISTRATION</p><h1>登记已核验的私有目录</h1><p class="note">将已审阅报告、目录与对应 PDF 登记到此站点，供论文管理页选择。</p></section><div class="notice">选择文件夹只做本机核验。只有点击登记才会上传到此站点的私有存储；这不会连接 Zotero 或保存任何论文到文库。</div><noscript><p class="notice">JavaScript 未启用，文件选择和登记保持禁用。</p></noscript><section class="panel"><h2>服务器指定的来源文件</h2><ul id="registration-details"><li>正在读取可信清单…</li></ul><p class="note">文件名、字节数与 SHA-256 必须符合服务器固定清单。其他文件不会上传；内容不会显示或保存到浏览器存储。</p><form id="registration-form" method="post" action="/api/zotero/catalog-registration/finalize"><label for="registration-files">选择包含来源文件与 PDF 的文件夹</label><input id="registration-files" type="file" webkitdirectory multiple disabled aria-describedby="registration-counts"><p id="registration-counts" role="status" aria-live="polite">尚未选择文件夹</p><button id="registration-submit" type="submit" disabled>登记私有目录</button><button id="registration-reset" type="button" class="secondary" disabled>清空选择</button></form></section><section class="panel"><h2>登记状态</h2><p id="registration-status" role="status" aria-live="polite">等待服务器配置确认。</p><button id="registration-refresh" type="button" class="secondary" disabled>刷新服务器状态</button><p class="note">若离开或网络中断，部分文件可能已暂存。重新打开后先刷新状态，再由你决定是否重新登记。</p></section></main><footer><a href="/zotero">返回论文管理</a> · 私有文件登记，需独立授权</footer></div><script nonce="${nonce}">(${registrationClient.toString()})();</script></body></html>`;
  return new Response(html, { headers: { 'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store, private',
    Pragma: 'no-cache', 'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer', 'X-Frame-Options': 'DENY',
    'Cross-Origin-Resource-Policy': 'same-origin', 'Permissions-Policy': 'camera=(), microphone=(), geolocation=()',
    'Content-Security-Policy': `default-src 'none'; script-src 'nonce-${nonce}'; style-src 'nonce-${nonce}'; connect-src 'self'; form-action 'none'; base-uri 'none'; frame-ancestors 'none'`,
  } });
}
