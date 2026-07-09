from __future__ import annotations

from dataclasses import dataclass
import os
import tomllib
from pathlib import Path
from typing import Any


EXTERNAL_SECRETS_ENV = "DAILY_AGENT_SECRETS_FILE"
DISABLE_EXTERNAL_SECRETS_ENV = "DAILY_AGENT_DISABLE_EXTERNAL_SECRETS"


ALIASES: dict[str, list[tuple[str, ...]]] = {
    "SERPAPI_API_KEY": [("serpapi", "api_key"), ("google_scholar", "api_key")],
    "SEMANTIC_SCHOLAR_API_KEY": [("semantic_scholar", "api_key")],
    "IEEE_XPLORE_API_KEY": [("ieee", "api_key"), ("ieee_xplore", "api_key")],
    "CORE_API_KEY": [("core", "api_key")],
    "UNPAYWALL_EMAIL": [("unpaywall", "email")],
    "GITHUB_TOKEN": [("github", "token"), ("github", "api_key")],
    "GH_TOKEN": [("github", "token"), ("github", "api_key")],
    "DAILY_AGENT_FEISHU_APP_ID": [("feishu", "app_id")],
    "DAILY_AGENT_FEISHU_APP_SECRET": [("feishu", "app_secret")],
    "DAILY_AGENT_FEISHU_FOLDER_TOKEN": [("feishu", "folder_token")],
    "DAILY_AGENT_FEISHU_DOC_TOKEN": [("feishu", "doc_token"), ("feishu", "document_id"), ("feishu", "document_token")],
}


PLACEHOLDER_VALUES = {"replace-me", "replace_me", "changeme", "change-me", "todo", "...", "your-key-here", "your-api-key"}


@dataclass(frozen=True)
class SecretStatus:
    label: str
    env_names: tuple[str, ...]
    status: str
    source: str
    path: str | None = None


FULL_PROFILE_SECRET_GROUPS: list[tuple[str, tuple[str, ...]]] = [
    ("SERPAPI_API_KEY", ("SERPAPI_API_KEY",)),
    ("SEMANTIC_SCHOLAR_API_KEY", ("SEMANTIC_SCHOLAR_API_KEY",)),
    ("IEEE_XPLORE_API_KEY", ("IEEE_XPLORE_API_KEY",)),
    ("CORE_API_KEY", ("CORE_API_KEY",)),
    ("UNPAYWALL_EMAIL", ("UNPAYWALL_EMAIL",)),
    ("GitHub token", ("GITHUB_TOKEN", "GH_TOKEN")),
    ("DAILY_AGENT_FEISHU_APP_ID", ("DAILY_AGENT_FEISHU_APP_ID",)),
    ("DAILY_AGENT_FEISHU_APP_SECRET", ("DAILY_AGENT_FEISHU_APP_SECRET",)),
    ("Feishu destination", ("DAILY_AGENT_FEISHU_FOLDER_TOKEN", "DAILY_AGENT_FEISHU_DOC_TOKEN")),
]


def render_external_secrets_template() -> str:
    return """# Daily Agent external secrets template.
# Save this file outside the repository, for example:
#   $HOME/.daily-agent/secrets.toml
# Then set:
#   export DAILY_AGENT_SECRETS_FILE=\"$HOME/.daily-agent/secrets.toml\"
#
# Values below are placeholders. Replace only the values you use.
# Keep this file private; do not commit it to git.

[daily_agent.env]
# Google Scholar via SerpAPI. Enables stable Scholar-like search.
SERPAPI_API_KEY = \"replace-me\"

# Semantic Scholar API key. Improves citation and influence signals.
SEMANTIC_SCHOLAR_API_KEY = \"replace-me\"

# IEEE Xplore Metadata API key. Enables IEEE metadata source.
IEEE_XPLORE_API_KEY = \"replace-me\"

# CORE API key. Enables CORE works search.
CORE_API_KEY = \"replace-me\"

# Unpaywall contact email. Enables DOI-based OA PDF and landing-page resolution.
UNPAYWALL_EMAIL = \"replace-me\"

# GitHub token. Avoids low unauthenticated rate limits.
GITHUB_TOKEN = \"replace-me\"

[feishu]
# Feishu delivery credentials.
app_id = \"replace-me\"
app_secret = \"replace-me\"

# Provide either folder_token for creating a weekly doc, or doc_token for updating an existing doc.
folder_token = \"replace-me\"
doc_token = \"replace-me\"
"""


def render_missing_external_secrets_template(statuses: list[SecretStatus] | None = None) -> str:
    statuses = statuses or audit_full_profile_secrets()
    missing = [item for item in statuses if item.status != "present"]
    if not missing:
        return """# Daily Agent missing secrets template.
# All full-profile secrets are present. Nothing to fill in.
"""
    env_keys: list[str] = []
    feishu_keys: list[str] = []
    comments: list[str] = []
    for item in missing:
        comments.append(f"# {item.label}: currently {item.status} ({'/'.join(item.env_names)})")
        if item.label == "GitHub token":
            env_keys.append("GITHUB_TOKEN")
        elif item.label == "DAILY_AGENT_FEISHU_APP_ID":
            feishu_keys.append("app_id")
        elif item.label == "DAILY_AGENT_FEISHU_APP_SECRET":
            feishu_keys.append("app_secret")
        elif item.label == "Feishu destination":
            feishu_keys.extend(["folder_token", "doc_token"])
        else:
            env_keys.append(item.env_names[0])
    lines = [
        "# Daily Agent missing secrets template.",
        "# Save this file outside the repository, then set DAILY_AGENT_SECRETS_FILE to its path.",
        "# Existing present secrets are omitted; replace every replace-me value you keep.",
        *comments,
    ]
    if env_keys:
        lines.append("")
        lines.append("[daily_agent.env]")
        for key in _unique(env_keys):
            lines.append(f'{key} = "replace-me"')
    if feishu_keys:
        lines.append("")
        lines.append("[feishu]")
        if "folder_token" in feishu_keys or "doc_token" in feishu_keys:
            lines.append("# Provide either folder_token for creating a weekly doc, or doc_token for updating an existing doc.")
        for key in _unique(feishu_keys):
            lines.append(f'{key} = "replace-me"')
    return "\n".join(lines) + "\n"


def write_external_secrets_template(path: str | Path, force: bool = False) -> Path:
    output = Path(path).expanduser()
    if output.exists() and not force:
        raise FileExistsError(str(output))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_external_secrets_template(), encoding="utf-8")
    return output


def write_missing_external_secrets_template(path: str | Path, force: bool = False) -> Path:
    output = Path(path).expanduser()
    if output.exists() and not force:
        raise FileExistsError(str(output))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_missing_external_secrets_template(), encoding="utf-8")
    return output


def credential_value(env_name: str | None) -> str | None:
    if not env_name:
        return None
    value = os.environ.get(env_name)
    if value and not _is_placeholder_secret(value):
        return value
    for path in _secret_files():
        payload = _load_toml(path)
        if not payload:
            continue
        value = _lookup_value(payload, env_name)
        if value:
            return value
    return None


def credential_present(env_name: str | None) -> bool:
    return bool(credential_value(env_name))


def audit_full_profile_secrets() -> list[SecretStatus]:
    return [_credential_group_status(label, env_names) for label, env_names in FULL_PROFILE_SECRET_GROUPS]


def render_secrets_status(statuses: list[SecretStatus] | None = None) -> str:
    statuses = statuses or audit_full_profile_secrets()
    lines = ["Secrets status:"]
    for item in statuses:
        names = "/".join(item.env_names)
        detail = item.source
        if item.path:
            detail += f" {item.path}"
        lines.append(f"- {item.label} {item.status} {detail} ({names})")
    return "\n".join(lines)


def _credential_group_status(label: str, env_names: tuple[str, ...]) -> SecretStatus:
    first_placeholder: SecretStatus | None = None
    for env_name in env_names:
        status = _credential_status(env_name, label=label, env_names=env_names)
        if status.status == "present":
            return status
        if status.status == "placeholder" and first_placeholder is None:
            first_placeholder = status
    if first_placeholder:
        return first_placeholder
    return SecretStatus(label=label, env_names=env_names, status="missing", source="not_found")


def _credential_status(env_name: str, label: str | None = None, env_names: tuple[str, ...] | None = None) -> SecretStatus:
    names = env_names or (env_name,)
    label = label or env_name
    value = os.environ.get(env_name)
    if value:
        return SecretStatus(label=label, env_names=names, status="placeholder" if _is_placeholder_secret(value) else "present", source="env")
    first_placeholder: SecretStatus | None = None
    for path in _secret_files():
        payload = _load_toml(path)
        if not payload:
            continue
        raw_value = _lookup_raw_value(payload, env_name)
        if raw_value:
            status = SecretStatus(
                label=label,
                env_names=names,
                status="placeholder" if _is_placeholder_secret(raw_value) else "present",
                source="external_file",
                path=str(path),
            )
            if status.status == "present":
                return status
            if first_placeholder is None:
                first_placeholder = status
    if first_placeholder:
        return first_placeholder
    return SecretStatus(label=label, env_names=names, status="missing", source="not_found")


def _secret_files() -> list[Path]:
    if os.environ.get(DISABLE_EXTERNAL_SECRETS_ENV):
        return []
    explicit = os.environ.get(EXTERNAL_SECRETS_ENV)
    if explicit:
        return [Path(item).expanduser() for item in explicit.split(os.pathsep) if item.strip()]
    home = Path.home()
    return [
        home / ".daily-agent" / "secrets.toml",
        home / ".config" / "daily-agent" / "secrets.toml",
        home / ".cc-connect" / "config.toml",
    ]


def _load_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _lookup_value(payload: dict[str, Any], env_name: str) -> str | None:
    value = _lookup_raw_value(payload, env_name)
    if _is_placeholder_secret(value):
        return None
    return value


def _lookup_raw_value(payload: dict[str, Any], env_name: str) -> str | None:
    for container_path in [(), ("daily_agent",), ("daily_agent", "env"), ("daily_agent", "secrets"), ("env",), ("secrets",)]:
        container = _nested_mapping(payload, container_path)
        value = _mapping_value(container, env_name)
        if value:
            return value
        value = _mapping_value(container, env_name.lower())
        if value:
            return value
    for alias_path in ALIASES.get(env_name, []):
        value = _nested_value(payload, alias_path)
        if value:
            return value
        value = _nested_value(payload, ("daily_agent", *alias_path))
        if value:
            return value
    value = _cc_connect_feishu_value(payload, env_name)
    if value:
        return value
    return None


def _cc_connect_feishu_value(payload: dict[str, Any], env_name: str) -> str | None:
    option_key = {
        "DAILY_AGENT_FEISHU_APP_ID": "app_id",
        "DAILY_AGENT_FEISHU_APP_SECRET": "app_secret",
        "DAILY_AGENT_FEISHU_FOLDER_TOKEN": "folder_token",
        "DAILY_AGENT_FEISHU_DOC_TOKEN": "doc_token",
    }.get(env_name)
    if not option_key:
        return None
    for project in payload.get("projects", []) or []:
        if not isinstance(project, dict):
            continue
        for platform in project.get("platforms", []) or []:
            if not isinstance(platform, dict) or platform.get("type") != "feishu":
                continue
            options = platform.get("options", {}) or {}
            if not isinstance(options, dict):
                continue
            value = _mapping_value(options, option_key)
            if value:
                return value
    return None


def _nested_mapping(payload: dict[str, Any], path: tuple[str, ...]) -> dict[str, Any]:
    value: Any = payload
    for key in path:
        if not isinstance(value, dict):
            return {}
        value = _mapping_raw_value(value, key)
    return value if isinstance(value, dict) else {}


def _nested_value(payload: dict[str, Any], path: tuple[str, ...]) -> str | None:
    value: Any = payload
    for key in path:
        if not isinstance(value, dict):
            return None
        value = _mapping_raw_value(value, key)
    return _string_value(value)


def _mapping_value(payload: dict[str, Any], key: str) -> str | None:
    return _string_value(_mapping_raw_value(payload, key))


def _mapping_raw_value(payload: dict[str, Any], key: str) -> Any:
    if key in payload:
        return payload[key]
    lowered = key.lower()
    for existing_key, value in payload.items():
        if str(existing_key).lower() == lowered:
            return value
    return None


def _unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _string_value(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _is_placeholder_secret(value: str | None) -> bool:
    if not value:
        return False
    normalized = value.strip().lower()
    return normalized in PLACEHOLDER_VALUES or normalized.startswith("replace-")
