from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from uuid import uuid4

from backlog_tamer.agents.intake_triage.schemas import IncomingContext
from backlog_tamer.agents.intake_triage.triage_workflow import (
    Approved,
    NeedsReview,
    Rejected,
    TriageWorkflow,
    TurnOutcome,
)
from backlog_tamer.config import Settings, get_settings
from backlog_tamer.integrations.notion import NotionWriter

from .confirmation_store import ConfirmationStore, utc_now
from .models import (
    ConfirmationRecord,
    ConfirmationStatus,
    IntakeResult,
)

logger = logging.getLogger(__name__)

# A Review in one of these has already ended; a late button press only
# reports where it stands.
TERMINAL_STATUSES = frozenset(
    {
        ConfirmationStatus.COMMITTING,
        ConfirmationStatus.COMMITTED,
        ConfirmationStatus.REJECTED,
        ConfirmationStatus.FAILED,
        ConfirmationStatus.DUPLICATE,
        ConfirmationStatus.UNDONE,
    }
)


def _kept_fields(confirmation: ConfirmationRecord) -> dict[str, str]:
    """Quick edits the next Revision must not undo."""
    return {field: edit.after for field, edit in confirmation.manual_edits.items()}


def _review_result(confirmation: ConfirmationRecord) -> IntakeResult:
    pending = confirmation.status is ConfirmationStatus.PENDING_REVIEW
    return IntakeResult(
        status="needs_review" if pending else confirmation.status.value,
        confirmation_id=confirmation.confirmation_id,
        draft_proposal=confirmation.draft_proposal,
        grounding=confirmation.grounding,
        review_message=confirmation.review_message,
        notion_project_url=confirmation.notion_project_url,
    )


def _log_corrections(confirmation: ConfirmationRecord) -> None:
    """Record which fields the agent got wrong, at the moment they stick.

    A quick edit runs no model and emits no trace, so without this the only
    surviving evidence of a correction is the committed value itself. One
    structured line per approval turns the buttons back into a measurable
    per-field accuracy signal.
    """
    corrections = confirmation.corrected_fields
    if not corrections:
        return

    logger.info(
        "intake_correction confirmation_id=%s corrected=%s",
        confirmation.confirmation_id,
        json.dumps(
            {
                field: {"agent": edit.before, "user": edit.after}
                for field, edit in corrections.items()
            },
            sort_keys=True,
        ),
    )


class IntakeService:
    def __init__(
        self,
        *,
        database_url: str | None = None,
        store: ConfirmationStore | None = None,
        settings: Settings | None = None,
        notion_writer: NotionWriter | None = None,
        workflow: TriageWorkflow | None = None,
    ):
        self.settings = settings or get_settings()
        self.database_url = database_url or self.settings.database_url
        self.store = store or ConfirmationStore(self.database_url)
        self.notion_writer = notion_writer
        self.workflow = workflow or TriageWorkflow.from_settings(
            self.settings,
            self.database_url,
        )

    async def start_intake(
        self,
        context: IncomingContext,
        user_id: str,
        chat_id: str | None = None,
        source_message_id: str | None = None,
    ) -> IntakeResult:
        outcome = await self.workflow.start(
            context,
            user_id,
            await self._known_topics(),
        )
        if not isinstance(outcome, NeedsReview):
            raise RuntimeError(
                f"A new Triage ended without a Draft to review: {outcome!r}",
            )

        confirmation = ConfirmationRecord(
            confirmation_id=str(uuid4()),
            user_id=user_id,
            chat_id=chat_id,
            source_message_id=source_message_id,
            checkpoint=outcome.checkpoint,
            status=ConfirmationStatus.PENDING_REVIEW,
            incoming_context=context,
            draft_proposal=outcome.draft,
            grounding=outcome.grounding,
            review_message=outcome.review_message,
            created_at=utc_now(),
            updated_at=utc_now(),
        )
        self.store.create_pending(confirmation)
        return IntakeResult(
            status="needs_review",
            confirmation_id=confirmation.confirmation_id,
            draft_proposal=outcome.draft,
            grounding=outcome.grounding,
            review_message=outcome.review_message,
        )

    def get_review(self, confirmation_id: str) -> IntakeResult | None:
        """The Draft as it stands now, or None for an unknown Confirmation."""
        confirmation = self.store.get(confirmation_id)
        if confirmation is None:
            return None
        return _review_result(confirmation)

    def apply_quick_edit(
        self,
        confirmation_id: str,
        field: str,
        value: str,
    ) -> IntakeResult:
        """Set one Draft field directly; the next Revision keeps it.

        Raises ValueError for an unknown Confirmation or a finished Review.
        """
        confirmation = self.store.apply_manual_edit(
            confirmation_id=confirmation_id,
            field=field,
            value=value,
        )
        return _review_result(confirmation)

    async def approve(self, confirmation_id: str) -> IntakeResult:
        return await self._resume(
            confirmation_id,
            lambda confirmation: self.workflow.approve(confirmation.checkpoint),
        )

    async def reject(self, confirmation_id: str) -> IntakeResult:
        return await self._resume(
            confirmation_id,
            lambda confirmation: self.workflow.reject(confirmation.checkpoint),
        )

    async def revise(self, confirmation_id: str, feedback: str) -> IntakeResult:
        """Send the Draft back to the agent with the user's Revision."""
        return await self._resume(
            confirmation_id,
            lambda confirmation: self.workflow.revise(
                confirmation.checkpoint,
                feedback,
                _kept_fields(confirmation),
            ),
        )

    async def refetch(self, confirmation_id: str) -> IntakeResult:
        """Redraft after a failed page fetch, from a fresh read of the page."""
        return await self._resume(
            confirmation_id,
            lambda confirmation: self.workflow.refetch(
                confirmation.checkpoint,
                _kept_fields(confirmation),
            ),
        )

    async def _resume(
        self,
        confirmation_id: str,
        turn: Callable[[ConfirmationRecord], Awaitable[TurnOutcome]],
    ) -> IntakeResult:
        confirmation = self.store.get(confirmation_id)
        if confirmation is None:
            raise ValueError(f"Unknown confirmation_id: {confirmation_id}")
        if confirmation.status in TERMINAL_STATUSES:
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

        outcome = await turn(confirmation)
        match outcome:
            case NeedsReview():
                self.store.update_after_resume(
                    confirmation_id=confirmation_id,
                    draft_proposal=outcome.draft,
                    checkpoint=outcome.checkpoint,
                    review_message=outcome.review_message,
                    grounding=outcome.grounding,
                )
                return IntakeResult(
                    status="needs_review",
                    confirmation_id=confirmation_id,
                    draft_proposal=outcome.draft,
                    grounding=outcome.grounding,
                    review_message=outcome.review_message,
                )
            case Approved():
                return await self.finalize_approval(confirmation_id)
            case Rejected():
                self.store.mark_rejected(confirmation_id)
                return IntakeResult(
                    status=ConfirmationStatus.REJECTED.value,
                    confirmation_id=confirmation_id,
                    draft_proposal=outcome.draft,
                )
        raise RuntimeError(f"Unknown Turn outcome: {outcome!r}")

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

        _log_corrections(confirmation)

        writer = self.notion_writer or NotionWriter.from_settings(self.settings)

        duplicate = await self._find_duplicate(writer, confirmation)
        if duplicate is not None:
            self.store.mark_duplicate(
                confirmation_id=confirmation_id,
                notion_project_id=duplicate.page_id,
                notion_project_url=duplicate.page_url,
            )
            return IntakeResult(
                status=ConfirmationStatus.DUPLICATE.value,
                confirmation_id=confirmation_id,
                draft_proposal=confirmation.draft_proposal,
                grounding=confirmation.grounding,
                notion_project_url=duplicate.page_url,
                duplicate_created_time=duplicate.created_time,
            )

        try:
            result = await writer.create_project_with_tasks(
                confirmation.draft_proposal,
                incoming_context=confirmation.incoming_context,
                grounding=confirmation.grounding,
            )
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
            notion_task_ids=result.task_ids,
        )
        return IntakeResult(
            status=ConfirmationStatus.COMMITTED.value,
            confirmation_id=confirmation_id,
            draft_proposal=confirmation.draft_proposal,
            grounding=confirmation.grounding,
            notion_project_url=result.project_url,
        )

    async def undo_commit(self, confirmation_id: str) -> IntakeResult:
        """Archive the pages this commit created and release the record."""
        confirmation = self.store.get(confirmation_id)
        if confirmation is None:
            raise ValueError(f"Unknown confirmation_id: {confirmation_id}")
        if confirmation.status is not ConfirmationStatus.COMMITTED:
            return IntakeResult(
                status=confirmation.status.value,
                confirmation_id=confirmation_id,
                draft_proposal=confirmation.draft_proposal,
                notion_project_url=confirmation.notion_project_url,
            )

        writer = self.notion_writer or NotionWriter.from_settings(self.settings)
        page_ids = [
            page_id
            for page_id in [
                confirmation.notion_project_id,
                *confirmation.notion_task_ids,
            ]
            if page_id
        ]
        await writer.archive_pages(page_ids)
        self.store.mark_undone(confirmation_id)
        return IntakeResult(
            status=ConfirmationStatus.UNDONE.value,
            confirmation_id=confirmation_id,
            draft_proposal=confirmation.draft_proposal,
        )

    async def add_to_existing_project(self, confirmation_id: str) -> IntakeResult:
        """Attach this draft's tasks to the project the URL already has."""
        confirmation = self.store.get(confirmation_id)
        if confirmation is None:
            raise ValueError(f"Unknown confirmation_id: {confirmation_id}")
        if confirmation.notion_project_id is None:
            raise ValueError(f"Confirmation {confirmation_id} has no existing project.")

        writer = self.notion_writer or NotionWriter.from_settings(self.settings)
        task_ids = await writer.add_tasks_to_project(
            project_id=confirmation.notion_project_id,
            draft=confirmation.draft_proposal,
            grounding=confirmation.grounding,
        )
        self.store.mark_committed(
            confirmation_id=confirmation_id,
            notion_project_id=confirmation.notion_project_id,
            notion_project_url=confirmation.notion_project_url or "",
            notion_task_ids=task_ids,
        )
        return IntakeResult(
            status=ConfirmationStatus.COMMITTED.value,
            confirmation_id=confirmation_id,
            draft_proposal=confirmation.draft_proposal,
            grounding=confirmation.grounding,
            notion_project_url=confirmation.notion_project_url,
        )

    async def _find_duplicate(self, writer, confirmation: ConfirmationRecord):
        source_url = (
            confirmation.grounding.canonical_url
            or confirmation.draft_proposal.source_url
        )
        if not source_url:
            return None
        return await writer.find_project_by_source(source_url)

    async def _known_topics(self) -> list[str]:
        """The tag vocabulary already in Notion, for the drafting prompt.

        Never fatal: a capture must still work when Notion is unreachable or
        not configured at all.
        """
        try:
            writer = self.notion_writer or NotionWriter.from_settings(self.settings)
            return await writer.list_tag_options()
        except Exception:
            logger.warning("Could not load the existing topic vocabulary.")
            return []
