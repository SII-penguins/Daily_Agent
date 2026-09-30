from pathlib import Path
import tomllib

from daily_agent.config import load_config


ROOT = Path(__file__).resolve().parents[1]


def test_external_secret_template_is_valid_toml_and_lists_full_profile_credentials():
    from daily_agent.secrets import render_external_secrets_template

    text = render_external_secrets_template()
    payload = tomllib.loads(text)

    assert payload["daily_agent"]["env"]["SERPAPI_API_KEY"] == "replace-me"
    assert payload["daily_agent"]["env"]["SEMANTIC_SCHOLAR_API_KEY"] == "replace-me"
    assert payload["daily_agent"]["env"]["IEEE_XPLORE_API_KEY"] == "replace-me"
    assert payload["daily_agent"]["env"]["CORE_API_KEY"] == "replace-me"
    assert payload["daily_agent"]["env"]["UNPAYWALL_EMAIL"] == "replace-me"
    assert payload["daily_agent"]["env"]["GITHUB_TOKEN"] == "replace-me"
    assert payload["feishu"]["app_id"] == "replace-me"
    assert payload["feishu"]["app_secret"] == "replace-me"
    assert payload["feishu"]["folder_token"] == "replace-me"
    assert "Google Scholar" in text
    assert "do not commit" in text.lower()


def test_external_secret_file_reads_daily_agent_env_and_common_aliases(tmp_path, monkeypatch):
    from daily_agent.secrets import credential_value

    secrets = tmp_path / "secrets.toml"
    secrets.write_text(
        """
[daily_agent.env]
SERPAPI_API_KEY = "serp-external"
SEMANTIC_SCHOLAR_API_KEY = "s2-external"
UNPAYWALL_EMAIL = "upw-env@example.com"

[github]
token = "github-external"

[feishu]
app_id = "feishu-app"
app_secret = "feishu-secret"
folder_token = "feishu-folder"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("DAILY_AGENT_SECRETS_FILE", str(secrets))
    for env_name in [
        "SERPAPI_API_KEY",
        "SEMANTIC_SCHOLAR_API_KEY",
        "UNPAYWALL_EMAIL",
        "GITHUB_TOKEN",
        "DAILY_AGENT_FEISHU_APP_ID",
        "DAILY_AGENT_FEISHU_APP_SECRET",
        "DAILY_AGENT_FEISHU_FOLDER_TOKEN",
    ]:
        monkeypatch.delenv(env_name, raising=False)

    assert credential_value("SERPAPI_API_KEY") == "serp-external"
    assert credential_value("SEMANTIC_SCHOLAR_API_KEY") == "s2-external"
    assert credential_value("UNPAYWALL_EMAIL") == "upw-env@example.com"
    assert credential_value("GITHUB_TOKEN") == "github-external"
    assert credential_value("DAILY_AGENT_FEISHU_APP_ID") == "feishu-app"
    assert credential_value("DAILY_AGENT_FEISHU_APP_SECRET") == "feishu-secret"
    assert credential_value("DAILY_AGENT_FEISHU_FOLDER_TOKEN") == "feishu-folder"


def test_quality_secrets_template_cli_prints_template(capsys):
    from daily_agent.cli import main

    assert main(["quality", "secrets-template"]) == 0

    out = capsys.readouterr().out
    assert "[daily_agent.env]" in out
    assert 'SERPAPI_API_KEY = "replace-me"' in out
    assert "[feishu]" in out


def test_missing_secret_template_omits_present_credentials_and_keeps_valid_toml(tmp_path, monkeypatch):
    from daily_agent.secrets import render_missing_external_secrets_template

    secrets = tmp_path / "secrets.toml"
    secrets.write_text(
        """
[daily_agent.env]
SERPAPI_API_KEY = "serp-present"
GITHUB_TOKEN = "github-present"

[feishu]
app_id = "feishu-app"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("DAILY_AGENT_SECRETS_FILE", str(secrets))
    for env_name in [
        "SERPAPI_API_KEY",
        "GITHUB_TOKEN",
        "SEMANTIC_SCHOLAR_API_KEY",
        "IEEE_XPLORE_API_KEY",
        "CORE_API_KEY",
        "UNPAYWALL_EMAIL",
        "DAILY_AGENT_FEISHU_APP_ID",
        "DAILY_AGENT_FEISHU_APP_SECRET",
        "DAILY_AGENT_FEISHU_FOLDER_TOKEN",
        "DAILY_AGENT_FEISHU_DOC_TOKEN",
    ]:
        monkeypatch.delenv(env_name, raising=False)

    text = render_missing_external_secrets_template()
    payload = tomllib.loads(text)

    assert "SERPAPI_API_KEY" not in text
    assert "GITHUB_TOKEN" not in text
    assert "serp-present" not in text
    assert "github-present" not in text
    assert payload["daily_agent"]["env"]["SEMANTIC_SCHOLAR_API_KEY"] == "replace-me"
    assert payload["daily_agent"]["env"]["IEEE_XPLORE_API_KEY"] == "replace-me"
    assert payload["daily_agent"]["env"]["CORE_API_KEY"] == "replace-me"
    assert payload["daily_agent"]["env"]["UNPAYWALL_EMAIL"] == "replace-me"
    assert payload["feishu"]["app_secret"] == "replace-me"
    assert payload["feishu"]["folder_token"] == "replace-me"
    assert payload["feishu"]["doc_token"] == "replace-me"


def test_missing_secret_template_reports_all_present_without_values(tmp_path, monkeypatch):
    from daily_agent.secrets import render_missing_external_secrets_template

    secrets = tmp_path / "secrets.toml"
    secrets.write_text(
        """
[daily_agent.env]
SERPAPI_API_KEY = "serp-present"
SEMANTIC_SCHOLAR_API_KEY = "s2-present"
IEEE_XPLORE_API_KEY = "ieee-present"
CORE_API_KEY = "core-present"
UNPAYWALL_EMAIL = "upw-present@example.com"
GITHUB_TOKEN = "github-present"

[feishu]
app_id = "feishu-app"
app_secret = "feishu-secret"
folder_token = "feishu-folder"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("DAILY_AGENT_SECRETS_FILE", str(secrets))
    for env_name in [
        "SERPAPI_API_KEY",
        "SEMANTIC_SCHOLAR_API_KEY",
        "IEEE_XPLORE_API_KEY",
        "CORE_API_KEY",
        "UNPAYWALL_EMAIL",
        "GITHUB_TOKEN",
        "DAILY_AGENT_FEISHU_APP_ID",
        "DAILY_AGENT_FEISHU_APP_SECRET",
        "DAILY_AGENT_FEISHU_FOLDER_TOKEN",
    ]:
        monkeypatch.delenv(env_name, raising=False)

    text = render_missing_external_secrets_template()
    payload = tomllib.loads(text)

    assert payload == {}
    assert "All full-profile secrets are present" in text
    assert "serp-present" not in text
    assert "feishu-secret" not in text


def test_quality_secrets_template_cli_writes_without_overwriting(tmp_path, capsys):
    from daily_agent.cli import main

    path = tmp_path / "secrets.toml"

    assert main(["quality", "secrets-template", "--path", str(path)]) == 0
    assert 'IEEE_XPLORE_API_KEY = "replace-me"' in path.read_text(encoding="utf-8")
    assert 'CORE_API_KEY = "replace-me"' in path.read_text(encoding="utf-8")
    assert 'UNPAYWALL_EMAIL = "replace-me"' in path.read_text(encoding="utf-8")

    path.write_text("keep = true\n", encoding="utf-8")
    assert main(["quality", "secrets-template", "--path", str(path)]) == 1
    assert path.read_text(encoding="utf-8") == "keep = true\n"
    assert "already exists" in capsys.readouterr().out

    assert main(["quality", "secrets-template", "--path", str(path), "--force"]) == 0
    assert "[daily_agent.env]" in path.read_text(encoding="utf-8")


def test_quality_secrets_template_cli_supports_missing_only(tmp_path, monkeypatch, capsys):
    from daily_agent.cli import main

    secrets = tmp_path / "present.toml"
    secrets.write_text('[daily_agent.env]\nSERPAPI_API_KEY = "serp-present"\n', encoding="utf-8")
    monkeypatch.setenv("DAILY_AGENT_SECRETS_FILE", str(secrets))
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    output = tmp_path / "missing.toml"

    assert main(["quality", "secrets-template", "--missing-only", "--path", str(output)]) == 0
    text = output.read_text(encoding="utf-8")

    assert "SERPAPI_API_KEY" not in text
    assert "SEMANTIC_SCHOLAR_API_KEY" in text
    assert tomllib.loads(text)["daily_agent"]["env"]["CORE_API_KEY"] == "replace-me"
    assert tomllib.loads(text)["daily_agent"]["env"]["UNPAYWALL_EMAIL"] == "replace-me"

    assert main(["quality", "secrets-template", "--missing-only"]) == 0
    assert "Daily Agent missing secrets template" in capsys.readouterr().out


def test_environment_secret_overrides_external_secret_file(tmp_path, monkeypatch):
    from daily_agent.secrets import credential_value

    secrets = tmp_path / "secrets.toml"
    secrets.write_text('[daily_agent.env]\nSERPAPI_API_KEY = "serp-external"\n', encoding="utf-8")
    monkeypatch.setenv("DAILY_AGENT_SECRETS_FILE", str(secrets))
    monkeypatch.setenv("SERPAPI_API_KEY", "serp-env")

    assert credential_value("SERPAPI_API_KEY") == "serp-env"


def test_placeholder_secret_values_do_not_count_as_credentials(tmp_path, monkeypatch):
    from daily_agent.secrets import credential_value

    secrets = tmp_path / "secrets.toml"
    secrets.write_text('[daily_agent.env]\nSERPAPI_API_KEY = "replace-me"\n', encoding="utf-8")
    monkeypatch.setenv("DAILY_AGENT_SECRETS_FILE", str(secrets))
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)

    assert credential_value("SERPAPI_API_KEY") is None


def test_quality_secrets_status_cli_reports_sources_without_leaking_values(tmp_path, monkeypatch, capsys):
    from daily_agent.cli import main

    secrets = tmp_path / "secrets.toml"
    secrets.write_text(
        """
[daily_agent.env]
SERPAPI_API_KEY = "serp-secret-value"
SEMANTIC_SCHOLAR_API_KEY = "replace-me"

[github]
token = "github-secret-value"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("DAILY_AGENT_SECRETS_FILE", str(secrets))
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    monkeypatch.delenv("SEMANTIC_SCHOLAR_API_KEY", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    assert main(["quality", "secrets-status"]) == 0

    out = capsys.readouterr().out
    assert "Secrets status:" in out
    assert "SERPAPI_API_KEY present external_file" in out
    assert "SEMANTIC_SCHOLAR_API_KEY placeholder external_file" in out
    assert "GitHub token present external_file" in out
    assert "IEEE_XPLORE_API_KEY missing" in out
    assert "UNPAYWALL_EMAIL missing" in out
    assert "serp-secret-value" not in out
    assert "github-secret-value" not in out


def test_external_secret_file_reads_cc_connect_feishu_platform_shape(tmp_path, monkeypatch):
    from daily_agent.secrets import credential_value

    secrets = tmp_path / "cc-connect.toml"
    secrets.write_text(
        """
[[projects]]
name = "daily-agent"

[[projects.platforms]]
type = "feishu"

[projects.platforms.options]
app_id = "cc-app"
app_secret = "cc-secret"
folder_token = "cc-folder"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("DAILY_AGENT_SECRETS_FILE", str(secrets))
    for env_name in ["DAILY_AGENT_FEISHU_APP_ID", "DAILY_AGENT_FEISHU_APP_SECRET", "DAILY_AGENT_FEISHU_FOLDER_TOKEN"]:
        monkeypatch.delenv(env_name, raising=False)

    assert credential_value("DAILY_AGENT_FEISHU_APP_ID") == "cc-app"
    assert credential_value("DAILY_AGENT_FEISHU_APP_SECRET") == "cc-secret"
    assert credential_value("DAILY_AGENT_FEISHU_FOLDER_TOKEN") == "cc-folder"


def test_secrets_status_prefers_later_present_secret_over_earlier_placeholder(tmp_path, monkeypatch):
    from daily_agent.secrets import audit_full_profile_secrets

    primary = tmp_path / "primary.toml"
    secondary = tmp_path / "cc-connect.toml"
    primary.write_text(
        """
[daily_agent.env]
DAILY_AGENT_FEISHU_APP_ID = "replace-me"
DAILY_AGENT_FEISHU_APP_SECRET = "replace-me"
""",
        encoding="utf-8",
    )
    secondary.write_text(
        """
[[projects]]
name = "daily-agent"

[[projects.platforms]]
type = "feishu"

[projects.platforms.options]
app_id = "feishu-app-present"
app_secret = "feishu-secret-present"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("DAILY_AGENT_SECRETS_FILE", f"{primary}:{secondary}")
    for env_name in ["DAILY_AGENT_FEISHU_APP_ID", "DAILY_AGENT_FEISHU_APP_SECRET"]:
        monkeypatch.delenv(env_name, raising=False)

    statuses = {status.label: status for status in audit_full_profile_secrets()}

    assert statuses["DAILY_AGENT_FEISHU_APP_ID"].status == "present"
    assert statuses["DAILY_AGENT_FEISHU_APP_ID"].path == str(secondary)
    assert statuses["DAILY_AGENT_FEISHU_APP_SECRET"].status == "present"
    assert statuses["DAILY_AGENT_FEISHU_APP_SECRET"].path == str(secondary)


def test_quality_check_uses_external_secret_file_for_full_profile(tmp_path, monkeypatch):
    from daily_agent.quality import run_quality_check

    secrets = tmp_path / "secrets.toml"
    secrets.write_text(
        """
[daily_agent.env]
SERPAPI_API_KEY = "serp-external"
SEMANTIC_SCHOLAR_API_KEY = "s2-external"
IEEE_XPLORE_API_KEY = "ieee-external"
CORE_API_KEY = "core-external"
UNPAYWALL_EMAIL = "upw-external@example.com"
GITHUB_TOKEN = "github-external"

[feishu]
app_id = "feishu-app"
app_secret = "feishu-secret"
folder_token = "feishu-folder"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("DAILY_AGENT_SECRETS_FILE", str(secrets))
    for env_name in ["SERPAPI_API_KEY", "SEMANTIC_SCHOLAR_API_KEY", "IEEE_XPLORE_API_KEY", "CORE_API_KEY", "UNPAYWALL_EMAIL", "GITHUB_TOKEN", "DAILY_AGENT_FEISHU_APP_ID", "DAILY_AGENT_FEISHU_APP_SECRET", "DAILY_AGENT_FEISHU_FOLDER_TOKEN"]:
        monkeypatch.delenv(env_name, raising=False)
    monkeypatch.setattr("daily_agent.quality._which", lambda name: f"/usr/bin/{name}" if name == "codex" else None)
    versions = {"fitz": "1.28.0", "pypdf": "6.14.2", "scholarly": "1.7.11"}
    monkeypatch.setattr("daily_agent.quality._package_version", lambda name: versions.get(name))

    config = load_config(ROOT)
    object.__setattr__(config, "root", tmp_path)
    profile = run_quality_check(config)
    by_key = {check.key: check for check in profile.checks}

    assert by_key["google_scholar"].status == "full"
    assert by_key["semantic_scholar"].status == "full"
    assert by_key["ieee"].status == "full"
    assert by_key["core"].status == "full"
    assert by_key["unpaywall"].status == "full"
    assert by_key["github"].status == "full"
    assert by_key["feishu"].status == "full"


def test_google_scholar_skip_reason_accepts_external_serpapi_key(tmp_path, monkeypatch):
    from daily_agent.connectors.google_scholar import google_scholar_skip_reason

    secrets = tmp_path / "secrets.toml"
    secrets.write_text('[daily_agent.env]\nSERPAPI_API_KEY = "serp-external"\n', encoding="utf-8")
    monkeypatch.setenv("DAILY_AGENT_SECRETS_FILE", str(secrets))
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    monkeypatch.setattr("daily_agent.connectors.google_scholar.scholarly_available", lambda: False)

    config = load_config(ROOT)
    object.__setattr__(config, "root", tmp_path)

    assert google_scholar_skip_reason(config) is None
