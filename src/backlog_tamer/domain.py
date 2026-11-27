"""Domain types shared by Triage, the application and the integrations."""

import re
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl

SUMMARY_MAX_CHARS = 600
MAX_TOPICS = 3


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
    summary: str = Field(min_length=1, max_length=SUMMARY_MAX_CHARS)
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
    topics: list[str] = Field(default_factory=list, max_length=MAX_TOPICS)

    def with_edits(self, fields: dict[str, str]) -> "ProjectDraft":
        """This Draft with `fields` applied, validated like a fresh one."""
        return ProjectDraft.model_validate({**self.model_dump(), **fields})


def fold_tag(tag: str) -> str:
    """Fold spelling differences that should not count as a second tag."""
    folded = re.sub(r"[^a-z0-9]+", " ", tag.casefold()).strip()
    return " ".join(
        word[:-1] if len(word) > 3 and word.endswith("s") else word
        for word in folded.split()
    )
