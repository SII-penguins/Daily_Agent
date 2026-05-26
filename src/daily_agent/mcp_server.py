from __future__ import annotations

import contextlib
import io
from typing import Any

from daily_agent.cli import main as cli_main


def setup_checklist() -> str:
    return """Daily Agent 初始化时，请先向用户确认这些配置：

1. 信源：是否启用 arXiv、GitHub、OpenAlex、Semantic Scholar、Crossref、IEEE、OpenReview、PMLR、NeurIPS。
2. 偏好内容：关注领域、关键词、排除关键词、顶会/期刊偏好、GitHub 项目偏好。
3. 工作时间节点：02:00 夜间 dry-run，07:20 复核/preflight，08:00 正式推送。
4. 推送方式：local、Feishu、cc-connect fallback，以及是否启用 HTML/飞书反馈入口。
5. 凭证：确认 API key 只放在环境变量或项目外部配置中，不写进仓库。

建议 Agent 做法：
- 先调用 source_check，确认各信源状态。
- 再调用 run_digest(dry_run=True)，生成一份本地日报让用户检查。
- 用户确认后，再调用 schedule_preview 或正式 run_digest(send="feishu")。
"""


def source_check(root: str = ".", date: str = "today", window_days: int = 7, sample_limit: int = 3) -> str:
    """Check configured sources without writing report state."""
    return _run_cli(
        [
            "source",
            "check",
            "--root",
            root,
            "--date",
            date,
            "--window-days",
            str(window_days),
            "--sample-limit",
            str(sample_limit),
        ]
    )


def run_digest(root: str = ".", date: str = "today", dry_run: bool = True, send: str = "local", use_llm: bool = True) -> str:
    """Run the Daily Agent pipeline."""
    args = ["run", "--root", root, "--date", date]
    if dry_run:
        args.append("--dry-run")
    else:
        args.extend(["--send", send])
    if use_llm:
        args.append("--llm")
    return _run_cli(args)


def schedule_preview(root: str = ".", backend: str = "cc-connect") -> str:
    """Print scheduler setup commands without installing them."""
    return _run_cli(["schedule", "preview", "--root", root, "--backend", backend])


def feedback_show(root: str = ".", limit: int = 20) -> str:
    """Show recent feedback events."""
    return _run_cli(["feedback", "show", "--root", root, "--limit", str(limit)])


def feedback_add_text(root: str = ".", text: str = "", date: str | None = None) -> str:
    """Record natural-language feedback such as 今天第 3 条不行，第 8 条不错."""
    args = ["feedback", "add", "--root", root, "--text", text]
    if date:
        args.extend(["--date", date])
    return _run_cli(args)


def create_server() -> Any:
    try:
        from mcp.server.fastmcp import FastMCP
    except Exception as exc:  # pragma: no cover - exercised only when optional dep is missing.
        raise RuntimeError('Install MCP support first: pip install -e ".[mcp]"') from exc

    mcp = FastMCP("Daily Agent")
    mcp.tool()(setup_checklist)
    mcp.tool()(source_check)
    mcp.tool()(run_digest)
    mcp.tool()(schedule_preview)
    mcp.tool()(feedback_show)
    mcp.tool()(feedback_add_text)
    return mcp


def main() -> None:
    create_server().run()


def _run_cli(args: list[str]) -> str:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = cli_main(args)
    output = stdout.getvalue().strip()
    error = stderr.getvalue().strip()
    parts = [f"exit_code={code}"]
    if output:
        parts.append(output)
    if error:
        parts.append(f"stderr:\n{error}")
    return "\n".join(parts)


if __name__ == "__main__":
    main()
