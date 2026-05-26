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
