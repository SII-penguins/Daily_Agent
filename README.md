# Daily Agent

Daily Agent is a local-first research intelligence assistant that collects recent papers and GitHub projects from multiple compliant sources, ranks them with explainable signals, and generates a concise Chinese daily digest for fast reading.

[中文说明](README.zh-CN.md)

## What It Does

- Collects scholarly items from arXiv, OpenAlex, Semantic Scholar, Crossref, IEEE Xplore metadata, OpenReview, PMLR, and NeurIPS proceedings.
- Collects GitHub repositories through GitHub Search, trending pages, releases, tags, and README evidence.
- Deduplicates papers across arXiv IDs, DOI, OpenAlex IDs, Semantic Scholar IDs, IEEE article numbers, conference IDs, and title/year fallback keys.
- Scores candidates with domain relevance, freshness, top-conference signals, citations, evidence quality, GitHub quality, update signals, and user feedback.
- Produces weekly Markdown and static HTML reports with daily blocks.
- Supports Feishu delivery and Feishu comment feedback sync.
- Supports local HTML feedback buttons through a localhost-only feedback receiver.

Daily Agent does not scrape Google Scholar or IEEE web pages. Scholar-like coverage is provided through OpenAlex, Semantic Scholar, Crossref, and official publisher/conference APIs where available.

## Repository Layout

```text
config/                 Runtime configuration
src/daily_agent/        Application source code
tests/                  Unit and pipeline tests
pyproject.toml          Python package metadata
README.md              English README
README.zh-CN.md         Chinese README
LICENSE                MIT license
```

Runtime outputs such as `data/`, `reports/`, `logs/`, and `tmp/` are intentionally ignored by git.

## Installation

```bash
cd Daily_Agent
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[test]"
```

The project requires Python 3.11 or newer.

## Configuration

The default configuration lives in `config/`:

- `config/interests.yaml`: domains, keywords, tags, and quotas.
- `config/sources.yaml`: source switches, windows, limits, and optional API key environment variable names.
- `config/delivery.yaml`: report paths, retention, delivery, and schedule intent.
- `config/feedback.yaml`: feedback scoring and Feishu comment sync settings.

Optional credentials are read from environment variables, not from repository files:

```bash
export GITHUB_TOKEN="..."
export SEMANTIC_SCHOLAR_API_KEY="..."
export IEEE_XPLORE_API_KEY="..."
export DAILY_AGENT_FEISHU_APP_ID="..."
export DAILY_AGENT_FEISHU_APP_SECRET="..."
export DAILY_AGENT_FEISHU_FOLDER_TOKEN="..."
export DAILY_AGENT_FEISHU_DOC_TOKEN="..."
```

All credentials are optional except when a credential-protected source or delivery mode is enabled.

## Usage

Run a local dry run:

```bash
PYTHONPATH="./src" python3 -m daily_agent.cli run --root "." --date today --dry-run
```

Run a formal local report:

```bash
PYTHONPATH="./src" python3 -m daily_agent.cli run --root "." --date today --send local
```

Run with Feishu delivery:

```bash
PYTHONPATH="./src" python3 -m daily_agent.cli run --root "." --date today --send feishu
```

Check source connectivity without writing report state:

```bash
PYTHONPATH="./src" python3 -m daily_agent.cli source check --root "." --date today --window-days 7
```

Preview scheduler setup:

```bash
PYTHONPATH="./src" python3 -m daily_agent.cli schedule preview --root "." --backend cc-connect
```

## Agent Tool Integration

Daily Agent is not a hosted web app or an MCP server. The integration model is intentionally simple: mainstream agent tools call the local CLI, inspect generated artifacts, and optionally schedule the same commands.

### Codex

Use Codex as a local operator or coding agent inside the repository:

```bash
cd Daily_Agent
PYTHONPATH="./src" python3 -m daily_agent.cli source check --root "." --date today --window-days 7
PYTHONPATH="./src" python3 -m daily_agent.cli run --root "." --date today --dry-run --llm
PYTHONPATH="./src" python3 -m daily_agent.cli feedback show --root "." --limit 20
```

Typical Codex tasks:

- Adjust `config/interests.yaml` when your research interests change.
- Run `source check` before enabling scheduled delivery.
- Run a dry run and inspect `reports/`, `data/selected/`, and `data/state/health.json`.
- Add tests before changing connectors, scoring, rendering, or feedback behavior.
- Keep API keys in environment variables or external local config, never in prompts or repository files.

### Claude Code

Claude Code can be used in two ways:

1. As an operator, by running the same CLI commands in this repository.
2. As the optional editorial drafting helper used by `--llm`.

When `--llm` is passed, Daily Agent tries to call the local Claude Code CLI:

```bash
claude -p "<structured editorial prompt>" --output-format json
```

If the `claude` command is unavailable or returns invalid output, the pipeline falls back to the rule-based writer and still generates a report.

Useful Claude Code commands:

```bash
PYTHONPATH="./src" python3 -m daily_agent.cli run --root "." --date today --dry-run --llm
PYTHONPATH="./src" python3 -m daily_agent.cli run --root "." --date today --send feishu --llm
```

### cc-connect

If you use cc-connect for scheduling and delivery, generate cron commands first:

```bash
PYTHONPATH="./src" python3 -m daily_agent.cli schedule preview --root "." --backend cc-connect
```

Review the printed commands before enabling them. The preview is read-only.

### launchd

For macOS-native scheduling, preview LaunchAgent plist content:

```bash
PYTHONPATH="./src" python3 -m daily_agent.cli schedule preview --root "." --backend launchd
```

The command only prints plist content. You must install and load the LaunchAgent manually after review.

## Recommended Daily Workflow

The default timezone is `Asia/Shanghai`, configured in `config/delivery.yaml`.

| Time | Stage | Recommended command | What happens |
| --- | --- | --- | --- |
| 02:00 | Overnight dry run | `PYTHONPATH="./src" python3 -m daily_agent.cli run --root "." --date today --dry-run --llm` | Fetches sources, deduplicates, scores, drafts, reviews, renders local Markdown/HTML, writes dry-run selected/editorial/health artifacts, but does not publish, deliver, or mark items as formally shown. |
| 07:20 | Review / preflight window | `PYTHONPATH="./src" python3 -m daily_agent.cli feedback sync --root "." --source feishu --week latest --dry-run` and `PYTHONPATH="./src" python3 -m daily_agent.cli source check --root "." --date today --window-days 7` | Pulls rank-based Feishu comments for inspection, checks source availability, and gives the user or an agent time to inspect overnight artifacts before final delivery. There is currently no dedicated review-only pipeline command; rerun the dry run if the overnight draft is missing or clearly broken. |
| 08:00 | Formal delivery | `PYTHONPATH="./src" python3 -m daily_agent.cli run --root "." --date today --send feishu --llm` | Generates the formal report, writes weekly Markdown/HTML, writes selected JSON, marks approved materials as published, updates `published_index.json` for feedback targeting, and delivers to Feishu when configured. |

The built-in `schedule preview` currently emits the 02:00 overnight dry-run job and the 08:00 formal delivery job. The 07:20 review/preflight slot is documented in configuration and should be scheduled explicitly if you want that operational checkpoint.

## Feedback

Record structured feedback:

```bash
PYTHONPATH="./src" python3 -m daily_agent.cli feedback add --root "." --date latest --rank 3 --signal like
PYTHONPATH="./src" python3 -m daily_agent.cli feedback add --root "." --date latest --rank 6 --signal dislike
```

Record natural-language feedback:

```bash
PYTHONPATH="./src" python3 -m daily_agent.cli feedback add --root "." --text "今天第 3 条不行，第 8 条不错"
```

Run the local feedback button receiver for generated HTML reports:

```bash
PYTHONPATH="./src" python3 -m daily_agent.cli feedback serve --root "." --host 127.0.0.1 --port 8765
```

HTML buttons and Feishu/Markdown feedback links submit `date + rank + signal` to this localhost service and write idempotent feedback events.

## Testing

```bash
PYTHONPATH="./src" python3 -m pytest tests
```

## Safety Notes

- Local state, reports, logs, and demo artifacts are ignored by git.
- API keys should only be provided through environment variables or external local config.
- The feedback receiver binds to `127.0.0.1` by default.
- The tool is designed for personal research monitoring, not multi-user hosting.

## License

MIT License. See [LICENSE](LICENSE).
