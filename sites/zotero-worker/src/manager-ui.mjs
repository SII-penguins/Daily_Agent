/** Owner-only manager renderer. The host MUST authorize the owner before calling it.
 * All catalog metadata and connection capability flags come from owner-scoped APIs.
 * No persistent browser state or previous import intent is resumed by this page.
 */
export function managerClient(initial, platform = globalThis) {
  const { document, fetch, setTimeout, clearTimeout, AbortController } = platform;
  const byId = id => document.getElementById(id);
  const nodes = Object.fromEntries(['connection-label', 'connection-english', 'connection-note',
    'credential-fields', 'credential-form', 'api-key', 'library-id', 'library-id-label', 'credential-submit', 'setup-note', 'disconnect-connection', 'credential-result', 'verify-connection',
    'refresh-connection', 'catalog-select', 'catalog-status', 'paper-cards', 'date-filter',
    'topic-filter', 'publication-filter', 'search-papers', 'selected-count', 'selection-status',
    'date-collections', 'topic-collections', 'publication-collections', 'selected-papers',
    'reset-selection', 'save-form', 'save-to-zotero', 'save-help', 'pdf-metadata', 'pdf-open-access',
    'job-status', 'job-items', 'job-refresh', 'job-history', 'history-status', 'refresh-jobs'].map(id => [id, byId(id)]));
  const text = (value, limit = 1500) => typeof value === 'string' ? value.slice(0, limit) : '';
  const idValid = value => typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(value);
  const jobIdValid = value => typeof value === 'string' && /^ze_[a-f0-9]{64}$/.test(value);
  const state = { connection: initial, catalog: null, catalogs: [], selected: new Set(),
    pdfIntent: '', submittedSelection: false, saving: false, connectionBusy: false, activeJob: null, jobEpoch: 0,
    catalogEpoch: 0, timer: null, alive: true, requests: new Set(), jobBusy: false, jobs: [], jobsBusy: false };
  const create = (tag, content, className) => {
    const element = document.createElement(tag);
    if (content !== undefined) element.textContent = content;
    if (className) element.className = className;
    return element;
  };
  const connected = () => state.connection?.ready === true && state.connection?.live_ready === true;
  const staged = () => ['provider_managed_d1', 'encrypted_pending_verification', 'encrypted_staged', 'staged', 'encrypted_verified']
    .includes(state.connection?.credential_storage);
  const canVerify = () => staged() && state.connection?.verification_enabled === true;
  const selectedPapers = () => (state.catalog?.papers || []).filter(paper => state.selected.has(paper.paper_id));
  const busy = () => state.saving || state.activeJob !== null;
  const tags = value => [...new Set((Array.isArray(value) ? value : typeof value === 'string' ? [value] : [])
    .filter(item => typeof item === 'string' && item.trim()).map(item => text(item.trim(), 160)))];
  const dateOf = paper => text(paper.report_date || state.catalog?.report_date || paper.date || paper.publication_date, 40) || '日期未提供';
  const topicLabels = { 'quantum-error-correction': '量子纠错', 'neural-quantum-states': '神经网络量子态',
    'quantum-optimization': '量子优化', 'quantum-machine-learning': '量子机器学习', 'ai-for-science': 'AI for Science',
    agents: '智能体', 'world-models': '世界模型', 'embodied-ai': '具身智能', 'other-reviewed': '其他已审阅研究' };
  const publicationLabels = { published_verified: '正式发表 · 已核验', preprint: '预印本', unverified: '发表状态未核验',
    journalArticle: '期刊论文', conferencePaper: '会议论文', document: '研究文档' };
  const topicsOf = paper => tags(paper.topics || paper.topic || state.catalog?.topics).map(topic => Object.hasOwn(topicLabels, topic) ? topicLabels[topic] : topic);
  const publicationOf = paper => {
    const value = paper.publication_status || (Object.hasOwn(publicationLabels, paper.publication) ? paper.publication : paper.publication_type);
    return (Object.hasOwn(publicationLabels, value) ? publicationLabels[value] : text(value, 160)) || '发表分类未提供';
  };
  const venueOf = paper => text(paper.venue || paper.publication_title ||
    (Object.hasOwn(publicationLabels, paper.publication) ? '' : paper.publication), 300);
  const authorsOf = paper => Array.isArray(paper.authors) ? paper.authors.map(author => typeof author === 'string'
    ? author : [text(author?.firstName, 80), text(author?.lastName, 80)].filter(Boolean).join(' ')).filter(Boolean).join(' · ') : text(paper.authors);
  async function request(path, body) {
    const controller = new AbortController();
    state.requests.add(controller);
    try {
      const response = await fetch(path, { method: body === undefined ? 'GET' : 'POST',
        credentials: 'same-origin', cache: 'no-store', redirect: 'error', signal: controller.signal,
        headers: body === undefined ? { Accept: 'application/json' }
          : { Accept: 'application/json', 'Content-Type': 'application/json', 'X-Zotero-Action': '1' },
        ...(body === undefined ? {} : { body: typeof body === 'string' ? body : JSON.stringify(body) }) });
      let data;
      try { data = await response.json(); } catch { throw new Error('invalid_response'); }
      return { ok: response.ok, status: response.status, data };
    } finally { state.requests.delete(controller); }
  }
  function renderConnection() {
    const ready = state.connection?.ready === true;
    const managed = state.connection?.credential_entry_mode === 'provider-managed-d1';
    nodes['library-id'].hidden = !managed; nodes['library-id-label'].hidden = !managed; nodes['library-id'].required = managed;
    nodes['credential-submit'].textContent = managed ? '核验并连接个人文库' : '加密并暂存凭据';
    nodes['setup-note'].textContent = managed
      ? '输入你在 Zotero 设置中确认的个人数字文库 ID 与 API 密钥，仅授权个人文库读取、写入及文件，不授权笔记或任何群组。提交时先向 Zotero 核验，成功后将密钥保存在私有 D1；Cloudflare 提供静态加密，但应用和数据库管理员可读取可用密钥。这不是应用层独立加密。请勿把密钥粘贴到聊天、日报或链接中。'
      : '需先取得一次性安全授权、确认个人文库，并完成服务器加密配置。密钥只在此密码输入框录入，不应粘贴到聊天、日报或链接中。';
    nodes['connection-label'].textContent = ready ? '已连接' : '未连接';
    nodes['connection-english'].textContent = ready ? 'CONNECTED' : 'NOT CONNECTED';
    nodes['connection-note'].textContent = ready
      ? connected() ? '个人文库连接及执行权限已核验。每次导入仍需你明确保存所选论文。'
        : state.connection?.catalog_preparation_unavailable === true ? '个人文库连接信息已保留。目录准备与保存暂时不可用，正在修复；无需重新输入密钥。' : state.connection?.catalog_ready === false ? '个人文库已连接。请先点击下方「准备私有论文目录」完成目录准备，再勾选论文保存。' : '个人文库连接已核验；导入执行尚未开放。还需核验目录与执行配置，当前不能保存论文。'
      : state.connection?.credential_storage === 'provider_managed_d1' ? '凭据保存在私有 D1，连接需要重新核验；当前不能保存论文。'
      : staged() ? '凭据已加密暂存，仍待连接核验；暂存不代表连接成功。'
        : '尚未连接 Zotero。你可以浏览真实目录并预览选择；保存暂不可用。';
    nodes['credential-fields'].disabled = state.connectionBusy || state.connection?.credential_entry_enabled !== true;
    nodes['verify-connection'].disabled = state.connectionBusy || busy() || !canVerify();
    nodes['refresh-connection'].disabled = state.connectionBusy;
    nodes['disconnect-connection'].disabled = state.connectionBusy || state.connection?.disconnect_enabled !== true;
    renderSelection();
  }
  function groupList(node, values, empty) {
    const counts = new Map();
    values.forEach(value => counts.set(value, (counts.get(value) || 0) + 1));
    node.replaceChildren();
    if (!counts.size) node.append(create('li', empty, 'muted'));
    else for (const [label, count] of counts) node.append(create('li', label + ' · ' + count + ' 篇'));
  }
  function renderSelection() {
    const papers = selectedPapers();
    nodes['selected-count'].textContent = String(papers.length);
    nodes['selection-status'].textContent = papers.length ? state.submittedSelection
      ? '此选择已提交，共 ' + papers.length + ' 篇；实际结果以下方逐项状态为准。'
      : state.saving ? '正在提交所选 ' + papers.length + ' 篇…'
        : '已选 ' + papers.length + ' 篇，尚未提交本次选择。筛选不会清除已选论文。' : '尚未选择论文。勾选只更新本页预览。';
    nodes['reset-selection'].disabled = busy() || (!papers.length && !state.pdfIntent);
    groupList(nodes['date-collections'], papers.map(dateOf), '选择后显示日报日期');
    groupList(nodes['topic-collections'], papers.flatMap(paper => topicsOf(paper).length ? topicsOf(paper) : ['主题未提供']), '选择后显示研究主题');
    groupList(nodes['publication-collections'], papers.map(publicationOf), '选择后显示发表分类');
    nodes['selected-papers'].replaceChildren();
    papers.forEach(paper => nodes['selected-papers'].append(create('li', text(paper.title) || paper.paper_id)));
    nodes['pdf-metadata'].disabled = busy() || !state.catalog;
    nodes['pdf-open-access'].disabled = busy() || !state.catalog;
    nodes['catalog-select'].disabled = busy() || !state.catalogs.length;
    nodes['save-to-zotero'].disabled = !connected() || state.connectionBusy || busy() || !papers.length || papers.length > 100 || !state.pdfIntent;
    nodes['save-help'].textContent = state.connection?.ready === true && !connected() ? '个人文库已连接，导入执行尚未开放；目录与执行配置核验完成前无法保存。'
      : !connected() ? '未连接，保存不可用。勾选本身不会触发导入。'
      : !state.catalog ? '尚无可用的已核验目录，保存不可用。'
      : state.connectionBusy ? '正在更新连接，请稍候。'
      : busy() ? '本次保存正在处理，重复提交已锁定。'
        : papers.length > 100 ? '每次最多保存 100 篇，请减少选择。'
          : !papers.length ? '先选择论文，再明确选择 PDF 处理方式。'
            : !state.pdfIntent ? '请明确选择：仅元数据，或同时尝试开放获取 PDF。'
              : '点击后仅提交当前所选论文，并继续此任务的有限步骤；不会执行旧意向。';
    renderHistory();
  }
  function fillFilter(node, values, label) {
    node.replaceChildren();
    const all = create('option', label); all.value = ''; node.append(all);
    [...new Set(values)].sort().forEach(value => { const option = create('option', value); option.value = value; node.append(option); });
    node.value = ''; node.disabled = false;
  }
  function renderPapers() {
    nodes['paper-cards'].replaceChildren();
    const query = nodes['search-papers'].value.trim().toLocaleLowerCase();
    const papers = (state.catalog?.papers || []).filter(paper =>
      (!nodes['date-filter'].value || dateOf(paper) === nodes['date-filter'].value) &&
      (!nodes['topic-filter'].value || topicsOf(paper).includes(nodes['topic-filter'].value)) &&
      (!nodes['publication-filter'].value || publicationOf(paper) === nodes['publication-filter'].value) &&
      (!query || [text(paper.title), authorsOf(paper), text(paper.doi)].join(' ').toLocaleLowerCase().includes(query)));
    nodes['catalog-status'].textContent = state.catalog ? papers.length + ' / ' + state.catalog.papers.length + ' 篇 · 默认不选' : '请先选择可用目录';
    if (!papers.length) nodes['paper-cards'].append(create('p', state.catalog?.papers.length ? '没有符合筛选条件的论文。已选论文仍保留在右侧。' : '此目录暂无可选论文。', 'empty-catalog'));
    for (const paper of papers) {
      const card = create('article', undefined, 'paper-card' + (state.selected.has(paper.paper_id) ? ' is-selected' : ''));
      card.append(create('p', dateOf(paper) + ' · ' + publicationOf(paper), 'paper-meta'));
      const label = create('label', undefined, 'paper-selection');
      const checkbox = create('input'); checkbox.type = 'checkbox'; checkbox.checked = state.selected.has(paper.paper_id);
      checkbox.disabled = busy(); checkbox.className = 'paper-checkbox';
      const title = create('span', text(paper.title) || paper.paper_id, 'paper-title');
      label.append(checkbox, title); card.append(label);
      checkbox.addEventListener('change', () => {
        if (busy()) { checkbox.checked = state.selected.has(paper.paper_id); return; }
        state.submittedSelection = false;
        if (checkbox.checked) state.selected.add(paper.paper_id); else state.selected.delete(paper.paper_id);
        card.className = 'paper-card' + (checkbox.checked ? ' is-selected' : ''); renderSelection();
      });
      const publication = [authorsOf(paper), venueOf(paper)].filter(Boolean).join(' · ');
      if (publication) card.append(create('p', publication, 'publication'));
      if (paper.abstract || paper.summary) card.append(create('p', text(paper.abstract || paper.summary, 600), 'paper-summary'));
      const bottom = create('div', undefined, 'paper-bottom');
      bottom.append(create('span', topicsOf(paper).join(' / ') || '主题未提供', 'topic-chip'));
      bottom.append(create('span', paper.pdf_available === true ? '开放 PDF 候选 · 保存时再次核验' : 'PDF 可用性待核验'));
      card.append(bottom); nodes['paper-cards'].append(card);
    }
  }
  function resetSelection() {
    if (busy()) return;
    state.selected.clear(); state.pdfIntent = ''; state.submittedSelection = false;
    nodes['pdf-metadata'].checked = false; nodes['pdf-open-access'].checked = false;
    renderPapers(); renderSelection();
  }
  async function loadCatalog(index) {
    if (busy()) return;
    const epoch = ++state.catalogEpoch;
    state.catalog = null; resetSelection();
    const catalog = state.catalogs[Number(index)];
    nodes['catalog-status'].textContent = '正在读取目录…';
    for (const id of ['date-filter', 'topic-filter', 'publication-filter', 'search-papers']) nodes[id].disabled = true;
    if (!catalog) { nodes['catalog-status'].textContent = '没有可用的服务器目录'; return; }
    try {
      const result = await request('/api/zotero/reports/' + encodeURIComponent(catalog.report_id) + '/catalogs/' + encodeURIComponent(catalog.catalog_id));
      if (!state.alive || epoch !== state.catalogEpoch) return;
      const data = result.data?.catalog || result.data;
      if (!result.ok || data.report_id !== catalog.report_id || data.catalog_id !== catalog.catalog_id ||
        !Number.isSafeInteger(data.catalog_version) || data.catalog_version < 1 || !Array.isArray(data.papers)) throw new Error('catalog_unavailable');
      const ids = new Set();
      const papers = data.papers.filter(paper => paper && typeof paper.paper_id === 'string' && paper.paper_id.length <= 512 && !ids.has(paper.paper_id) && ids.add(paper.paper_id));
      state.catalog = { ...catalog, ...data, papers };
      fillFilter(nodes['date-filter'], papers.map(dateOf), '全部日报日期');
      fillFilter(nodes['topic-filter'], papers.flatMap(topicsOf), '全部研究主题');
      fillFilter(nodes['publication-filter'], papers.map(publicationOf), '全部发表分类');
      nodes['search-papers'].disabled = false; nodes['search-papers'].value = '';
      renderPapers(); renderSelection();
    } catch {
      if (!state.alive || epoch !== state.catalogEpoch) return;
      nodes['catalog-status'].textContent = '目录暂时无法读取，请重新选择或刷新页面。'; renderSelection();
    }
  }
  async function loadCatalogs() {
    try {
      const result = await request('/api/zotero/catalogs');
      if (!state.alive) return;
      if (!result.ok || !Array.isArray(result.data.catalogs)) throw new Error('catalog_unavailable');
      state.catalogs = result.data.catalogs.filter(catalog => idValid(catalog.report_id) && idValid(catalog.catalog_id));
      nodes['catalog-select'].replaceChildren();
      state.catalogs.forEach((catalog, index) => {
        const option = create('option', [text(catalog.report_date, 40), text(catalog.title || catalog.report_title, 150) || catalog.report_id].filter(Boolean).join(' · '));
        option.value = String(index); nodes['catalog-select'].append(option);
      });
      if (state.catalogs.length) { nodes['catalog-select'].value = '0'; await loadCatalog('0'); }
      else { nodes['catalog-status'].textContent = '尚无可用的服务器目录。不会显示合成论文或创建保存任务。'; nodes['paper-cards'].replaceChildren(create('p', '等待经过核验的真实论文目录。', 'empty-catalog')); }
      renderSelection();
    } catch {
      if (!state.alive) return;
      nodes['catalog-status'].textContent = '无法读取目录。请检查登录状态或稍后刷新。';
    }
  }
  async function refreshConnection() {
    try {
      const result = await request('/api/zotero/connection');
      if (!state.alive) return false;
      state.connection = result.ok ? result.data : { ready: false, live_ready: false };
      renderConnection(); return result.ok;
    } catch {
      if (!state.alive) return false;
      state.connection = { ready: false, live_ready: false };
      renderConnection(); nodes['connection-note'].textContent = '连接状态暂时无法确认，保存已停用。请点击刷新状态重试。'; return false;
    }
  }
  const stateLabels = { queued: '等待处理', pending: '等待处理', running: '处理中', processing: '处理中',
    retry_wait: '等待重试', retry_pending: '等待重试', metadata_saved: '元数据已保存', pdf_pending: 'PDF 待处理',
    complete: '已完成', already_exists: '已存在', failure: '失败', failed: '失败', terminal_failure: '失败，已停止',
    partial_failure: '部分失败，已停止', partial_success: '部分完成，已停止', connection_required: '需要重新连接',
    blocked_connection: '等待连接，未执行', preview_only: '仅预览，未执行', cancelled: '已取消' };
  const terminal = new Set(['complete', 'already_exists', 'failure', 'failed', 'terminal_failure', 'partial_failure',
    'partial_success', 'connection_required', 'blocked_connection', 'preview_only', 'cancelled']);
  const advanceable = new Set(['queued', 'pending', 'running', 'processing', 'retry_wait', 'retry_pending', 'metadata_saved', 'pdf_pending']);
  const publicJob = data => data?.execution || data?.job || data;
  const resumable = job => advanceable.has(job.state) && (job.state !== 'pdf_pending' || job.retryable === true ||
    (Array.isArray(job.items) && job.items.some(item => item.retryable === true)));
  function renderHistory() {
    nodes['job-history'].replaceChildren();
    nodes['refresh-jobs'].disabled = state.jobsBusy;
    for (const job of state.jobs) {
      const row = create('li');
      row.append(create('span', (text(job.report_id, 128) || '保存任务') + ' · ' +
        (stateLabels[job.state] || '状态待确认') + ' · ' + (Array.isArray(job.items) ? job.items.length : 0) + ' 篇'));
      const view = create('button', '查看结果', 'secondary'); view.type = 'button'; view.disabled = busy();
      view.addEventListener('click', () => { if (!busy()) renderJob(job); }); row.append(view);
      if (resumable(job)) {
        const resume = create('button', '继续此任务', 'secondary'); resume.type = 'button';
        resume.disabled = !connected() || busy();
        resume.addEventListener('click', () => {
          if (!connected() || busy() || !resumable(job)) return;
          const id = job.job_id || job.execution_id;
          if (!jobIdValid(id)) return;
          const epoch = ++state.jobEpoch;
          state.activeJob = { id, job };
          if (acceptJob(job, epoch)) scheduleJob(job, epoch, 0);
          renderSelection(); renderPapers();
        });
        row.append(resume);
      }
      nodes['job-history'].append(row);
    }
    if (!state.jobs.length) nodes['job-history'].append(create('li', '尚无实际保存任务。', 'muted'));
  }
  async function loadJobs() {
    if (state.jobsBusy) return;
    state.jobsBusy = true; nodes['refresh-jobs'].disabled = true;
    try {
      const result = await request('/api/zotero/executions');
      if (!state.alive) return;
      if (!result.ok || !Array.isArray(result.data.jobs)) throw new Error('jobs_unavailable');
      state.jobs = result.data.jobs.filter(job => job && jobIdValid(job.job_id || job.execution_id) &&
        !['blocked_connection', 'preview_only'].includes(job.state));
      nodes['history-status'].textContent = '只列出实际保存任务。未完成任务需你明确点击继续；不会自动运行。';
    } catch {
      if (state.alive) nodes['history-status'].textContent = '暂时无法读取任务记录，请点击刷新任务记录重试。';
    } finally { state.jobsBusy = false; if (state.alive) renderHistory(); }
  }

  function renderJob(job) {
    nodes['job-status'].textContent = '本次任务：' + (stateLabels[job.state] || '状态待确认') + (job.retry_at ? ' · 下次重试：' + text(job.retry_at, 50) : '');
    nodes['job-items'].replaceChildren();
    for (const item of Array.isArray(job.items) ? job.items : []) {
      const paper = state.catalog?.papers.find(candidate => candidate.paper_id === item.paper_id);
      const status = stateLabels[item.state] || '状态待确认';
      let detail = status;
      if (item.metadata_saved === true && !['metadata_saved', 'complete', 'already_exists'].includes(item.state)) detail += ' · 元数据已保存';
      if (item.classification_saved === false && item.metadata_saved === true) detail += ' · 分类尚未完成';
      if (item.pdf_verified === true) detail += ' · PDF 已核验';
      if (item.state === 'failure' && item.metadata_saved === true && (job.pdf_intent || state.pdfIntent) === 'open_access_if_available' && item.pdf_verified !== true) detail += ' · PDF 未完成';
      if (item.metadata_state && item.metadata_state !== item.state) detail += ' · 元数据：' + (stateLabels[item.metadata_state] || '待确认');
      if (item.pdf_state && item.pdf_state !== item.state) detail += ' · PDF：' + (stateLabels[item.pdf_state] || (item.pdf_state === 'not_requested' ? '未请求' : '待确认'));
      const errorLabels = { VERIFIED_BLOB_UNAVAILABLE: '已核验 PDF 文件暂不可用', VERIFIED_PDF_UNAVAILABLE: '缺少已核验的开放 PDF',
        AUTHORIZATION_DENIED: '权限已失效', INSUFFICIENT_PERMISSION: '权限不足', QUOTA_OR_SIZE_EXCEEDED: '存储额度或大小超限',
        RATE_LIMITED: '服务限流', NETWORK_FAILURE: '网络连接中断', REQUEST_TIMEOUT: '请求超时',
        PDF_DIGEST_MISMATCH: 'PDF 内容校验失败', PDF_READBACK_MISMATCH: 'PDF 回读校验失败',
        UNCERTAIN_PDF_NOT_REGISTERED: '附件登记结果待人工确认', FALLBACK_IDENTITY_REVIEW_REQUIRED: '已有相似论文，身份信息需人工核对；未新建或合并' };
      if (Object.hasOwn(errorLabels, item.error_code)) detail += ' · ' + errorLabels[item.error_code];
      if (item.uncertain === true) detail += ' · 操作结果仍待核验';
      if (item.state === 'metadata_saved' && (job.pdf_intent || state.pdfIntent) === 'open_access_if_available') detail += ' · PDF 尚未确认完成';
      nodes['job-items'].append(create('li', (text(paper?.title) || text(item.paper_id, 512) || '论文') + '：' + detail));
    }
  }
  function stopJob(message) {
    clearTimeout(state.timer); state.timer = null; state.activeJob = null; state.jobBusy = false;
    nodes['job-refresh'].disabled = true;
    if (message) nodes['job-status'].textContent = message;
    renderSelection(); renderPapers();
  }
  function scheduleJob(job, epoch, delay = 1500) {
    clearTimeout(state.timer);
    const retry = typeof job.retry_at === 'string' ? Date.parse(job.retry_at) : Number.NaN;
    const wait = Number.isFinite(retry) ? Math.max(500, retry - Date.now()) : delay;
    state.timer = setTimeout(() => void tickJob(epoch), Math.min(2_147_000_000, wait));
  }
  function acceptJob(data, epoch) {
    if (!state.alive || epoch !== state.jobEpoch || !state.activeJob) return false;
    const job = publicJob(data);
    if (!job || (job.job_id || job.execution_id) !== state.activeJob.id) { stopJob('任务响应无法确认，已停止自动继续。请刷新后检查。'); return false; }
    state.activeJob.job = job;
    const index = state.jobs.findIndex(item => (item.job_id || item.execution_id) === state.activeJob.id);
    if (index < 0) state.jobs.unshift(job); else state.jobs[index] = job;
    renderJob(job); renderHistory();
    if (terminal.has(job.state)) {
      if (job.state === 'connection_required' || job.state === 'blocked_connection') { state.connection = { ...state.connection, ready: false, live_ready: false }; renderConnection(); }
      stopJob(); return false;
    }
    if (job.state === 'pdf_pending' && job.retryable !== true && !(Array.isArray(job.items) && job.items.some(item => item.retryable === true))) {
      stopJob('本次任务：PDF 待处理，元数据与附件结果见下方；缺少可重试的已核验附件，已停止自动继续。'); return false;
    }
    if (!advanceable.has(job.state)) { stopJob('任务状态无法确认，已停止自动继续；没有假定保存成功。'); return false; }
    return true;
  }
  async function tickJob(epoch, readOnly = false) {
    if (!state.alive || epoch !== state.jobEpoch || !state.activeJob || state.jobBusy) return;
    state.jobBusy = true; nodes['job-refresh'].disabled = true;
    try {
      const path = '/api/zotero/executions/' + encodeURIComponent(state.activeJob.id);
      const read = await request(path);
      if (!state.alive || epoch !== state.jobEpoch || !state.activeJob) return;
      if (!read.ok) {
        if ([401, 403, 404].includes(read.status)) { stopJob('无法访问此任务，已停止继续。请重新登录后检查。'); return; }
        throw new Error('progress_unavailable');
      }
      if (!acceptJob(read.data, epoch)) return;
      const retry = Date.parse(state.activeJob.job.retry_at);
      if (readOnly || (Number.isFinite(retry) && retry > Date.now())) { scheduleJob(state.activeJob.job, epoch); return; }
      if (!connected()) { stopJob('连接状态未确认，任务已暂停。核验连接后须明确点击继续任务。'); return; }
      const advance = await request(path + '/advance', { action: 'continue_import' });
      if (!state.alive || epoch !== state.jobEpoch || !state.activeJob) return;
      if (!advance.ok) {
        if (['connection_required', 'live_execution_disabled'].includes(advance.data?.error)) {
          state.connection = { ...state.connection, ready: false, live_ready: false }; renderConnection();
          stopJob('连接或执行权限已失效，任务已停止；需重新连接并再次明确保存。'); return;
        }
        const retryAt = typeof advance.data?.retry_at === 'string' && Number.isFinite(Date.parse(advance.data.retry_at))
          ? advance.data.retry_at : Number.isFinite(advance.data?.retry_after_ms) && advance.data.retry_after_ms > 0
            ? new Date(Date.now() + advance.data.retry_after_ms).toISOString() : null;
        if ([409, 429, 503].includes(advance.status) && retryAt && advance.data?.retryable !== false) {
          state.activeJob.job.retry_at = retryAt; scheduleJob(state.activeJob.job, epoch); return;
        }
        if ([400, 401, 403, 404, 409, 422].includes(advance.status)) { stopJob('服务器未接受继续操作，已停止。请刷新检查任务状态。'); return; }
        if (advance.data?.retry_at) state.activeJob.job.retry_at = advance.data.retry_at;
        scheduleJob(state.activeJob.job, epoch, 5000); return;
      }
      if (acceptJob(advance.data, epoch)) scheduleJob(state.activeJob.job, epoch);
    } catch {
      if (!state.alive || epoch !== state.jobEpoch || !state.activeJob) return;
      nodes['job-status'].textContent = '进度暂时无法确认，稍后先读取服务器状态；不会假定保存成功。';
      scheduleJob(state.activeJob.job, epoch, 5000);
    } finally {
      state.jobBusy = false; nodes['job-refresh'].disabled = !state.activeJob;
    }
  }
  nodes['save-form'].addEventListener('submit', async event => {
    event.preventDefault();
    if (nodes['save-to-zotero'].disabled || busy() || !connected()) return;
    const papers = selectedPapers();
    if (!papers.length || papers.length > 100 || !['metadata_only', 'open_access_if_available'].includes(state.pdfIntent)) return;
    state.saving = true; renderSelection(); renderPapers();
    const epoch = ++state.jobEpoch;
    const payload = { action: 'save_selected', report_id: state.catalog.report_id, catalog_id: state.catalog.catalog_id,
      catalog_version: state.catalog.catalog_version, paper_ids: papers.map(paper => paper.paper_id), pdf_intent: state.pdfIntent };
    nodes['job-status'].textContent = '正在提交本次选择…'; nodes['job-items'].replaceChildren();
    try {
      const result = await request('/api/zotero/executions', payload);
      if (!state.alive || epoch !== state.jobEpoch) return;
      const job = publicJob(result.data);
      if (!result.ok) {
        if (['connection_required', 'live_execution_disabled'].includes(result.data?.error)) {
          state.connection = { ...state.connection, ready: false, live_ready: false }; renderConnection();
          nodes['job-status'].textContent = '需要连接并核验后再次明确保存。本次不会自动恢复。';
        } else nodes['job-status'].textContent = '未确认创建任务。请检查目录版本、权限或连接状态，再由你决定是否重试。';
        return;
      }
      const id = job.job_id || job.execution_id;
      if (!jobIdValid(id)) throw new Error('job_unconfirmed');
      state.activeJob = { id, job }; state.submittedSelection = true;
      if (acceptJob(job, epoch)) { nodes['job-refresh'].disabled = false; scheduleJob(job, epoch, 0); }
    } catch {
      if (state.alive) nodes['job-status'].textContent = '提交结果尚未确认，未自动重发。请刷新检查状态后再决定是否保存。';
    } finally { state.saving = false; if (state.alive) { renderSelection(); renderPapers(); } }
  });
  nodes['credential-form'].addEventListener('submit', async event => {
    event.preventDefault();
    const key = nodes['api-key'];
    if (state.connectionBusy || nodes['credential-fields'].disabled || state.connection?.credential_entry_enabled !== true) { key.value = ''; return; }
    if (!nodes['credential-form'].checkValidity()) return;
    state.connectionBusy = true; renderConnection();
    const managed = state.connection?.credential_entry_mode === 'provider-managed-d1';
    let payload = JSON.stringify(managed ? { action: 'connect_personal_library', library_id: nodes['library-id'].value.trim(), api_key: key.value }
      : { action: 'stage_credentials', api_key: key.value }); key.value = '';
    try {
      const result = await request('/api/zotero/connect', payload); payload = '';
      if (!state.alive) return;
      nodes['credential-result'].textContent = result.ok ? managed ? '个人文库凭据已核验并保存。连接不会自动导入；保存是否可用以目录与执行状态为准。'
        : '已加密暂存；尚未连接。请在服务器开放核验后明确点击「核验连接」。'
        : managed ? ({ key_owner_mismatch: '文库 ID 与密钥账号不匹配，未保存新凭据。', excess_permission: '密钥权限超出范围。请取消笔记和所有群组授权后重试。', insufficient_permission: '需要个人文库读取、写入及文件权限。', library_rebind_required: '此站点已绑定另一文库。更换文库需要单独审查，不能直接重绑。', disconnect_required: '已有其他凭据，请先断开连接。' }[result.data?.error] || '连接结果未确认。请刷新状态，检查 ID、密钥权限或稍后重试。')
          : '暂存未确认。请刷新状态或重新申请一次性安全授权。';
      await refreshConnection();
    } catch {
      if (state.alive) { state.connection = { ready: false, live_ready: false }; nodes['credential-result'].textContent = '凭据提交结果尚未确认。请刷新状态后再操作。'; }
    } finally { payload = ''; key.value = ''; state.connectionBusy = false; if (state.alive) renderConnection(); }
  });
  nodes['verify-connection'].addEventListener('click', async () => {
    if (state.connectionBusy || busy() || !canVerify()) return;
    state.connectionBusy = true; renderConnection();
    try {
      const result = await request('/api/zotero/verify-connection', { action: 'verify_connection' });
      if (!state.alive) return;
      await refreshConnection();
      nodes['credential-result'].textContent = result.ok && state.connection?.ready === true ? connected()
        ? '连接已核验。旧意向不会执行；请重新检查本页选择，再明确保存。'
        : '个人文库连接已核验，导入执行尚未开放。目录与执行配置完成前不能保存。'
        : '尚未确认可用连接，保存仍受服务器状态限制。';
    } catch {
      state.connection = { ready: false, live_ready: false };
      if (state.alive) nodes['credential-result'].textContent = '核验结果无法确认，请刷新状态后重试。';
    } finally { state.connectionBusy = false; if (state.alive) renderConnection(); }
  });
  nodes['disconnect-connection'].addEventListener('click', async () => {
    if (state.connectionBusy || nodes['disconnect-connection'].disabled) return;
    state.connectionBusy = true; state.jobEpoch++; stopJob('连接正在断开，已停止本页继续导入。'); renderConnection();
    try {
      const result = await request('/api/zotero/disconnect', { action: 'disconnect' });
      if (!state.alive) return;
      await refreshConnection();
      nodes['credential-result'].textContent = result.ok
        ? '已删除活动凭据并断开连接。已在途请求可能完成。备份可能仍保留密钥，请到 Zotero 设置撤销该密钥，使备份副本也失效。'
        : '断开结果尚未确认。请刷新状态；如需立即停用，请到 Zotero 撤销密钥。';
    } catch { if (state.alive) { state.connection = { ready: false, live_ready: false }; nodes['credential-result'].textContent = '断开结果尚未确认，请刷新检查或到 Zotero 撤销密钥。'; } }
    finally { state.connectionBusy = false; if (state.alive) renderConnection(); }
  });
  nodes['refresh-connection'].addEventListener('click', async () => {
    if (state.connectionBusy) return;
    state.connectionBusy = true; renderConnection(); await refreshConnection(); state.connectionBusy = false; if (state.alive) renderConnection();
  });
  nodes['catalog-select'].addEventListener('change', () => void loadCatalog(nodes['catalog-select'].value));
  ['date-filter', 'topic-filter', 'publication-filter'].forEach(id => nodes[id].addEventListener('change', renderPapers));
  nodes['search-papers'].addEventListener('input', renderPapers);
  nodes['reset-selection'].addEventListener('click', resetSelection);
  nodes['save-form'].addEventListener('reset', event => { event.preventDefault(); resetSelection(); });
  [['pdf-metadata', 'metadata_only'], ['pdf-open-access', 'open_access_if_available']].forEach(([id, value]) => nodes[id].addEventListener('change', () => {
    if (busy()) return;
    state.pdfIntent = nodes[id].checked ? value : ''; state.submittedSelection = false; renderSelection();
  }));
  nodes['job-refresh'].addEventListener('click', () => void tickJob(state.jobEpoch, true));
  nodes['refresh-jobs'].addEventListener('click', () => void loadJobs());
  const dispose = () => {
    state.alive = false; state.catalogEpoch++; state.jobEpoch++; clearTimeout(state.timer);
    state.requests.forEach(controller => controller.abort()); state.requests.clear();
    nodes['api-key'].value = ''; state.selected.clear(); state.pdfIntent = ''; state.activeJob = null;
    nodes['save-to-zotero'].disabled = true; nodes['credential-fields'].disabled = true;
  };
  platform.addEventListener('pagehide', dispose);
  // A BFCache restore must not resume an old authorized execution or stale selection.
  platform.addEventListener('pageshow', event => { if (event.persisted) platform.location.reload(); });
  renderConnection();
  const ready = Promise.all([loadCatalogs(), refreshConnection(), loadJobs()]);
  return { ready, dispose };
}

const STYLE = `
:root{color-scheme:light dark;--bg:#f6f4ed;--card:#fffef9;--ink:#172c29;--muted:#5d6c66;--line:#ccd3c8;--accent:#14634c;--soft:#e9eee4;--warm:#f2ecd9;--focus:#925025}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.8 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}h1,h2,h3,p{margin:0}a{color:var(--accent);text-underline-offset:5px}button,input,select{font:inherit}button,input,select{touch-action:manipulation}a:focus-visible,button:focus-visible,input:focus-visible,select:focus-visible,summary:focus-visible{outline:3px solid var(--focus);outline-offset:4px}button{cursor:pointer}button:disabled{cursor:not-allowed;background:var(--soft);color:var(--muted);border-color:var(--line)}.page{max-width:1220px;margin:auto;padding:0 40px}.masthead{display:flex;justify-content:space-between;align-items:center;gap:20px;border-bottom:1px solid var(--ink);padding:26px 0 20px}.brand{color:var(--ink);text-decoration:none;font:700 32px Georgia,serif}.edition,.eyebrow{font-size:12px;letter-spacing:1.6px;color:var(--muted)}.hero{display:flex;justify-content:space-between;align-items:center;gap:28px;padding:36px 0 26px}h1{font-size:clamp(28px,3.2vw,40px);line-height:1.4;letter-spacing:-.8px;margin:10px 0}.intro,.muted,.note{color:var(--muted)}.status{border:1px solid var(--line);border-radius:4px;background:var(--card);padding:10px 16px;white-space:nowrap;font-weight:600}.status small{display:block;font-size:12px;letter-spacing:1px;font-weight:400;color:var(--muted)}.notice{background:var(--warm);border-left:3px solid var(--focus);padding:13px 18px;font-size:14px}.selection-layout{display:grid;grid-template-columns:minmax(0,1fr) 340px;gap:30px;padding:30px 0}.section-heading{display:flex;justify-content:space-between;gap:12px;align-items:baseline;border-top:1px solid var(--ink);padding:15px 0}h2{font-size:18px}h3{font-size:14px;color:var(--accent);margin:15px 0 6px}.paper-cards{display:grid;gap:16px}.paper-card{border:1px solid var(--line);border-radius:4px;padding:20px 23px;background:var(--card);overflow-wrap:anywhere}.paper-card.is-selected{border-color:var(--accent);background:var(--soft)}.paper-meta{font-size:12px;color:var(--muted)}.paper-selection{display:flex;gap:14px;align-items:flex-start;margin:12px 0;min-height:44px;cursor:pointer}.paper-checkbox{accent-color:var(--accent);width:21px;height:21px;flex:none;margin:5px 0 0}.paper-title{font-size:20px;line-height:1.55;font-weight:650}.publication,.paper-summary{font-size:14px;color:var(--muted);margin:8px 0}.paper-bottom{display:flex;justify-content:space-between;align-items:center;gap:10px;border-top:1px solid var(--line);margin-top:16px;padding-top:12px;font-size:12px;color:var(--muted)}.topic-chip{color:var(--accent);background:var(--soft);border-radius:3px;padding:2px 8px}.selection-panel{position:sticky;top:20px;align-self:start;border:1px solid var(--line);border-radius:4px;background:var(--card);padding:0 20px 22px}.selection-panel .section-heading{border-top:0;border-bottom:1px solid var(--line)}.selection-total{display:flex;justify-content:space-between;align-items:center;padding:18px 0 8px}#selected-count{font:48px/1.1 Georgia,serif;color:var(--accent)}.count-label{font-size:14px;color:var(--muted);margin-left:10px}.selection-status,.note{font-size:13px}.selection-panel ul{font-size:13px;padding-left:18px;margin:5px 0;overflow-wrap:anywhere}.reset-button{border:0;background:transparent;color:var(--accent);text-decoration:underline;min-height:44px;font-size:14px}.control-label{display:block;font-size:13px;font-weight:600;margin:10px 0 4px}select,input[type=search],input[type=password]{width:100%;border:1px solid var(--line);border-radius:4px;background:var(--card);color:var(--ink);min-height:44px;padding:8px 10px}.filters{display:grid;grid-template-columns:1fr 1fr;gap:8px 14px;margin:10px 0 16px}.filters .search{grid-column:1/-1}#catalog-status{font-size:12px;margin:8px 0 14px;color:var(--muted)}.empty-catalog{border:1px dashed var(--line);padding:24px;color:var(--muted)}fieldset{border:0;padding:0;margin:0}legend{font-size:14px;font-weight:600;padding:0}.pdf-choice{display:flex;align-items:flex-start;gap:8px;font-size:13px;margin:9px 0}.pdf-choice input{accent-color:var(--accent);margin-top:6px}.save-area{border-top:1px solid var(--line);padding-top:16px;margin-top:18px}button.primary{background:var(--accent);color:var(--card);border:1px solid var(--accent);border-radius:4px;padding:10px 16px;min-height:48px;font-size:14px}button.primary:disabled{background:var(--soft);color:var(--muted);border-color:var(--line)}#save-to-zotero{width:100%;margin-top:12px}#save-help{font-size:12px;color:var(--muted);margin-top:10px}.panel{background:var(--card);border:1px solid var(--line);border-radius:4px;padding:22px;margin-bottom:24px}.panel p{font-size:14px;margin:8px 0}.panel button{margin:10px 8px 0 0}.secondary{background:transparent;border:1px solid var(--line);border-radius:4px;color:var(--accent);padding:9px 14px;min-height:44px;font-size:14px}.step{font-size:12px;color:var(--accent);letter-spacing:1.5px}.security-details{margin-top:16px;border-top:1px solid var(--line);padding-top:12px}.security-details summary{cursor:pointer;font-size:14px}#api-key{max-width:600px}#job-history{padding-left:20px;font-size:14px}#job-history li{padding:5px 0;overflow-wrap:anywhere}#job-history button{margin-left:12px}#job-items{padding-left:20px;font-size:14px;overflow-wrap:anywhere}.footer{border-top:1px solid var(--line);padding:18px 0 24px;font-size:12px;color:var(--muted);display:flex;justify-content:space-between;gap:20px}.skip-link{position:absolute;top:-100px;left:20px;background:var(--card);padding:8px;z-index:2}.skip-link:focus{top:12px}.noscript-note{padding:16px 0;color:var(--focus)}details.selected-details{font-size:13px;margin-top:12px}details.selected-details ul{max-height:180px;overflow:auto}button:disabled,input:disabled,select:disabled{opacity:.8}
@media(prefers-color-scheme:dark){:root{--bg:#142320;--card:#1c2e29;--ink:#e6ece0;--muted:#adbdb3;--line:#41564b;--accent:#a7d3ae;--soft:#263c32;--warm:#36382b;--focus:#eab98a}}
@media(max-width:860px){.page{padding:0 24px}.selection-layout{grid-template-columns:minmax(0,1fr) 300px;gap:22px}.paper-card{padding:18px}.selection-panel{padding:0 16px 18px}}
@media(max-width:720px){.page{padding:0 20px}.hero{flex-wrap:wrap;gap:16px;padding:26px 0 22px}.edition{display:none}.brand{font-size:28px}.status{display:flex;gap:12px;align-items:center}.selection-layout{grid-template-columns:1fr}.selection-panel{position:static}.paper-title{font-size:20px}.paper-bottom{flex-wrap:wrap}.panel{padding:18px}.footer{flex-wrap:wrap}.filters{grid-template-columns:1fr}.filters .search{grid-column:auto}}
@media(prefers-reduced-motion:reduce){html{scroll-behavior:auto}}
`;

export function renderManagerPage({ connection = {} } = {}) {
  // Strict allowlist: credentials, library identifiers, internal errors and arbitrary
  // caller data never enter HTML or the executable bootstrap payload.
  const initial = {
    ready: connection.ready === true, live_ready: connection.live_ready === true,
    ...(typeof connection.catalog_ready === 'boolean' ? {catalog_ready:connection.catalog_ready} : {}),
    ...(connection.catalog_preparation_unavailable === true ? {catalog_preparation_unavailable:true} : {}),
    credential_entry_enabled: connection.credential_entry_enabled === true,
    verification_enabled: connection.verification_enabled === true,
    disconnect_enabled: connection.disconnect_enabled === true,
    credential_entry_mode: connection.credential_entry_mode === 'provider-managed-d1' ? 'provider-managed-d1' : 'aead',
    credential_storage: ['not_stored', 'provider_managed_d1', 'not_staged', 'encrypted_pending_verification', 'encrypted_staged', 'staged', 'encrypted_verified'].includes(connection.credential_storage)
      ? connection.credential_storage : 'not_staged',
  };
  const ready = initial.ready;
  const nonce = btoa(String.fromCharCode(...crypto.getRandomValues(new Uint8Array(18))));
  const html = `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Zotero 论文管理 · Daily Agent</title><style nonce="${nonce}">${STYLE}</style></head><body>
<a class="skip-link" href="#paper-list">跳至论文选择</a><div class="page">
<header class="masthead"><a class="brand" href="/">Daily Agent</a><span class="edition">研究日报 / ZOTERO MANAGER</span></header>
<main><section class="hero"><div><p class="eyebrow">YOUR READING, YOUR CHOICE</p><h1>挑选值得留存的论文</h1><p class="intro">阅读真实目录，检查分类，再由你决定进入 Zotero 的内容。</p></div>
<div class="status"><span id="connection-label">${ready ? '已连接' : '未连接'}</span><small id="connection-english">${ready ? 'CONNECTED' : 'NOT CONNECTED'}</small></div></section>
<div class="notice"><strong>默认不选，明确保存</strong><p>勾选本身不会触发导入。页面不恢复旧选择或旧意向；连接成功后也不会自动保存。</p></div>
<noscript><p class="noscript-note">JavaScript 未启用：所有提交控件保持禁用，无法录入凭据或保存论文。请启用后刷新本页。</p></noscript>
<div class="selection-layout"><section id="paper-list" aria-labelledby="paper-list-title"><div class="section-heading"><h2 id="paper-list-title">选择论文</h2><span class="note">服务器核验目录</span></div>
<p class="note"><a href="/zotero/register">准备私有论文目录 →</a></p>
<label class="control-label" for="catalog-select">日报目录</label><select id="catalog-select" disabled><option>正在读取目录…</option></select>
<div class="filters"><div><label class="control-label" for="date-filter">日报日期</label><select id="date-filter" disabled><option value="">全部日报日期</option></select></div><div><label class="control-label" for="topic-filter">研究主题</label><select id="topic-filter" disabled><option value="">全部研究主题</option></select></div><div><label class="control-label" for="publication-filter">发表分类</label><select id="publication-filter" disabled><option value="">全部发表分类</option></select></div><div class="search"><label class="control-label" for="search-papers">搜索标题、作者或 DOI</label><input id="search-papers" type="search" autocomplete="off" placeholder="在当前目录中搜索" disabled></div></div>
<p id="catalog-status" role="status" aria-live="polite">正在读取目录…</p><div id="paper-cards" class="paper-cards"></div></section>
<aside class="selection-panel" aria-labelledby="preview-title"><div class="section-heading"><h2 id="preview-title">分类预览</h2><span class="note">仅本页选择</span></div>
<div class="selection-total"><div><span id="selected-count">0</span><span class="count-label">篇已选</span></div><button id="reset-selection" class="reset-button" type="button" disabled>清空选择</button></div>
<p id="selection-status" class="selection-status" role="status" aria-live="polite">尚未选择论文。勾选只更新本页预览。</p>
<h3>按日报日期</h3><ul id="date-collections"><li>选择后显示日报日期</li></ul><h3>按研究主题</h3><ul id="topic-collections"><li>选择后显示研究主题</li></ul><h3>按发表分类</h3><ul id="publication-collections"><li>选择后显示发表分类</li></ul>
<details class="selected-details"><summary>检查全部已选标题</summary><ul id="selected-papers"></ul></details>
<form id="save-form" class="save-area" method="post" action="/api/zotero/executions"><fieldset><legend>PDF 处理方式（必选）</legend>
<label class="pdf-choice"><input id="pdf-metadata" type="radio" name="pdf-choice" value="metadata_only" disabled>仅保存元数据</label>
<label class="pdf-choice"><input id="pdf-open-access" type="radio" name="pdf-choice" value="open_access_if_available" disabled>同时尝试开放获取 PDF</label>
<p class="note">只使用已核验的开放来源；遇到受限访问或附件失败会单独显示。</p></fieldset>
<button id="save-to-zotero" class="primary" type="submit" aria-describedby="save-help" disabled>Save to Zotero · 保存所选</button><p id="save-help">请等待目录和连接状态读取完成。</p></form></aside></div>
<section class="panel" aria-labelledby="job-title"><p class="step">IMPORT / CURRENT SESSION</p><h2 id="job-title">当前任务进度</h2><p id="job-status" role="status" aria-live="polite">尚未提交本次选择。</p><ul id="job-items"></ul><button id="job-refresh" class="secondary" type="button" disabled>刷新本次进度</button><p class="note">元数据已保存、PDF 待处理、已完成、已存在与失败分别显示。离开页面会停止本页继续操作，已提交的服务器步骤可能仍会完成。</p></section>
<section class="panel" aria-labelledby="history-title"><p class="step">EXECUTIONS / EXPLICIT RESUME</p><h2 id="history-title">保存任务记录</h2><p id="history-status" role="status" aria-live="polite">正在读取任务记录…</p><ul id="job-history"></ul><button id="refresh-jobs" class="secondary" type="button" disabled>刷新任务记录</button><p class="note">关闭页面或网络中断后，可在这里检查实际任务，再明确继续。旧的预览或等待连接意向不会出现在可执行列表中。</p></section>
<section class="panel" aria-labelledby="connection-title"><p class="step">CONNECTION / OWNER ONLY</p><h2 id="connection-title">连接与安全设置</h2><p id="connection-note">连接状态待确认。</p><button id="refresh-connection" class="secondary" type="button" disabled>刷新连接状态</button><button id="verify-connection" class="secondary" type="button" disabled>核验连接</button><button id="disconnect-connection" class="secondary" type="button" disabled>断开连接并删除活动凭据</button><p class="note">断开连接会停止后续导入，已在途请求可能完成。数据库备份可能仍保留密钥；请到 <a href="https://www.zotero.org/settings/keys" target="_blank" rel="noopener noreferrer">Zotero API 密钥设置</a> 撤销该密钥，使备份中的副本也失效。</p>
<details class="security-details"><summary>安全录入凭据</summary><p id="setup-note">需先取得一次性安全授权、确认个人文库，并完成服务器加密配置。密钥只在此密码输入框录入，不应粘贴到聊天、日报或链接中。</p>
<form id="credential-form" method="post" action="/api/zotero/connect" autocomplete="off"><fieldset id="credential-fields" disabled><label id="library-id-label" class="control-label" for="library-id" hidden>个人数字文库 ID（Zotero User ID）</label><input id="library-id" type="text" inputmode="numeric" pattern="[1-9][0-9]{0,15}" maxlength="16" autocomplete="off" hidden><label class="control-label" for="api-key">个人文库 Zotero API 密钥</label><input id="api-key" type="password" autocomplete="off" spellcheck="false" autocapitalize="none" minlength="16" maxlength="128" required aria-describedby="setup-note"><br><button id="credential-submit" class="primary" type="submit">核验并连接</button></fieldset></form></details><p id="credential-result" role="status" aria-live="polite"></p></section>
</main><footer class="footer"><span>Daily Agent / Zotero · 仅你的账户可访问</span><a href="/">返回研究日报</a></footer></div>
<script nonce="${nonce}">(${managerClient.toString()})(${JSON.stringify(initial)});</script></body></html>`;
  return new Response(html, { headers: {
    'Content-Type': 'text/html; charset=utf-8', 'Cache-Control': 'no-store, private', Pragma: 'no-cache',
    'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer', 'Cross-Origin-Resource-Policy': 'same-origin',
    'X-Frame-Options': 'DENY', 'Permissions-Policy': 'camera=(), microphone=(), geolocation=()',
    'Content-Security-Policy': `default-src 'none'; script-src 'nonce-${nonce}'; style-src 'nonce-${nonce}'; connect-src 'self'; form-action 'none'; base-uri 'none'; frame-ancestors 'none'`,
  } });
}
