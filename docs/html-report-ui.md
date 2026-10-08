# Portable editorial HTML reports

`daily_agent.rendering.editorial.render_editorial_html` returns a complete UTF-8 HTML document from existing `ApprovedItem` records. It is deterministic, uses the shared approved paper-composition functions, and never calls a model or reruns evidence checks. Scientific content is not rewritten for presentation. Paper methods, results (with required conditions), limitations and evidence gaps remain available; repository maturity statements remain explicit.

The cloud delivery layer supplies the sealed original body (with local filesystem paths redacted) as `coverage`, plus `kind`, excluded count, approval hash, original-body hash and handoff identity. The renderer does not send, acknowledge or publish anything. Existing local preview/Feishu HTML and its real feedback service remain separate.

## Reader experience

- Original editorial composition inspired by the restrained reading column of [TLDR AI](https://tldr.tech/ai/2026-10-07) and consistent issue/date metadata of [Hugging Face Daily Papers](https://huggingface.co/papers)
- Warm paper / dark green visual system, dated issue, accurate counts, in-document contents, paper and repository cards
- Native keyboard-operable `details` for methods, results and evidence; offline content remains fully present
- Narrow-screen layout, system light/dark preference, visible focus, skip link, reduced-motion support and print styles
- Source links only; no local PDF/note links, external fonts/images/scripts, trackers, working-looking feedback controls, fabricated reading-time numbers or archive controls
- CSP disables remote resource loading and forms; source strings and URLs are escaped and unsafe URL schemes are rejected
- Pilot label is prominent; empty/status editions do not invent content. Original body and run/source disclosures are retained (local filesystem paths are redacted) in an expandable provenance section

A downloaded file is read-only. Feedback is given in the conversation using the visible item numbers. URLs open original sources and require network connectivity.

## Validation

Run `.venv/bin/python -m pytest -q`. Focused coverage lives in `tests/test_editorial_html.py` (determinism, retained content, safety, offline asset policy, anchors, empty states). Visual QA must cover desktop, 390px mobile, dark mode, long expanded content, navigation and repeated keyboard toggles before a renderer change ships.

## Dated archive export

`python -m daily_agent.report_archive --root STATE_ROOT --output STATIC_DIRECTORY`
exports a complete static site: `index.html`, dated `reports/YYYY-MM-DD/index.html`,
separate `examples/YYYY-MM-DD-IDENTITY/index.html`, original standalone HTML files,
content-addressed PNG assets, and `manifest.json`. Repeat `--root` to include all
verified retained state roots. No external service is contacted. The exporter never
reads or judges papers, changes source approval/delivery seals, sends a message, or
marks delivery confirmed. Past page bytes and original HTML bytes are immutable.
Existing entries remain even when a state root is omitted on a later export.

Normally only accepted/confirmed handoffs are included. Accepted delivery with
partial message/file observation remains explicitly unconfirmed. A current sealed
candidate can be staged only with all three `--current-root`, `--current-date`,
`--current-identity` arguments; exact identity must match verified prepared state.
Its audit label says website preview, chat delivery not yet accepted. Pilot entries
never populate the formal date archive. No placeholder dates or invented history.

`--base-url` optionally displays an already verified HTTPS private archive address;
it does not publish anything or establish audience access. Deployment remains a
separate caller-owned action. Deploy only static manifest-listed files plus the
manifest, excluding `.export.lock`. After every update run
`python -m daily_agent.report_archive --output STATIC_DIRECTORY --verify`.
The manifest gives SHA-256 and size of each artifact, report identity and approval
hash. Do not copy source state, conversations, recipient IDs or credentials into
the site. The caller can bind the manifest hash to a private deployment/version URL.

Report renderer v2 displays stored publication status, including explicit arXiv
preprint status; the interface never equates all eligible papers with formal
publication. Source-bound original figures, tables and formula/objective crops
use the visual-assets contract. Independent source/page/crop provenance remains
visible with each item. Standalone reports embed verified PNGs; archive pages
externalize them to hash-named local files. Source links require the network, but
figures, navigation and report text work offline. Author/lab context includes
source links and explicit unknowns rather than guessed affiliations or roles.

### Enhanced calibration verification (2026-10-08)

A separate `pilot-enhanced-2026-10-08-r3` state root was created from the verified
October 8 calibration. Source draft/review and Markdown bytes were copied intact.
Only `raw.paper_visual_selection`, `raw.research_context`, and
`reading.paper_visual_assets` were added. Removing these additions gives exact
canonical equality with the complete original approval rows and their approval
hash. Source-bound crop preparation, context building, ready-report sealing and
handoff preparation used the normal validated APIs; no reading, model calls,
review changes or forced PASS occurred. Its receipt remains `prepared`.

Actual cloud Chromium QA against the local server verified:
- All five embedded PNGs and all five archived local hash-addressed PNGs decoded
- Original figure captions, conditions, page/bbox and both hashes are readable
- Nine authors, the explicit corresponding author, and separate lab relationship
  caveats render correctly
- Report and index have no horizontal overflow at desktop width 1180 CSS pixels
  or 300% ordinary browser zoom (393 CSS pixels)
- Latest-example/date navigation, directory return, browser Back/Forward, and
  repeated keyboard Enter expansion/collapse work
- Browser zoom restored to 100%; production URL was not opened for these checks

These are layout/integrity checks, not replication of scientific results or proof
of remote publication. Deployment and recipient access remain caller-owned.

### Concise scientific reading layout (renderer v3)

The portable report uses three continuous sections: scientific insight, key idea,
and evidence/boundaries. Exact reviewed claims are grouped only within the same
provenance kind, with a soft 260-character target and breaks at whole-claim
boundaries. A long individual claim is never truncated. Provenance labels appear
at scope changes rather than before every adjacent sentence. The scientific
narrative is visible; duplicate legacy introductions, detailed methods and audit
material remain in an optional secondary panel. Original figures remain visible
with their source captions and caveats. This is a presentation boundary, not a
second scientific synthesis: the renderer reads through `reviewed_analysis` and
cannot bypass source, review or identity validation.

### Desktop/mobile reader controls (renderer v4)

The desktop directory stays in view, with its own bounded scroll region for long
issues. Below 760px, a separate native `details` directory starts closed so a
10-item edition does not displace the first article. The inactive directory uses
`display:none`; article IDs remain unique. Both versions use the same source
order and labels. Article footers return to the directory, while papers with
verified visible figures have an anchor to that figure section. Navigation,
source/PDF links and disclosure controls use at least 44px target heights.

Core narrative uses 16px system type with generous line spacing. Original crops
stay uncropped in the responsive column. Each verified figure retains its exact
caption, conditions and integrity disclosures and gains an explicit original-PDF
page link. This opens the original document; it is not a new in-page zoom viewer.
No scripts, dependencies, model calls, claim edits or evidence reselection are
introduced. The visual HTML helper does not participate in durable reading or
visual-selection protocol identity; semantic scientific validation is unchanged.

Reference rationale (desktop inspected October 8, 2026):
- [Nature article](https://www.nature.com/articles/s41586-025-09215-4): continuous narrative, numbered original figures, captions and full-size access
- [PLOS article](https://journals.plos.org/plosone/article?id=10.1371%2Fjournal.pone.0323112): persistent section navigation and an explicit figures entry
- [PLOS site help](https://journals.plos.org/plosone/s/help-using-this-site): inline figure legends and full-size figure access
- [eLife responsive design, 2014](https://elifesciences.org/inside-elife/b95bfc2a/getting-your-elife-on-the-move): historical rationale for one column, readable type and generous mobile controls

The closed mobile directory is our static adaptation, not a claim about current
mobile behavior on those sites. `tmp/html-ui/synthetic-ten-item-r7.html` is a
clearly labelled synthetic stress-test preview only and must never be included
in a published archive. Actual browser QA remains a separate release gate.

The prepublication r7a typography correction keeps narrative at 16px, raises
publication status, section labels, disclosure/source controls and figure
captions/conditions to 14px, and removes every sub-12px type declaration,
including breakpoint overrides. Secondary metadata remains at least 12px.
The already sealed r7 stays immutable and is superseded without publication;
r7a must be generated afresh from authoritative r6 scientific content.
