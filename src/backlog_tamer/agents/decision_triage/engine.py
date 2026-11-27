"""Decision triage: the TriageWorkflow seam without an agent loop.

Each turn reads the source once, asks a decision model typed questions
(type, then intent/tags/priority with the type known) and runs one small LLM
call for the title, in parallel. No summary is written and no Tasks are
proposed: the summary is the page's own description, and Tasks are left to
the user.

A Revision asks the decision model which fields the feedback is about, then
re-decides only those; every other field, and every quick edit, is kept.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import DateTime, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sqlalchemy.pool import NullPool

from backlog_tamer.config import Settings
from backlog_tamer.database_urls import to_sync_database_url, uses_external_pooler
from backlog_tamer.integrations.source_reader import FetchedUrl, SourceReader

from ..intake_triage.prompts import build_review_message, build_review_snapshot
from ..intake_triage.schemas import DraftGrounding, IncomingContext, ProjectDraft
from ..intake_triage.triage_workflow import (
    Approved,
    Checkpoint,
    NeedsReview,
    Rejected,
    TurnOutcome,
    _grounding_from_state,
)
from ..intake_triage.workflow import FETCHED_CONTEXT_STATE_KEY
from . import questions as q
from .client import DecisionClient

logger = logging.getLogger(__name__)

INVOCATION_ID = "decision-triage"
SUMMARY_MAX = 600


# ------------------------------------------------------------------ session


class _Base(DeclarativeBase):
    pass


class _SessionRow(_Base):
    __tablename__ = "decision_triage_sessions"

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


@dataclass
class _Session:
    context: IncomingContext
    known_topics: list[str]
    fetched: dict[str, Any] | None
    draft: ProjectDraft | None = None
    confidence: dict[str, float] = field(default_factory=dict)
    turn: int = 0

    def dump(self) -> str:
        return json.dumps(
            {
                "context": self.context.model_dump(mode="json"),
                "known_topics": self.known_topics,
                "fetched": self.fetched,
                "draft": self.draft.model_dump(mode="json") if self.draft else None,
                "confidence": self.confidence,
                "turn": self.turn,
            }
        )

    @classmethod
    def load(cls, raw: str) -> _Session:
        data = json.loads(raw)
        return cls(
            context=IncomingContext.model_validate(data["context"]),
            known_topics=data["known_topics"],
            fetched=data["fetched"],
            draft=ProjectDraft.model_validate(data["draft"]) if data["draft"] else None,
            confidence=data.get("confidence") or {},
            turn=data.get("turn", 0),
        )


class DecisionSessionStore:
    """Holds an Intake's input and Draft between turns (Lambda keeps nothing)."""

    def __init__(self, database_url: str):
        self._engine = create_engine(
            to_sync_database_url(database_url),
            poolclass=NullPool if uses_external_pooler(database_url) else None,
        )
        self._sessions = sessionmaker(bind=self._engine, expire_on_commit=False)
        _Base.metadata.create_all(self._engine)

    def save(self, session_id: str, session: _Session) -> None:
        with self._sessions.begin() as db:
            db.merge(
                _SessionRow(
                    session_id=session_id,
                    payload_json=session.dump(),
                    updated_at=datetime.now(UTC),
                )
            )

    def load(self, session_id: str) -> _Session:
        with self._sessions() as db:
            row = db.get(_SessionRow, session_id)
            if row is None:
                raise RuntimeError(f"No decision triage session {session_id}.")
            return _Session.load(row.payload_json)


# ------------------------------------------------------------------ engine


class DecisionTriage:
    """Same five operations as TriageWorkflow, so IntakeService can hold either."""

    def __init__(
        self, client: DecisionClient, reader: SourceReader, store: DecisionSessionStore
    ):
        self._client = client
        self._reader = reader
        self._store = store

    @classmethod
    def from_settings(
        cls, settings: Settings, database_url: str | None = None
    ) -> DecisionTriage:
        return cls(
            DecisionClient(
                settings.openrouter_api_key.get_secret_value(),
                decision_model=settings.triage.decision_model,
                title_model=settings.triage.title_model,
            ),
            SourceReader.from_api_key(settings.browserbase_api_key.get_secret_value()),
            DecisionSessionStore(database_url or settings.database_url),
        )

    async def start(
        self, context: IncomingContext, user_id: str, known_topics: list[str]
    ) -> TurnOutcome:
        session = _Session(
            context=context,
            known_topics=known_topics,
            fetched=await self._read(context),
        )
        await self._classify(session, kept_fields={})
        return self._needs_review(user_id, str(uuid4()), session)

    async def refetch(
        self, checkpoint: Checkpoint, kept_fields: dict[str, str]
    ) -> TurnOutcome:
        session = self._store.load(checkpoint.session_id)
        session.fetched = await self._read(session.context)
        await self._classify(session, kept_fields=kept_fields)
        return self._needs_review(checkpoint.user_id, checkpoint.session_id, session)

    async def revise(
        self, checkpoint: Checkpoint, feedback: str, kept_fields: dict[str, str]
    ) -> TurnOutcome:
        session = self._store.load(checkpoint.session_id)
        current = _with_kept(session.draft, kept_fields)
        vocabulary = q.subject_vocabulary(session.known_topics)
        base = self._state(session)
        state = {
            **base,
            "current_draft": _draft_view(current),
            "reviewer_feedback": feedback,
        }

        # Routing, type and the details in one round trip: questions cannot see
        # each other, but none of these needs another's answer except intent,
        # which reads the current type from current_draft.
        state_with_type = {**state, "decided_project_type": current.project_type}
        answers = await self._client.decide(
            state_with_type,
            {
                **q.routing_questions(),
                "project_type": q.type_question(revising=True),
                **q.detail_questions(vocabulary, has_note=True, revising=True),
            },
        )
        routed = q.read_routes(answers) - set(kept_fields)
        updates: dict[str, Any] = {}
        if "project_type" in routed:
            value, session.confidence["project_type"] = q.read_choice(
                answers, "project_type"
            )
            updates["project_type"] = value or current.project_type
        if "intent" in routed:
            value, session.confidence["intent"] = q.read_choice(answers, "intent")
            updates["intent"] = value or current.intent
        if "priority" in routed:
            updates["priority"], session.confidence["priority"] = q.read_priority(
                answers
            )
        if "topics" in routed:
            updates["topics"] = q.read_topics(answers, vocabulary)
        if "project_name" in routed:
            title = await self._client.title(
                _page_view(session), feedback=feedback, current=current.project_name
            )
            updates["project_name"] = title.project_name
            updates["short_name"] = title.short_name

        logger.info(
            "decision_triage_revision routed=%s kept=%s",
            sorted(routed),
            sorted(kept_fields),
        )
        session.draft = current.model_copy(update=updates)
        return self._needs_review(checkpoint.user_id, checkpoint.session_id, session)

    async def approve(self, checkpoint: Checkpoint) -> TurnOutcome:
        return Approved()

    async def reject(self, checkpoint: Checkpoint) -> TurnOutcome:
        try:
            return Rejected(draft=self._store.load(checkpoint.session_id).draft)
        except RuntimeError:
            return Rejected(draft=None)

    # -------------------------------------------------------------- helpers

    async def _read(self, context: IncomingContext) -> dict[str, Any] | None:
        if not context.links:
            return None
        result: FetchedUrl = await self._reader.read(str(context.links[0].url))
        return result.model_dump(mode="json")

    async def _classify(
        self, session: _Session, *, kept_fields: dict[str, str]
    ) -> None:
        state = self._state(session)
        vocabulary = q.subject_vocabulary(session.known_topics)
        has_note = bool(_note(session.context))

        async def details() -> tuple[dict[str, Any], dict[str, Any]]:
            first = await self._client.decide(
                state, {"project_type": q.type_question()}
            )
            decided, _ = q.read_choice(first, "project_type")
            second = await self._client.decide(
                {**state, "decided_project_type": decided},
                q.detail_questions(vocabulary, has_note=has_note),
            )
            return first, second

        (first, second), title = await asyncio.gather(
            details(), self._client.title(_page_view(session))
        )

        project_type, type_conf = q.read_choice(first, "project_type")
        intent, intent_conf = q.read_choice(second, "intent")
        priority, priority_conf = q.read_priority(second)
        session.confidence = {
            "project_type": type_conf,
            "intent": intent_conf,
            "priority": priority_conf,
        }
        draft = ProjectDraft(
            project_name=title.project_name,
            short_name=title.short_name,
            summary=_summary(session),
            project_type=project_type or "product",
            intent=intent or "unclear",
            priority=priority,
            source_url=_source_url(session),
            topics=q.read_topics(second, vocabulary),
            tasks=[],
        )
        session.draft = _with_kept(draft, kept_fields)
        logger.info(
            "decision_triage_draft confidence=%s", json.dumps(session.confidence)
        )

    def _state(self, session: _Session) -> dict[str, Any]:
        state: dict[str, Any] = {"user": q.USER_CONTEXT, **_page_view(session)}
        note = _note(session.context)
        if note:
            state["user_note"] = note
        return state

    def _needs_review(
        self, user_id: str, session_id: str, session: _Session
    ) -> NeedsReview:
        session.turn += 1
        self._store.save(session_id, session)
        draft = session.draft
        assert draft is not None
        fetched = _fetched_state(session, draft)
        return NeedsReview(
            draft=draft,
            grounding=_grounding(fetched, draft),
            review_message=build_review_message(
                draft_snapshot=build_review_snapshot(
                    draft, fetched.get(FETCHED_CONTEXT_STATE_KEY)
                )
            ),
            checkpoint=Checkpoint(
                user_id=user_id,
                session_id=session_id,
                invocation_id=INVOCATION_ID,
                request_input_call_id=f"turn-{session.turn}",
            ),
        )


# ------------------------------------------------------------------ pure helpers


def _note(context: IncomingContext) -> str | None:
    """The user's own words, not a pasted URL repeated as the note."""
    links = {str(link.url).rstrip("/") for link in context.links}
    for text in (context.note, context.raw_text):
        cleaned = (text or "").strip()
        if cleaned and cleaned.rstrip("/") not in links and cleaned.lower() != "none":
            return cleaned[:2000]
    return None


def _page_view(session: _Session) -> dict[str, Any]:
    fetched = session.fetched or {}
    url = str(session.context.links[0].url) if session.context.links else None
    view: dict[str, Any] = {"url": url}
    if fetched.get("status") == "success":
        for key in (
            "domain",
            "page_kind",
            "site_name",
            "title",
            "description",
            "author",
            "published_at",
            "key_points",
            "content_preview",
        ):
            if fetched.get(key):
                view[key] = fetched[key]
    elif url:
        view["fetch"] = "The page could not be fetched; judge from the URL alone."
    else:
        view["text"] = session.context.raw_text[:4000]
    return view


def _summary(session: _Session) -> str:
    fetched = session.fetched or {}
    text = (
        fetched.get("description")
        or fetched.get("title")
        or _note(session.context)
        or session.context.raw_text
    )
    return text.strip()[:SUMMARY_MAX] or "Saved item."


def _source_url(session: _Session) -> str | None:
    fetched = session.fetched or {}
    if fetched.get("status") == "success":
        return (
            fetched.get("canonical_url")
            or fetched.get("final_url")
            or fetched.get("requested_url")
        )
    return str(session.context.links[0].url) if session.context.links else None


def _fetched_state(session: _Session, draft: ProjectDraft) -> dict[str, Any]:
    if not session.fetched:
        return {}
    return {FETCHED_CONTEXT_STATE_KEY: {draft.source_url or "source": session.fetched}}


def _grounding(fetched_state: dict[str, Any], draft: ProjectDraft) -> DraftGrounding:
    if not fetched_state:
        return DraftGrounding(fetch_status="skipped")
    return _grounding_from_state(fetched_state, draft)


def _draft_view(draft: ProjectDraft) -> dict[str, Any]:
    return {
        "title": draft.project_name,
        "project_type": draft.project_type,
        "intent": draft.intent,
        "priority": draft.priority,
        "topics": draft.topics,
    }


def _with_kept(draft: ProjectDraft | None, kept_fields: dict[str, str]) -> ProjectDraft:
    if draft is None:
        raise RuntimeError("Decision triage session has no Draft.")
    if not kept_fields:
        return draft
    return ProjectDraft.model_validate({**draft.model_dump(), **kept_fields})
