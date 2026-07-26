from __future__ import annotations

from typing import Any
from uuid import uuid4

from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService
from google.genai import types

from backlog_tamer.agents.intake_triage.schemas import IncomingContext, ProjectDraft
from backlog_tamer.agents.intake_triage.workflow import (
    build_triage_message,
    build_triage_state_delta,
)
from backlog_tamer.config import Settings, get_settings
from backlog_tamer.integrations.notion import NotionWriter

from .confirmation_store import ConfirmationStore, utc_now
from .database_urls import to_adk_session_database_url
from .models import ConfirmationRecord, ConfirmationStatus, IntakeResult

APP_NAME = "backlog_tamer"
REQUEST_INPUT_TOOL_NAME = "adk_request_input"
_LANGSMITH_CONFIGURED = False


def _to_session_service_db_url(database_url: str) -> str:
    return to_adk_session_database_url(database_url)


def _with_manual_edits(review_reply: str, manual_edits: dict[str, str]) -> str:
    """Tell the agent which fields the user already fixed with the buttons.

    Quick edits patch the stored draft only; the workflow session still holds
    the agent's own last draft, so without this the next revision would
    silently undo them.
    """
    if not manual_edits or review_reply in {"approve", "reject"}:
        return review_reply

    applied = ", ".join(f"{field}={value}" for field, value in manual_edits.items())
    return (
        f"I already corrected these fields myself, keep them exactly as they are: "
        f"{applied}.\n\n{review_reply}"
    )


class IntakeService:
    def __init__(
        self,
        *,
        database_url: str | None = None,
        store: ConfirmationStore | None = None,
        settings: Settings | None = None,
        notion_writer: NotionWriter | None = None,
        app_name: str = APP_NAME,
    ):
        self.settings = settings or get_settings()
        self.database_url = database_url or self.settings.database_url
        self.session_database_url = _to_session_service_db_url(self.database_url)
        self.app_name = app_name
        self.store = store or ConfirmationStore(self.database_url)
        self.notion_writer = notion_writer
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
            state_delta=build_triage_state_delta(context),
        )

        session_state = await self._get_session_state(
            user_id=user_id,
            session_id=session_id,
        )
        draft = self._extract_draft_from_state(session_state)
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
        if confirmation.status in {
            ConfirmationStatus.COMMITTING,
            ConfirmationStatus.COMMITTED,
            ConfirmationStatus.REJECTED,
            ConfirmationStatus.FAILED,
        }:
            return IntakeResult(
                status=confirmation.status.value,
                confirmation_id=confirmation_id,
                draft_proposal=confirmation.draft_proposal,
                notion_project_url=confirmation.notion_project_url,
            )
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
                _with_manual_edits(review_reply, confirmation.manual_edits),
            ),
        )
        session_state = await self._get_session_state(
            user_id=confirmation.user_id,
            session_id=confirmation.session_id,
        )

        interrupt = self._try_extract_request_input(events)
        if interrupt is not None:
            draft = self._extract_draft_from_state(session_state)
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
            return await self.finalize_approval(confirmation_id)
        if route == "rejected":
            self.store.mark_rejected(confirmation_id)
            return IntakeResult(
                status="rejected",
                confirmation_id=confirmation_id,
                draft_proposal=self._try_extract_draft_from_state(session_state)
                or confirmation.draft_proposal,
            )

        raise RuntimeError(
            "Workflow resume produced neither a review interrupt nor a final route.",
        )

    async def finalize_approval(self, confirmation_id: str) -> IntakeResult:
        confirmation, acquired = self.store.mark_committing_once(confirmation_id)
        if confirmation.status is ConfirmationStatus.COMMITTED:
            return IntakeResult(
                status=ConfirmationStatus.COMMITTED.value,
                confirmation_id=confirmation_id,
                draft_proposal=confirmation.draft_proposal,
                notion_project_url=confirmation.notion_project_url,
            )
        if not acquired:
            return IntakeResult(
                status=confirmation.status.value,
                confirmation_id=confirmation_id,
                draft_proposal=confirmation.draft_proposal,
                notion_project_url=confirmation.notion_project_url,
            )

        try:
            writer = self.notion_writer or NotionWriter.from_settings(self.settings)
            result = await writer.create_project_with_tasks(confirmation.draft_proposal)
        except Exception as exc:
            self.store.mark_failed(
                confirmation_id=confirmation_id,
                failure_reason=str(exc),
            )
            failed = self.store.get(confirmation_id)
            return IntakeResult(
                status=ConfirmationStatus.FAILED.value,
                confirmation_id=confirmation_id,
                draft_proposal=confirmation.draft_proposal,
                notion_project_url=failed.notion_project_url if failed else None,
                failure_reason=str(exc),
            )

        self.store.mark_committed(
            confirmation_id=confirmation_id,
            notion_project_id=result.project_id,
            notion_project_url=result.project_url,
        )
        return IntakeResult(
            status=ConfirmationStatus.COMMITTED.value,
            confirmation_id=confirmation_id,
            draft_proposal=confirmation.draft_proposal,
            notion_project_url=result.project_url,
        )

    async def _run_turn(
        self,
        *,
        user_id: str,
        session_id: str,
        message: types.Content,
        invocation_id: str | None = None,
        state_delta: dict[str, Any] | None = None,
    ) -> list[Any]:
        events: list[Any] = []
        async for event in self.runner.run_async(
            user_id=user_id,
            session_id=session_id,
            invocation_id=invocation_id,
            new_message=message,
            state_delta=state_delta,
        ):
            events.append(event)
        return events

    async def _get_session_state(
        self,
        *,
        user_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        session = await self.session_service.get_session(
            app_name=self.app_name,
            user_id=user_id,
            session_id=session_id,
        )
        if session is None:
            raise RuntimeError(
                "Session was not found after workflow execution: "
                f"user_id={user_id}, session_id={session_id}",
            )
        return dict(session.state)

    def _extract_draft_from_state(
        self,
        session_state: dict[str, Any],
    ) -> ProjectDraft:
        draft = self._try_extract_draft_from_state(session_state)
        if draft is None:
            raise RuntimeError("No draft_proposal found in session state.")
        return draft

    def _try_extract_draft_from_state(
        self,
        session_state: dict[str, Any],
    ) -> ProjectDraft | None:
        draft_payload = session_state.get("draft_proposal")
        if draft_payload is None:
            return None
        return ProjectDraft.model_validate(draft_payload)

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
            self.settings.export_to_env()
            from langsmith.integrations.google_adk import configure_google_adk

            configure_google_adk()
            _LANGSMITH_CONFIGURED = True

        from backlog_tamer.agents.intake_triage.agent import root_agent

        return root_agent
