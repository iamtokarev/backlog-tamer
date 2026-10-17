"""The callback_data carried by review-card buttons, written and read in one place.

Cards already sent keep their buttons, so the wire format outlives a deploy:
`action:confirmation_id`, `edit:field:confirmation_id` and
`pick:field:value:confirmation_id`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

# Telegram rejects a button whose callback_data is longer than this.
CALLBACK_DATA_LIMIT = 64

_EDIT = "edit"
_PICK = "pick"

# Single-letter codes keep callback_data inside the limit once a 36-character
# confirmation id is appended.
_FIELD_CODES = {
    "priority": "p",
    "intent": "i",
    "project_type": "t",
}
_FIELDS_BY_CODE = {code: field for field, code in _FIELD_CODES.items()}


class Action(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    REVISE = "revise"
    CANCEL = "cancel"
    REFETCH = "refetch"
    RETRY = "retry"
    UNDO = "undo"
    ADD_TASK = "addtask"
    BACK = "back"


@dataclass(frozen=True)
class ConfirmationAction:
    """A button that acts on the whole Confirmation."""

    action: Action
    confirmation_id: str


@dataclass(frozen=True)
class OpenPicker:
    """Swap the review keyboard for the options of one Draft field."""

    field: str
    confirmation_id: str


@dataclass(frozen=True)
class PickValue:
    """A Quick edit: set one Draft field to one option."""

    field: str
    value: str
    confirmation_id: str


Callback = ConfirmationAction | OpenPicker | PickValue


def encode_callback(callback: Callback) -> str:
    """Raises ValueError for anything Telegram or `decode_callback` would refuse."""
    match callback:
        case ConfirmationAction():
            parts = [callback.action.value]
        case OpenPicker():
            parts = [_EDIT, _FIELD_CODES.get(callback.field, "")]
        case PickValue():
            parts = [_PICK, _FIELD_CODES.get(callback.field, ""), callback.value]
    if any(not part or ":" in part for part in parts) or not callback.confirmation_id:
        raise ValueError(f"Cannot encode {callback!r}")

    data = ":".join([*parts, callback.confirmation_id])
    if len(data.encode("utf-8")) > CALLBACK_DATA_LIMIT:
        raise ValueError(
            f"callback_data is over {CALLBACK_DATA_LIMIT} bytes: {data!r}",
        )
    return data


def decode_callback(data: str) -> Callback | None:
    """None for anything `encode_callback` did not write."""
    kind, _, rest = data.partition(":")
    if kind == _EDIT:
        code, _, confirmation_id = rest.partition(":")
        field = _FIELDS_BY_CODE.get(code)
        if field is None or not confirmation_id:
            return None
        return OpenPicker(field, confirmation_id)

    if kind == _PICK:
        code, _, rest = rest.partition(":")
        value, _, confirmation_id = rest.partition(":")
        field = _FIELDS_BY_CODE.get(code)
        if field is None or not value or not confirmation_id:
            return None
        return PickValue(field, value, confirmation_id)

    try:
        action = Action(kind)
    except ValueError:
        return None
    if not rest:
        return None
    return ConfirmationAction(action, rest)
