# Daily Agent

Daily Agent 是一个本地优先的科研情报日报助手。它会从多个合规来源收集最新论文和 GitHub 项目，按领域相关性、时效、顶会来源、引用、证据完整度和个人反馈进行排序，生成适合快速浏览的中文日报。

[English README](README.md)

## 功能概览

- 从 arXiv、OpenAlex、Semantic Scholar、Google Scholar、Crossref、CORE、DBLP、IEEE Xplore Metadata、OpenReview、PMLR、NeurIPS proceedings 收集论文元数据。
- 从 GitHub Search、Trending、release/tag、README 证据收集项目更新。
- 按 arXiv ID、DOI、OpenAlex ID、Semantic Scholar ID、Google Scholar ID、CORE ID、DBLP key、IEEE article number、会议来源 ID、标题/年份 fallback 去重合并。
- 使用 `keywords.expand` 中的相关术语扩展检索词，提高多学术源召回率，同时不抓取搜索结果页。
- 使用领域匹配、新鲜度、顶会/权威来源、引用影响、证据完整度、GitHub 质量、更新信号、用户反馈进行综合排序。
- 为每条入选内容生成证据化推荐理由，能说明多源交叉、全文章节覆盖、引用脉络和 Google Scholar 可追溯性等信号。
- 生成周级 Markdown 和静态 HTML 报告，每天一个日报块。论文条目会展示解决问题、方法、为什么有效、相对已有工作的“新意/差异”、结果、局限、链接和反馈入口。
- 加入轻量跨条目洞察，用于总结共同趋势、方法差异、研究空白、引用脉络和后续追踪信号。
- 使用 OpenAlex 为已批准论文补充引用上下文，让日报展示上游关键参考、下游引用论文和被引次数，而不是额外做一个引用图 UI。
- 基于历史入选或高分论文的引用邻域发现新论文，使用 OpenAlex 的 citing-paper 查询作为无需额外 key 的个性化补源。
- 在全文抽取前通过 OpenAlex 解析开放 PDF 或落地页，让只有 DOI/出版元数据的论文也更有机会进入“读全文”链路，而不是停在摘要层。
- 当配置 `UNPAYWALL_EMAIL` 后，通过 Unpaywall 解析 DOI 对应的开放 PDF 或落地页。Unpaywall 只作为补链增强层，不作为主搜索信源。
- 为已批准论文缓存验证过的开放 PDF，保存到 `data/pdfs/YYYY-MM-DD/`，并在日报里加入本地 PDF 链接，方便后续精读。
- 为入选内容自动生成 BibTeX、RIS、CSV 和 EndNote XML sidecar，和 `selected-*.json` 放在一起，方便把值得精读的条目导入 Zotero、EndNote、LaTeX、表格或自己的阅读队列。
- 支持飞书投递和飞书评论反馈同步。
- 支持本地 HTML 按钮反馈，通过 localhost-only 服务写入反馈事件。

Google Scholar 支持是可配置的：稳定的无人值守路径通过 `SERPAPI_API_KEY` 调用 SerpAPI 的 Google Scholar API；实验 fallback 可以安装 `scholarly` 做有人监督的本地尝试，但默认不在运行时启用，因为它可能触发 captcha/浏览器自动化并挂住定时任务。CORE 通过 `CORE_API_KEY` 调用官方 CORE API。DBLP 作为无 key 的计算机文献目录补源使用。IEEE 覆盖只使用官方 IEEE Xplore Metadata API。

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
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[test]"
```

要求 Python 3.11 或更高版本。

如果要让 Codex 或 Claude Code 把 Daily Agent 当作工具调用，请安装 MCP 支持：

```bash
pip install -e ".[mcp,test]"
which daily-agent-mcp
```

如果希望使用“满血版”本地能力，包括更强 PDF 解析和实验性 Google Scholar fallback：

```bash
pip install -e ".[full,test]"
```

## 配置

默认配置在 `config/`：

- `config/interests.yaml`：领域、关键词、标签和配额。
- `config/sources.yaml`：数据源开关、时间窗口、数量限制和可选 API key 环境变量名。
- `config/delivery.yaml`：报告路径、保留策略、投递方式和调度时间。
- `config/feedback.yaml`：反馈评分和飞书评论同步配置。

可选凭证从环境变量或项目外部本地 secrets 文件读取，不写入仓库文件：

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

也可以把凭证放在项目外部 TOML 文件里，再让 Daily Agent 读取：

```bash
python -m daily_agent.cli quality secrets-template --path "$HOME/.daily-agent/secrets.toml"
export DAILY_AGENT_SECRETS_FILE="$HOME/.daily-agent/secrets.toml"
```

检查凭证就绪状态，但不打印任何 secret 值：

```bash
python -m daily_agent.cli quality secrets-status
```

只生成当前缺失或仍是占位符的满血凭证模板：

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

如果没有设置 `DAILY_AGENT_SECRETS_FILE`，Daily Agent 也会检查 `$HOME/.daily-agent/secrets.toml`、`$HOME/.config/daily-agent/secrets.toml`、`$HOME/.cc-connect/config.toml`。cc-connect 的飞书 `projects.platforms.options` 配置结构也支持。环境变量永远优先于外部 secrets 文件。`replace-me` 这类占位符会被视为缺失，不会误判为真实凭证。

除了启用需要凭证的数据源或投递方式外，这些凭证都不是必需的。默认高召回配置里，Google Scholar 会优先使用 `SERPAPI_API_KEY`；没有它时会跳过 Google Scholar，除非你显式开启有人监督的 `scholarly` fallback。CORE 没有 `CORE_API_KEY` 时会跳过；Unpaywall 没有 `UNPAYWALL_EMAIL` 时会跳过 DOI 开放全文补链；这些都不阻塞日报生成。

如果只是想本地临时试一次 Google Scholar fallback，不改配置文件，可以安装 full extra 后加运行时开关：

```bash
pip install -e ".[full,test]"
python -m daily_agent.cli quality check --root "." --supervised-scholar-fallback
python -m daily_agent.cli source check --root "." --supervised-scholar-fallback
python -m daily_agent.cli run --root "." --date today --dry-run --supervised-scholar-fallback
```

这个开关只建议在本地有人看着的时候用。有人监督的 fallback 受 `google_scholar.scholarly_runtime_timeout_seconds` 运行预算保护；如果 Scholar/captcha 自动化卡住，本轮会放弃 Google Scholar 结果而不是拖住整条流水线。无人值守的 02:00/07:20/08:00 调度，优先配置 `SERPAPI_API_KEY`。

## 满血版配置

仓库内置的 `config/sources.yaml` 已按高召回个人科研雷达调优：

- arXiv、GitHub、OpenAlex、Semantic Scholar、Crossref、CORE、DBLP、IEEE、OpenReview、PMLR、NeurIPS 的候选池更大。
- GitHub Search 会覆盖多个 trending language（`python`、`typescript`、`jupyter-notebook`、`rust`），避免项目发现只局限在单一生态。
- OpenAlex、Semantic Scholar、Crossref 在满血配置下会启用 publication type 白名单：保留 article、review、conference/proceedings paper、preprint，过滤 book、chapter、dataset、editorial 等更容易带来噪声的元数据记录。
- Google Scholar 默认开启，优先 SerpAPI；实验性 `scholarly` fallback 保留配置能力，但默认不在无人值守运行时启用。
- SerpAPI Google Scholar 查询默认按日期排序（`scisbd=2`），并按每页 20 条自动分页；因此 `max_results_per_query` 大于 20 时不会被 Scholar 单页上限悄悄吞掉。
- 查询扩展默认开启。每个领域可以在 `keywords.expand` 里配置相关术语；当 `query_expansion.enabled=true` 时，这些词会合并进学术信源检索，类似 PaperLens 的同义词扩展思路，但保持本地、显式、可审计。GitHub Search 会在每个领域的查询预算内，把显式 `github_queries` 和已启用的扩展词交错使用；学术信源不会使用 GitHub 专用查询，缺少论文关键词时会回退到领域名。
- Google Scholar 结果里的 PDF resources 会按顺序保留为全文候选；如果 publisher PDF 失败，全文阶段还能继续尝试同一 Scholar 结果中的后续开放 PDF 链接。
- OpenAlex 开放获取链接解析默认开启，并且发生在全文抽取之前。对于只有 DOI、OpenAlex ID 或 arXiv DOI 线索、但没有 PDF 的论文，Daily Agent 会向 OpenAlex 查询 primary OA PDF、best OA location 和 landing page，并把这些链接写入全文/PDF 读取器可用的证据字段。
- Unpaywall DOI 解析默认开启，执行顺序在 OpenAlex OA resolver 之后、全文抽取之前。当 `UNPAYWALL_EMAIL` 存在时，Daily Agent 会调用官方 Unpaywall API，优先使用 `best_oa_location.url_for_pdf`，其次使用 `url_for_landing_page`，再把这些链接交给同一套 PDF/HTML 正文读取器和日报链接层。解析成功的 DOI 链接会缓存到 `data/cache/unpaywall.json`，后续重复或历史候选即使遇到 API 临时不可用，也可以复用已知 OA 链接。
- SerpAPI 返回的 Google Scholar 可追溯入口也会保留：cited-by、related pages、versions、cached page、cite endpoint 会进入元数据，其中 cited-by/related/versions 会展示在日报链接里。
- SerpAPI Google Scholar Cite 增强默认开启。Daily Agent 每轮最多对 50 条 Scholar 结果调用官方 `google_scholar_cite` endpoint，保存 MLA/APA/Chicago 等引用片段，并在日报里展示 Scholar BibTeX、EndNote、RefMan、RefWorks 导出链接。
- PDF/全文片段抓取默认开启，单次最多处理 50 篇论文，并提高 PDF、HTML landing page fallback 和原文扫描预算。Daily Agent 会先扫描更大的 PDF/HTML 原文窗口，再抽取最终片段，避免方法、结果、局限章节被过长 introduction 挤掉。全文阶段仍然有无人值守运行边界：下载采用流式限量读取、每篇最多尝试 4 个全文 URL、整轮全文抓取预算 180 秒；预算耗尽后剩余论文会自动降级到元数据/摘要证据，不拖死整份日报。
- OpenAlex 引用上下文默认开启，单次最多补充 30 篇论文的下游引用论文、上游关键参考和被引次数，并把这些信号交给写稿器和日报展示。
- 引用邻域发现默认开启。Daily Agent 会使用最多 20 篇历史入选或高分论文作为 seed，从 OpenAlex 为每个 seed 抓取最多 5 篇近期 citing paper，并给这些候选加入明确的 `citation_discovery` 排序信号。
- 入选 PDF 缓存默认开启，单次最多处理 10 篇已批准论文。Daily Agent 会验证下载内容确实以 PDF 头开头，把有效 OA PDF 存到 `data/pdfs/YYYY-MM-DD/`，在 Markdown/HTML 日报中加入本地 PDF 链接，并按 selected-item 保留窗口清理旧 PDF 缓存目录。
- 每篇完成全文增强的论文都会记录全文证据覆盖状态，包括来源类型、覆盖到的章节、方法/结果/局限章节笔记、缺失证据，以及是否足够支撑深度摘要。健康报告会标记已批准论文中的弱全文证据。
- 写稿字段新增 `novelty_or_difference`，用于说明论文相对已有工作、关键参考或常见 baseline 的差异，而不是只复述摘要。
- 论文入选理由不再只是评分标签，会优先说明多源交叉、全文方法/结果/局限覆盖、引用脉络影响和 Google Scholar 可追溯性。
- 规则版 `今日洞察` 已在 `config/sources.yaml` 中显式开启，会比较已批准条目，在论文列表前输出最多五条跨条目总结：共同趋势、方法差异、研究空白、引用脉络、值得追踪的信号。
- BibTeX、RIS、CSV 和 EndNote XML sidecar 导出默认开启，会在当天 selected JSON 旁生成 `selected-YYYY-MM-DD.bib/.ris/.csv/.xml` 或 `selected-YYYY-MM-DD.dry-run.bib/.ris/.csv/.xml`。
- 反馈事件和飞书评论回复同步也纳入满血配置；HTML、CLI、飞书里的喜欢/不相关反馈会继续影响后续排序，而不是停留在一次性的人工记录。
- `selection.top_candidates_for_llm` 提到 50，`paper_review_multiplier` 提到 4，让 Codex / Claude Code 在最终日报前审更多候选。
- 正式日报数量仍由 `config/interests.yaml` 控制，保持快速浏览体验。

检查当前机器是否真的处于“满血版”运行状态：

```bash
python -m daily_agent.cli quality check --root "."
```

这个质量检查会先把配置漂移修复回满血版，然后报告本地 Claude Code 写稿、PDF/HTML 正文抽取、OpenAlex OA 链接解析、Unpaywall DOI 解析、引用上下文富化、引用邻域发现、查询扩展、章节笔记抽取、今日洞察生成、高召回信源参数、Google Scholar、CORE、IEEE、飞书投递、顶会来源、02:00/07:20/08:00 工作流分别是 full、fallback、disabled 还是 missing。只有在你明确要诊断当前非满血配置时，才加 `--no-enforce-full`。

把配置漂移修回满血画像：

```bash
python -m daily_agent.cli quality enforce-full --root "." --dry-run
python -m daily_agent.cli quality enforce-full --root "." --write
```

## Codex / Claude Code 快速接入

```bash
codex mcp add daily-agent -- daily-agent-mcp
claude mcp add daily-agent -- daily-agent-mcp
```

然后直接对 Agent 说：

```text
开始使用 Daily Agent。请先调用 setup_checklist，然后提醒我自定义信源、偏好内容、工作时间节点和推送方式。配置确认后，帮我运行严格 quality_check、source_check 和一次 require_full dry-run 日报。
```

正常使用时，不需要手动先运行 `daily-agent-mcp`。Codex 或 Claude Code 需要调用工具时会自动启动它。

## 手动 CLI 用法

手动 `run` 和 Python `run_pipeline()` API 默认启用满血版 LLM 写稿路径；只有你明确要降级到规则写稿器时才加 `--no-llm` 或传 `use_llm=false`。

本地 dry-run：

```bash
python -m daily_agent.cli run --root "." --date today --dry-run
```

本地 dry-run 也要求满血运行能力：

```bash
python -m daily_agent.cli run --root "." --date today --dry-run --require-full
```

正式本地报告：

```bash
python -m daily_agent.cli run --root "." --date today --send local
```

飞书投递：

```bash
python -m daily_agent.cli run --root "." --date today --send feishu
```

正式外部投递会先执行 full-quality preflight；如果质量画像不是 full，会在发布前直接退出。只有你明确接受降级正式发送时才加 `--allow-degraded`。

只检查数据源连通性，不写报告状态：

```bash
python -m daily_agent.cli source check --root "." --date today --window-days 7
```

`source check` 会先把配置漂移修复回满血版，然后使用有界探针参数，避免 07:20 复核节点被满血候选池拖慢；真正的完整收集仍由 `run` 执行，并且有人监督的 Google Scholar fallback 也会受运行预算保护。只有调试某个非满血配置状态时，才加 `--no-enforce-full`。

检查满血版运行能力：

```bash
python -m daily_agent.cli quality check --root "."
```

如果没有 SerpAPI key，但想检查有人监督的 `scholarly` fallback 路径：

```bash
python -m daily_agent.cli quality check --root "." --supervised-scholar-fallback
```

要求所有质量能力都是 full，否则直接返回非零退出码：

```bash
python -m daily_agent.cli quality check --root "." --require-full
```

预览调度配置：

```bash
python -m daily_agent.cli schedule preview --root "." --backend cc-connect
```

## MCP 工具能让 Agent 做什么

Daily Agent 的定位是一个给 Codex / Claude Code 调用的本地工具。配置一次 MCP 后，Agent 就可以主动调用 Daily Agent 检查信源、收集信息、生成日报、查看反馈，并提醒你完善配置。

Daily Agent 暴露这些 MCP tools：

- `setup_checklist`：告诉 Agent 首次使用前应该向你确认哪些配置。
- `quality_check`：先修复满血配置，再默认要求 LLM 写稿、PDF 正文抽取、Scholar/CORE/IEEE 凭证、飞书投递、调度设置全部为 full；只有希望 Agent 报告缺口但不阻断时才传 `require_full=false`，本地有人监督地检查 Scholar fallback 时可传 `supervised_scholar_fallback=true`，只有调试配置漂移时才传 `enforce_full=false`。
- `enforce_full_profile`：报告或应用满血版配置画像，包括高召回信源参数、顶会来源列表、查询扩展、OpenAlex OA 链接解析、Unpaywall DOI 解析、全文抽取、引用上下文富化、引用邻域发现、今日洞察和 02:00/07:20/08:00 调度。
- `secrets_template`：打印或写入项目外部 TOML 凭证模板，方便 Agent 帮你补齐满血模式需要的 key，同时不碰仓库文件；传 `missing_only=true` 时只列出缺失或仍是占位符的凭证。
- `secrets_status`：检查满血凭证是否就绪，但不暴露任何 secret 值。
- `source_check`：先修复满血配置，再检查启用的数据源，不写报告状态；只有本地有人监督时才建议传 `supervised_scholar_fallback=true`，只有调试配置漂移时才传 `enforce_full=false`。
- `run_digest`：运行 dry-run 或正式日报；MCP 默认 `require_full=true`，会阻止降级 dry-run/local 运行，只有调试时才显式传 `require_full=false`；没有 SerpAPI 且本地有人监督时，可临时传 `supervised_scholar_fallback=true`。
- `schedule_preview`：生成 cc-connect 或 launchd 调度预览。
- `preview_report`：启动本地 HTML 日报预览和反馈按钮接收服务，并返回可点击的 HTML/Markdown 链接。
- `feedback_show`：查看近期反馈事件。
- `feedback_add_text`：写入自然语言反馈。

当 `run_digest(..., use_llm=True)` 时，Daily Agent 也会尝试调用本地 Claude Code 进行结构化写稿；如果 `claude` 不可用，会自动回退到规则写稿器。

## 每日工作流

默认时区是 `Asia/Shanghai`，配置在 `config/delivery.yaml`。可以让 Codex、Claude Code、cc-connect 或 launchd 按这个节奏运行：

| 时间 | 阶段 | Agent 目标 |
| --- | --- | --- |
| 02:00 | 夜间收集 | 先调用 `enforce_full_profile(write=True)`，再调用 `quality_check(require_full=True)`；只有质量画像为 full 时，才调用 `run_digest(dry_run=True, use_llm=True)` 拉取信源、去重、评分、写稿、审稿，生成本地 Markdown/HTML，但不正式发布。MCP `run_digest` 默认 `require_full=true`。 |
| 07:20 | 复核节点 | 先调用 `enforce_full_profile(write=True)`，检查或同步反馈，再调用 `quality_check` 和 `source_check`，提醒用户确认信源、偏好内容、工作时间节点、推送方式是否需要调整。 |
| 08:00 | 正式推送 | 先调用 `enforce_full_profile(write=True)`，再调用 `quality_check(require_full=True)`；只有质量画像为 full 时，才调用 `run_digest(dry_run=False, send="feishu", use_llm=True)` 生成正式日报并更新反馈索引。MCP `run_digest` 默认 `require_full=true`。 |

当前内置的 `schedule_preview` 会生成完整三个任务，并按同样顺序执行：先修复满血配置，再做对应检查，最后执行 dry-run、复核 checkpoint 或飞书正式投递。

## 反馈

正常使用时，直接让 Codex 或 Claude Code 打开日报即可，例如：

> 打开最新 Daily Agent 日报，并启用反馈按钮。

Agent 应调用 `preview_report(root=".", report="latest")`。这一个工具调用会同时启动本地 HTML 预览服务和反馈按钮接收服务，并返回可点击的日报链接。日常使用不需要你再去终端里单独启动服务。

结构化反馈：

```bash
python -m daily_agent.cli feedback add --root "." --date latest --rank 3 --signal like
python -m daily_agent.cli feedback add --root "." --date latest --rank 6 --signal dislike
```

自然语言反馈：

```bash
python -m daily_agent.cli feedback add --root "." --text "今天第 3 条不行，第 8 条不错"
```

如果你在不经过 MCP 的情况下本地调试，等价命令是：

```bash
python -m daily_agent.cli preview start --root "." --report latest
```

HTML 按钮会把 `date + rank + signal` 提交到 `preview_report` / `preview start` 启动的本地接收服务，并写入幂等的反馈事件。飞书评论也可以通过 `feedback sync` 同步。

## 测试

```bash
python -m pytest tests
```

## 安全说明

- 本地状态、报告、日志和 demo 产物不会上传到 git。
- API key 只应通过环境变量或项目外部本地配置提供。
- 反馈按钮服务默认只监听 `127.0.0.1`。
- 这个工具面向个人科研监控，不是多用户 Web 服务。

## License

MIT License. See [LICENSE](LICENSE).
