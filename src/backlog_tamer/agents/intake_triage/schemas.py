from typing import Literal

from pydantic import BaseModel, Field, HttpUrl


class SourceLink(BaseModel):
    url: HttpUrl


class IncomingContext(BaseModel):
    raw_text: str = Field(min_length=1)
    note: str | None = None
    links: list[SourceLink] = Field(default_factory=list)


class FetchedUrl(BaseModel):
    status: Literal["success", "error"]
    requested_url: str = Field(min_length=1)
    final_url: str | None = None
    canonical_url: str | None = None
    domain: str | None = None
    page_kind: Literal["html", "pdf", "text", "unknown"] = "unknown"
    content_type: str | None = None
    status_code: int | None = None
    title: str | None = None
    description: str | None = None
    site_name: str | None = None
    author: str | None = None
    published_at: str | None = None
    key_points: list[str] = Field(default_factory=list)
    content_preview: str | None = Field(default=None, max_length=1600)
    notes: list[str] = Field(default_factory=list)
    error: str | None = None


class DraftProposal(BaseModel):
    title: str = Field(min_length=1)
    description: str = Field(min_length=1, max_length=300)

    resource_type: Literal[
        "article",
        "paper",
        "video",
        "course",
        "documentation",
        "repository",
        "idea",
        "unknown",
    ]

    intent: Literal[
        "learn",
        "build",
        "research",
        "explore",
        "unclear",
    ]

    source_url: str | None = None
    reasoning: str = Field(min_length=1, max_length=200)


class ReviewDecision(BaseModel):
    action: Literal[
        "approve",
        "reject",
        "revise",
    ]
    feedback: str | None = None
