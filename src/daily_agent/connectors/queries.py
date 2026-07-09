from __future__ import annotations

from daily_agent.config import DomainConfig


def scholarly_queries(domain: DomainConfig) -> list[str]:
    return domain.include_keywords or [domain.name]
