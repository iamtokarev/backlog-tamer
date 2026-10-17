"""One Triage turn in, one Turn outcome out.

Everything ADK-shaped lives behind this module: the Runner and session
service, the session-state keys, the review interrupt and the reply that
resumes it. Callers name the human decision and get back a NeedsReview,
Approved or Rejected; they hold a Checkpoint between turns without reading it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from google.adk.runners import Runner
from google.adk.sessions import BaseSessionService, DatabaseSessionService
from google.genai import types

from backlog_tamer.config import Settings
from backlog_tamer.database_urls import (
    async_engine_options,
    to_adk_session_database_url,
)

from .prompts import REFETCH_FEEDBACK, build_revision_feedback
from .schemas import DraftGrounding, IncomingContext, ProjectDraft
from .task_names import with_composed_task_names
from .tools import fetch_url
from .workflow import (
    DRAFT_PROPOSAL_STATE_KEY,
    FETCHED_CONTEXT_STATE_KEY,
    ROUTE_APPROVED,
    ROUTE_REJECTED,
    ReviewDecision,
    build_review_reply,
    build_triage_message,
    build_triage_state_delta,
)

APP_NAME = "backlog_tamer"
REQUEST_INPUT_TOOL_NAME = "adk_request_input"
MAX_GROUNDING_KEY_POINTS = 4
_LANGSMITH_CONFIGURED = False


@dataclass(frozen=True)
class Checkpoint:
    """Where a paused Triage waits on the human; the next turn resumes here."""

    user_id: str
    session_id: str
    invocation_id: str
    request_input_call_id: str


@dataclass(frozen=True)
class NeedsReview:
    draft: ProjectDraft
    grounding: DraftGrounding
    review_message: str
    checkpoint: Checkpoint


@dataclass(frozen=True)
class Approved:
    pass


@dataclass(frozen=True)
class Rejected:
    draft: ProjectDraft


TurnOutcome = NeedsReview | Approved | Rejected


class TriageWorkflow:
    def __init__(self, graph: Any, session_service: BaseSessionService):
        self._session_service = session_service
        self._runner = Runner(
            agent=graph,
            app_name=APP_NAME,
            session_service=session_service,
        )

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        database_url: str | None = None,
        *,
        session_service: BaseSessionService | None = None,
    ) -> TriageWorkflow:
        """The production graph, over DB-backed sessions unless given others.

        The agent module is imported here, not at the top: it builds the LLM
        client at import time and needs the OpenRouter key to do it.
        """
        _configure_langsmith(settings)
        from .agent import root_agent

        if session_service is None:
            url = database_url or settings.database_url
            session_service = DatabaseSessionService(
                db_url=to_adk_session_database_url(url),
                **async_engine_options(url),
            )
        return cls(root_agent, session_service)

    async def start(
        self,
        context: IncomingContext,
        user_id: str,
        known_topics: list[str],
    ) -> TurnOutcome:
        session_id = str(uuid4())
        await self._session_service.create_session(
            app_name=APP_NAME,
            user_id=user_id,
            session_id=session_id,
        )
        events = await self._run_turn(
            user_id=user_id,
            session_id=session_id,
            message=build_triage_message(context, known_topics),
            state_delta=build_triage_state_delta(context, known_topics),
        )
        return await self._outcome(user_id, session_id, events)

    async def revise(
        self,
        checkpoint: Checkpoint,
        feedback: str,
        kept_fields: dict[str, str],
    ) -> TurnOutcome:
        return await self._resume(
            checkpoint,
            build_review_reply(
                ReviewDecision.REVISE,
                build_revision_feedback(feedback, kept_fields),
            ),
        )

    async def refetch(
        self,
        checkpoint: Checkpoint,
        kept_fields: dict[str, str],
    ) -> TurnOutcome:
        """Redraft from a fresh read of the source page.

        The fetch cache goes first, or the agent would be handed the same
        page it already read instead of reaching the site again.
        """
        fetch_url.clear_cache()
        return await self.revise(checkpoint, REFETCH_FEEDBACK, kept_fields)

    async def approve(self, checkpoint: Checkpoint) -> TurnOutcome:
        return await self._resume(
            checkpoint, build_review_reply(ReviewDecision.APPROVE)
        )

    async def reject(self, checkpoint: Checkpoint) -> TurnOutcome:
        return await self._resume(checkpoint, build_review_reply(ReviewDecision.REJECT))

    async def _resume(
        self,
        checkpoint: Checkpoint,
        reply: dict[str, str],
    ) -> TurnOutcome:
        events = await self._run_turn(
            user_id=checkpoint.user_id,
            session_id=checkpoint.session_id,
            invocation_id=checkpoint.invocation_id,
            message=types.Content(
                role="user",
                parts=[
                    types.Part(
                        function_response=types.FunctionResponse(
                            id=checkpoint.request_input_call_id,
                            name=REQUEST_INPUT_TOOL_NAME,
                            response=reply,
                        )
                    )
                ],
            ),
        )
        return await self._outcome(checkpoint.user_id, checkpoint.session_id, events)

    async def _run_turn(
        self,
        *,
        user_id: str,
        session_id: str,
        message: types.Content,
        invocation_id: str | None = None,
        state_delta: dict[str, Any] | None = None,
    ) -> list[Any]:
        return [
            event
            async for event in self._runner.run_async(
                user_id=user_id,
                session_id=session_id,
                invocation_id=invocation_id,
                new_message=message,
                state_delta=state_delta,
            )
        ]

    async def _outcome(
        self,
        user_id: str,
        session_id: str,
        events: list[Any],
    ) -> TurnOutcome:
        state = await self._session_state(user_id, session_id)
        draft = _draft_from_state(state)

        interrupt = _find_review_interrupt(events)
        if interrupt is not None:
            event, call = interrupt
            return NeedsReview(
                draft=draft,
                grounding=_grounding_from_state(state, draft),
                review_message=call.args.get("message", ""),
                checkpoint=Checkpoint(
                    user_id=user_id,
                    session_id=session_id,
                    invocation_id=event.invocation_id,
                    request_input_call_id=call.id,
                ),
            )

        route = _find_route(events)
        if route == ROUTE_APPROVED:
            return Approved()
        if route == ROUTE_REJECTED:
            return Rejected(draft=draft)
        raise RuntimeError(
            "Triage turn produced neither a review interrupt nor a final route.",
        )

    async def _session_state(self, user_id: str, session_id: str) -> dict[str, Any]:
        session = await self._session_service.get_session(
            app_name=APP_NAME,
            user_id=user_id,
            session_id=session_id,
        )
        if session is None:
            raise RuntimeError(
                "Session was not found after the Triage turn: "
                f"user_id={user_id}, session_id={session_id}",
            )
        return dict(session.state)


def _configure_langsmith(settings: Settings) -> None:
    global _LANGSMITH_CONFIGURED

    if (
        settings.langsmith_tracing
        and settings.langsmith_api_key
        and not _LANGSMITH_CONFIGURED
    ):
        settings.export_to_env()
        from langsmith.integrations.google_adk import configure_google_adk

        configure_google_adk()
        _LANGSMITH_CONFIGURED = True


def _draft_from_state(state: dict[str, Any]) -> ProjectDraft:
    payload = state.get(DRAFT_PROPOSAL_STATE_KEY)
    if payload is None:
        raise RuntimeError(f"No {DRAFT_PROPOSAL_STATE_KEY} found in session state.")
    # Name the default task after its subject before anyone sees the draft,
    # so the review card, Telegram and Notion all agree.
    return with_composed_task_names(ProjectDraft.model_validate(payload))


def _find_review_interrupt(events: list[Any]) -> tuple[Any, Any] | None:
    for event in events:
        if not event.content or not event.content.parts:
            continue
        call = getattr(event.content.parts[0], "function_call", None)
        if call is not None and call.name == REQUEST_INPUT_TOOL_NAME:
            return event, call
    return None


def _find_route(events: list[Any]) -> str | None:
    for event in events:
        route = getattr(event.actions, "route", None)
        if route:
            return route
    return None


def _grounding_from_state(
    state: dict[str, Any],
    draft: ProjectDraft,
) -> DraftGrounding:
    """Summarise what fetch_url found, for the card and the Notion page."""
    fetched = state.get(FETCHED_CONTEXT_STATE_KEY)
    if not isinstance(fetched, dict) or not fetched:
        return DraftGrounding(fetch_status="skipped")

    payload = _match_fetched_entry(fetched, draft.source_url)
    if not isinstance(payload, dict):
        return DraftGrounding(fetch_status="skipped")

    if payload.get("status") != "success":
        return DraftGrounding(
            fetch_status="error",
            fetch_error=payload.get("error"),
        )

    key_points = [
        str(point) for point in (payload.get("key_points") or []) if str(point)
    ]
    return DraftGrounding(
        fetch_status="success",
        site_name=payload.get("site_name"),
        page_title=payload.get("title"),
        canonical_url=payload.get("canonical_url") or payload.get("final_url"),
        key_points=key_points[:MAX_GROUNDING_KEY_POINTS],
    )


def _match_fetched_entry(fetched: dict[str, Any], source_url: str | None) -> Any:
    """Prefer the entry for the draft's own source, else the first fetch."""
    if source_url:
        for key, payload in fetched.items():
            if key == source_url:
                return payload
        for payload in fetched.values():
            if isinstance(payload, dict) and source_url in {
                payload.get("final_url"),
                payload.get("canonical_url"),
                payload.get("requested_url"),
            }:
                return payload
    return next(iter(fetched.values()), None)
