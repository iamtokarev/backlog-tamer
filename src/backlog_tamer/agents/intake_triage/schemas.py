from typing import Any, Literal

from pydantic import BaseModel, Field, HttpUrl, model_validator

LEGACY_RESOURCE_TYPE_TO_PROJECT_TYPE = {
    "article": "article",
    "paper": "paper",
    "video": "video",
    "course": "course",
    "documentation": "tool",
    "repository": "repository",
    "idea": "product",
    "unknown": "product",
}


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
    short_name: str = Field(
        default="",
        max_length=60,
        description=(
            "The bare handle the project is known by, 2 to 4 words, with no "
            'payoff clause: "SKILL.state", "NVIDIA PAIR", "Stanford CS146S".'
        ),
    )
    summary: str = Field(min_length=1, max_length=600)
    project_type: Literal[
        "paper",
        "article",
        "video",
        "course",
        "repository",
        "product",
        "company",
        "model",
        "tool",
    ] = Field(
        description=(
            "The kind of thing the user wants to explore, not the page or URL "
            "that introduced it."
        )
    )
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

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_resource_type(cls, value: Any) -> Any:
        """Load drafts persisted before project_type replaced resource_type.

        Source-shaped legacy values cannot always recover the exact target.
        Preserve the two unambiguous classifications and use a stable closest
        match for the others so pending confirmations remain reviewable.
        """
        if not isinstance(value, dict):
            return value

        migrated = dict(value)
        legacy_type = migrated.pop("resource_type", None)
        project_type = migrated.get("project_type", legacy_type)
        if isinstance(project_type, str):
            project_type = LEGACY_RESOURCE_TYPE_TO_PROJECT_TYPE.get(
                project_type,
                project_type,
            )
        migrated["project_type"] = project_type
        return migrated

    @property
    def effective_short_name(self) -> str:
        """The handle to build task names from.

        Drafts persisted before short_name existed fall back to the part of
        project_name before the payoff clause, which is where the agent
        already puts the handle ("SKILL.state: scalable long-horizon ...").
        """
        if self.short_name.strip():
            return self.short_name.strip()

        head = self.project_name.split(":", 1)[0].strip()
        return head or self.project_name.strip()


class ReviewDecision(BaseModel):
    action: Literal[
        "approve",
        "reject",
        "revise",
    ]
    feedback: str | None = None
