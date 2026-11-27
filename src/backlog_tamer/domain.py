"""Domain types shared by Triage, the application and the integrations."""

import re
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


class Grounding(BaseModel):
    """What was actually read from the source behind a Draft, kept beside it.

    Persisting it lets the review card show its confidence, the Notion page
    carry the key points, and a Revision judge the page without a refetch.
    Rows stored before the page fields existed load with them empty.
    """

    fetch_status: Literal["success", "error", "skipped"] = "skipped"
    fetch_error: str | None = None
    site_name: str | None = None
    page_title: str | None = None
    canonical_url: str | None = None
    final_url: str | None = None
    description: str | None = None
    author: str | None = None
    published_at: str | None = None
    domain: str | None = None
    page_kind: str | None = None
    key_points: list[str] = Field(default_factory=list)
    content_preview: str | None = None

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

    def with_edits(self, fields: dict[str, str]) -> "ProjectDraft":
        """This Draft with `fields` applied, validated like a fresh one."""
        if not fields:
            return self
        return ProjectDraft.model_validate({**self.model_dump(), **fields})


def fold_tag(tag: str) -> str:
    """Fold spelling differences that should not count as a second tag."""
    folded = re.sub(r"[^a-z0-9]+", " ", tag.casefold()).strip()
    return " ".join(
        word[:-1] if len(word) > 3 and word.endswith("s") else word
        for word in folded.split()
    )
