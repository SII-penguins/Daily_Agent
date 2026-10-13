"""Static safety tests plus opt-in real Chromium offline interaction checks.

Run without extra packages:
  PYTHONPATH=src python -m unittest discover -s tests -p test_zotero_preview.py
Browser QA (Playwright + Chromium already installed):
  DAILY_AGENT_RUN_BROWSER_TESTS=1 PYTHONPATH=src python -m unittest discover \
    -s tests -p test_zotero_preview.py
Set DAILY_AGENT_PREVIEW_SCREENSHOTS to an output directory to keep QA images.
"""
from datetime import date
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from daily_agent.rendering.zotero_preview import (
    CSP, SAMPLE_PAPERS, SCRIPT, STYLES, SyntheticPaper,
    render_zotero_preview, write_zotero_preview,
)


class Tags(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.elements = []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))


class ZoteroPreviewTests(unittest.TestCase):
    def test_default_document_is_disconnected_and_synthetic(self):
        html = render_zotero_preview()
        self.assertEqual(html, render_zotero_preview())
        for text in ("NOT CONNECTED", "未连接", "合成示例", "非生产日报", "不会导入任何条目",
                     "正式发表 · 已核验（演示状态）", "发表状态未核验（演示状态）",
                     "仅预印本 · 不代表同行评审通过（演示状态）", "DailyAgent", "Topics", "2026-10-10"):
            self.assertIn(text, html)
        self.assertNotIn("Saved", html)
        self.assertIn("勾选本身不会触发导入", html)

    def test_initial_controls_are_safe_even_without_javascript(self):
        tags = Tags(render_zotero_preview()).elements
        boxes = [attrs for tag, attrs in tags if tag == "input"]
        self.assertEqual(len(boxes), 3)
        self.assertTrue(all(b["type"] == "checkbox" and "checked" not in b and "disabled" in b for b in boxes))
        save = next(attrs for _, attrs in tags if attrs.get("id") == "save-to-zotero")
        self.assertIn("disabled", save)
        self.assertEqual(save["aria-describedby"], "save-help")
        self.assertIn("save.disabled = true", SCRIPT)
        self.assertNotIn("save.disabled = false", SCRIPT)

    def test_escape_titles_and_topics(self):
        title = '<script>alert("fixture")</script>'
        topic = '" onmouseover="bad <topic>'
        html = render_zotero_preview((SyntheticPaper(title, topic),))
        self.assertNotIn(title, html)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn('<topic>', html)
        tags = Tags(html).elements
        checkbox = next(attrs for tag, attrs in tags if tag == "input")
        self.assertEqual(checkbox["data-topic"], topic)
        self.assertFalse(any(key.startswith("on") for _, attrs in tags for key in attrs))

    def test_asset_csp_and_no_remote_or_mutating_surfaces(self):
        tags = Tags(render_zotero_preview()).elements
        self.assertIn("connect-src 'none'", CSP)
        self.assertIn("form-action 'none'", CSP)
        self.assertNotIn("unsafe-inline", CSP)
        self.assertNotIn("unsafe-eval", CSP)
        self.assertFalse({"form", "iframe", "object", "embed", "img", "style"} & {tag for tag, _ in tags})
        resources = [attrs["src"] for _, attrs in tags if "src" in attrs]
        self.assertEqual(resources, ["preview.js"])
        links = [attrs["href"] for _, attrs in tags if "href" in attrs]
        self.assertTrue(all(link == "styles.css" or link.startswith("#") for link in links))
        for forbidden in ("fetch(", "XMLHttpRequest", "WebSocket", "EventSource", "sendBeacon", "localStorage",
                          "sessionStorage", "indexedDB", "document.cookie", "innerHTML", "outerHTML", "eval(",
                          "http://", "https://", "api_key", "library_id", "PDF-", "@import", "url("):
            self.assertNotIn(forbidden, SCRIPT + STYLES + render_zotero_preview())

    def test_accessible_ids_labels_and_responsive_styles(self):
        tags = Tags(render_zotero_preview()).elements
        ids = [a["id"] for _, a in tags if "id" in a]
        self.assertEqual(len(ids), len(set(ids)))
        for _, attrs in tags:
            for field in ("for", "aria-labelledby", "aria-describedby"):
                if field in attrs:
                    self.assertTrue(all(value in ids for value in attrs[field].split()))
        self.assertIn('aria-live="polite"', render_zotero_preview())
        self.assertIn("focus-visible", STYLES)
        self.assertIn("prefers-reduced-motion", STYLES)
        self.assertIn("prefers-color-scheme:dark", STYLES)
        self.assertIn("max-width:760px", STYLES)
        self.assertIn('window.addEventListener("pageshow", clearSelection)', SCRIPT)

    def test_empty_fixture_and_custom_date(self):
        html = render_zotero_preview((), date(2030, 1, 2))
        self.assertNotIn('type="checkbox"', html)
        self.assertIn("当前没有合成示例", html)
        self.assertIn("2030-01-02", html)
        self.assertIn("2030<ol>", html)
        self.assertIn('id="selected-count">0', html)

    def test_renderer_rejects_production_objects_and_unknown_states(self):
        with self.assertRaises(TypeError):
            render_zotero_preview(({"title": "fixture"},))
        with self.assertRaises(ValueError):
            render_zotero_preview((SyntheticPaper("fixture", "topic", "connected"),))

    def test_only_three_deterministic_assets_and_no_overwrites(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "prototype"
            index = write_zotero_preview(output)
            self.assertEqual({p.name for p in output.iterdir()}, {"index.html", "styles.css", "preview.js"})
            self.assertEqual(index.read_text(encoding="utf-8"), render_zotero_preview())
            with self.assertRaises(FileExistsError):
                write_zotero_preview(output)

    def test_cli_rejects_archive_destinations(self):
        script = Path(__file__).resolve().parents[1] / "scripts" / "preview_zotero.py"
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, str(script), "--output", str(Path(directory) / "reports" / "prototype")], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn("outside reports and archives", result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node required for DOM-state regression harness")
    def test_javascript_selection_reset_and_restored_state(self):
        # A minimal DOM exercises the real generated script, without browser,
        # network, storage, dependencies, or claiming visual/keyboard QA.
        harness = r'''
const vm = require("node:vm");
const assert = require("node:assert/strict");
class Element {
  constructor() {
    this.textContent = ""; this.children = []; this.listeners = {};
    this.disabled = true; this.checked = false;
    this.classList = { toggle: (name, state) => { this.selected = state; } };
  }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  closest() { return this; }
  focus() { this.focused = true; }
}
const ids = Object.fromEntries(["selected-count", "date-count", "selection-status", "topic-collections", "reset-selection", "save-to-zotero"].map(id => [id, new Element()]));
const boxes = ["<fixture-topic>", "Another topic", "<fixture-topic>"].map(topic => {
  const box = new Element(); box.dataset = {topic}; box.checked = true; return box;
});
const document = {
  querySelectorAll: () => boxes,
  getElementById: id => ids[id],
  createElement: () => new Element(),
};
const window = new Element();
const context = {document, window};
vm.runInNewContext(SOURCE, context);
function check(index, checked) { boxes[index].checked = checked; boxes[index].listeners.change(); }
function count(expected) {
  assert.equal(ids["selected-count"].textContent, String(expected));
  assert.equal(ids["date-count"].textContent, `${expected} 篇`);
  assert.equal(ids["save-to-zotero"].disabled, true);
  assert.equal(ids["reset-selection"].disabled, expected === 0);
}
count(0);
assert.ok(boxes.every(box => !box.checked && !box.disabled));
check(0, true); count(1);
assert.equal(ids["topic-collections"].children[0].children[0].textContent, "<fixture-topic>");
check(2, true); count(2);
assert.equal(ids["topic-collections"].children.length, 1);
assert.equal(ids["topic-collections"].children[0].children[1].textContent, "2 篇");
check(1, true); count(3);
assert.equal(ids["topic-collections"].children.length, 2);
check(0, false); count(2);
check(0, true); count(3);
check(0, true); count(3);
ids["reset-selection"].listeners.click(); count(0);
assert.ok(boxes[0].focused);
assert.equal(ids["topic-collections"].children[0].textContent, "选择论文后显示主题");
check(2, true); count(1);
window.listeners.pageshow(); count(0);
assert.ok(boxes.every(box => !box.checked && !box.selected));
assert.deepEqual(Object.keys(ids["save-to-zotero"].listeners), []);
assert.equal(ids["selection-status"].textContent.includes("尚未选择论文"), true);
console.log("selection, counts, grouped topics, repeat, reset, restored-page state: passed");
'''
        result = subprocess.run([shutil.which("node"), "-e", "const SOURCE = " + json.dumps(SCRIPT) + ";\n" + harness], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("passed", result.stdout)


@unittest.skipUnless(os.environ.get("DAILY_AGENT_RUN_BROWSER_TESTS") == "1", "Opt-in Chromium UI QA")
class ZoteroPreviewBrowserTests(unittest.TestCase):
    def test_offline_desktop_mobile_keyboard_reset_repeated_and_dark(self):
        from playwright.sync_api import sync_playwright

        screenshot_dir = os.environ.get("DAILY_AGENT_PREVIEW_SCREENSHOTS")
        if screenshot_dir:
            Path(screenshot_dir).mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as directory, sync_playwright() as playwright:
            index = write_zotero_preview(Path(directory) / "prototype")
            browser = playwright.chromium.launch(
                executable_path=shutil.which("chromium"), headless=True,
                args=["--no-sandbox"], env={**os.environ, "XDG_CONFIG_HOME": directory},
            )
            context = browser.new_context(viewport={"width": 1280, "height": 1000}, color_scheme="light", offline=True)
            page = context.new_page()
            errors, requests = [], []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
            page.on("request", lambda request: requests.append(request.url))
            page.goto(index.as_uri())
            boxes = page.locator(".paper-checkbox")
            self.assertEqual(boxes.count(), len(SAMPLE_PAPERS))
            self.assertEqual(page.locator("#selected-count").inner_text(), "0")
            self.assertTrue(page.locator("#save-to-zotero").is_disabled())
            self.assertTrue(page.locator("#reset-selection").is_disabled())
            self.assertTrue(all(not boxes.nth(i).is_checked() for i in range(boxes.count())))
            if screenshot_dir:
                page.screenshot(path=str(Path(screenshot_dir) / "desktop-empty.png"), full_page=True)
            boxes.nth(0).focus()
            page.keyboard.press("Space")
            self.assertEqual(page.locator("#selected-count").inner_text(), "1")
            self.assertIn("检索与记忆", page.locator("#topic-collections").inner_text())
            boxes.nth(2).check()
            self.assertEqual(page.locator("#selected-count").inner_text(), "2")
            self.assertEqual(page.locator("#topic-collections li").count(), 1)
            self.assertIn("2 篇", page.locator("#topic-collections").inner_text())
            boxes.nth(1).check()
            self.assertEqual(page.locator("#topic-collections li").count(), 2)
            self.assertTrue(page.locator("#save-to-zotero").is_disabled())
            self.assertIn("NOT CONNECTED", page.locator(".connection-status").inner_text())
            if screenshot_dir:
                page.screenshot(path=str(Path(screenshot_dir) / "desktop-selected.png"), full_page=True)
            boxes.nth(0).uncheck()
            boxes.nth(0).check()
            boxes.nth(0).uncheck()
            self.assertEqual(page.locator("#selected-count").inner_text(), "2")
            self.assertEqual(page.locator("#date-count").inner_text(), "2 篇")
            page.locator("#reset-selection").focus()
            page.keyboard.press("Enter")
            self.assertEqual(page.locator("#selected-count").inner_text(), "0")
            self.assertEqual(page.locator(":focus").get_attribute("id"), "paper-1")
            self.assertTrue(page.locator("#reset-selection").is_disabled())
            self.assertEqual(page.locator("#topic-collections").inner_text(), "选择论文后显示主题")
            boxes.nth(0).check()
            page.reload()
            self.assertEqual(page.locator("#selected-count").inner_text(), "0")
            for width in (1280, 760, 390, 320):
                page.set_viewport_size({"width": width, "height": 900})
                self.assertTrue(page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), f"Horizontal overflow at {width}px")
                boxes.nth(1).check()
                self.assertEqual(page.locator("#selected-count").inner_text(), "1")
                self.assertTrue(page.locator("#save-to-zotero").is_disabled())
                if screenshot_dir and width == 390:
                    page.screenshot(path=str(Path(screenshot_dir) / "mobile-selected.png"), full_page=True)
                page.locator("#reset-selection").click()
            page.emulate_media(color_scheme="dark", reduced_motion="reduce")
            if screenshot_dir:
                page.screenshot(path=str(Path(screenshot_dir) / "mobile-dark-empty.png"), full_page=True)
            self.assertEqual(errors, [])
            self.assertTrue(all(url.startswith("file:") for url in requests))
            self.assertEqual(len(requests), 6)  # three local assets, then reload
            context.close()
            no_script = browser.new_context(java_script_enabled=False, offline=True)
            fallback = no_script.new_page()
            fallback.goto(index.as_uri())
            self.assertTrue(fallback.locator(".noscript-note").is_visible())
            self.assertTrue(fallback.locator(".paper-checkbox").first.is_disabled())
            self.assertTrue(fallback.locator("#save-to-zotero").is_disabled())
            no_script.close()
            browser.close()


if __name__ == "__main__":
    unittest.main()
