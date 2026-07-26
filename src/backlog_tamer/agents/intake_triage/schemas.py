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


class DraftGrounding(BaseModel):
    """What the fetch tool actually learned, kept beside the draft.

    The tool results live in ADK session state and were discarded once the
    draft existed; persisting this lets the review card show its confidence
    and the Notion page carry the key points.
    """

    fetch_status: Literal["success", "error", "skipped"] = "skipped"
    fetch_error: str | None = None
    site_name: str | None = None
    page_title: str | None = None
    canonical_url: str | None = None
    key_points: list[str] = Field(default_factory=list)

    @property
    def is_degraded(self) -> bool:
        return self.fetch_status == "error"


class ProjectDraft(BaseModel):
    project_name: str = Field(min_length=1)
    summary: str = Field(min_length=1, max_length=600)
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
        "reference",
        "unclear",
    ]
    priority: Literal["Low", "Medium", "High"]
    source_url: str | None = None
    topics: list[str] = Field(default_factory=list, max_length=3)
    tasks: list[str] = Field(default_factory=list)


class ReviewDecision(BaseModel):
    action: Literal[
        "approve",
        "reject",
        "revise",
    ]
    feedback: str | None = None
