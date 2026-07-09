from __future__ import annotations

import contextlib
import io
from typing import Any

from daily_agent.cli import main as cli_main


def setup_checklist() -> str:
    return """Daily Agent 初始化时，请先向用户确认这些配置：

1. 信源：是否启用 arXiv、GitHub、OpenAlex、Semantic Scholar、Google Scholar、Crossref、DBLP、IEEE、OpenReview、PMLR、NeurIPS；Google Scholar 正式调度优先 SERPAPI_API_KEY，本地有人监督时才临时启用 scholarly fallback。
2. 偏好内容：关注领域、关键词、排除关键词、顶会/期刊偏好、GitHub 项目偏好。
3. 工作时间节点：02:00 夜间 dry-run，07:20 复核/preflight，08:00 正式推送。
4. 推送方式：local、Feishu、cc-connect fallback，以及是否启用 HTML/飞书反馈入口。
5. 凭证：确认 API key 只放在环境变量或项目外部配置中，不写进仓库。

建议 Agent 做法：
- 先调用 secrets_status，确认满血凭证是否就绪且没有占位符；如果缺 key，可调用 secrets_template(missing_only=true) 生成只包含缺失项的外部 TOML 模板。
- 再调用 quality_check，默认会先自动修复 full profile，并要求质量画像必须为 full；如果只是诊断缺口，可传 require_full=false。
- 再调用 source_check，默认会先自动修复 full profile，再确认各信源状态；没有 SerpAPI 但要本地试 Scholar 时，可传 supervised_scholar_fallback=true。
- 再调用 run_digest(dry_run=True)，生成一份本地满血日报让用户检查；MCP 默认 require_full=true，会阻止降级运行。
- 用户确认后，再调用 schedule_preview 或正式 run_digest(send="feishu")；MCP 默认同样会要求 full-quality preflight。
调试配置漂移时，quality_check/source_check 可传 enforce_full=false 来只检查当前配置，不自动修复。
"""


def source_check(root: str = ".", date: str = "today", window_days: int = 7, sample_limit: int = 3, supervised_scholar_fallback: bool = False, enforce_full: bool = True) -> str:
    """Repair the full-quality profile, then check configured sources without writing report state."""
    args = [
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
    if supervised_scholar_fallback:
        args.append("--supervised-scholar-fallback")
    if not enforce_full:
        args.append("--no-enforce-full")
    return _run_cli(args)


def quality_check(root: str = ".", require_full: bool = True, supervised_scholar_fallback: bool = False, enforce_full: bool = True) -> str:
    """Repair the full-quality profile, then require full-quality runtime readiness by default."""
    args = ["quality", "check", "--root", root]
    if require_full:
        args.append("--require-full")
    if supervised_scholar_fallback:
        args.append("--supervised-scholar-fallback")
    if not enforce_full:
        args.append("--no-enforce-full")
    return _run_cli(args)


def enforce_full_profile(root: str = ".", write: bool = False) -> str:
    """Report or apply full-quality config settings without touching secrets."""
    args = ["quality", "enforce-full", "--root", root]
    args.append("--write" if write else "--dry-run")
    return _run_cli(args)


def secrets_template(path: str = "", force: bool = False, missing_only: bool = False) -> str:
    """Print or write an external secrets TOML template for full-quality credentials."""
    args = ["quality", "secrets-template"]
    if path:
        args.extend(["--path", path])
    if force:
        args.append("--force")
    if missing_only:
        args.append("--missing-only")
    return _run_cli(args)


def secrets_status() -> str:
    """Report full-quality credential status without exposing secret values."""
    return _run_cli(["quality", "secrets-status"])


def run_digest(root: str = ".", date: str = "today", dry_run: bool = True, send: str = "local", use_llm: bool = True, supervised_scholar_fallback: bool = False, require_full: bool = True) -> str:
    """Run the Daily Agent pipeline; MCP defaults to require_full=true to block degraded runs."""
    args = ["run", "--root", root, "--date", date]
    if dry_run:
        args.append("--dry-run")
    else:
        args.extend(["--send", send])
    args.append("--llm" if use_llm else "--no-llm")
    if require_full:
        args.append("--require-full")
    if supervised_scholar_fallback:
        args.append("--supervised-scholar-fallback")
    return _run_cli(args)


def schedule_preview(root: str = ".", backend: str = "cc-connect") -> str:
    """Print scheduler setup commands without installing them."""
    return _run_cli(["schedule", "preview", "--root", root, "--backend", backend])


def preview_report(root: str = ".", report: str = "latest") -> str:
    """Start local report preview plus feedback-button service, then return clickable report URLs."""
    return _run_cli(["preview", "start", "--root", root, "--report", report])


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
    mcp.tool()(quality_check)
    mcp.tool()(enforce_full_profile)
    mcp.tool()(secrets_template)
    mcp.tool()(secrets_status)
    mcp.tool()(source_check)
    mcp.tool()(run_digest)
    mcp.tool()(schedule_preview)
    mcp.tool()(preview_report)
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
