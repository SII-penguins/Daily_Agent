# Workflow recovery / 调度恢复

The controller shares sealed report snapshots, delivery outbox records, and confirmed receipts between scheduled and manual external delivery. Publication history is committed after confirmation. `--send local` remains a formal local publication; dry-run files remain excluded from publication history.

定时和手动外发共用封存日报、投递日志和成功收据；确认投递后才提交发表历史。`--send local` 仍是正式本地发表，dry-run 文件始终隔离。

## Inspect / 检查

```bash
python -m daily_agent.cli schedule status --root . --date today
python -m daily_agent.cli schedule preview --root . --backend launchd
```

Status is read-only, including when records are corrupt. It reports stage state, heartbeat and hand-off problems; it does not install monitoring or repair records. Preview only prints scheduler configuration. The default Asia/Shanghai stages are 00:10 generation, 05:30 review/recovery, and 07:50 delivery, targeting arrival before 08:00.

`status` 只读检查阶段、心跳和交接产物，遇到损坏记录也不会修改文件。`preview` 只输出任务配置。默认使用上海时区，00:10 生成、05:30 复核补救、07:50 投递，目标 08:00 前收到。

## Repair / 恢复

After investigating the reported cause:

```bash
python -m daily_agent.cli schedule repair --root . --date today
```

Repair holds the controller lease, restores corrupt state only from a validated backup, and checks retained process identities before releasing interrupted stages. Without a valid backup or confirmed process cleanup, it preserves evidence and blocks. It never clears an uncertain delivery result automatically.

调查原因后运行 `repair`。它持锁恢复有效备份并检查遗留进程；无有效备份、身份不符、组长消失但组内仍有活进程或无法确认停止时，保留证据并阻断。`cleanup_pending` 不能通过版本变化或重置预算跳过。确认清理后保留已用尝试次数和检查点。

For a diagnosed and corrected failure, an explicit stage budget reset is available:

```bash
python -m daily_agent.cli schedule repair --root . --date today --stage review --reset-budget
```

This reopens the stage circuit; it does not send a report. Retry counts and backoff persist across controller restarts. Waiting consumes the original time window. Recovery settings live under `schedule.recovery` in [delivery.yaml](../config/delivery.yaml); the default maximum is three attempts. Morning generation recovery uses the remaining review window.

明确修复故障后才重置对应阶段预算。重置本身不发送日报；重试次数与等待时刻跨重启保存，等待计入原窗口，晨间补生成使用剩余复核窗口。

## Resolve ambiguous delivery / 核对不确定投递

Failures after entering a delivery adapter may mean the remote operation succeeded but its receipt was lost. Check the actual remote document/message before choosing an outcome:

```bash
python -m daily_agent.cli schedule resolve-delivery --root . --date YYYY-MM-DD \
  --outcome sent --note "Describe the remote evidence confirming delivery"
```

Use `--outcome not-sent` only after confirming no delivery occurred. This command never sends a message; `sent` records confirmation and reconciles local publication, while `not-sent` records that a later retry is permissible. A confirmed receipt cannot be downgraded to not-sent. Investigate and repair any remaining stage circuit before explicitly rerunning the delivery stage.

进入投递适配器后的失败可能已经产生远端结果，必须先核对实际云文档或消息。确认已发送用 `sent`，确认未发送才用 `not-sent`，并记录核对依据。该命令不发送消息；已发送时补齐本地发表，未发送时允许后续重试，但不会自动重开其他熔断。已有成功收据不能改为未发送。

## Guarantees and limits / 验证边界

Immutable snapshots bind report content to approval evidence. Both paper and repository quotas gate early completion. Missing delivery tools/configuration fail preflight before the outbox records an attempted send. Critical JSON rejects malformed, duplicate-key and non-finite records rather than silently overwriting history.

Tests cover temporary-directory fault injection, mocked delivery adapters, and real local child/process-group cleanup. They do not establish live end-to-end delivery or whole-corpus reading acceptance. No independent monitoring daemon is installed. Remote APIs lack an end-to-end idempotency key, so exactly-once delivery is not promised. Process identity uses a coarse snapshot; escaped descendants and unrecorded process groups are not guaranteed to be reclaimed.

封存内容与审批证据绑定，论文和项目配额同时满足才提前结束。本地投递预检先于发送日志；关键 JSON 损坏时保留证据。测试覆盖隔离故障注入、模拟远端和真实本地进程清理，不代表真实端到端投递或整批论文阅读验收。未安装独立巡检服务，不承诺远端 exactly-once，也不保证回收逃离已记录进程组的后代。

Regression entry points: [workflow resilience](../tests/test_workflow_resilience.py), [recovery contracts](../tests/test_recovery_contracts.py), and [process cleanup](../tests/test_process_cleanup.py).
