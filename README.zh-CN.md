# Daily Agent

Daily Agent 是一个本地优先的科研情报日报助手。它会从多个合规来源收集最新论文和 GitHub 项目，按领域相关性、时效、顶会来源、引用、证据完整度和个人反馈进行排序，生成适合快速浏览的中文日报。

[English README](README.md)

## 功能概览

- 从 arXiv、OpenAlex、Semantic Scholar、Crossref、IEEE Xplore Metadata、OpenReview、PMLR、NeurIPS proceedings 收集论文元数据。
- 从 GitHub Search、Trending、release/tag、README 证据收集项目更新。
- 按 arXiv ID、DOI、OpenAlex ID、Semantic Scholar ID、IEEE article number、会议来源 ID、标题/年份 fallback 去重合并。
- 使用领域匹配、新鲜度、顶会/权威来源、引用影响、证据完整度、GitHub 质量、更新信号、用户反馈进行综合排序。
- 生成周级 Markdown 和静态 HTML 报告，每天一个日报块。
- 支持飞书投递和飞书评论反馈同步。
- 支持本地 HTML 按钮反馈，通过 localhost-only 服务写入反馈事件。

Daily Agent 不直接抓取 Google Scholar，也不抓取 IEEE 网页。类似 Google Scholar 的覆盖由 OpenAlex、Semantic Scholar、Crossref 以及官方出版方/会议来源补充。

## 仓库结构

```text
config/                 运行配置
src/daily_agent/        应用源码
tests/                  单元测试和流程测试
pyproject.toml          Python 包配置
README.md              英文 README
README.zh-CN.md         中文 README
LICENSE                MIT License
```

`data/`、`reports/`、`logs/`、`tmp/` 等本地运行产物不会上传到 git。

## 安装

```bash
cd Daily_Agent
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[test]"
```

要求 Python 3.11 或更高版本。

## 配置

默认配置在 `config/`：

- `config/interests.yaml`：领域、关键词、标签和配额。
- `config/sources.yaml`：数据源开关、时间窗口、数量限制和可选 API key 环境变量名。
- `config/delivery.yaml`：报告路径、保留策略、投递方式和调度时间。
- `config/feedback.yaml`：反馈评分和飞书评论同步配置。

可选凭证只从环境变量读取，不写入仓库文件：

```bash
export GITHUB_TOKEN="..."
export SEMANTIC_SCHOLAR_API_KEY="..."
export IEEE_XPLORE_API_KEY="..."
export DAILY_AGENT_FEISHU_APP_ID="..."
export DAILY_AGENT_FEISHU_APP_SECRET="..."
export DAILY_AGENT_FEISHU_FOLDER_TOKEN="..."
export DAILY_AGENT_FEISHU_DOC_TOKEN="..."
```

除了启用需要凭证的数据源或投递方式外，这些凭证都不是必需的。

## 使用

本地 dry-run：

```bash
PYTHONPATH="./src" python3 -m daily_agent.cli run --root "." --date today --dry-run
```

正式本地报告：

```bash
PYTHONPATH="./src" python3 -m daily_agent.cli run --root "." --date today --send local
```

飞书投递：

```bash
PYTHONPATH="./src" python3 -m daily_agent.cli run --root "." --date today --send feishu
```

只检查数据源连通性，不写报告状态：

```bash
PYTHONPATH="./src" python3 -m daily_agent.cli source check --root "." --date today --window-days 7
```

预览调度配置：

```bash
PYTHONPATH="./src" python3 -m daily_agent.cli schedule preview --root "." --backend cc-connect
```

## 接入主流 Agent 工具

Daily Agent 不是托管 Web 应用，也不是 MCP server。推荐的接入方式很朴素：Codex、Claude Code、cc-connect、launchd 等工具调用本地 CLI，读取生成的报告和状态文件，并按同一套命令调度运行。

### Codex

可以把 Codex 当作本地操作员或代码维护 Agent，在仓库目录里执行：

```bash
cd Daily_Agent
PYTHONPATH="./src" python3 -m daily_agent.cli source check --root "." --date today --window-days 7
PYTHONPATH="./src" python3 -m daily_agent.cli run --root "." --date today --dry-run --llm
PYTHONPATH="./src" python3 -m daily_agent.cli feedback show --root "." --limit 20
```

适合交给 Codex 的任务：

- 修改 `config/interests.yaml`，维护你的研究兴趣和关键词。
- 在启用定时投递前运行 `source check`。
- 运行 dry-run，并检查 `reports/`、`data/selected/`、`data/state/health.json`。
- 修改 connector、scoring、rendering、feedback 前先补测试。
- API key 只放在环境变量或项目外部本地配置中，不写进 prompt 或仓库文件。

### Claude Code

Claude Code 有两种用法：

1. 作为操作员，在仓库里运行同样的 CLI 命令。
2. 作为 `--llm` 启用后的可选写稿助手。

传入 `--llm` 时，Daily Agent 会尝试调用本地 Claude Code CLI：

```bash
claude -p "<structured editorial prompt>" --output-format json
```

如果没有安装 `claude`，或返回格式不合法，pipeline 会自动退回规则写稿器，日报仍会生成。

常用 Claude Code 命令：

```bash
PYTHONPATH="./src" python3 -m daily_agent.cli run --root "." --date today --dry-run --llm
PYTHONPATH="./src" python3 -m daily_agent.cli run --root "." --date today --send feishu --llm
```

### cc-connect

如果用 cc-connect 做定时和投递，先生成 cron 预览：

```bash
PYTHONPATH="./src" python3 -m daily_agent.cli schedule preview --root "." --backend cc-connect
```

这个命令只打印建议命令，不会真的安装调度任务。确认无误后再复制执行。

### launchd

macOS 原生调度可以预览 LaunchAgent plist：

```bash
PYTHONPATH="./src" python3 -m daily_agent.cli schedule preview --root "." --backend launchd
```

它只打印 plist 内容，需要你审阅后手动安装和加载。

## 推荐每日工作流

默认时区是 `Asia/Shanghai`，配置在 `config/delivery.yaml`。

| 时间 | 阶段 | 推荐命令 | 工作内容 |
| --- | --- | --- | --- |
| 02:00 | 夜间 dry-run | `PYTHONPATH="./src" python3 -m daily_agent.cli run --root "." --date today --dry-run --llm` | 拉取多源候选，去重、评分、写稿、审稿，生成本地 Markdown/HTML、dry-run selected、editorial artifacts 和 health 状态；不投递、不发布、不把条目标记为正式已展示。 |
| 07:20 | 人工/Agent 复核窗口 | `PYTHONPATH="./src" python3 -m daily_agent.cli feedback sync --root "." --source feishu --week latest --dry-run` 和 `PYTHONPATH="./src" python3 -m daily_agent.cli source check --root "." --date today --window-days 7` | 检查飞书评论反馈、数据源可用性和夜间产物质量；给用户或 Agent 留出修正关键词、反馈偏好、重跑 dry-run 的窗口。目前还没有专门的 review-only pipeline 命令，如果夜间草稿缺失或明显异常，建议重新 dry-run。 |
| 08:00 | 正式投递 | `PYTHONPATH="./src" python3 -m daily_agent.cli run --root "." --date today --send feishu --llm` | 生成正式日报，写入周级 Markdown/HTML 和 selected JSON，标记 approved materials 为已发布，更新 `published_index.json` 以支持后续按排名反馈，并在配置完整时投递到飞书。 |

当前内置的 `schedule preview` 会生成 02:00 夜间 dry-run 和 08:00 正式投递两个任务。`07:20` 复核/preflight 时间已经写在配置里，但需要你按上表显式添加对应命令。

## 反馈

结构化反馈：

```bash
PYTHONPATH="./src" python3 -m daily_agent.cli feedback add --root "." --date latest --rank 3 --signal like
PYTHONPATH="./src" python3 -m daily_agent.cli feedback add --root "." --date latest --rank 6 --signal dislike
```

自然语言反馈：

```bash
PYTHONPATH="./src" python3 -m daily_agent.cli feedback add --root "." --text "今天第 3 条不行，第 8 条不错"
```

启动本地 HTML 按钮反馈服务：

```bash
PYTHONPATH="./src" python3 -m daily_agent.cli feedback serve --root "." --host 127.0.0.1 --port 8765
```

HTML 按钮以及飞书/Markdown 中的反馈链接会把 `date + rank + signal` 提交到本地服务，并写入幂等的反馈事件。

## 测试

```bash
PYTHONPATH="./src" python3 -m pytest tests
```

## 安全说明

- 本地状态、报告、日志和 demo 产物不会上传到 git。
- API key 只应通过环境变量或项目外部本地配置提供。
- 反馈按钮服务默认只监听 `127.0.0.1`。
- 这个工具面向个人科研监控，不是多用户 Web 服务。

## License

MIT License. See [LICENSE](LICENSE).
