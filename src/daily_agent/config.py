from __future__ import annotations

from dataclasses import dataclass, field
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
    base_include_keywords: list[str] = field(default_factory=list)
    expanded_keywords: list[str] = field(default_factory=list)


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
    source_policy = (sources.get('reading', {}) or {}).get('source_evidence_policy', 'strict_fidelity_v1')
    if source_policy not in {'strict_fidelity_v1', 'native_claim_evidence_v1'}:
        raise ValueError('Unknown source evidence policy')
    if source_policy == 'native_claim_evidence_v1':
        if sources['reading'].get('require_scientific_analysis', True) is not True:
            raise ValueError('Native daily selection requires scientific analysis')
        sources['reading']['require_scientific_analysis'] = True
    delivery = _load_yaml(delivery_path)
    feedback = _load_yaml(feedback_path)
    query_expansion = sources.get("query_expansion", {}) or {}
    expansion_enabled = bool(query_expansion.get("enabled", False))
    max_expand_terms = int(query_expansion.get("max_expand_terms_per_domain", 0))

    domains = []
    for domain in interests.get("domains", []):
        keywords = domain.get("keywords", {}) or {}
        base_include_keywords = [str(item) for item in _as_list(keywords.get("include"))]
        expanded_keywords = _limited_unique([str(item) for item in _as_list(keywords.get("expand"))], max_expand_terms)
        include_keywords = _unique([*base_include_keywords, *expanded_keywords]) if expansion_enabled else base_include_keywords
        domains.append(
            DomainConfig(
                name=str(domain.get("name", "unnamed")),
                quota_group=str(domain.get("quota_group", "exploratory")),
                priority=float(domain.get("priority", 0.5)),
                description=str(domain.get("description", "")),
                base_include_keywords=base_include_keywords,
                expanded_keywords=expanded_keywords,
                include_keywords=include_keywords,
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
        "paper_target": 8,
        "paper_review_multiplier": 2,
        "github_target": 2,
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


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = value.strip()
        key = text.lower()
        if text and key not in seen:
            seen.add(key)
            result.append(text)
    return result


def _limited_unique(values: list[str], limit: int) -> list[str]:
    unique = _unique(values)
    return unique[:limit] if limit > 0 else unique
