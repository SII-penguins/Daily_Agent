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
