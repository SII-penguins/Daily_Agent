from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    if yaml is None:
        return {}
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


@dataclass(frozen=True)
class DomainConfig:
    name: str
    quota_group: str
    priority: float
    description: str
    include_keywords: list[str]
    exclude_keywords: list[str]
    arxiv_categories: list[str]
    github_queries: list[str]


@dataclass(frozen=True)
class AppConfig:
    root: Path
    interests_path: Path
    sources_path: Path
    delivery_path: Path
    feedback_path: Path
    domains: list[DomainConfig]
    fixed_tags: list[str]
    quota: dict[str, int]
    sources: dict[str, Any]
    delivery: dict[str, Any]
    feedback: dict[str, Any]

    @property
    def reports_dir(self) -> Path:
        output_dir = self.delivery.get("report", {}).get("output_dir", "reports")
        return self.root / output_dir

    @property
    def selected_dir(self) -> Path:
        selected_dir = self.delivery.get("report", {}).get("selected_dir", "data/selected")
        return self.root / selected_dir

    @property
    def state_dir(self) -> Path:
        state_dir = self.delivery.get("report", {}).get("state_dir", "data/state")
        return self.root / state_dir

    @property
    def logs_dir(self) -> Path:
        logs_dir = self.delivery.get("report", {}).get("logs_dir", "data/logs")
        return self.root / logs_dir


def load_config(root: str | Path | None = None) -> AppConfig:
    root_path = Path(root) if root else PROJECT_ROOT
    config_dir = root_path / "config"
    interests_path = config_dir / "interests.yaml"
    sources_path = config_dir / "sources.yaml"
    delivery_path = config_dir / "delivery.yaml"
    feedback_path = config_dir / "feedback.yaml"

    interests = _load_yaml(interests_path)
    sources = _load_yaml(sources_path)
    delivery = _load_yaml(delivery_path)
    feedback = _load_yaml(feedback_path)

    domains = []
    for domain in interests.get("domains", []):
        keywords = domain.get("keywords", {}) or {}
        domains.append(
            DomainConfig(
                name=str(domain.get("name", "unnamed")),
                quota_group=str(domain.get("quota_group", "exploratory")),
                priority=float(domain.get("priority", 0.5)),
                description=str(domain.get("description", "")),
                include_keywords=[str(item) for item in _as_list(keywords.get("include"))],
                exclude_keywords=[str(item) for item in _as_list(keywords.get("exclude"))],
                arxiv_categories=[str(item) for item in _as_list(domain.get("arxiv_categories"))],
                github_queries=[str(item) for item in _as_list(domain.get("github_queries"))],
            )
        )

    quota = interests.get("quota", {}) or {}
    default_quota = {
        "max_items": 10,
        "quantum_target": 6,
        "exploratory_target": 4,
        "paper_target": 7,
        "github_target": 3,
    }
    default_quota.update({key: int(value) for key, value in quota.items()})

    return AppConfig(
        root=root_path,
        interests_path=interests_path,
        sources_path=sources_path,
        delivery_path=delivery_path,
        feedback_path=feedback_path,
        domains=domains,
        fixed_tags=[str(item) for item in interests.get("fixed_tags", [])],
        quota=default_quota,
        sources=sources,
        delivery=delivery,
        feedback=feedback,
    )
