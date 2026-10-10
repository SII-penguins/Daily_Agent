# Daily Agent

**An evidence-grounded research digest for papers and open-source projects.** Daily Agent discovers relevant work, reads available source material, checks scientific claims, and turns qualified items into a Chinese daily report with original figures, author context, and links for deeper reading.

[中文说明](README.zh-CN.md) · [Documentation](docs/README.md) · [Change and validation history](docs/CHANGELOG.md)

## What you get

- **A focused research radar.** The supplied topics balance AI for quantum and quantum for AI, alongside exploratory embodied AI, VLA, world models and agents. Domains, keywords and quotas are configurable.
- **Publication-aware discovery.** Verified formal publication is preferred; arXiv preprints remain eligible with lower publication weight and explicit labels. Newly discovered papers use a three-calendar-month window, not a fixed 90-day approximation.
- **A reusable paper pool.** Reviewed papers omitted only for daily capacity remain available across days. Screening-only overflow is retained separately. Version- and evidence-bound caches reuse valid reading work without treating an unreviewed candidate as approved.
- **Scientific explanation with traceable evidence.** Reports explain the key insight, necessary idea, decisive results, conditions and unresolved questions. Independent review checks support; missing reading or analysis is disclosed.
- **Original visual evidence and author context.** Reports can show source-bound PDF crops of figures, tables and equations, preserving captions and conditions. Author, correspondence and research-group claims require primary evidence; unknowns stay unknown.
- **Readable reports and a dated archive.** Markdown and portable HTML support deeper reading, source links and citation exports. A parent-assisted cloud deployment can deliver a private Site link and retain immutable dated editions, with calibration examples kept separate.
- **Recoverable execution.** Checkpoints, bounded retries, immutable handoffs, delivery reservations and confirmed receipts protect against lost progress and duplicate publication.

The supplied target is **8 papers + 2 repositories**, with roughly **6 quantum + 4 exploratory items**. These are targets, not permission to lower evidence standards. A qualified report may contain fewer items.

## How it works

1. **Discover and merge:** collect enabled scholarly sources and GitHub signals; normalize DOI/arXiv and other identities; deduplicate across sources.
2. **Select:** score relevance, publication evidence, freshness, repository quality and feedback; exclude already delivered or unresolved-send identities.
3. **Read and verify:** retrieve permitted PDF/HTML evidence, read chunks and required page images, check quotes/numbers/conditions, and independently review semantic support.
4. **Explain and render:** add independently reviewed scientific insight, supported author context and original visual assets; seal the report without rewriting approved claims for layout.
5. **Deliver and reconcile:** send the sealed edition through the chosen workflow. Publication history advances only after the required confirmation; ambiguous outcomes remain blocked for reconciliation.

Reading extracted chunks is not the same as reading a complete paper. Configuration readiness, inclusion, rendered screenshots and successful delivery are also distinct from scientific acceptance. See the [reading validation guide](scripts/VALIDATE_READING.md).

## Choose a runtime

### Local / Feishu workflow

The original local-first workflow uses an authenticated local Codex CLI for model writing. Codex or Claude Code can act as MCP clients; local Markdown/HTML, cc-connect and Feishu integrations remain available when configured. Optional keyed sources expand coverage.

Use the [local setup and CLI reference](docs/local-guide.md) for MCP registration, credentials, feedback and scheduling.

### Public-source, parent-assisted cloud workflow

A separate configuration root uses the explicit **`parent_queue`** provider. The repository exports immutable model jobs; an authorized parent assistant dispatches workers, imports independently reviewed results, publishes authorized private HTML, and verifies delivery readback. The repository does not contain messaging or Sites credentials.

This profile disables Google Scholar/SerpAPI, CORE, IEEE, Unpaywall and Feishu; public endpoints may still deny access or rate-limit. It is **not full-source coverage or a standalone autonomous cloud service**. Do not use legacy `run` or `schedule run-stage` on a cloud root, and do not apply the legacy full profile to it. See [cloud operations and recovery](docs/cloud-migration.md).

## Safe quick start

Requires Python **3.11+**. From a fresh checkout:

```bash
git clone https://github.com/SII-penguins/Daily_Agent.git
cd Daily_Agent
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[full,test]"
python -m daily_agent.cli quality check --root . --no-enforce-full
python -m pytest tests
```

`full` installs optional MCP/PDF/Scholar dependencies; it does not supply credentials or prove runtime readiness. Offline tests use mocked external transport. To install only the core package and tests, use `pip install -e ".[test]"`.

### Try the local profile

Review `config/` first, then preview without external delivery:

```bash
python -m daily_agent.cli run --root . --date today --dry-run
python -m daily_agent.cli preview start --root . --report latest
```

**A dry run is not read-only or offline:** `run` enforces the legacy full profile, writes local artifacts, fetches sources and uses the configured local Codex writer. `--no-llm` explicitly selects a reduced rule-based drafting path; it does not establish full scientific acceptance. Default `quality check` and `source check` also repair configuration unless `--no-enforce-full` is supplied.

### Initialize an isolated cloud profile

Use a new directory; initialization refuses to overwrite an existing configuration:

```bash
python -m daily_agent.cloud_workflow init --source . --root data/my-cloud
DAILY_AGENT_DISABLE_EXTERNAL_SECRETS=1 python -m daily_agent.cli quality check \
  --root data/my-cloud --no-enforce-full
```

This only creates and inspects the profile. It does not register a dispatcher, install a schedule, publish a Site or send a message. Complete the [cloud setup](docs/cloud-migration.md), including source access, job claims, recipient authorization, backup and readback, before scheduling. A queued-writer exit code of `75` is a resumable checkpoint, not successful generation.

## Opt-in simpler paper workflow

For a fresh issue, the [paper-first v3 workflow](docs/paper-first-v3.md) consolidates full-paper reading, explanation and visual selection into one job, followed by one independent claim-and-pixel review. It supports one bounded repair and qualified partial HTML. It is an explicit parent-assisted path; legacy production and delivery remain unchanged.

## Configuration

- [`config/interests.yaml`](config/interests.yaml): research domains, include/expand/exclude terms, tags and daily targets
- [`config/sources.yaml`](config/sources.yaml): connectors, publication policy, reading and model budgets, review, scientific analysis and visual assets
- [`config/delivery.yaml`](config/delivery.yaml): output paths, retention, timezone, delivery and scheduling intent
- [`config/feedback.yaml`](config/feedback.yaml): feedback and optional Feishu comment integration
- [`config/research-context-evidence.json`](config/research-context-evidence.json): reviewed primary-source author/group context

Keep credentials in environment variables or an external secret file, never in committed YAML. Optional integrations use `GITHUB_TOKEN`, `SEMANTIC_SCHOLAR_API_KEY`, `SERPAPI_API_KEY`, `CORE_API_KEY`, `IEEE_XPLORE_API_KEY` and `UNPAYWALL_EMAIL`; Feishu has separate credentials. The [local reference](docs/local-guide.md#configuration) explains templates, precedence and missing-key behavior. Cloud operation uses `DAILY_AGENT_DISABLE_EXTERNAL_SECRETS=1` to avoid importing legacy secrets.

The default schedule intent is **00:10 generation → 05:30 review/recovery → 07:50 delivery**, in **Asia/Shanghai**, aiming for delivery before 08:00. Schedule previews do not install jobs. Local schedulers and a cloud parent dispatcher are separate mechanisms, and must be verified in their actual environment.

## Documentation map

- [Setup, CLI, MCP and feedback](docs/local-guide.md) / [中文](docs/local-guide.zh-CN.md)
- [Cloud profile, handoffs, backups and delivery](docs/cloud-migration.md)
- [Research scope and publication policy](docs/research-source-policy.md)
- [Cross-day review reuse](docs/deferred-review-cache.md)
- [Scientific analysis](docs/scientific-analysis.md) · [Original figures/tables](docs/ORIGINAL_SCIENTIFIC_ASSETS.md) · [Author/group evidence](docs/author-research-context.md)
- [HTML reader and dated archive](docs/html-report-ui.md)
- [Workflow recovery](docs/workflow-recovery.md) · [Delivery audit](docs/cloud-delivery-audit.md)
- [GitHub source/artifact branches and restore](docs/github-persistence.md) · [Restart-safe private state](docs/durable-recovery.md)
- [Reading validation](scripts/VALIDATE_READING.md) · [Writing validation](scripts/VALIDATE_WRITING.md)
- [Change and validation history](docs/CHANGELOG.md)

## Project layout and safety

```text
config/             Topic, source, delivery and feedback configuration
src/daily_agent/    Collectors, reading, review, rendering and workflow code
tests/             Offline unit and regression tests
scripts/           Evidence and writing validation utilities
docs/              Guides, technical contracts and dated validation records
```

Code and workflow changes belong on `main`; accepted public-safe HTML editions and assets belong on the separate `daily-artifacts` branch. Branches inherit this repository’s public visibility. Complete private runtime snapshots and verified recovery receipts remain outside GitHub. See [persistence and restore](docs/github-persistence.md).

Runtime `data/`, `reports/`, `logs/` and `tmp/` are ignored by Git. They are not automatically backed up; preserve mutable state separately from source code. Local feedback listens on `127.0.0.1`. Private archive access and publication are deployment responsibilities, not guarantees provided by static HTML. Respect source access restrictions; missing evidence stays visible.

The [8 October 2026 checkpoint](docs/CHANGELOG.md#2026-10-08) records the isolated Site-link pilot. Later native-evidence and restart-recovery work has offline validation, with its boundaries documented in the technical guides. Passing tests, a stored code commit and a pilot do not establish unattended daily production reliability or delivery of a new edition.

## License

[MIT](LICENSE)
