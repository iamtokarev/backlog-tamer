from __future__ import annotations

from typing import Any
from uuid import uuid4

from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService
from google.genai import types

from backlog_tamer.agents.intake_triage.schemas import DraftProposal, IncomingContext
from backlog_tamer.agents.intake_triage.workflow import build_triage_message
from backlog_tamer.config import Settings, get_settings

from .confirmation_store import ConfirmationStore, utc_now
from .models import ConfirmationRecord, ConfirmationStatus, IntakeResult

APP_NAME = "backlog_tamer"
REQUEST_INPUT_TOOL_NAME = "adk_request_input"
_LANGSMITH_CONFIGURED = False


def _to_session_service_db_url(database_url: str) -> str:
    if database_url.startswith("sqlite+aiosqlite:///"):
        return database_url
    if database_url.startswith("sqlite:///"):
        return database_url.replace("sqlite:///", "sqlite+aiosqlite:///", 1)
    if database_url.startswith("postgresql+asyncpg://"):
        return database_url
    if database_url.startswith("postgresql+psycopg://"):
        return database_url.replace("postgresql+psycopg://", "postgresql+asyncpg://", 1)
    if database_url.startswith("postgresql://"):
        return database_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return database_url


def _to_store_db_url(database_url: str) -> str:
    if database_url.startswith("sqlite+aiosqlite:///"):
        return database_url.replace("sqlite+aiosqlite:///", "sqlite:///", 1)
    if database_url.startswith("postgresql+asyncpg://"):
        return database_url.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)
    return database_url


class IntakeService:
    def __init__(
        self,
        *,
        database_url: str | None = None,
        store: ConfirmationStore | None = None,
        settings: Settings | None = None,
        app_name: str = APP_NAME,
    ):
        self.settings = settings or get_settings()
        self.database_url = database_url or self.settings.database_url
        self.store_database_url = _to_store_db_url(self.database_url)
        self.session_database_url = _to_session_service_db_url(self.database_url)
        self.app_name = app_name
        self.store = store or ConfirmationStore(self.store_database_url)
        self.session_service = DatabaseSessionService(db_url=self.session_database_url)
        self.runner = Runner(
            agent=self._get_root_agent(),
            app_name=self.app_name,
            session_service=self.session_service,
        )

    async def start_intake(
        self,
        context: IncomingContext,
        user_id: str,
        chat_id: str | None = None,
        source_message_id: str | None = None,
    ) -> IntakeResult:
        session_id = str(uuid4())
        await self.session_service.create_session(
            app_name=self.app_name,
            user_id=user_id,
            session_id=session_id,
        )

        events = await self._run_turn(
            user_id=user_id,
            session_id=session_id,
            message=build_triage_message(context),
        )

        draft = self._extract_draft(events)
        interrupt = self._extract_request_input(events)
        confirmation = ConfirmationRecord(
            confirmation_id=str(uuid4()),
            user_id=user_id,
            chat_id=chat_id,
            source_message_id=source_message_id,
            session_id=session_id,
            invocation_id=interrupt["invocation_id"],
            request_input_call_id=interrupt["request_input_call_id"],
            status=ConfirmationStatus.PENDING_REVIEW,
            incoming_context=context,
            draft_proposal=draft,
            review_message=interrupt["review_message"],
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        self.store.create_pending(confirmation)
        return IntakeResult(
            status="needs_review",
            confirmation_id=confirmation.confirmation_id,
            draft_proposal=draft,
            review_message=confirmation.review_message,
        )

    async def resume_intake(
        self,
        confirmation_id: str,
        review_reply: str,
    ) -> IntakeResult:
        confirmation = self.store.get(confirmation_id)
        if confirmation is None:
            raise ValueError(f"Unknown confirmation_id: {confirmation_id}")
        if confirmation.status is not ConfirmationStatus.PENDING_REVIEW:
            raise ValueError(
                "Confirmation "
                f"{confirmation_id} is not pending review: "
                f"{confirmation.status.value}",
            )

        events = await self._run_turn(
            user_id=confirmation.user_id,
            session_id=confirmation.session_id,
            invocation_id=confirmation.invocation_id,
            message=self._build_review_response(
                confirmation.request_input_call_id,
                review_reply,
            ),
        )

        interrupt = self._try_extract_request_input(events)
        if interrupt is not None:
            draft = self._extract_draft(events)
            self.store.update_after_resume(
                confirmation_id=confirmation_id,
                draft_proposal=draft,
                invocation_id=interrupt["invocation_id"],
                request_input_call_id=interrupt["request_input_call_id"],
                review_message=interrupt["review_message"],
            )
            return IntakeResult(
                status="needs_review",
                confirmation_id=confirmation_id,
                draft_proposal=draft,
                review_message=interrupt["review_message"],
            )

        route = self._extract_route(events)
        if route == "approved":
            self.store.mark_approved(confirmation_id)
            return IntakeResult(
                status="approved",
                confirmation_id=confirmation_id,
                draft_proposal=confirmation.draft_proposal,
            )
        if route == "rejected":
            self.store.mark_rejected(confirmation_id)
            return IntakeResult(
                status="rejected",
                confirmation_id=confirmation_id,
                draft_proposal=confirmation.draft_proposal,
            )

        raise RuntimeError(
            "Workflow resume produced neither a review interrupt nor a final route.",
        )

    async def _run_turn(
        self,
        *,
        user_id: str,
        session_id: str,
        message: types.Content,
        invocation_id: str | None = None,
    ) -> list[Any]:
        events: list[Any] = []
        async for event in self.runner.run_async(
            user_id=user_id,
            session_id=session_id,
            invocation_id=invocation_id,
            new_message=message,
        ):
            events.append(event)
        return events

    def _extract_draft(self, events: list[Any]) -> DraftProposal:
        for event in reversed(events):
            state_delta = getattr(event.actions, "state_delta", None) or {}
            if "draft_proposal" in state_delta:
                return DraftProposal.model_validate(state_delta["draft_proposal"])
        raise RuntimeError("No draft_proposal found in workflow events.")

    def _extract_request_input(self, events: list[Any]) -> dict[str, str]:
        interrupt = self._try_extract_request_input(events)
        if interrupt is None:
            raise RuntimeError("No review interrupt found in workflow events.")
        return interrupt

    def _try_extract_request_input(self, events: list[Any]) -> dict[str, str] | None:
        for event in events:
            if not event.content or not event.content.parts:
                continue
            function_call = getattr(event.content.parts[0], "function_call", None)
            if function_call is None or function_call.name != REQUEST_INPUT_TOOL_NAME:
                continue
            message = function_call.args.get("message", "")
            return {
                "invocation_id": event.invocation_id,
                "request_input_call_id": function_call.id,
                "review_message": message,
            }
        return None

    def _extract_route(self, events: list[Any]) -> str | None:
        for event in events:
            route = getattr(event.actions, "route", None)
            if route:
                return route
        return None

    def _build_review_response(self, call_id: str, reply_text: str) -> types.Content:
        return types.Content(
            role="user",
            parts=[
                types.Part(
                    function_response=types.FunctionResponse(
                        id=call_id,
                        name=REQUEST_INPUT_TOOL_NAME,
                        response={"value": reply_text},
                    )
                )
            ],
        )

    def _get_root_agent(self):
        global _LANGSMITH_CONFIGURED

        if (
            self.settings.langsmith_tracing
            and self.settings.langsmith_api_key
            and not _LANGSMITH_CONFIGURED
        ):
            from langsmith.integrations.google_adk import configure_google_adk

            configure_google_adk()
            _LANGSMITH_CONFIGURED = True

        from backlog_tamer.agents.intake_triage.agent import root_agent

        return root_agent
