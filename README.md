# Daily Agent

Daily Agent is a local-first research intelligence assistant that collects recent papers and GitHub projects from multiple compliant sources, ranks them with explainable signals, and generates a concise Chinese daily digest for fast reading.

[中文说明](README.zh-CN.md)

## Workflow recovery

Scheduled and manual external delivery share immutable report snapshots, an outbox, and confirmed receipts. Publication history is committed only after delivery confirmation. Retries are bounded across triggers; permanent errors and ambiguous delivery results open a circuit. Morning recovery uses the remaining review window. Expensive reading runs in small batches while retaining a broad candidate pool. Both paper and repository quotas must be met before early completion. `schedule status` is read-only; `schedule repair` reconciles under a lease; `schedule resolve-delivery` records an operator-confirmed outcome without sending anything. See [the workflow recovery guide](docs/workflow-recovery.md). Local fault injection does not validate live delivery or install an independent monitoring daemon.

The recovery follow-up adds retry timing across controller restarts, local delivery preflight before writing the outbox, and strict validation of formal selection/history records. Missing tools or unavailable delivery configuration do not create ambiguous-send records; failures after invoking the adapter still require remote reconciliation. Preview filenames always remain excluded from publication history.

The process cleanup follow-up applies lessons from Supervisor and psutil. Missing leaders or unconfirmed termination retain process evidence and block new same-day stages as `cleanup_pending`; version changes and budget resets cannot bypass cleanup. Read-only status explains the blocker; confirmed cleanup preserves attempts and checkpoints. Process identity remains a coarse snapshot, and descendants that escape the recorded group are not automatically reclaimed.

## Reading recovery fixes (2026-10-02)

Full-text retrieval follows explicit PDF metadata on landing pages and retries abstract/partial caches. Parser upgrades first reuse hash-verified local PDFs. Page transcription and independent review resume from checkpoints; bounded corrections can include the original page and detail crops. Fidelity uses a 7200-second stage budget and 420-second call timeout. Budget changes preserve verified reading caches.

Acceptance distinguishes reading all extracted chunks from reading a complete document. Full acceptance requires complete source pages, visual fidelity, and supported claims. Fully reviewed page images can resolve native OCR failures while retaining native evidence. Access denial, exhausted budgets, and unreadable symbols remain explicit gaps; inclusion or successful delivery does not establish full acceptance.

## Evidence-based reading

Papers retain page/section locations and are read in chunks before synthesis. Quotes, numbers and result conditions are checked, followed by a separate model pass for semantic support. Daily cards disclose coverage and gaps; detailed local notes live under `reports/notes/` and are not uploaded automatically. Incomplete reading is never labelled complete, and papers without located problem/method evidence are not published.

Configure budgets under `reading` in `config/sources.yaml`. Budget exhaustion is disclosed. Version, content and reading configuration changes invalidate caches; unchanged published papers are suppressed by default. Configuration readiness from `quality check` is not a certification of reading or factual accuracy; inspect per-item status and health reading metrics.

## Evidence repair and acceptance

Page screenshots are presentation artifacts; enabling them does not skip visual verification. Image transcription receives a separate review and at most one feedback-guided repair. Failed quote, number or semantic checks can trigger one rewrite using bounded, already-read source chunks. All repaired claims must pass the same checks.

Audit an existing approval snapshot without calling a model or publishing:

```bash
python scripts/audit_evidence.py \
  --input data/editorial/YYYY-MM-DD/approval.json \
  --output tmp/evidence-audit.json
```

The audit distinguishes extracted-chunk completion, full-text reading, visual fidelity and claim support. Inclusion is not full quality acceptance. OpenReview HTTP 401/403 remains an explicit source failure; other sources cannot substitute for access to its reviews.

Validation on 2026-09-30: a real single-page image/transcription/independent-review cycle passed after one correction (five API calls). This does not certify a whole paper. The eight-paper snapshot still has unresolved gaps: four complete-body readings, three abstract-only documents and one structurally incomplete PDF. Full corpus quality acceptance has not passed; limited-evidence labels remain.

See [reading validation](scripts/VALIDATE_READING.md) and [writing validation](scripts/VALIDATE_WRITING.md). Live validation uses the configured model and may incur API charges; outputs remain local.

## What It Does

- Collects scholarly items from arXiv, OpenAlex, Semantic Scholar, Google Scholar, Crossref, CORE, DBLP, IEEE Xplore metadata, OpenReview, PMLR, and NeurIPS proceedings.
- Collects GitHub repositories through GitHub Search, trending pages, releases, tags, and README evidence.
- Deduplicates papers across arXiv IDs, DOI, OpenAlex IDs, Semantic Scholar IDs, Google Scholar IDs, CORE IDs, DBLP keys, IEEE article numbers, conference IDs, and title/year fallback keys.
- Expands configured topic keywords with related terms from `keywords.expand`, improving recall across scholarly sources without scraping search result pages.
- Scores candidates with domain relevance, freshness, top-conference signals, citations, evidence quality, GitHub quality, update signals, and user feedback.
- Explains each selected item with evidence-aware recommendation reasons, including multi-source agreement, full-text section coverage, citation context, and Scholar traceability when available.
- Produces weekly Markdown and static HTML reports with daily blocks. Paper entries include problem, method, why it works, novelty/difference from prior work, results, limitations, links, and feedback controls.
- Adds a compact cross-item insight section for shared trends, method differences, research gaps, citation context, and follow-up signals.
- Enriches approved papers with OpenAlex citation context, so reports can show upstream references, downstream citing papers, and citation counts without adding a separate graph UI.
- Discovers new papers from the citation neighborhood of previously selected or high-scoring papers, using OpenAlex citing-paper queries as a no-key personalization source.
- Resolves open-access PDF or landing-page links through OpenAlex before full-text extraction, increasing the chance that DOI-only papers can still be read beyond their abstract.
- Resolves DOI-based open-access PDFs and landing pages through Unpaywall when `UNPAYWALL_EMAIL` is configured. Unpaywall is used as an enrichment layer, not as a primary search source.
- Caches verified open-access PDFs for approved papers under `data/pdfs/YYYY-MM-DD/` and adds a local PDF link to the report when a valid PDF is available.
- Writes BibTeX, RIS, CSV, and EndNote XML sidecars next to `selected-*.json`, so promising items can move into Zotero, EndNote, LaTeX, spreadsheets, or a reading queue without manual retyping.
- Supports Feishu delivery and Feishu comment feedback sync.
- Supports local HTML feedback buttons through a localhost-only feedback receiver.

Google Scholar support is configurable. The stable unattended path uses SerpAPI's Google Scholar API through `SERPAPI_API_KEY`. An experimental `scholarly` fallback can be installed for supervised local use, but runtime use is disabled by default because it may trigger captcha/browser automation and hang scheduled jobs. CORE uses the official CORE API through `CORE_API_KEY`. DBLP is used as a no-key computer-science bibliography source. IEEE coverage uses the official IEEE Xplore Metadata API only.

## Repository Layout

```text
config/                 Runtime configuration
src/daily_agent/        Application source code
tests/                  Unit and pipeline tests
docs/                   Workflow recovery guide
scripts/                Local reading and writing validation
pyproject.toml          Python package metadata
README.md              English README
README.zh-CN.md         Chinese README
LICENSE                MIT license
```

Runtime outputs such as `data/`, `reports/`, `logs/`, and `tmp/` are intentionally ignored by git.

## Installation

```bash
cd Daily_Agent
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[test]"
```

The project requires Python 3.11 or newer.

Install MCP support when you want Codex or Claude Code to call Daily Agent as a tool:

```bash
pip install -e ".[mcp,test]"
which daily-agent-mcp
```

Install the full-quality local profile when you want stronger PDF extraction and experimental Google Scholar fallback:

```bash
pip install -e ".[full,test]"
```

## Configuration

The default configuration lives in `config/`:

- `config/interests.yaml`: domains, keywords, tags, and quotas.
- `config/sources.yaml`: source switches, windows, limits, and optional API key environment variable names.
- `config/delivery.yaml`: report paths, retention, delivery, and schedule intent.
- `config/feedback.yaml`: feedback scoring and Feishu comment sync settings.

Optional credentials are read from environment variables or external local secret files, not from repository files:

```bash
export GITHUB_TOKEN="..."
export SEMANTIC_SCHOLAR_API_KEY="..."
export SERPAPI_API_KEY="..."
export IEEE_XPLORE_API_KEY="..."
export CORE_API_KEY="..."
export UNPAYWALL_EMAIL="you@example.com"
export DAILY_AGENT_FEISHU_APP_ID="..."
export DAILY_AGENT_FEISHU_APP_SECRET="..."
export DAILY_AGENT_FEISHU_FOLDER_TOKEN="..."
export DAILY_AGENT_FEISHU_DOC_TOKEN="..."
```

You can also keep credentials in an external TOML file and point Daily Agent to it:

```bash
python -m daily_agent.cli quality secrets-template --path "$HOME/.daily-agent/secrets.toml"
export DAILY_AGENT_SECRETS_FILE="$HOME/.daily-agent/secrets.toml"
```

Check credential readiness without printing secret values:

```bash
python -m daily_agent.cli quality secrets-status
```

Generate a template containing only missing or placeholder full-quality credentials:

```bash
python -m daily_agent.cli quality secrets-template --missing-only --path "$HOME/.daily-agent/missing-secrets.toml"
```

```toml
[daily_agent.env]
GITHUB_TOKEN = "..."
SEMANTIC_SCHOLAR_API_KEY = "..."
SERPAPI_API_KEY = "..."
IEEE_XPLORE_API_KEY = "..."
CORE_API_KEY = "..."
UNPAYWALL_EMAIL = "you@example.com"

[feishu]
app_id = "..."
app_secret = "..."
folder_token = "..."
doc_token = "..."
```

When `DAILY_AGENT_SECRETS_FILE` is not set, Daily Agent also checks `$HOME/.daily-agent/secrets.toml`, `$HOME/.config/daily-agent/secrets.toml`, and `$HOME/.cc-connect/config.toml`. The cc-connect Feishu `projects.platforms.options` shape is supported. Environment variables always override external secret files. Placeholder values such as `replace-me` are treated as missing.

All credentials are optional except when a credential-protected source or delivery mode is enabled. With the default high-recall configuration, Google Scholar uses `SERPAPI_API_KEY` when present; without it, Google Scholar is skipped unless you explicitly enable the supervised `scholarly` fallback. CORE is skipped unless `CORE_API_KEY` is available. Unpaywall OA-link enrichment is skipped unless `UNPAYWALL_EMAIL` is available.

For a one-off supervised Google Scholar experiment without editing config, install the full extra and pass the runtime flag:

```bash
pip install -e ".[full,test]"
python -m daily_agent.cli quality check --root "." --supervised-scholar-fallback
python -m daily_agent.cli source check --root "." --supervised-scholar-fallback
python -m daily_agent.cli run --root "." --date today --dry-run --supervised-scholar-fallback
```

Use this only for local supervised runs. The supervised fallback has a runtime budget (`google_scholar.scholarly_runtime_timeout_seconds`) and returns no Google Scholar items for that run if Scholar/captcha automation stalls. For unattended 00:10/05:30/07:50 schedules, prefer `SERPAPI_API_KEY`.

## Full-Quality Profile

The checked-in `config/sources.yaml` is tuned for a high-recall personal radar:

- Larger candidate pools for arXiv, GitHub, OpenAlex, Semantic Scholar, Crossref, CORE, DBLP, IEEE, OpenReview, PMLR, and NeurIPS.
- GitHub search includes multiple trending languages (`python`, `typescript`, `jupyter-notebook`, and `rust`) so project discovery is not limited to a single ecosystem.
- OpenAlex, Semantic Scholar, and Crossref use publication-type allowlists in the full profile, keeping articles, reviews, conference/proceedings papers, and preprints while filtering lower-value metadata records such as books, chapters, datasets, and editorials when the source exposes a type field.
- Google Scholar enabled through SerpAPI first; the optional `scholarly` fallback is installed/configurable but runtime-disabled by default for unattended runs.
- SerpAPI Google Scholar queries are date-sorted by default (`scisbd=2`) and paginated in 20-result pages, so `max_results_per_query` above 20 is honored instead of silently collapsing to one Scholar page.
- Query expansion enabled. Each domain can define `keywords.expand` terms that are merged into scholarly source queries when `query_expansion.enabled=true`, similar to PaperLens-style synonym expansion but fully local and explicit. GitHub search interleaves explicit `github_queries` with enabled expansion terms, while scholarly sources ignore GitHub-only queries and fall back to the domain name when no paper keywords are configured.
- Google Scholar PDF resources are preserved as ordered full-text candidates, so if a publisher PDF fails, later open-access PDF links from the same Scholar result can still be tried by the full-text stage.
- OpenAlex open-access link resolving is enabled before full-text extraction. For DOI, OpenAlex, or arXiv-backed papers that do not already have a PDF, Daily Agent asks OpenAlex for the primary OA PDF, best OA location, and landing page, then stores those links as evidence for the PDF/HTML reader.
- Unpaywall DOI resolving is enabled after the OpenAlex resolver and before full-text extraction. When `UNPAYWALL_EMAIL` is present, Daily Agent asks the official Unpaywall API for `best_oa_location.url_for_pdf` first and `url_for_landing_page` second, then feeds those links into the same PDF/HTML reader and report-link layer. Resolved DOI links are cached under `data/cache/unpaywall.json`, so repeated or historical candidates can reuse known OA links even when the API is unavailable for a later run.
- Google Scholar traceability links are preserved when SerpAPI returns them: cited-by, related pages, versions, cached page, and cite endpoints are kept in metadata, with cited-by/related/versions exposed in the report links.
- SerpAPI Google Scholar Cite enrichment is enabled. For up to 50 Scholar results per run, Daily Agent calls the official `google_scholar_cite` endpoint, stores MLA/APA/Chicago-style snippets when returned, and exposes Scholar BibTeX/EndNote/RefMan/RefWorks export links in the report.
- PDF/full-text excerpt extraction enabled for up to 50 papers per run, with larger PDF and HTML landing-page budgets. Daily Agent scans a larger raw text window before selecting the final excerpt, so Methods, Results, and Limitations sections are less likely to be hidden behind a long introduction. The full-text stage is still bounded for unattended operation: it streams downloads, tries at most four full-text URLs per paper, and uses a 180-second run budget before gracefully downgrading remaining papers to metadata/abstract evidence.
- OpenAlex citation-context enrichment enabled for up to 30 papers per run, adding top downstream citing papers and key upstream references to the writing evidence and report.
- Citation-neighborhood discovery enabled. Daily Agent uses up to 20 previously selected or high-scoring seed papers, fetches up to five recent citing papers per seed from OpenAlex, and gives those candidates an explicit `citation_discovery` ranking signal.
- Selected PDF caching is enabled for up to 10 approved papers per run. Daily Agent verifies that downloaded content starts with a PDF header, stores valid OA PDFs under `data/pdfs/YYYY-MM-DD/`, adds a local PDF link to the Markdown/HTML report, and cleans old PDF cache directories with the selected-item retention window.
- Full-text evidence coverage is recorded for each enriched paper, including source type, section coverage, method/results/limitations notes, missing evidence, and whether the excerpt is strong enough for a deep summary. Run health flags approved papers whose full-text evidence is weak.
- Writer outputs include `novelty_or_difference`, so each paper can explain how it differs from prior work, key references, or common baselines instead of only restating the abstract.
- Paper recommendation reasons are evidence-aware rather than label-only: reports can say when an item has multi-source agreement, full-text method/result/limitation coverage, citation-context impact, and Google Scholar traceability.
- A rule-based daily insight layer is explicitly enabled in `config/sources.yaml`; it compares approved items and adds up to five cross-item notes before the paper list: common trends, method differences, research gaps, citation-context signals, and follow-up signals.
- BibTeX, RIS, CSV, and EndNote XML sidecar exports are enabled, writing `selected-YYYY-MM-DD.bib/.ris/.csv/.xml` or `selected-YYYY-MM-DD.dry-run.bib/.ris/.csv/.xml` alongside the selected JSON for the day.
- Feedback events and Feishu comment reply sync are part of the full-quality profile, so likes/dislikes from HTML, CLI, or Feishu can keep shaping future ranking instead of becoming a separate manual chore.
- `selection.top_candidates_for_llm` set to 50 and `paper_review_multiplier` set to 4 so Codex/Claude Code can review more candidates before the final digest.
- Final report size remains capped by `config/interests.yaml` so the digest stays readable.

Check whether the current machine is actually running this full-quality profile:

```bash
python -m daily_agent.cli quality check --root "."
```

The quality check first repairs config drift back to the full-quality profile, then reports whether the configured local LLM writer, PDF/HTML text extraction, OpenAlex OA link resolving, Unpaywall DOI resolving, citation-context enrichment, citation-neighborhood discovery, query expansion, section-note extraction, daily insight synthesis, high-recall source limits, Google Scholar, CORE, IEEE, Feishu delivery, major conference sources, and the 00:10/05:30/07:50 workflow are full, fallback, disabled, or missing. Add `--no-enforce-full` only when you intentionally want to inspect the current config without repairing it.

Repair config drift back to the full-quality profile:

```bash
python -m daily_agent.cli quality enforce-full --root "." --dry-run
python -m daily_agent.cli quality enforce-full --root "." --write
```

## Quick Start for Codex and Claude Code

```bash
codex mcp add daily-agent -- daily-agent-mcp
claude mcp add daily-agent -- daily-agent-mcp
```

Then tell your agent:

```text
Start using Daily Agent. First call setup_checklist, then ask me to customize sources, topic preferences, work time nodes, and delivery method. After that, run strict quality_check, source_check, and a require_full dry-run report.
```

Normal use does not require you to run `daily-agent-mcp` manually. Codex or Claude Code starts it when it needs the tool.

## Manual CLI Usage

Manual `run` and the Python `run_pipeline()` API use the full-quality LLM drafting path by default. Add `--no-llm` or pass `use_llm=false` only when you explicitly want the rule-based fallback.

Run a local dry run:

```bash
python -m daily_agent.cli run --root "." --date today --dry-run
```

Require full-quality readiness even for a local dry run:

```bash
python -m daily_agent.cli run --root "." --date today --dry-run --require-full
```

Run a formal local report:

```bash
python -m daily_agent.cli run --root "." --date today --send local
```

Run with Feishu delivery:

```bash
python -m daily_agent.cli run --root "." --date today --send feishu
```

Formal external delivery runs a full-quality preflight first. If the profile is not full, the command exits before publishing. Use `--allow-degraded` only for an intentional emergency override.

Check source connectivity without writing report state:

```bash
python -m daily_agent.cli source check --root "." --date today --window-days 7
```

`source check` first repairs config drift back to the full-quality profile, then uses bounded probe limits so the review checkpoint can return quickly even when the full digest profile has high-recall source limits. Full collection still happens in `run`, where the supervised Google Scholar fallback is also protected by its runtime budget. Add `--no-enforce-full` only when debugging a specific non-full config state.

Check full-quality runtime readiness:

```bash
python -m daily_agent.cli quality check --root "."
```

Check the supervised `scholarly` fallback path when SerpAPI is not configured:

```bash
python -m daily_agent.cli quality check --root "." --supervised-scholar-fallback
```

Fail fast unless every quality capability is full:

```bash
python -m daily_agent.cli quality check --root "." --require-full
```

Preview scheduler setup:

```bash
python -m daily_agent.cli schedule preview --root "." --backend cc-connect
```

## What the MCP Tool Gives Your Agent

Daily Agent is intended to be used like a local tool for Codex or Claude Code. After one-time MCP registration, the agent can call Daily Agent to check sources, run collection, generate reports, inspect feedback, and help you tune the configuration.

Daily Agent exposes these MCP tools:

- `setup_checklist`: tells the agent what to ask you before first use.
- `quality_check`: repairs the full-quality profile, then requires LLM drafting, PDF extraction, Scholar/CORE/IEEE credentials, Feishu delivery, and schedule settings to be full by default. Pass `require_full=false` only when the agent should report gaps without blocking, `supervised_scholar_fallback=true` for a local supervised Scholar fallback check, and `enforce_full=false` only for config drift debugging.
- `enforce_full_profile`: reports or applies the full-quality config profile, including high-recall source limits, conference source lists, query expansion, OpenAlex OA link resolving, Unpaywall DOI resolving, full-text extraction, citation-context enrichment, citation-neighborhood discovery, insight generation, and the 00:10/05:30/07:50 schedule.
- `secrets_template`: prints or writes an external TOML credential template so the agent can help you fill the keys needed for full mode without touching repository files. Pass `missing_only=true` to include only missing or placeholder credentials.
- `secrets_status`: reports full-quality credential readiness without exposing secret values.
- `source_check`: repairs the full-quality profile, then checks enabled sources without writing report state. Pass `supervised_scholar_fallback=true` only for local supervised Scholar fallback checks, and `enforce_full=false` only for config drift debugging.
- `run_digest`: runs a dry-run or formal digest. MCP defaults to `require_full=true`, so degraded dry-run/local runs are blocked unless you explicitly pass `require_full=false` for debugging. Pass `supervised_scholar_fallback=true` only for supervised local runs without SerpAPI.
- `schedule_preview`: prints cc-connect or launchd schedule setup.
- `preview_report`: starts the local report preview and feedback-button receiver, then returns clickable HTML/Markdown URLs.
- `feedback_show`: shows recent feedback events.
- `feedback_add_text`: records natural-language feedback.

When `run_digest(..., use_llm=True)` is selected, Daily Agent always uses the authenticated local `codex` CLI for its internal summarization and full-text writing. Claude Code can still call Daily Agent through MCP, but it is only the outer tool client and is not the report writer. Full-text drafting allows up to 10 minutes per batch and four hours for the whole writing stage, fitting inside the 00:10-to-05:30 preparation window. Prompts cap each evidence excerpt, require concise typed JSON, and use low reasoning effort to avoid wasting that window. The first backend failure still opens a circuit breaker so a broken model connection cannot block the entire morning run; runtime health records this fallback explicitly.

## Daily Workflow

The default timezone is `Asia/Shanghai`, configured in `config/delivery.yaml`. Let Codex, Claude Code, cc-connect, or launchd schedule this flow:

| Time | Stage | Agent goal |
| --- | --- | --- |
| 00:10 | Overnight generation | Collect candidates, read evidence, draft and review items, and seal the approved daily report. |
| 05:30 | Review and recovery | Recover a missing snapshot within the remaining review window; sync feedback and check sources. |
| 07:50 | Delivery | Send the sealed report without regenerating it. The launchd preview includes retries at 07:55 and 07:58; confirmed receipts suppress duplicate sends. Target arrival is before 08:00. |

`schedule preview` prints the three workflow jobs without installing them. Each job invokes `schedule run-stage` for its stage; the controller handles prerequisites, bounded recovery, and delivery confirmation. See the [recovery guide](docs/workflow-recovery.md) for status and operator actions.

## Feedback

For normal use, ask Codex or Claude Code to open the report, for example:

> Open the latest Daily Agent report and enable feedback buttons.

The agent should call `preview_report(root=".", report="latest")`. That single tool call starts the local HTML preview server and the local feedback-button receiver, then returns clickable report links. You do not need to start a separate terminal service for everyday use.

Record structured feedback:

```bash
python -m daily_agent.cli feedback add --root "." --date latest --rank 3 --signal like
python -m daily_agent.cli feedback add --root "." --date latest --rank 6 --signal dislike
```

Record natural-language feedback:

```bash
python -m daily_agent.cli feedback add --root "." --text "今天第 3 条不行，第 8 条不错"
```

For local debugging without MCP, the equivalent command is:

```bash
python -m daily_agent.cli preview start --root "." --report latest
```

HTML buttons submit `date + rank + signal` to the localhost receiver started by `preview_report` / `preview start` and write idempotent feedback events. Feishu comments can also be synced through `feedback sync`.

## Testing

```bash
python -m pytest tests
```

## Safety Notes

- Local state, reports, logs, and demo artifacts are ignored by git.
- API keys should only be provided through environment variables or external local config.
- The feedback receiver binds to `127.0.0.1` by default.
- The tool is designed for personal research monitoring, not multi-user hosting.

## License

MIT License. See [LICENSE](LICENSE).
