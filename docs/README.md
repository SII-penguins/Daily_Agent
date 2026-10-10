# Documentation / 文档导航

Start with the [English project guide](../README.md) or [中文项目指南](../README.zh-CN.md). The READMEs explain what the project does and how to choose a runtime; this directory holds operational details and technical contracts.

## Setup and operation / 安装与运行

- [Local / Feishu reference](local-guide.md) · [本地 / 飞书参考](local-guide.zh-CN.md): installation, optional sources, external secrets, CLI, MCP, feedback and local schedule intent
- [Cloud operations](cloud-migration.md): separate public-source `parent_queue` root, worker claims, resumable generation, immutable handoffs, backup/restore and private HTML transport
- [GitHub persistence](github-persistence.md): `main` for source, `daily-artifacts` for public-safe HTML and assets, and separate private runtime recovery
- [Restart-safe recovery](durable-recovery.md): complete private checkpoints, remote readback, mutation fences and interrupted-write reconciliation
- [Workflow recovery](workflow-recovery.md): local controller state, cleanup, retry budgets and operator reconciliation
- [Cloud delivery audit](cloud-delivery-audit.md): delivery-state invariants, ambiguous outcomes and recovery checks
- [Paper-first v3](paper-first-v3.md): opt-in two-job full-paper workflow, bounded repair, qualified partial HTML and small base/delta checkpoints
- [Incremental cloud execution](incremental_execution.md): per-paper completion, shared stage budgets, supervised attempts and recovery boundaries
- [Batch execution contract](batch_execution_contract.md): durable operation slots, conservative accounting and completion transactions
- [Production revisions](production_revision.md): explicit republishing authorization, preserved original editions and transaction-fenced executor-loss recovery

The local full profile and cloud profile are separate. Never apply local full-profile enforcement or legacy scheduled generation to an explicit cloud root. A setup check is not a per-paper evidence certification.

## Research and reporting / 研究与日报

- [Research source policy](research-source-policy.md): bidirectional quantum/AI scope, three-calendar-month discovery, formal-publication preference and eligible preprints; this supersedes the historical formal-only cloud gate
- [Deferred review reuse](deferred-review-cache.md): durable screening/reviewed overflow, integrity checks and stage-specific invalidation
- [Scientific analysis](scientific-analysis.md): source-bound insight, independent review and separation of scientific prose from presentation
- [Original scientific assets](ORIGINAL_SCIENTIFIC_ASSETS.md): original PDF figures, tables and equations, selection limits, captions and provenance
- [Author and research-group context](author-research-context.md): primary evidence, explicit unknowns and bounded selected-paper research
- [HTML reader and archive](html-report-ui.md): portable reports, accessible desktop/mobile reading and immutable date archives

## Validation and history / 验收与历史

- [Reading validation](../scripts/VALIDATE_READING.md): full-document, visual-fidelity and claim-support checks
- [Writing validation](../scripts/VALIDATE_WRITING.md): isolated model-writing and semantic-review validation
- [Change and validation history](CHANGELOG.md): dated milestones and the limits of what each one demonstrated
- [8 October cloud acceptance record](cloud-acceptance-20261008.md): isolated pilot outcomes and remaining production boundary
- [Cloud dependency lock](cloud-requirements.lock.txt): deployment dependency snapshot, not runtime state or credentials

Historical test counts, pilot labels and migration observations describe their checkpoints, not ongoing monitoring or future guarantees. Public regression [fixtures](../tests/fixtures/README.md) use synthetic text and identities where original validation records cannot be redistributed. Read current policy and operation guides before using an old command or interpreting an old result.
