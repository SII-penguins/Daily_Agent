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

Install MCP support when you want Codex or Claude Code to call Daily Agent as a tool:

```bash
pip install -e ".[mcp,test]"
which daily-agent-mcp
```

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

## Quick Start for Codex and Claude Code

```bash
codex mcp add daily-agent -- daily-agent-mcp
claude mcp add daily-agent -- daily-agent-mcp
```

Then tell your agent:

```text
Start using Daily Agent. First call setup_checklist, then ask me to customize sources, topic preferences, work time nodes, and delivery method. After that, run source_check and a dry-run report.
```

Normal use does not require you to run `daily-agent-mcp` manually. Codex or Claude Code starts it when it needs the tool.

## Manual CLI Usage

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

## What the MCP Tool Gives Your Agent

Daily Agent is intended to be used like a local tool for Codex or Claude Code. After one-time MCP registration, the agent can call Daily Agent to check sources, run collection, generate reports, inspect feedback, and help you tune the configuration.

Daily Agent exposes these MCP tools:

- `setup_checklist`: tells the agent what to ask you before first use.
- `source_check`: checks enabled sources without writing report state.
- `run_digest`: runs a dry-run or formal digest.
- `schedule_preview`: prints cc-connect or launchd schedule setup.
- `feedback_show`: shows recent feedback events.
- `feedback_add_text`: records natural-language feedback.

Claude Code is also used internally when `run_digest(..., use_llm=True)` is selected. If the local `claude` command is unavailable, Daily Agent falls back to the rule-based writer.

## Daily Workflow

The default timezone is `Asia/Shanghai`, configured in `config/delivery.yaml`. Let Codex, Claude Code, cc-connect, or launchd schedule this flow:

| Time | Stage | Agent goal |
| --- | --- | --- |
| 02:00 | Overnight collection | Run `run_digest(dry_run=True, use_llm=True)` to fetch sources, deduplicate, score, draft, review, and render local Markdown/HTML without publishing. |
| 07:20 | Review checkpoint | Sync or inspect feedback, call `source_check`, and ask the user whether sources, preferences, time nodes, or delivery settings need adjustment. |
| 08:00 | Formal delivery | Run `run_digest(dry_run=False, send="feishu", use_llm=True)` to publish the formal report, update feedback targeting, and push to Feishu when configured. |

The built-in `schedule_preview` currently prints the 02:00 dry-run and 08:00 formal delivery jobs. Add a separate 07:20 checkpoint if you want the agent to explicitly review feedback and configuration before delivery.

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
