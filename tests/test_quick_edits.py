from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from backlog_tamer.agents.intake_triage.schemas import IncomingContext, ProjectDraft
from backlog_tamer.application.confirmation_store import ConfirmationStore, utc_now
from backlog_tamer.application.intake_service import _with_manual_edits
from backlog_tamer.application.models import ConfirmationRecord, ConfirmationStatus
from backlog_tamer.integrations.telegram.rendering import (
    FIELD_OPTIONS,
    FIELD_TYPE,
    build_picker_keyboard,
    build_review_keyboard,
)

CALLBACK_DATA_LIMIT = 64


def test_quick_edit_patches_the_draft_without_touching_the_agent(tmp_path: Path):
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
    assert updated.manual_edits == {"priority": "Low"}

    stored = store.get(record.confirmation_id)
    assert stored is not None
    assert stored.draft_proposal.priority == "Low"


def test_quick_edits_accumulate_and_clear_on_the_next_agent_draft(tmp_path: Path):
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
    assert edited.manual_edits == {"priority": "Low", "intent": "reference"}

    store.update_after_resume(
        confirmation_id=record.confirmation_id,
        draft_proposal=edited.draft_proposal,
        invocation_id="invocation-2",
        request_input_call_id="call-2",
        review_message="Review again.",
    )

    redrafted = store.get(record.confirmation_id)
    assert redrafted is not None
    assert redrafted.manual_edits == {}


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


def test_manual_edits_are_replayed_into_free_text_revisions():
    prompt = _with_manual_edits("make the title shorter", {"priority": "Low"})

    assert "priority=Low" in prompt
    assert prompt.endswith("make the title shorter")


def test_manual_edits_are_not_replayed_into_approve_or_reject():
    assert _with_manual_edits("approve", {"priority": "Low"}) == "approve"
    assert _with_manual_edits("reject", {"priority": "Low"}) == "reject"


def test_callback_data_fits_telegram_limit_for_every_option():
    confirmation_id = str(uuid4())
    draft = _build_confirmation().draft_proposal
    keyboards = [
        build_review_keyboard(draft, confirmation_id),
        *(
            build_picker_keyboard(field_code, confirmation_id)
            for field_code in FIELD_OPTIONS
        ),
    ]

    payloads = [
        button.callback_data
        for keyboard in keyboards
        for row in keyboard.inline_keyboard
        for button in row
    ]

    assert payloads
    for payload in payloads:
        assert len(payload.encode("utf-8")) <= CALLBACK_DATA_LIMIT, payload


def test_picker_offers_every_schema_option():
    keyboard = build_picker_keyboard(FIELD_TYPE, str(uuid4()))
    values = [
        button.callback_data.split(":")[2]
        for row in keyboard.inline_keyboard
        for button in row
        if button.callback_data.startswith("pick:")
    ]

    assert values == list(FIELD_OPTIONS[FIELD_TYPE])


def _build_store(tmp_path: Path) -> ConfirmationStore:
    return ConfirmationStore(f"sqlite:///{tmp_path / 'confirmations.db'}")


def _build_confirmation() -> ConfirmationRecord:
    now = utc_now()
    return ConfirmationRecord(
        confirmation_id=str(uuid4()),
        user_id="user-id",
        chat_id="chat-id",
        source_message_id="message-id",
        session_id="session-id",
        invocation_id="invocation-id",
        request_input_call_id="call-id",
        status=ConfirmationStatus.PENDING_REVIEW,
        incoming_context=IncomingContext(raw_text="https://example.com"),
        draft_proposal=ProjectDraft(
            project_name="Example project",
            summary="A small example project.",
            resource_type="article",
            intent="explore",
            priority="Medium",
            source_url="https://example.com",
            tasks=["Read"],
        ),
        review_message="Review this draft.",
        created_at=now,
        updated_at=now,
    )
