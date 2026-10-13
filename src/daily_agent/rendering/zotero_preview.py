"""Synthetic, disconnected selection prototype, separate from report rendering.

This module has no Zotero transport, credentials, persistence, report ingestion,
or dependency on production renderers. Its three static assets work offline.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from html import escape
from pathlib import Path


@dataclass(frozen=True)
class SyntheticPaper:
    """Display fixture only; never an importable paper or verified source."""

    title: str
    topic: str
    publication: str = "unverified"


SAMPLE_DATE = date(2026, 10, 10)
SAMPLE_PAPERS = (
    SyntheticPaper("示例论文 A：检索与记忆的协同设计", "检索与记忆", "verified"),
    SyntheticPaper("示例论文 B：多步骤智能体的评测框架", "智能体评测", "unverified"),
    SyntheticPaper("示例论文 C：长任务中的记忆更新", "检索与记忆", "preprint"),
)

PUBLICATION_LABELS = {
    "verified": "正式发表 · 已核验（演示状态）",
    "unverified": "发表状态未核验（演示状态）",
    "preprint": "仅预印本 · 不代表同行评审通过（演示状态）",
}

CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; "
    "connect-src 'none'; img-src 'none'; font-src 'none'; "
    "object-src 'none'; base-uri 'none'; form-action 'none'"
)


def render_zotero_preview(
    papers: tuple[SyntheticPaper, ...] = SAMPLE_PAPERS,
    issue_date: date = SAMPLE_DATE,
) -> str:
    """Render escaped synthetic fixtures; all selections start empty.

    This intentionally does not accept production report objects, URLs, Zotero
    library identifiers, attachments, API keys, or source-verification evidence.
    A fixture's publication label demonstrates a UI state, not a factual claim.
    """
    cards = []
    for number, paper in enumerate(papers, 1):
        if not isinstance(paper, SyntheticPaper):
            raise TypeError("This preview accepts SyntheticPaper fixtures only")
        if paper.publication not in PUBLICATION_LABELS:
            raise ValueError("Unknown synthetic publication state")
        cards.append(f'''<article class="paper-card" aria-labelledby="paper-title-{number}">
<div class="paper-meta"><span class="eyebrow">SYNTHETIC / {number:02d}</span><span class="fixture-label">合成示例</span></div>
<label class="paper-selection" for="paper-{number}">
<input type="checkbox" id="paper-{number}" class="paper-checkbox" data-topic="{escape(paper.topic, quote=True)}" aria-describedby="publication-{number}" disabled>
<span><span class="paper-title" id="paper-title-{number}">{escape(paper.title, quote=True)}</span><span class="selection-hint">选择此篇，预览归类位置</span></span>
</label>
<p class="publication {paper.publication}" id="publication-{number}">{PUBLICATION_LABELS[paper.publication]}</p>
<p class="sample-description">虚构题目，仅用于检查选择与分类界面，不代表真实论文或研究结论。</p>
<div class="paper-bottom"><span class="topic-chip">{escape(paper.topic, quote=True)}</span><span>无原文 / 无 PDF</span></div>
</article>''')
    empty = '<p class="empty-catalog">当前没有合成示例可供选择。</p>'
    return f'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="{escape(CSP, quote=True)}">
<meta name="referrer" content="no-referrer">
<title>Zotero 选文预览 · 未连接 · Daily Agent</title>
<link rel="stylesheet" href="styles.css">
<script src="preview.js" defer></script>
</head>
<body>
<a class="skip-link" href="#paper-list">跳至论文选择</a>
<div class="page">
<header class="masthead"><a class="brand" href="#top">Daily Agent<span>研究日报<small>SELECTION PREVIEW</small></span></a><span class="edition">离线交互原型<br>合成示例 · {issue_date.isoformat()}</span></header>
<main id="top">
<section class="hero" aria-labelledby="page-title"><div><p class="eyebrow">YOUR READING, YOUR CHOICE</p><h1 id="page-title">挑选值得留存的论文</h1><p class="hero-intro">先勾选，再检查分类。你来决定哪些论文进入 Zotero。</p></div><span class="connection-status"><span class="status-dot" aria-hidden="true"></span>未连接<small>NOT CONNECTED</small></span></section>
<div class="preview-notice"><strong>仅作界面预览</strong><p>以下全部为合成示例。未连接 Zotero，未读取你的文库，也不会导入任何条目。</p></div>
<noscript><p class="noscript-note">JavaScript 未启用：选择控件保持禁用，当前为只读示例，已选 0 篇。</p></noscript>
<div class="selection-layout">
<section class="paper-list" id="paper-list" aria-labelledby="paper-list-title"><div class="section-heading"><h2 id="paper-list-title">选择论文</h2><span>{len(papers)} 篇合成示例 · 默认不选</span></div><div class="paper-cards">{''.join(cards) or empty}</div><p class="selection-footnote">所有发表标签也都是演示状态，不是来源核验结果。</p></section>
<aside class="selection-panel" aria-labelledby="preview-title"><div class="section-heading"><h2 id="preview-title">分类预览</h2><span>仅当前页面</span></div>
<div class="selection-total"><div><span id="selected-count">0</span><span class="count-label">篇已选</span></div><button id="reset-selection" class="reset-button" type="button" disabled>清空选择</button></div>
<p id="selection-status" class="selection-status" role="status" aria-live="polite" aria-atomic="true">尚未选择论文。勾选左侧示例后，可查看分类预览。</p>
<section class="collection-section" aria-labelledby="date-heading"><h3 id="date-heading">按日报日期</h3><ol class="collection-tree"><li>DailyAgent<ol><li>{issue_date.year}<ol><li><span id="date-collection">{issue_date.isoformat()}</span><span class="tree-count" id="date-count">0 篇</span></li></ol></li></ol></li></ol></section>
<section class="collection-section" aria-labelledby="topics-heading"><h3 id="topics-heading">按研究主题</h3><p class="topics-root">Topics</p><ul id="topic-collections" class="topic-collections"><li class="empty-topics">选择论文后显示主题</li></ul></section>
<p class="collection-note">以上仅是目标分类示意。未来同一条目可归入日期和主题分类，不应重复创建论文。</p>
<div class="save-area"><button type="button" id="save-to-zotero" class="save-button" disabled aria-describedby="save-help">Save to Zotero · 保存所选</button><p id="save-help">未连接，保存不可用。未来导入必须由你明确点击保存；勾选本身不会触发导入。</p></div>
</aside>
</div>
<section class="boundary-note" aria-labelledby="boundary-title"><h2 id="boundary-title">这一步，只预览你的选择</h2><p>不请求账号或密钥，不发起网络请求，不保存选择。刷新或重新打开页面，选择归零。</p><p>正式接入前，还需确认目标文库、分类规则与授权范围。</p></section>
</main>
<footer class="page-footer"><span>Daily Agent / Zotero</span><span>离线原型 · 非生产日报</span><a href="#top">回到顶部 ↑</a></footer>
</div>
</body>
</html>
'''


STYLES = r''':root{color-scheme:light dark;--bg:#f6f4ed;--card:#fffef9;--ink:#172c29;--muted:#5d6c66;--line:#ccd3c8;--accent:#14634c;--soft:#e9eee4;--warm:#f2ecd9;--focus:#925025}
*{box-sizing:border-box}html{scroll-behavior:smooth;scroll-padding-top:24px}body{margin:0;background:var(--bg);color:var(--ink);font-family:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;font-size:16px;line-height:1.8}h1,h2,h3,p{margin:0}a{color:var(--accent);text-underline-offset:5px}button,input{font:inherit}button,a,input{touch-action:manipulation}a:focus-visible,button:focus-visible,input:focus-visible{outline:3px solid var(--focus);outline-offset:5px}button:disabled{cursor:not-allowed}button{cursor:pointer}.page{max-width:1180px;margin:auto;padding:0 48px}.masthead{display:flex;justify-content:space-between;align-items:center;gap:20px;border-bottom:1px solid var(--ink);padding:26px 0 20px}.brand{display:flex;gap:14px;align-items:center;color:var(--ink);text-decoration:none;font:700 32px Georgia,serif;white-space:nowrap}.brand>span{border-left:1px solid var(--line);padding-left:14px;font:600 13px/1.6 sans-serif;letter-spacing:1.5px}.brand small{display:block;font-size:12px;letter-spacing:1px;color:var(--muted)}.edition{font-size:12px;line-height:1.9;text-align:right;color:var(--muted)}.hero{display:flex;justify-content:space-between;align-items:center;gap:28px;padding:36px 0 26px}.eyebrow{font:600 12px/1.5 sans-serif;letter-spacing:1.8px;color:var(--accent)}h1{font-size:clamp(28px,3.2vw,40px);line-height:1.4;letter-spacing:-.8px;margin:10px 0}.hero-intro{font-size:16px;color:var(--muted)}.connection-status{display:grid;grid-template-columns:10px auto;gap:0 9px;align-items:center;border:1px solid var(--line);border-radius:4px;padding:10px 16px;font-size:16px;font-weight:600;white-space:nowrap;background:var(--card)}.connection-status small{grid-column:2;font-size:12px;letter-spacing:1px;font-weight:500;color:var(--muted)}.status-dot{height:7px;width:7px;border-radius:50%;background:var(--muted)}.preview-notice{display:flex;gap:20px;align-items:baseline;background:var(--warm);border-left:3px solid var(--focus);padding:13px 18px;font-size:14px}.preview-notice strong{flex-shrink:0}.preview-notice p{color:var(--muted)}.selection-layout{display:grid;grid-template-columns:minmax(0,1fr) 350px;gap:34px;padding:32px 0}.section-heading{display:flex;justify-content:space-between;gap:12px;align-items:baseline;border-top:1px solid var(--ink);padding:15px 0 19px}.section-heading h2{font-size:17px;font-weight:650}.section-heading>span{font-size:12px;color:var(--muted)}.paper-cards{display:grid;gap:16px}.paper-card{border:1px solid var(--line);border-radius:4px;padding:21px 24px;background:var(--card);transition:border-color .12s ease,background-color .12s ease;overflow-wrap:anywhere}.paper-card.is-selected{border-color:var(--accent);background:var(--soft)}.paper-meta{display:flex;gap:12px;align-items:center}.fixture-label{font-size:12px;color:var(--muted)}.paper-selection{display:flex;gap:14px;align-items:flex-start;margin:14px 0 12px;min-height:44px;cursor:pointer}.paper-selection>span{min-width:0}.paper-checkbox{appearance:auto;accent-color:var(--accent);width:21px;height:21px;flex:none;margin:5px 0 0;cursor:pointer}.paper-title{display:block;font-size:21px;line-height:1.5;font-weight:650;letter-spacing:-.3px}.selection-hint{display:block;font-size:12px;color:var(--muted);margin-top:4px}.publication{font-size:14px;color:var(--muted);margin:0 0 10px}.publication.verified{color:var(--accent)}.sample-description{font-size:14px;line-height:1.85;color:var(--muted)}.paper-bottom{display:flex;justify-content:space-between;align-items:center;gap:10px;border-top:1px solid var(--line);margin-top:16px;padding-top:12px;font-size:12px;color:var(--muted)}.topic-chip{color:var(--accent);background:var(--soft);border-radius:3px;padding:2px 9px}.is-selected .topic-chip{background:var(--card)}.selection-footnote{font-size:12px;color:var(--muted);padding-top:14px}.selection-panel{position:sticky;top:24px;align-self:start;border:1px solid var(--line);border-radius:4px;background:var(--card);padding:0 22px 22px}.selection-panel .section-heading{border-top:0;padding:18px 0 12px;border-bottom:1px solid var(--line)}.selection-total{display:flex;align-items:center;justify-content:space-between;gap:16px;padding:19px 0 6px}#selected-count{font:52px/1.1 Georgia,serif;color:var(--accent)}.count-label{font-size:14px;color:var(--muted);margin-left:10px}.reset-button{border:0;background:transparent;color:var(--accent);text-decoration:underline;text-underline-offset:4px;min-height:44px;padding:8px 2px;font-size:14px}.reset-button:disabled{color:var(--muted);text-decoration:none}.selection-status{color:var(--muted);font-size:14px;line-height:1.8;min-height:51px;margin:8px 0 18px}.collection-section{margin:17px 0}.collection-section h3{font-size:14px;font-weight:600;margin-bottom:9px;color:var(--accent)}.collection-tree,.collection-tree ol,.topic-collections{margin:0;padding:0;list-style:none;font-size:14px}.collection-tree ol{margin:5px 0 0 7px;padding:1px 0 0 15px;border-left:1px solid var(--line)}.collection-tree li{overflow-wrap:anywhere}.tree-count{font-size:12px;color:var(--muted);margin-left:9px}.topics-root{font-size:14px}.topic-collections{margin:5px 0 0 7px;padding-left:15px;border-left:1px solid var(--line)}.topic-collections li{display:flex;justify-content:space-between;gap:10px;padding:2px 0;overflow-wrap:anywhere}.topic-collections .empty-topics{display:block;color:var(--muted);font-size:12px}.topic-collections .tree-count{flex-shrink:0}.collection-note{font-size:12px;color:var(--muted);line-height:1.85;margin:18px 0}.save-area{border-top:1px solid var(--line);padding-top:18px}.save-button{display:block;width:100%;min-height:48px;background:var(--soft);color:var(--muted);border:1px solid var(--line);border-radius:4px;font-size:14px;font-weight:600}.save-area p{font-size:12px;color:var(--muted);margin-top:12px;line-height:1.85}.boundary-note{border-top:1px solid var(--line);padding:24px 0 30px}.boundary-note h2{font-size:17px;font-weight:600;margin-bottom:8px}.boundary-note p{font-size:14px;color:var(--muted);line-height:1.9}.page-footer{display:flex;align-items:center;gap:24px;border-top:1px solid var(--line);padding:18px 0 24px;font-size:12px;color:var(--muted)}.page-footer a{margin-left:auto;display:flex;align-items:center;min-height:44px;font-size:14px}.skip-link{position:absolute;top:-100px;left:20px;background:var(--card);padding:8px;z-index:2}.skip-link:focus{top:12px}.empty-catalog{border:1px dashed var(--line);padding:24px;color:var(--muted)}.noscript-note{padding:16px 0;color:var(--focus);font-size:14px}
@media(prefers-color-scheme:dark){:root{--bg:#142320;--card:#1c2e29;--ink:#e6ece0;--muted:#adbdb3;--line:#41564b;--accent:#a7d3ae;--soft:#263c32;--warm:#36382b;--focus:#eab98a}}
@media(max-width:900px){.page{padding:0 28px}.selection-layout{grid-template-columns:minmax(0,1fr) 310px;gap:24px}.paper-card{padding:20px}.selection-panel{padding:0 18px 18px}.brand{font-size:28px}}
@media(max-width:760px){.page{padding:0 20px}.masthead{padding:20px 0 17px}.brand{font-size:26px;gap:10px}.brand>span{padding-left:10px;letter-spacing:.4px}.brand small{font-size:12px;letter-spacing:0}.edition{display:none}.hero{align-items:flex-start;flex-wrap:wrap;gap:18px;padding:26px 0 22px}h1{font-size:30px;margin:9px 0 12px}.hero-intro{line-height:1.8}.connection-status{display:flex;gap:9px;font-size:14px;padding:7px 12px}.connection-status small{font-size:12px}.preview-notice{display:block;padding:12px 15px}.preview-notice strong{display:block;margin-bottom:3px}.selection-layout{grid-template-columns:1fr;gap:26px;padding:26px 0}.section-heading{padding:13px 0 16px}.paper-card{padding:20px}.paper-title{font-size:21px}.selection-panel{position:static;padding:0 20px 20px}.selection-status{min-height:0}.selection-panel .section-heading{padding:18px 0 12px}.collection-note{font-size:14px}.save-area p{font-size:14px}.page-footer{gap:10px;flex-wrap:wrap}.page-footer>span:nth-child(2){display:none}.boundary-note{padding:22px 0}.selection-footnote{font-size:14px}}
@media(max-width:380px){.page{padding:0 16px}.brand{font-size:25px}.brand>span{font-size:12px}.brand small{letter-spacing:-.2px}.paper-card{padding:18px 16px}h1{font-size:28px}.paper-title{font-size:20px}.paper-bottom{flex-wrap:wrap}.section-heading>span{font-size:12px}}
@media(prefers-reduced-motion:reduce){html{scroll-behavior:auto}.paper-card{transition:none}}
@media print{body{background:white;color:black}.page{max-width:none;padding:0}.selection-layout{display:block}.selection-panel{position:static;margin-top:24px}.paper-card,.selection-panel{break-inside:avoid}.skip-link,.reset-button,.page-footer a{display:none}.paper-card,.selection-panel{box-shadow:none}}
'''


SCRIPT = r'''"use strict";
(() => {
  const checkboxes = Array.from(document.querySelectorAll(".paper-checkbox"));
  const count = document.getElementById("selected-count");
  const dateCount = document.getElementById("date-count");
  const status = document.getElementById("selection-status");
  const topics = document.getElementById("topic-collections");
  const reset = document.getElementById("reset-selection");
  const save = document.getElementById("save-to-zotero");

  function updatePreview() {
    const selected = checkboxes.filter((checkbox) => checkbox.checked);
    count.textContent = String(selected.length);
    dateCount.textContent = `${selected.length} 篇`;
    status.textContent = selected.length
      ? `已选择 ${selected.length} 篇合成示例，以下仅为分类预览。未连接 Zotero，未导入。`
      : "尚未选择论文。勾选上方或左侧示例后，可查看分类预览。";
    const membership = new Map();
    selected.forEach((checkbox) => {
      const topic = checkbox.dataset.topic;
      membership.set(topic, (membership.get(topic) || 0) + 1);
    });
    topics.replaceChildren();
    if (membership.size === 0) {
      const item = document.createElement("li");
      item.className = "empty-topics";
      item.textContent = "选择论文后显示主题";
      topics.append(item);
    } else {
      membership.forEach((total, topic) => {
        const item = document.createElement("li");
        const name = document.createElement("span");
        name.textContent = topic;
        const totalLabel = document.createElement("span");
        totalLabel.className = "tree-count";
        totalLabel.textContent = `${total} 篇`;
        item.append(name, totalLabel);
        topics.append(item);
      });
    }
    checkboxes.forEach((checkbox) => {
      checkbox.closest(".paper-card").classList.toggle("is-selected", checkbox.checked);
    });
    reset.disabled = selected.length === 0;
    // A local preview is never evidence of connection or import permission.
    save.disabled = true;
  }

  function clearSelection() {
    checkboxes.forEach((checkbox) => { checkbox.checked = false; });
    updatePreview();
  }

  checkboxes.forEach((checkbox) => {
    checkbox.disabled = false;
    checkbox.addEventListener("change", updatePreview);
  });
  reset.addEventListener("click", () => {
    clearSelection();
    // Reset disables itself. Keep keyboard focus at a useful, enabled control.
    if (checkboxes[0]) checkboxes[0].focus();
  });
  // Clear restored form state too: selection is intentionally page-local.
  window.addEventListener("pageshow", clearSelection);
  clearSelection();
})();
'''


def write_zotero_preview(output: Path) -> Path:
    """Write a new directory, refusing overwrites of reports or other artifacts."""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    (output / "index.html").write_text(render_zotero_preview(), encoding="utf-8")
    (output / "styles.css").write_text(STYLES, encoding="utf-8")
    (output / "preview.js").write_text(SCRIPT, encoding="utf-8")
    return output / "index.html"
