from __future__ import annotations

from html import escape
from urllib.parse import urlparse

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from backlog_tamer.agents.intake_triage.schemas import (
    DraftGrounding,
    IncomingContext,
    ProjectDraft,
)
from backlog_tamer.application.models import ConfirmationStatus

CALLBACK_APPROVE = "approve"
CALLBACK_REVISE = "revise"
CALLBACK_REJECT = "reject"
CALLBACK_RETRY = "retry"
CALLBACK_CANCEL = "cancel"
CALLBACK_EDIT = "edit"
CALLBACK_REFETCH = "refetch"
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

REVISION_PLACEHOLDER = "What should change?"

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


def render_revision_prompt(draft: ProjectDraft) -> str:
    return (
        f"✏️ What should I change about <b>{escape(draft.project_name)}</b>?\n"
        "Reply with your feedback, or press Cancel on the draft above."
    )


def render_change_summary(before: ProjectDraft, after: ProjectDraft) -> str | None:
    """One line naming what the revision actually moved."""
    changes: list[str] = []
    for field, label in (
        ("priority", "priority"),
        ("intent", "intent"),
        ("resource_type", "type"),
    ):
        old = getattr(before, field)
        new = getattr(after, field)
        if old != new:
            changes.append(f"{label} {old} → {new}")

    if len(before.tasks) != len(after.tasks):
        changes.append(f"tasks {len(before.tasks)} → {len(after.tasks)}")
    elif before.tasks != after.tasks:
        changes.append("tasks rewritten")

    if before.project_name != after.project_name:
        changes.append("title rewritten")
    if before.summary != after.summary:
        changes.append("summary rewritten")
    if before.source_url != after.source_url:
        changes.append("source changed")

    if not changes:
        return None
    return f"✏️ <i>Changed: {escape(' · '.join(changes))}</i>"


def build_cancel_revision_keyboard(confirmation_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✕ Cancel revision",
                    callback_data=f"{CALLBACK_CANCEL}:{confirmation_id}",
                )
            ]
        ]
    )


def render_progress_message(incoming: IncomingContext) -> str:
    """Shown while the agent works: the typing action expires after ~5s."""
    if incoming.links:
        return f"🔎 Reading {escape(_display_url(str(incoming.links[0].url)))}…"
    return "🧠 Triaging your note…"


def render_draft_message(
    draft: ProjectDraft,
    grounding: DraftGrounding | None = None,
) -> str:
    """Render the review card: title first, then meta, summary, source, tasks."""
    lines = [
        f"<b>{escape(draft.project_name)}</b>",
        _render_chips(draft),
        "",
        escape(draft.summary),
    ]

    if draft.source_url:
        lines.append("")
        lines.append(_render_source(draft.source_url, grounding))

    if draft.tasks:
        lines.append("")
        lines.extend(f"☑︎ {escape(task)}" for task in draft.tasks)

    if draft.topics:
        lines.append("")
        lines.append(f"🏷 {escape(' · '.join(draft.topics))}")

    if grounding is not None and grounding.is_degraded:
        lines.append("")
        lines.append(_render_fetch_warning(grounding))

    return "\n".join(lines)


def _render_source(source_url: str, grounding: DraftGrounding | None) -> str:
    line = f"🔗 {_link(source_url, _display_url(source_url))}"
    if grounding is not None and grounding.site_name:
        line = f"{line} · {escape(grounding.site_name)}"
    return line


def _render_fetch_warning(grounding: DraftGrounding) -> str:
    """State the uncertainty rather than letting a guess look grounded."""
    reason = f" ({escape(grounding.fetch_error)})" if grounding.fetch_error else ""
    return (
        f"⚠️ <i>Couldn't open the page{reason}. Drafted from the URL and your "
        "note — check the title.</i>"
    )


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
    grounding: DraftGrounding | None = None,
) -> InlineKeyboardMarkup:
    """Approve/reject, plus one-tap pickers for the three enum fields."""
    refetch_row = []
    if grounding is not None and grounding.is_degraded:
        refetch_row = [
            [
                InlineKeyboardButton(
                    "🔁 Retry fetch",
                    callback_data=f"{CALLBACK_REFETCH}:{confirmation_id}",
                )
            ]
        ]

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
            *refetch_row,
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
