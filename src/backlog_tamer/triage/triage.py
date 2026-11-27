"""Triage: a Draft from typed decisions, with no agent loop and no state.

Classify asks the decision model typed questions (type, then intent/tags/
priority with the type known) and runs one small LLM call for the title, in
parallel. No summary is written and no Tasks are proposed: the summary is the
page's own description, and Tasks are left to the user.

A Revision asks the decision model which fields the feedback is about, then
re-decides only those; every other field is kept. Quick edits are the
caller's: Triage never sees them.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from backlog_tamer.config import Settings
from backlog_tamer.domain import (
    SUMMARY_MAX_CHARS,
    Grounding,
    IncomingContext,
    ProjectDraft,
)

from . import questions as q
from .client import DecisionClient

logger = logging.getLogger(__name__)

RAW_TEXT_MAX = 4000
NOTE_MAX = 2000
UNREADABLE_PAGE = "The page could not be fetched; judge from the URL alone."


class Triage:
    def __init__(self, client: DecisionClient):
        self._client = client

    @classmethod
    def from_settings(cls, settings: Settings) -> Triage:
        return cls(
            DecisionClient(
                settings.openrouter_api_key.get_secret_value(),
                decision_model=settings.triage.decision_model,
                title_model=settings.triage.title_model,
            )
        )

    async def probe(self) -> None:
        """Raise unless both configured models answer; see DecisionClient.probe."""
        await self._client.probe()

    async def classify(
        self,
        context: IncomingContext,
        grounding: Grounding,
        vocabulary: list[str],
    ) -> ProjectDraft:
        page = _page_view(context, grounding)
        state = _state(context, page)
        tags = q.subject_vocabulary(vocabulary)
        has_note = bool(_note(context))

        async def details() -> tuple[dict[str, Any], dict[str, Any]]:
            first = await self._client.decide(
                state, {"project_type": q.type_question()}
            )
            decided, _ = q.read_choice(first, "project_type")
            second = await self._client.decide(
                {**state, "decided_project_type": decided},
                q.detail_questions(tags, has_note=has_note),
            )
            return first, second

        (first, second), title = await asyncio.gather(
            details(), self._client.title(page)
        )

        project_type, type_conf = q.read_choice(first, "project_type")
        intent, intent_conf = q.read_choice(second, "intent")
        priority, priority_conf = q.read_priority(second)
        logger.info(
            "decision_triage_draft confidence=%s",
            json.dumps(
                {
                    "project_type": type_conf,
                    "intent": intent_conf,
                    "priority": priority_conf,
                }
            ),
        )
        return ProjectDraft(
            project_name=title,
            summary=_summary(context, grounding),
            project_type=project_type or "product",
            intent=intent or "unclear",
            priority=priority or "Medium",
            source_url=_source_url(context, grounding),
            topics=q.read_topics(second, tags) or [],
        )

    async def revise(
        self,
        context: IncomingContext,
        grounding: Grounding,
        draft: ProjectDraft,
        feedback: str,
        vocabulary: list[str],
    ) -> ProjectDraft:
        page = _page_view(context, grounding)
        tags = q.subject_vocabulary(vocabulary)
        # Routing, type and the details in one round trip: questions cannot see
        # each other, but none of these needs another's answer except intent,
        # which reads the current type from decided_project_type.
        state = {
            **_state(context, page),
            "current_draft": _draft_view(draft),
            "reviewer_feedback": feedback,
            "decided_project_type": draft.project_type,
        }
        answers = await self._client.decide(
            state,
            {
                **q.routing_questions(),
                "project_type": q.type_question(revising=True),
                **q.detail_questions(tags, has_note=True, revising=True),
            },
        )
        routed = q.read_routes(answers)
        updates: dict[str, Any] = {}
        confidence: dict[str, float] = {}
        if "project_type" in routed:
            value, confidence["project_type"] = q.read_choice(answers, "project_type")
            updates["project_type"] = value or draft.project_type
        if updates.get("project_type", draft.project_type) != draft.project_type:
            # The intent asked above judged the old type; ask again with the
            # new one. Only a type change pays for this second round trip.
            retyped = await self._client.decide(
                {**state, "decided_project_type": updates["project_type"]},
                {"intent": q.intent_question(revising=True)},
            )
            answers = {**answers, "intent": retyped.get("intent")}
            routed.add("intent")
        if "intent" in routed:
            value, confidence["intent"] = q.read_choice(answers, "intent")
            updates["intent"] = value or draft.intent
        if "priority" in routed:
            value, confidence["priority"] = q.read_priority(answers)
            updates["priority"] = value or draft.priority
        if "topics" in routed:
            topics = q.read_topics(answers, tags)
            updates["topics"] = draft.topics if topics is None else topics
        if "project_name" in routed:
            updates["project_name"] = await self._client.title(
                page, feedback=feedback, current=draft.project_name
            )

        logger.info(
            "decision_triage_revision routed=%s confidence=%s",
            sorted(routed),
            json.dumps(confidence),
        )
        return ProjectDraft.model_validate({**draft.model_dump(), **updates})


def _state(context: IncomingContext, page: dict[str, Any]) -> dict[str, Any]:
    state: dict[str, Any] = {"user": q.USER_CONTEXT, **page}
    note = _note(context)
    if note:
        state["user_note"] = note
    return state


def _note(context: IncomingContext) -> str | None:
    """The user's own words, not a pasted URL repeated as the note."""
    links = {str(link.url).rstrip("/") for link in context.links}
    for text in (context.note, context.raw_text):
        cleaned = (text or "").strip()
        if cleaned and cleaned.rstrip("/") not in links and cleaned.lower() != "none":
            return cleaned[:NOTE_MAX]
    return None


def _page_view(context: IncomingContext, grounding: Grounding) -> dict[str, Any]:
    url = str(context.links[0].url) if context.links else None
    view: dict[str, Any] = {"url": url}
    if grounding.fetch_status == "success":
        for key, value in (
            ("domain", grounding.domain),
            ("page_kind", grounding.page_kind),
            ("site_name", grounding.site_name),
            ("title", grounding.page_title),
            ("description", grounding.description),
            ("author", grounding.author),
            ("published_at", grounding.published_at),
            ("key_points", grounding.key_points),
            ("content_preview", grounding.content_preview),
        ):
            if value:
                view[key] = value
    elif grounding.fetch_status == "error":
        view["fetch"] = UNREADABLE_PAGE
    else:
        view["text"] = context.raw_text[:RAW_TEXT_MAX]
    return view


def _summary(context: IncomingContext, grounding: Grounding) -> str:
    text = (
        grounding.description
        or grounding.page_title
        or _note(context)
        or context.raw_text
    )
    return text.strip()[:SUMMARY_MAX_CHARS] or "Saved item."


def _source_url(context: IncomingContext, grounding: Grounding) -> str | None:
    if grounding.canonical_url or grounding.final_url:
        return grounding.canonical_url or grounding.final_url
    return str(context.links[0].url) if context.links else None


def _draft_view(draft: ProjectDraft) -> dict[str, Any]:
    return {
        "title": draft.project_name,
        "project_type": draft.project_type,
        "intent": draft.intent,
        "priority": draft.priority,
        "topics": draft.topics,
    }
