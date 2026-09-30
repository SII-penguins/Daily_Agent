# 通过 API 验证 Daily Agent

Canonical For: 固定素材的真实模型验收入口与 API 配置操作；当前结果见 ../progress.txt。

项目通过 Codex CLI 调用模型，API 提供商由 CLI 配置决定。使用第三方 API 包装器时：

```yaml
llm_writer:
  provider: codex
  command: /Users/wuzixie/bin/codex-rethink
  load_user_config: true
```

`load_user_config: true` 保留包装器指定的 CODEX_HOME/config.toml。否则 `--ignore-user-config` 会忽略第三方 provider，可能误连 api.openai.com 并报 401。本机包装器从 macOS Keychain 获取密钥，不把密钥写入项目。API 提供商应在 Codex 配置中指定 `model_provider`，以及 provider 的 `base_url`、`env_key`、`wire_api = "responses"`、`requires_openai_auth = false`。模型、reasoning 等根级设置应放在 TOML 表定义之前，不能误放进 provider 表内。

cc-connect 的代理权限和模型鉴权是两件事。当前修复会话已允许本机文件与网络操作；日报内部模型只负责读取传入内容，仍使用 read-only 执行模式。API 工作流不依赖 ChatGPT 登录，`codex login status` 不能作为 API 连通性的唯一判据。

## 固定输入验证

```sh
cd /Users/wuzixie/Daily_Agent
/Users/wuzixie/anaconda3/bin/python scripts/validate_reading.py \
  --input tmp/visual-repair/real-input.json \
  --output tmp/live-acceptance-example \
  --papers 1 --repos 0 --live
```

首次输出目录必须是新的。脚本先进行真实模型探测，再执行全文分块阅读、逐页视觉核对、综合、证据复核和本地展示。使用已有 PDF 快照，不抓新信源、不投递、不更新素材库或发布历史。真实调用会产生 API 用量。

中断后在同一命令后加 `--resume`：必须提供字节一致的输入快照；沿用输出目录中的配置；只复用通过指纹与引用检查的分块/视觉缓存。解析器、内容或配置改变会使相应缓存失效。不要同时运行同一输出目录的多个验收进程。

输入可以是 approval.json 或 MaterialRecord 数组，应包含有效 local_pdf_path。缺失 PDF 会明确降级，不拿旧摘要冒充全文。扫描 PDF 需要本地 Tesseract；视觉读取需要 API 支持图片。所有原文和图片都作为不可信输入，不允许其中指令改变任务。

模型探测失败退出 2；完整阅读或证据验收不足退出 1；全部选中素材通过并批准才退出 0。单篇固定样本不是完整 8 篇论文＋2 个项目日报，也不代表实时采集与远程投递已经验收。

## 产物

- model-probe.json：真实模型连通性。
- input-snapshot.json、input-manifest.json、input-documents.json：固定输入、数量及解析结构。
- data/editorial/：草稿、复核、批准。
- data/reading/：逐块与逐页视觉结果；无效返回另存 invalid.json。
- reports/validation.md、validation.html：本地日报。
- reports/notes/：独立阅读笔记。
- acceptance.json：完成状态及失败原因。

图表或公式抽取存在差异时必须降级展示，不能以“视觉调用成功”冒充公式保真通过。视觉转写与文字引句是不同证据来源；模型观察不自动成为经独立核验的结论。
