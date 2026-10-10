# Daily Agent

**以原文证据为基础的论文与开源项目日报助手。** Daily Agent 发现相关研究，阅读可获取的原始材料，核对科学论断，再把合格内容整理成中文日报，附原文图表、作者背景和进一步阅读的入口。

[English](README.md) · [文档导航](docs/README.md) · [更新与验收历史](docs/CHANGELOG.md)

## 它能做什么

- **聚焦研究方向。** 默认同时关注 AI for quantum 与 quantum for AI，并保留具身智能、VLA、世界模型和 Agent 等探索方向。领域、关键词和配额都可调整。
- **区分发表证据。** 优先考虑已核实的正式发表论文；arXiv 预印本仍可入选，但发表权重更低并明确标注。新发现论文采用最近三个自然月窗口，而不是固定 90 天。
- **长期复用论文池。** 已充分阅读、仅因当天数量上限未入选的论文跨日保留；只经过初筛的候选单独记录。缓存绑定论文版本与证据，既复用有效阅读，也不把未审候选冒充已批准论文。
- **给出有依据的科学解读。** 解释最重要的洞察、关键思路、决定性结果、适用条件与未解问题；独立复核论断支持，阅读或分析缺口明确展示。
- **保留原始图表与作者背景。** 展示绑定原 PDF 的图、表及公式裁剪，保留图注和实验条件。作者、通讯角色和研究组关系需要一手证据，未知信息不会靠猜测补齐。
- **提供适合阅读的日报与日期归档。** 输出 Markdown 和便携 HTML，支持深入阅读、来源链接及引用导出。云端由上层助手配合，可投递私有 Site 链接并保留不可变的历史日报；校准样例与正式日报分开。
- **支持恢复与防重复。** 检查点、有界重试、不可变交接、待核对投递占位和确认收据，减少丢失进度与重复发表。

默认目标为 **8 篇论文 + 2 个项目**，约 **6 条量子 + 4 条探索内容**。数量是目标，不是降低证据门槛的理由；合格日报可以少于目标。

## 工作方式

1. **发现与合并：** 从已启用的学术信源和 GitHub 收集线索，规范 DOI、arXiv 等身份，跨信源去重。
2. **筛选：** 综合相关性、发表证据、新鲜度、项目质量和反馈排序，排除已投递或投递结果尚未核实的条目。
3. **阅读与核验：** 获取允许访问的 PDF/HTML，逐块阅读并按需核对页面图像，检查引句、数字和条件，再独立复核语义支持。
4. **解读与排版：** 加入经过独立审核的科学洞察、有证据的作者背景和原始视觉材料；排版不重写已批准的科学论断，完成后封存日报。
5. **投递与核对：** 按所选流程发送封存版本，满足确认条件后才登记发表历史；不确定结果保留阻断状态，等待核对。

“读完抽取块”不等于“读完整篇论文”。配置就绪、允许入选、截图展示、投递成功，也都不同于科学质量验收。详见[阅读验收指南](scripts/VALIDATE_READING.md)。

## 选择运行方式

### 本地 / 飞书流程

原有本地优先流程使用已登录的本机 Codex CLI 进行模型写作。Codex 或 Claude Code 均可作为 MCP 客户端；配置完成后可使用本地 Markdown/HTML、cc-connect 和飞书集成，也可通过可选凭证扩展信源。

MCP 注册、凭证、反馈与调度详见[本地使用参考](docs/local-guide.zh-CN.md)。

### 公开信源、上层助手配合的云端流程

独立配置根目录显式使用 **`parent_queue`**。仓库导出不可变模型任务，由获授权的上层助手分派工作、导入独立审核结果、发布获授权的私有 HTML，并核对投递回读。仓库自身不包含消息或 Sites 凭证。

此画像禁用 Google Scholar/SerpAPI、CORE、IEEE、Unpaywall 和飞书；公开端点仍可能拒绝访问或限流。它**不代表全信源覆盖，也不是独立全自动云服务**。不要对云端根目录运行旧版 `run`、`schedule run-stage`，也不要向它套用本地完整画像。详见[云端操作与恢复](docs/cloud-migration.md)。

## 安全快速开始

需要 **Python 3.11+**。从全新 checkout 开始：

```bash
git clone https://github.com/SII-penguins/Daily_Agent.git
cd Daily_Agent
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[full,test]"
python -m daily_agent.cli quality check --root . --no-enforce-full
python -m pytest tests
```

`full` 安装 MCP、PDF、Scholar 等可选依赖，不提供凭证，也不代表运行环境已就绪。离线测试使用模拟外部传输。若只需核心包与测试，可用 `pip install -e ".[test]"`。

### 体验本地画像

先检查 `config/`，再生成不外发的预览：

```bash
python -m daily_agent.cli run --root . --date today --dry-run
python -m daily_agent.cli preview start --root . --report latest
```

**dry-run 不等于只读或离线：** `run` 会修复本地完整画像、写入本地产物、访问信源，并使用已配置的本机 Codex 写稿器。`--no-llm` 显式选择降级规则写稿，不代表完整科学验收。默认 `quality check` 和 `source check` 也会修复配置；若只检查现有配置，需加 `--no-enforce-full`。

### 初始化隔离云端画像

使用新目录；初始化拒绝覆盖已有配置：

```bash
python -m daily_agent.cloud_workflow init --source . --root data/my-cloud
DAILY_AGENT_DISABLE_EXTERNAL_SECRETS=1 python -m daily_agent.cli quality check \
  --root data/my-cloud --no-enforce-full
```

这两步只创建并检查画像，不会注册任务分派器、安装定时任务、发布 Site 或发送消息。安排调度前，请完成[云端配置](docs/cloud-migration.md)，核实信源访问、任务认领、收件人授权、备份与回读。等待上层模型响应时返回 `75` 表示可恢复检查点，不表示生成成功。

## 可选的精简论文流程

新建一期时，可显式选择[paper-first v3](docs/paper-first-v3.md)：一次完整论文阅读、中文解释与原图选择，再做一次独立的论断和图像核验。支持一轮局部修正，以及只含已合格论文的部分 HTML。它仍需上层助手配合，不会自动替换既有生产任务或发送日报。

## 配置入口

- [`config/interests.yaml`](config/interests.yaml)：研究领域、包含/扩展/排除关键词、标签与每日目标
- [`config/sources.yaml`](config/sources.yaml)：信源、发表政策、阅读及模型预算、审核、科学解读与原文视觉材料
- [`config/delivery.yaml`](config/delivery.yaml)：输出路径、保留策略、时区、投递与调度意图
- [`config/feedback.yaml`](config/feedback.yaml)：反馈及可选飞书评论集成
- [`config/research-context-evidence.json`](config/research-context-evidence.json)：已审核的一手作者/研究组证据

凭证放在环境变量或项目外部 secret 文件，不提交到 YAML。可选集成使用 `GITHUB_TOKEN`、`SEMANTIC_SCHOLAR_API_KEY`、`SERPAPI_API_KEY`、`CORE_API_KEY`、`IEEE_XPLORE_API_KEY` 和 `UNPAYWALL_EMAIL`；飞书另有凭证。[本地参考](docs/local-guide.zh-CN.md#配置)说明模板、优先级和缺失凭证的处理。云端运行使用 `DAILY_AGENT_DISABLE_EXTERNAL_SECRETS=1`，避免导入旧本地凭证。

默认调度意图是 **00:10 生成 → 05:30 复核补救 → 07:50 投递**，时区 **Asia/Shanghai**，目标为 08:00 前收到。调度预览不会安装任务。本地调度器和云端上层分派器是不同机制，都需要在实际运行环境中验证。

## 文档地图

- [本地安装、CLI、MCP 与反馈](docs/local-guide.zh-CN.md) / [English](docs/local-guide.md)
- [云端画像、交接、备份与投递](docs/cloud-migration.md)
- [研究范围与发表证据政策](docs/research-source-policy.md)
- [跨日阅读审核复用](docs/deferred-review-cache.md)
- [科学分析](docs/scientific-analysis.md) · [原图、表与公式](docs/ORIGINAL_SCIENTIFIC_ASSETS.md) · [作者及研究组证据](docs/author-research-context.md)
- [HTML 阅读界面与日期归档](docs/html-report-ui.md)
- [流程恢复](docs/workflow-recovery.md) · [投递审计](docs/cloud-delivery-audit.md)
- [阅读验收](scripts/VALIDATE_READING.md) · [写作验收](scripts/VALIDATE_WRITING.md)
- [更新与验收历史](docs/CHANGELOG.md)

## 仓库结构与安全

```text
config/             领域、信源、投递与反馈配置
src/daily_agent/    采集、阅读、审核、排版与流程代码
tests/             离线单元与回归测试
scripts/           证据与写作验收工具
docs/              使用指南、技术约定与带日期的验收记录
```

运行产物 `data/`、`reports/`、`logs/`、`tmp/` 已被 Git 忽略，不等于自动备份；可变状态与源码应分别保存。本地反馈服务监听 `127.0.0.1`。私有归档访问控制和发布属于部署责任，静态 HTML 本身不保证私密性。尊重信源访问限制，缺失证据保持可见。

截至[2026 年 10 月 8 日检查点](docs/CHANGELOG.md#2026-10-08)，修正后的隔离 Site 链接样例已确认投递。10 月 9 日首轮定时生产流程仍待运行；样例成功和测试通过不代表日常生产可靠性已验收。

## 许可

[MIT](LICENSE)

## GitHub 持久化与恢复

代码、流程和 Daily Agent 本体修改提交到 `main`；通过验收且可公开的 HTML 日报和资源保存到独立的 `daily-artifacts` 分支。分支继承仓库的公开可见性，不能当作私密备份。完整运行状态、私有投递记录及恢复凭证继续保存在私有恢复存储中。详见 [分支与恢复说明](docs/github-persistence.md) 和 [重启恢复契约](docs/durable-recovery.md)。GitHub 提交成功不等于已生成、发布或送达新日报。
