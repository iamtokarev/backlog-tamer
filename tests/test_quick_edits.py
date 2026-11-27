from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from backlog_tamer.application.confirmation_store import (
    ConfirmationStore,
    utc_now,
)
from backlog_tamer.application.models import (
    ConfirmationRecord,
    ConfirmationStatus,
    ManualEdit,
)
from backlog_tamer.domain import IncomingContext, ProjectDraft
from backlog_tamer.integrations.telegram.callbacks import PickValue, decode_callback
from backlog_tamer.integrations.telegram.rendering import (
    FIELD_OPTIONS,
    build_picker_keyboard,
)


def test_quick_edit_patches_the_draft_without_a_triage_call(tmp_path: Path):
    store = _build_store(tmp_path)
    record = _build_confirmation()
    store.create_pending(record)

    updated = store.apply_manual_edit(
        confirmation_id=record.confirmation_id,
        field="priority",
        value="Low",
    )

    assert updated.draft_proposal.priority == "Low"
    assert updated.draft_proposal.project_name == "Example project"
    assert updated.manual_edits == {
        "priority": ManualEdit(before="Medium", after="Low")
    }

    stored = store.get(record.confirmation_id)
    assert stored is not None
    assert stored.draft_proposal.priority == "Low"


def test_quick_edits_accumulate_and_outlast_a_new_draft(tmp_path: Path):
    store = _build_store(tmp_path)
    record = _build_confirmation()
    store.create_pending(record)

    store.apply_manual_edit(
        confirmation_id=record.confirmation_id,
        field="priority",
        value="Low",
    )
    edited = store.apply_manual_edit(
        confirmation_id=record.confirmation_id,
        field="intent",
        value="reference",
    )
    assert edited.manual_edits == {
        "priority": ManualEdit(before="Medium", after="Low"),
        "intent": ManualEdit(before="explore", after="reference"),
    }

    store.update_draft(record.confirmation_id, edited.draft_proposal)

    redrafted = store.get(record.confirmation_id)
    assert redrafted is not None
    assert redrafted.manual_edits == edited.manual_edits


def test_quick_edit_is_rejected_once_the_draft_is_resolved(tmp_path: Path):
    store = _build_store(tmp_path)
    record = _build_confirmation()
    store.create_pending(record)
    store.mark_rejected(record.confirmation_id)

    with pytest.raises(ValueError):
        store.apply_manual_edit(
            confirmation_id=record.confirmation_id,
            field="priority",
            value="Low",
        )


def test_picker_offers_every_schema_option():
    keyboard = build_picker_keyboard("project_type", str(uuid4()))
    callbacks = [
        decode_callback(button.callback_data)
        for row in keyboard.inline_keyboard
        for button in row
    ]
    values = [
        callback.value for callback in callbacks if isinstance(callback, PickValue)
    ]

    assert values == list(FIELD_OPTIONS["project_type"])


def _build_store(tmp_path: Path) -> ConfirmationStore:
    return ConfirmationStore(f"sqlite:///{tmp_path / 'confirmations.db'}")


def _build_confirmation() -> ConfirmationRecord:
    now = utc_now()
    return ConfirmationRecord(
        confirmation_id=str(uuid4()),
        user_id="user-id",
        chat_id="chat-id",
        source_message_id="message-id",
        status=ConfirmationStatus.PENDING_REVIEW,
        incoming_context=IncomingContext(raw_text="https://example.com"),
        draft_proposal=ProjectDraft(
            project_name="Example project",
            summary="A small example project.",
            project_type="product",
            intent="explore",
            priority="Medium",
            source_url="https://example.com",
        ),
        created_at=now,
        updated_at=now,
    )
