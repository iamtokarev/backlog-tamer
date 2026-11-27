from __future__ import annotations

import json
import logging
from uuid import uuid4

from backlog_tamer.config import Settings, get_settings
from backlog_tamer.domain import (
    Grounding,
    IncomingContext,
    ProjectDraft,
)
from backlog_tamer.integrations.notion import NotionWriter
from backlog_tamer.integrations.source_reader import SourceReader
from backlog_tamer.triage import Triage

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


def _needs_review(
    confirmation_id: str,
    draft: ProjectDraft,
    grounding: Grounding,
) -> IntakeResult:
    return IntakeResult(
        status="needs_review",
        confirmation_id=confirmation_id,
        draft_proposal=draft,
        grounding=grounding,
    )


def _review_result(confirmation: ConfirmationRecord) -> IntakeResult:
    if confirmation.status is ConfirmationStatus.PENDING_REVIEW:
        return _needs_review(
            confirmation.confirmation_id,
            confirmation.draft_proposal,
            confirmation.grounding,
        )
    return IntakeResult(
        status=confirmation.status.value,
        confirmation_id=confirmation.confirmation_id,
        draft_proposal=confirmation.draft_proposal,
        grounding=confirmation.grounding,
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
        settings: Settings | None = None,
        database_url: str | None = None,
        store: ConfirmationStore | None = None,
        triage: Triage | None = None,
        reader: SourceReader | None = None,
        notion_writer: NotionWriter | None = None,
    ):
        self.settings = settings or get_settings()
        self.database_url = database_url or self.settings.database_url
        self.store = store or ConfirmationStore(self.database_url)
        self.triage = triage or Triage.from_settings(self.settings)
        self.reader = reader or SourceReader.from_api_key(
            self.settings.browserbase_api_key.get_secret_value()
        )
        self.notion_writer = notion_writer

    async def start_intake(
        self,
        context: IncomingContext,
        user_id: str,
        chat_id: str | None = None,
        source_message_id: str | None = None,
    ) -> IntakeResult:
        grounding = await self._read(context)
        draft = await self.triage.classify(
            context, grounding, await self._known_topics()
        )

        now = utc_now()
        confirmation = ConfirmationRecord(
            confirmation_id=str(uuid4()),
            user_id=user_id,
            chat_id=chat_id,
            source_message_id=source_message_id,
            status=ConfirmationStatus.PENDING_REVIEW,
            incoming_context=context,
            draft_proposal=draft,
            grounding=grounding,
            created_at=now,
            updated_at=now,
        )
        self.store.create_pending(confirmation)
        return _needs_review(confirmation.confirmation_id, draft, grounding)

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
        """Set one Draft field directly; it lasts for the rest of the Review.

        Raises ValueError for an unknown Confirmation or a finished Review.
        """
        confirmation = self.store.apply_manual_edit(
            confirmation_id=confirmation_id,
            field=field,
            value=value,
        )
        return _review_result(confirmation)

    async def approve(self, confirmation_id: str) -> IntakeResult:
        pending = self._pending(confirmation_id)
        if isinstance(pending, IntakeResult):
            return pending
        return await self.finalize_approval(confirmation_id)

    async def reject(self, confirmation_id: str) -> IntakeResult:
        pending = self._pending(confirmation_id)
        if isinstance(pending, IntakeResult):
            return pending
        self.store.mark_rejected(confirmation_id)
        return IntakeResult(
            status=ConfirmationStatus.REJECTED.value,
            confirmation_id=confirmation_id,
            draft_proposal=pending.draft_proposal,
        )

    async def revise(self, confirmation_id: str, feedback: str) -> IntakeResult:
        """Re-decide the fields the user's Revision is about."""
        pending = self._pending(confirmation_id)
        if isinstance(pending, IntakeResult):
            return pending
        revised = await self.triage.revise(
            pending.incoming_context,
            pending.grounding,
            pending.draft_proposal,
            feedback,
            await self._known_topics(),
        )
        draft = revised.with_edits(pending.kept_fields)
        self.store.update_draft(confirmation_id, draft)
        return _needs_review(confirmation_id, draft, pending.grounding)

    async def refetch(self, confirmation_id: str) -> IntakeResult:
        """Redraft after a failed page fetch, from a fresh read of the page."""
        pending = self._pending(confirmation_id)
        if isinstance(pending, IntakeResult):
            return pending
        grounding = await self._read(pending.incoming_context)
        classified = await self.triage.classify(
            pending.incoming_context, grounding, await self._known_topics()
        )
        draft = classified.with_edits(pending.kept_fields)
        self.store.update_draft(confirmation_id, draft, grounding)
        return _needs_review(confirmation_id, draft, grounding)

    def _pending(self, confirmation_id: str) -> ConfirmationRecord | IntakeResult:
        """The Confirmation under Review, or the result of one already ended.

        Raises ValueError for an unknown Confirmation.
        """
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
        return confirmation

    async def _read(self, context: IncomingContext) -> Grounding:
        """Grounding for the Intake's first link; a failed read is an error one."""
        if not context.links:
            return Grounding(fetch_status="skipped")
        fetched = await self.reader.read(str(context.links[0].url))
        return fetched.to_grounding()

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
            result = await writer.create_project(
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
        )
        return IntakeResult(
            status=ConfirmationStatus.COMMITTED.value,
            confirmation_id=confirmation_id,
            draft_proposal=confirmation.draft_proposal,
            grounding=confirmation.grounding,
            notion_project_url=result.project_url,
        )

    async def undo_commit(self, confirmation_id: str) -> IntakeResult:
        """Archive the page this commit created and release the record."""
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
        if confirmation.notion_project_id:
            await writer.archive_pages([confirmation.notion_project_id])
        self.store.mark_undone(confirmation_id)
        return IntakeResult(
            status=ConfirmationStatus.UNDONE.value,
            confirmation_id=confirmation_id,
            draft_proposal=confirmation.draft_proposal,
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
