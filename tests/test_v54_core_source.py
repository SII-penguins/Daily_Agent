from datetime import datetime, timezone
from pathlib import Path
import shutil

import httpx
import yaml

from daily_agent.config import load_config
from daily_agent.models import ApprovedItem, DigestItem, MaterialRecord, RunStatus
from daily_agent.rendering.markdown import render_daily_markdown
from daily_agent.scoring.dedup import deduplicate_items


ROOT = Path(__file__).resolve().parents[1]


class _Client:
    def __init__(self, handler, **kwargs):
        self.handler = handler
        self.kwargs = kwargs

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get(self, url, **kwargs):
        return self.handler(url, **kwargs)


def _copy_config(tmp_path: Path) -> Path:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    for name in ["sources.yaml", "interests.yaml", "delivery.yaml", "feedback.yaml"]:
        shutil.copy(ROOT / "config" / name, config_dir / name)
    return tmp_path


def test_core_fetch_skips_without_api_key(monkeypatch):
    from daily_agent.connectors.core import fetch_core

    config = load_config(ROOT)
    config.sources["core"] = {"enabled": True, "api_key_env": "CORE_API_KEY"}
    monkeypatch.delenv("CORE_API_KEY", raising=False)
    monkeypatch.setenv("DAILY_AGENT_DISABLE_EXTERNAL_SECRETS", "1")

    assert fetch_core(config, datetime(2026, 7, 7, tzinfo=timezone.utc), window_days=30) == []


def test_core_fetch_normalizes_work_with_key(monkeypatch):
    from daily_agent.connectors.core import fetch_core

    config = load_config(ROOT)
    config.sources["core"] = {
        "enabled": True,
        "api_key_env": "CORE_API_KEY",
        "max_results_per_query": 2,
        "timeout_seconds": 45,
    }
    monkeypatch.setenv("CORE_API_KEY", "core-token")
    seen: dict[str, object] = {"queries": []}

    def handler(url, **kwargs):
        seen["url"] = url
        seen["params"] = kwargs["params"]
        seen["headers"] = kwargs["headers"]
        seen["queries"].append(kwargs["params"]["q"])
        return httpx.Response(
            200,
            request=httpx.Request("GET", url),
            json={
                "results": [
                    {
                        "id": "core-123",
                        "title": "Quantum compilation with formal guarantees",
                        "abstract": "We introduce a compiler that preserves circuit semantics.",
                        "doi": "https://doi.org/10.5555/core.123",
                        "yearPublished": 2026,
                        "publishedDate": "2026-07-01",
                        "authors": [{"name": "Alice Core"}, {"name": "Bob Index"}],
                        "downloadUrl": "https://core.ac.uk/download/123.pdf",
                        "fullTextLink": "https://core.ac.uk/works/123",
                        "sourceFulltextUrls": ["https://publisher.test/core.pdf"],
                        "citationCount": 17,
                        "topics": ["Quantum computing", "Compilers"],
                        "publisher": "CORE Test Press",
                    }
                ]
            },
        )

    monkeypatch.setattr("daily_agent.connectors.core.httpx.Client", lambda **kwargs: _Client(handler, **kwargs))

    items = fetch_core(config, datetime(2026, 7, 7, tzinfo=timezone.utc), window_days=30)

    assert seen["url"] == "https://api.core.ac.uk/v3/search/works"
    assert seen["headers"] == {"Authorization": "Bearer core-token"}
    assert seen["params"]["limit"] == 2
    assert seen["params"]["yearFilter"] == "2026"
    assert any("quantum" in str(query).lower() for query in seen["queries"])
    assert len(items) == 1
    assert items[0].source == "core"
    assert items[0].item_type == "paper"
    assert items[0].doi == "10.5555/core.123"
    assert items[0].url == "https://core.ac.uk/works/123"
    assert items[0].pdf_url == "https://core.ac.uk/download/123.pdf"
    assert items[0].authors == ["Alice Core", "Bob Index"]
    assert items[0].published_at == "2026-07-01"
    assert items[0].raw["core_id"] == "core-123"
    assert items[0].raw["citation_count"] == 17
    assert items[0].raw["venue"] == "CORE Test Press"
    assert items[0].raw["topics"] == ["Quantum computing", "Compilers"]


def test_core_dedup_merges_aliases_and_renders_core_link():
    arxiv = DigestItem(
        id="2607.00001",
        source="arxiv",
        item_type="paper",
        title="Quantum compilation with formal guarantees",
        url="https://arxiv.org/abs/2607.00001",
        pdf_url="https://arxiv.org/pdf/2607.00001",
        arxiv_id="2607.00001",
        doi="10.5555/core.123",
        raw={"entry_id": "https://arxiv.org/abs/2607.00001"},
    )
    core = DigestItem(
        id="core-123",
        source="core",
        item_type="paper",
        title="Quantum compilation with formal guarantees",
        url="https://core.ac.uk/works/123",
        pdf_url="https://core.ac.uk/download/123.pdf",
        doi="10.5555/core.123",
        raw={"core_id": "core-123", "core_url": "https://core.ac.uk/works/123"},
    )

    merged = deduplicate_items([arxiv, core])
    material = MaterialRecord.from_item(merged[0])
    material.source_aliases["core"] = "core-123"
    material.raw["core_url"] = "https://core.ac.uk/works/123"
    approved = ApprovedItem(
        key=material.key,
        item_type="paper",
        title=material.title,
        source=material.source,
        url=material.url,
        final_fields={
            "problem": "compiler correctness is hard to preserve",
            "method": "formal compilation checks",
            "why_it_works": "the proof constraints rule out invalid rewrites",
            "method_steps": ["parse", "rewrite", "verify"],
            "key_result": "the compiler preserves semantics in tested circuits",
            "possible_use_or_impact": "safer quantum compilation",
            "limitations": "not_stated",
        },
        material=material,
    )

    markdown = render_daily_markdown([approved], datetime(2026, 7, 7, tzinfo=timezone.utc).date(), RunStatus())

    assert len(merged) == 1
    assert merged[0].canonical_key() == "arxiv:2607.00001"
    assert merged[0].raw["source_aliases"]["core"] == "core-123"
    assert "core" in merged[0].raw["evidence"]["sources"]
    assert "arXiv / CORE" in markdown
    assert "[CORE](https://core.ac.uk/works/123)" in markdown


def test_source_check_skips_core_without_api_key(tmp_path, monkeypatch):
    from daily_agent.source_check import run_source_check

    config = load_config(ROOT)
    object.__setattr__(config, "root", tmp_path)
    config.sources["core"] = {"enabled": True, "api_key_env": "CORE_API_KEY"}
    monkeypatch.delenv("CORE_API_KEY", raising=False)
    monkeypatch.setenv("DAILY_AGENT_DISABLE_EXTERNAL_SECRETS", "1")

    for name in [
        "fetch_arxiv",
        "fetch_github",
        "fetch_openalex",
        "fetch_semantic_scholar",
        "fetch_google_scholar",
        "fetch_crossref",
        "fetch_dblp",
        "fetch_ieee",
        "fetch_openreview",
        "fetch_pmlr",
        "fetch_neurips",
    ]:
        monkeypatch.setattr(f"daily_agent.source_check.{name}", lambda config, target_dt, window_days=None: [])
    monkeypatch.setattr("daily_agent.source_check.scholarly_available", lambda: False)

    results = run_source_check(config, datetime(2026, 7, 7, tzinfo=timezone.utc), window_days=7)
    by_key = {result.key: result for result in results}

    assert by_key["core"].skipped is True
    assert by_key["core"].credential_status == "missing CORE_API_KEY"


def test_quality_check_requires_core_key_for_full_profile(tmp_path, monkeypatch):
    from daily_agent.quality import run_quality_check

    config = load_config(ROOT)
    object.__setattr__(config, "root", tmp_path)
    config.sources["core"] = {"enabled": True, "api_key_env": "CORE_API_KEY", "max_results_per_query": 100, "timeout_seconds": 60}
    monkeypatch.setenv("CORE_API_KEY", "core-token")

    by_key = {check.key: check for check in run_quality_check(config).checks}

    assert by_key["core"].status == "full"
    assert by_key["core"].detail == "CORE_API_KEY present"


def test_enforce_full_profile_adds_core_source(tmp_path):
    from daily_agent.full_profile import enforce_full_profile

    root = _copy_config(tmp_path)
    sources_path = root / "config" / "sources.yaml"
    sources = yaml.safe_load(sources_path.read_text(encoding="utf-8"))
    sources.pop("core", None)
    sources_path.write_text(yaml.safe_dump(sources, allow_unicode=True, sort_keys=False), encoding="utf-8")

    result = enforce_full_profile(root, write=True)
    config = load_config(root)

    assert result.changed is True
    assert config.sources["core"]["enabled"] is True
    assert config.sources["core"]["api_key_env"] == "CORE_API_KEY"
    assert config.sources["core"]["max_results_per_query"] == 100
    assert config.sources["core"]["timeout_seconds"] == 60
