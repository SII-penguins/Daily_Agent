from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

SourceName = Literal["arxiv", "github", "openalex", "semantic_scholar", "google_scholar", "crossref", "core", "dblp", "ieee", "openreview", "pmlr", "neurips", "nature"]
ItemType = Literal["paper", "repo"]
UpdateLabel = Literal["version_update", "major_update"] | None
QualityStatus = Literal["candidate", "library", "rejected", "published", "archived"]
ReviewVerdict = Literal["PASS", "FAIL"]
FeedbackSignal = Literal["like", "dislike"]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _source_aliases_from_item(item: "DigestItem") -> dict[str, str]:
    aliases: dict[str, str] = {}
    if item.source == "arxiv" and item.arxiv_id:
        aliases["arxiv"] = item.arxiv_id
    if item.source == "github" and item.id:
        aliases["github"] = item.id.lower()
    if item.source == "openalex" and item.raw.get("openalex_id"):
        aliases["openalex"] = str(item.raw["openalex_id"])
    if item.source == "semantic_scholar" and item.raw.get("semantic_scholar_id"):
        aliases["semantic_scholar"] = str(item.raw["semantic_scholar_id"])
    if item.source == "google_scholar" and item.raw.get("google_scholar_id"):
        aliases["google_scholar"] = str(item.raw["google_scholar_id"])
    if item.source == "crossref" and item.doi:
        aliases["crossref"] = item.doi.lower()
    if item.source == "core" and item.raw.get("core_id"):
        aliases["core"] = str(item.raw["core_id"])
    if item.source == "dblp" and item.raw.get("dblp_key"):
        aliases["dblp"] = str(item.raw["dblp_key"])
    if item.source == "ieee" and item.raw.get("ieee_article_number"):
        aliases["ieee"] = str(item.raw["ieee_article_number"])
    if item.source == "openreview" and item.raw.get("openreview_id"):
        aliases["openreview"] = str(item.raw["openreview_id"])
    if item.source == "pmlr" and item.raw.get("pmlr_id"):
        aliases["pmlr"] = str(item.raw["pmlr_id"])
    if item.source == "neurips" and item.raw.get("neurips_id"):
        aliases["neurips"] = str(item.raw["neurips_id"])
    return aliases


def _evidence_from_item(item: "DigestItem") -> dict[str, Any]:
    return {
        "sources": {
            item.source: {
                "title": item.title,
                "url": item.url,
                "pdf_url": item.pdf_url,
                "pdf_urls": item.raw.get("pdf_urls") or [],
                "abstract": item.abstract,
                "doi": item.doi,
                "published_at": item.published_at,
                "updated_at": item.updated_at,
                "venue": item.raw.get("venue"),
                "publication_type": item.raw.get("publication_type"),
                "publication_types": item.raw.get("publication_types"),
                "publication_status": item.raw.get("publication_status"),
                "journal_ref": item.raw.get("journal_ref"),
                "primary_verification": item.raw.get("primary_verification"),
                "primary_landing_verified": item.raw.get("primary_landing_verified"),
                "publication_date": item.published_at,
                "publication_date_precision": item.raw.get("publication_date_precision"),
                "publication_date_basis": item.raw.get("publication_date_basis"),
                "publication_date_source_url": item.raw.get("publication_date_source_url"),
                "publication_stage": item.raw.get("publication_stage"),
                "abstract_source": item.raw.get("abstract_source"),
                "abstract_status": item.raw.get("abstract_status"),
                "official_metadata_status": item.raw.get("official_metadata_status"),
                "metadata_evidence": item.raw.get("metadata_evidence"),
                "citation_count": item.raw.get("citation_count") or item.raw.get("cited_by_count"),
            }
        }
    }


def _merge_evidence(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    left_sources = (left or {}).get("sources", {})
    right_sources = (right or {}).get("sources", {})
    return {"sources": {**left_sources, **right_sources}}


@dataclass
class DigestItem:
    id: str
    source: SourceName
    item_type: ItemType
    title: str
    url: str
    pdf_url: str | None = None
    code_url: str | None = None
    authors: list[str] = field(default_factory=list)
    abstract: str | None = None
    repo_description: str | None = None
    published_at: str | None = None
    updated_at: str | None = None
    first_seen_at: str | None = None
    source_tags: list[str] = field(default_factory=list)
    fixed_tags: list[str] = field(default_factory=list)
    llm_tags: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    language: str | None = None
    stars: int | None = None
    forks: int | None = None
    arxiv_id: str | None = None
    arxiv_version: str | None = None
    doi: str | None = None
    score: float = 0.0
    score_breakdown: dict[str, float] = field(default_factory=dict)
    summary_zh: str | None = None
    technical_route: str | None = None
    possible_use_or_impact: str | None = None
    reusable_point: str | None = None
    recommendation_reason: str | None = None
    quota_group: str | None = None
    is_historical_supplement: bool = False
    update_label: UpdateLabel = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def all_tags(self) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for tag in [*self.fixed_tags, *self.llm_tags, *self.source_tags]:
            if tag and tag not in seen:
                seen.add(tag)
                result.append(tag)
        return result

    def canonical_key(self) -> str:
        if self.source == "arxiv" and self.arxiv_id:
            return f"arxiv:{self.arxiv_id}"
        if self.item_type == "paper" and self.doi:
            return f"doi:{self.doi.lower()}"
        if self.source == "github":
            return f"github:{self.id.lower()}"
        return f"{self.source}:{self.id.lower()}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DigestItem":
        allowed = cls.__dataclass_fields__.keys()
        return cls(**{key: value for key, value in data.items() if key in allowed})


@dataclass
class MaterialRecord:
    key: str
    source: SourceName
    item_type: ItemType
    title: str
    url: str
    pdf_url: str | None = None
    code_url: str | None = None
    authors: list[str] = field(default_factory=list)
    abstract: str | None = None
    repo_description: str | None = None
    readme_excerpt: str | None = None
    paper_text_excerpt: str | None = None
    paper_text_status: dict[str, Any] = field(default_factory=dict)
    paper_document: dict[str, Any] = field(default_factory=dict)
    reading: dict[str, Any] = field(default_factory=dict)
    categories: list[str] = field(default_factory=list)
    language: str | None = None
    stars: int | None = None
    forks: int | None = None
    doi: str | None = None
    first_seen_at: str = field(default_factory=utc_now_iso)
    last_seen_at: str = field(default_factory=utc_now_iso)
    source_updated_at: str | None = None
    quota_group: str | None = None
    tags: list[str] = field(default_factory=list)
    score: float = 0.0
    score_breakdown: dict[str, float] = field(default_factory=dict)
    quality_status: QualityStatus = "candidate"
    published_dates: list[str] = field(default_factory=list)
    update_label: UpdateLabel = None
    source_aliases: dict[str, str] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)
    detail: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_digest_item(self) -> DigestItem:
        item = DigestItem(
            id=self.key.split(":", 1)[1] if ":" in self.key else self.key,
            source=self.source,
            item_type=self.item_type,
            title=self.title,
            url=self.url,
            pdf_url=self.pdf_url,
            code_url=self.code_url,
            authors=self.authors,
            abstract=self.abstract,
            repo_description=self.repo_description,
            updated_at=self.source_updated_at,
            source_tags=self.tags,
            categories=self.categories,
            language=self.language,
            stars=self.stars,
            forks=self.forks,
            doi=self.doi,
            score=self.score,
            score_breakdown=self.score_breakdown,
            quota_group=self.quota_group,
            update_label=self.update_label,
            raw={**self.raw, "evidence": _merge_evidence(self.raw.get("evidence") or {}, self.evidence)},
        )
        if self.source == "arxiv":
            item.arxiv_id = self.key.split(":", 1)[1]
            item.arxiv_version = self.raw.get("arxiv_version")
            item.published_at = self.raw.get("published_at")
        return item

    @classmethod
    def from_item(cls, item: DigestItem, seen_at: str | None = None) -> "MaterialRecord":
        timestamp = seen_at or utc_now_iso()
        return cls(
            key=item.canonical_key(),
            source=item.source,
            item_type=item.item_type,
            title=item.title,
            url=item.url,
            pdf_url=item.pdf_url,
            code_url=item.code_url,
            authors=item.authors,
            abstract=item.abstract,
            repo_description=item.repo_description,
            categories=item.categories,
            language=item.language,
            stars=item.stars,
            forks=item.forks,
            doi=item.doi,
            first_seen_at=timestamp,
            last_seen_at=timestamp,
            source_updated_at=item.updated_at,
            quota_group=item.quota_group,
            tags=item.all_tags,
            score=item.score,
            score_breakdown=item.score_breakdown,
            quality_status="library" if item.score > -50 else "candidate",
            update_label=item.update_label,
            source_aliases={**_source_aliases_from_item(item), **(item.raw.get("source_aliases") or {})},
            evidence=_merge_evidence(_evidence_from_item(item), item.raw.get("evidence") or {}),
            paper_text_status=item.raw.get("paper_text_status") or {},
            raw={
                **item.raw,
                "arxiv_version": item.arxiv_version,
                "published_at": item.published_at,
                "is_historical_supplement": item.is_historical_supplement,
            },
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MaterialRecord":
        allowed = cls.__dataclass_fields__.keys()
        return cls(**{key: value for key, value in data.items() if key in allowed})


@dataclass
class EditorialDraft:
    key: str
    item_type: ItemType
    title: str
    draft_fields: dict[str, Any]
    evidence_used: list[str] = field(default_factory=list)
    writer_notes: str | None = None
    claim_evidence: list[dict[str, Any]] = field(default_factory=list)
    verification: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EditorialDraft":
        return cls(**data)


@dataclass
class EditorialReview:
    key: str
    verdict: ReviewVerdict
    issues: list[str] = field(default_factory=list)
    required_changes: list[str] = field(default_factory=list)
    reader_value_score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EditorialReview":
        return cls(**data)


@dataclass
class ApprovedItem:
    key: str
    item_type: ItemType
    title: str
    source: SourceName
    url: str
    final_fields: dict[str, Any]
    material: MaterialRecord
    approval_notes: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["material"] = self.material.to_dict()
        return payload


@dataclass
class DeliveryStatus:
    requested_mode: str = "local"
    final_mode: str = "local"
    ok: bool = True
    fallback_used: bool = False
    document_url: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DeliveryStatus":
        return cls(**data)


@dataclass
class SourceStatus:
    name: str
    ok: bool
    item_count: int = 0
    retries: int = 0
    error: str | None = None
    skipped: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RunStatus:
    generated_at: str = field(default_factory=utc_now_iso)
    sources: list[SourceStatus] = field(default_factory=list)
    fallback: str | None = None
    errors: list[str] = field(default_factory=list)
    delivery: DeliveryStatus | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "sources": [source.to_dict() for source in self.sources],
            "fallback": self.fallback,
            "errors": self.errors,
            "delivery": self.delivery.to_dict() if self.delivery else None,
        }

    def source_line(self) -> str:
        parts = []
        for source in self.sources:
            if source.skipped:
                state = "跳过"
                detail = source.error or "未启用"
            else:
                state = "成功" if source.ok else "失败"
                detail = f"{source.item_count} 条" if source.ok else (source.error or "未知错误")
            parts.append(f"{source.name} {state}（{detail}）")
        return "；".join(parts) if parts else "未记录数据源状态（可能由离线重渲染或素材库 fallback 生成）"


@dataclass
class SelectedRecord:
    key: str
    source: SourceName
    item_type: ItemType
    title: str
    url: str
    selected_at: str
    pdf_url: str | None = None
    local_pdf_path: str | None = None
    arxiv_version: str | None = None
    github_pushed_at: str | None = None
    github_latest_release_tag: str | None = None
    github_latest_release_published_at: str | None = None
    github_latest_tag_name: str | None = None
    stars: int | None = None
    update_label: UpdateLabel = None
    rank: int | None = None
    tags: list[str] = field(default_factory=list)
    quota_group: str | None = None
    score: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_item(cls, item: DigestItem, selected_at: str | None = None, rank: int | None = None) -> "SelectedRecord":
        return cls(
            key=item.canonical_key(),
            source=item.source,
            item_type=item.item_type,
            title=item.title,
            url=item.url,
            selected_at=selected_at or utc_now_iso(),
            pdf_url=item.pdf_url,
            local_pdf_path=item.raw.get("local_pdf_path"),
            arxiv_version=item.arxiv_version,
            github_pushed_at=item.raw.get("pushed_at") if item.source == "github" else None,
            github_latest_release_tag=item.raw.get("latest_release_tag") if item.source == "github" else None,
            github_latest_release_published_at=item.raw.get("latest_release_published_at") if item.source == "github" else None,
            github_latest_tag_name=item.raw.get("latest_tag_name") if item.source == "github" else None,
            stars=item.stars,
            update_label=item.update_label,
            rank=rank,
            tags=item.all_tags,
            quota_group=item.quota_group,
            score=item.score,
        )

    @classmethod
    def from_material(cls, material: MaterialRecord, selected_at: str | None = None, rank: int | None = None) -> "SelectedRecord":
        return cls(
            key=material.key,
            source=material.source,
            item_type=material.item_type,
            title=material.title,
            url=material.url,
            selected_at=selected_at or utc_now_iso(),
            pdf_url=material.pdf_url,
            local_pdf_path=material.raw.get("local_pdf_path"),
            arxiv_version=material.raw.get("arxiv_version"),
            github_pushed_at=material.raw.get("pushed_at") if material.source == "github" else None,
            github_latest_release_tag=material.raw.get("latest_release_tag") if material.source == "github" else None,
            github_latest_release_published_at=material.raw.get("latest_release_published_at") if material.source == "github" else None,
            github_latest_tag_name=material.raw.get("latest_tag_name") if material.source == "github" else None,
            stars=material.stars,
            update_label=material.update_label,
            rank=rank,
            tags=material.tags,
            quota_group=material.quota_group,
            score=material.score,
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SelectedRecord":
        allowed = cls.__dataclass_fields__.keys()
        return cls(**{key: value for key, value in data.items() if key in allowed})


@dataclass
class FeedbackEvent:
    event_id: str
    created_at: str
    run_date: str
    rank: int
    key: str
    title: str
    item_type: ItemType
    signal: FeedbackSignal
    channel: str = "local"
    tags_snapshot: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    note: str | None = None
    origin: str = "cli"
    external_id: str | None = None
    external_parent_id: str | None = None
    source_updated_at: str | None = None
    fingerprint: str | None = None
    status: str = "active"
    status_reason: str | None = None
    status_updated_at: str | None = None
    superseded_by_event_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FeedbackEvent":
        allowed = cls.__dataclass_fields__.keys()
        return cls(**{key: value for key, value in data.items() if key in allowed})
