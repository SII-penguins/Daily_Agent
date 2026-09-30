# 真实 API 写作验收

`validate_writing_api.py` 使用已收集的固定素材，复用生产代码的写手提示词、结构验证、引句/数字检查、独立语义审核、批准、笔记及 Markdown/HTML 编排。它不触发实时采集，不调用外部 CLI，也不投递或登记发表。

凭据从命令指定的本地文件读取，格式为 `baseurl: ...` 与 `apikey: ...`；默认 `secret.md` 已被 Git 忽略。支持 HTTPS 基址、`/v1` 基址及完整 `/v1/chat/completions` URL。密钥仅放入请求头；调用证据不保存密钥。不能把密钥作为命令行参数传入。

每次运行最多 80 次新 API 调用；每个阅读块和每篇写作各最多一次修正。模型输出必须完整结束并可解析为 JSON。调用按接口、模型和完整提示词缓存；续跑会核对固定输入和模型身份，复用有效响应，不盲目重试失败请求。

示例（`input` 为固定的批准记录数组，所选新增论文须有可读本地 PDF）：

```bash
python3 scripts/validate_writing_api.py \
  --input tmp/api-writing-acceptance/fixed-input.json \
  --reuse-reading tmp/live-fidelity-v2/data/editorial/2026-09-28/approval.json \
  --paper-key doi:10.3389/fcomp.2026.1879630 \
  --paper-key arxiv:2609.29406 \
  --paper-key arxiv:2608.03315 \
  --repo-key github:haozhe-xing/agent_learning \
  --model gpt-6-sol --stream \
  --output tmp/writing-api-live-v3
```

探测确认该网关支持 Chat Completions 与模型列表接口。`--stream` 使用 SSE 接收长响应，降低网关等待完整 JSON 时发生 524 的风险；仍要求正常结束和完整 JSON，截断响应不会进入缓存。

已有同一目录时添加 `--resume`，其他参数保持一致。正文/引句内容属于本地私有运行产物，保存在被忽略的 `tmp/` 下。

主要产物：

- `manifest.json`：固定输入哈希、所选条目、模型及接口哈希。
- `reading-inputs.json`、`api-calls/`：文本阅读输入与真实响应、用量、耗时。
- `drafts/`：审核前原稿及唯一一次修正稿。
- `data/editorial/<date>/`：草稿、编辑审核、批准记录。
- `reports/`：整期 Markdown/HTML、独立阅读笔记及可用本地 PDF。
- `composition-audit.json`：展示段落与批准字段的映射。
- `editorial-assessment.json`：独立模型对整期的终审意见。
- `acceptance.json`：分别列明文本阅读、语义审核、关键结果支持、整期写作验收与严格保真状态。

写作验收要求请求的论文和项目全部获准、文本读完、论文核心结果均受证据支持，且整期终审没有 major/critical 问题。现有视觉缺口仍会使论文降为简讯；写作验收不等于图表/公式的严格保真认证，也不等于完整 8+2 配额验收。
