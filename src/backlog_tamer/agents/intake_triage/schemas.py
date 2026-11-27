"""Re-exports the domain types until the ADK agent is removed."""

from typing import Literal

from pydantic import BaseModel

from backlog_tamer.domain import (
    LEGACY_RESOURCE_TYPE_TO_PROJECT_TYPE,
    Grounding,
    IncomingContext,
    ProjectDraft,
    SourceLink,
    fold_tag,
)

__all__ = [
    "LEGACY_RESOURCE_TYPE_TO_PROJECT_TYPE",
    "Grounding",
    "IncomingContext",
    "ProjectDraft",
    "ReviewDecision",
    "SourceLink",
    "fold_tag",
]


class ReviewDecision(BaseModel):
    action: Literal[
        "approve",
        "reject",
        "revise",
    ]
    feedback: str | None = None
