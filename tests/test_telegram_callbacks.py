"""The callback_data on review-card buttons: what is written is what is read."""

from __future__ import annotations

from uuid import uuid4

import pytest

from backlog_tamer.agents.intake_triage.schemas import DraftGrounding, ProjectDraft
from backlog_tamer.application.models import ConfirmationStatus
from backlog_tamer.integrations.telegram.callbacks import (
    Action,
    ConfirmationAction,
    OpenPicker,
    PickValue,
    decode_callback,
    encode_callback,
)
from backlog_tamer.integrations.telegram.rendering import (
    FIELD_OPTIONS,
    build_cancel_revision_keyboard,
    build_picker_keyboard,
    build_review_keyboard,
    build_terminal_keyboard,
)

CONFIRMATION_ID = "0b6f1c1e-3f0a-4d4e-9d7b-2a3c4e5f6a7b"

# The wire strings are pinned: cards already sitting in a chat carry them, so
# changing one would break every button sent before the deploy.
WIRE_FORMAT = [
    (ConfirmationAction(Action.APPROVE, CONFIRMATION_ID), f"approve:{CONFIRMATION_ID}"),
    (ConfirmationAction(Action.REJECT, CONFIRMATION_ID), f"reject:{CONFIRMATION_ID}"),
    (ConfirmationAction(Action.REVISE, CONFIRMATION_ID), f"revise:{CONFIRMATION_ID}"),
    (ConfirmationAction(Action.CANCEL, CONFIRMATION_ID), f"cancel:{CONFIRMATION_ID}"),
    (
        ConfirmationAction(Action.REFETCH, CONFIRMATION_ID),
        f"refetch:{CONFIRMATION_ID}",
    ),
    (ConfirmationAction(Action.RETRY, CONFIRMATION_ID), f"retry:{CONFIRMATION_ID}"),
    (ConfirmationAction(Action.UNDO, CONFIRMATION_ID), f"undo:{CONFIRMATION_ID}"),
    (
        ConfirmationAction(Action.ADD_TASK, CONFIRMATION_ID),
        f"addtask:{CONFIRMATION_ID}",
    ),
    (ConfirmationAction(Action.BACK, CONFIRMATION_ID), f"back:{CONFIRMATION_ID}"),
    (OpenPicker("priority", CONFIRMATION_ID), f"edit:p:{CONFIRMATION_ID}"),
    (OpenPicker("intent", CONFIRMATION_ID), f"edit:i:{CONFIRMATION_ID}"),
    (OpenPicker("project_type", CONFIRMATION_ID), f"edit:t:{CONFIRMATION_ID}"),
    (PickValue("priority", "Low", CONFIRMATION_ID), f"pick:p:Low:{CONFIRMATION_ID}"),
    (
        PickValue("intent", "reference", CONFIRMATION_ID),
        f"pick:i:reference:{CONFIRMATION_ID}",
    ),
    (
        PickValue("project_type", "repository", CONFIRMATION_ID),
        f"pick:t:repository:{CONFIRMATION_ID}",
    ),
]


@pytest.mark.parametrize(("callback", "wire"), WIRE_FORMAT, ids=lambda v: str(v))
def test_every_callback_keeps_its_wire_format(callback, wire):
    assert encode_callback(callback) == wire
    assert decode_callback(wire) == callback


@pytest.mark.parametrize(
    "data",
    [
        "",
        "approve",
        "approve:",
        "launch:" + CONFIRMATION_ID,
        "edit:" + CONFIRMATION_ID,
        "edit:x:" + CONFIRMATION_ID,
        "edit:p:",
        "pick:p:" + CONFIRMATION_ID,
        "pick:x:Low:" + CONFIRMATION_ID,
        "pick:p::" + CONFIRMATION_ID,
    ],
)
def test_malformed_callback_data_decodes_to_nothing(data):
    assert decode_callback(data) is None


def test_a_value_the_picker_no_longer_offers_still_decodes():
    # Old cards may carry a retired option; the store normalizes it, not the codec.
    assert decode_callback(f"pick:t:documentation:{CONFIRMATION_ID}") == PickValue(
        "project_type", "documentation", CONFIRMATION_ID
    )


def test_callback_data_over_the_telegram_limit_is_refused():
    with pytest.raises(ValueError, match="64 bytes"):
        encode_callback(PickValue("intent", "x" * 30, CONFIRMATION_ID))


@pytest.mark.parametrize(
    "callback",
    [
        OpenPicker("summary", CONFIRMATION_ID),
        PickValue("priority", "Lo:w", CONFIRMATION_ID),
        PickValue("priority", "", CONFIRMATION_ID),
        ConfirmationAction(Action.APPROVE, ""),
    ],
)
def test_callbacks_that_would_not_decode_are_refused(callback):
    with pytest.raises(ValueError):
        encode_callback(callback)


def test_every_button_the_bot_builds_fits_telegram_and_decodes():
    confirmation_id = str(uuid4())
    draft = ProjectDraft(
        project_name="Example project",
        summary="A small example project.",
        project_type="product",
        intent="explore",
        priority="Medium",
        source_url="https://example.com",
        tasks=["Read"],
    )
    keyboards = [
        build_review_keyboard(
            draft, confirmation_id, DraftGrounding(fetch_status="error")
        ),
        build_cancel_revision_keyboard(confirmation_id),
        *(build_picker_keyboard(field, confirmation_id) for field in FIELD_OPTIONS),
        *(
            build_terminal_keyboard(status, confirmation_id, "https://notion.so/p")
            for status in ConfirmationStatus
        ),
    ]

    payloads = [
        button.callback_data
        for keyboard in keyboards
        if keyboard is not None
        for row in keyboard.inline_keyboard
        for button in row
        if button.callback_data is not None
    ]

    assert payloads
    for payload in payloads:
        assert len(payload.encode("utf-8")) <= 64, payload
        assert decode_callback(payload) is not None, payload
