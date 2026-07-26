from __future__ import annotations

from html import escape
from urllib.parse import urlparse

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from backlog_tamer.agents.intake_triage.schemas import ProjectDraft
from backlog_tamer.application.models import ConfirmationStatus

CALLBACK_APPROVE = "approve"
CALLBACK_REVISE = "revise"
CALLBACK_REJECT = "reject"
CALLBACK_RETRY = "retry"
CALLBACK_EDIT = "edit"
CALLBACK_PICK = "pick"
CALLBACK_BACK = "back"

# Single-letter codes keep callback_data inside Telegram's 64-byte limit
# once a 36-character confirmation id is appended.
FIELD_PRIORITY = "p"
FIELD_INTENT = "i"
FIELD_TYPE = "t"

DRAFT_FIELD_NAMES = {
    FIELD_PRIORITY: "priority",
    FIELD_INTENT: "intent",
    FIELD_TYPE: "resource_type",
}

REVISION_PROMPT = "✏️ Send your revision as a reply."

RESOURCE_TYPE_ICONS = {
    "article": "📄",
    "paper": "🧪",
    "video": "🎬",
    "course": "🎓",
    "documentation": "📘",
    "repository": "📦",
    "idea": "💡",
    "unknown": "❔",
}

INTENT_ICONS = {
    "learn": "📚",
    "build": "🔨",
    "research": "🔬",
    "explore": "🧭",
    "reference": "🔖",
    "unclear": "❔",
}

PRIORITY_ICONS = {
    "High": "🔺",
    "Medium": "▪️",
    "Low": "🔻",
}

FIELD_OPTIONS = {
    FIELD_PRIORITY: ("High", "Medium", "Low"),
    FIELD_INTENT: tuple(INTENT_ICONS),
    FIELD_TYPE: tuple(RESOURCE_TYPE_ICONS),
}

FIELD_ICONS = {
    FIELD_PRIORITY: PRIORITY_ICONS,
    FIELD_INTENT: INTENT_ICONS,
    FIELD_TYPE: RESOURCE_TYPE_ICONS,
}


def render_draft_message(draft: ProjectDraft) -> str:
    """Render the review card: title first, then meta, summary, source, tasks."""
    lines = [
        f"<b>{escape(draft.project_name)}</b>",
        _render_chips(draft),
        "",
        escape(draft.summary),
    ]

    if draft.source_url:
        lines.append("")
        lines.append(f"🔗 {_link(draft.source_url, _display_url(draft.source_url))}")

    if draft.tasks:
        lines.append("")
        lines.extend(f"☑︎ {escape(task)}" for task in draft.tasks)

    return "\n".join(lines)


def _render_chips(draft: ProjectDraft) -> str:
    chips = [
        f"{RESOURCE_TYPE_ICONS.get(draft.resource_type, '❔')} {draft.resource_type}",
        f"{INTENT_ICONS.get(draft.intent, '❔')} {draft.intent}",
        f"{PRIORITY_ICONS.get(draft.priority, '▪️')} {draft.priority}",
    ]
    return escape(" · ".join(chips))


def _display_url(url: str) -> str:
    """Show the domain rather than a URL that wraps three times on a phone."""
    host = urlparse(url).netloc
    if not host:
        return url
    return host.removeprefix("www.")


def build_review_keyboard(
    draft: ProjectDraft,
    confirmation_id: str,
) -> InlineKeyboardMarkup:
    """Approve/reject, plus one-tap pickers for the three enum fields."""
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Approve & save",
                    callback_data=f"{CALLBACK_APPROVE}:{confirmation_id}",
                ),
                InlineKeyboardButton(
                    "❌ Reject",
                    callback_data=f"{CALLBACK_REJECT}:{confirmation_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    f"{PRIORITY_ICONS.get(draft.priority, '▪️')} {draft.priority} ▸",
                    callback_data=f"{CALLBACK_EDIT}:{FIELD_PRIORITY}:{confirmation_id}",
                ),
                InlineKeyboardButton(
                    f"{INTENT_ICONS.get(draft.intent, '❔')} {draft.intent} ▸",
                    callback_data=f"{CALLBACK_EDIT}:{FIELD_INTENT}:{confirmation_id}",
                ),
                InlineKeyboardButton(
                    f"{RESOURCE_TYPE_ICONS.get(draft.resource_type, '❔')} "
                    f"{draft.resource_type} ▸",
                    callback_data=f"{CALLBACK_EDIT}:{FIELD_TYPE}:{confirmation_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "📝 Revise with a note",
                    callback_data=f"{CALLBACK_REVISE}:{confirmation_id}",
                ),
            ],
        ]
    )


def build_picker_keyboard(
    field_code: str,
    confirmation_id: str,
) -> InlineKeyboardMarkup:
    """Replace the review keyboard with the options for one field."""
    options = FIELD_OPTIONS[field_code]
    icons = FIELD_ICONS[field_code]
    buttons = [
        InlineKeyboardButton(
            f"{icons.get(option, '❔')} {option}",
            callback_data=f"{CALLBACK_PICK}:{field_code}:{option}:{confirmation_id}",
        )
        for option in options
    ]
    rows = [buttons[index : index + 3] for index in range(0, len(buttons), 3)]
    rows.append(
        [
            InlineKeyboardButton(
                "↩︎ Back",
                callback_data=f"{CALLBACK_BACK}:{confirmation_id}",
            )
        ]
    )
    return InlineKeyboardMarkup(rows)


def render_terminal_message(
    draft: ProjectDraft,
    status: ConfirmationStatus,
    notion_project_url: str | None = None,
    failure_reason: str | None = None,
) -> str:
    body = render_draft_message(draft)
    badge = _terminal_badge(status)
    if status is ConfirmationStatus.FAILED:
        reason = _short_failure_reason(failure_reason)
        return f"{badge}\n{reason}\n\n{body}"
    if notion_project_url:
        link = _link(notion_project_url, notion_project_url)
        return f"{badge}\n<b>Notion:</b> {link}\n\n{body}"
    return f"{badge}\n\n{body}"


def build_retry_keyboard(confirmation_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🔁 Retry save",
                    callback_data=f"{CALLBACK_RETRY}:{confirmation_id}",
                )
            ]
        ]
    )


def _short_failure_reason(failure_reason: str | None) -> str:
    """One readable line: exception strings are long and often carry a URL."""
    if not failure_reason:
        return "Your draft is safe — press retry to try Notion again."
    reason = " ".join(failure_reason.split())
    if len(reason) > 160:
        reason = f"{reason[:157]}…"
    return f"{escape(reason)}\nYour draft is safe — press retry to try Notion again."


def _terminal_badge(status: ConfirmationStatus) -> str:
    if status is ConfirmationStatus.COMMITTED:
        return "✅ <b>Saved</b>"
    if status is ConfirmationStatus.COMMITTING:
        return "⏳ <b>Saving</b>"
    if status is ConfirmationStatus.FAILED:
        return "⚠️ <b>Save failed</b>"
    if status is ConfirmationStatus.REJECTED:
        return "❌ <b>Rejected</b>"
    return "⏳ <b>Pending</b>"


def _link(url: str, label: str) -> str:
    return f'<a href="{escape(url, quote=True)}">{escape(label)}</a>'
