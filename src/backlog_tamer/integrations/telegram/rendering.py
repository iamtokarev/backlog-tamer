from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.helpers import escape_markdown

from backlog_tamer.agents.intake_triage.schemas import DraftProposal
from backlog_tamer.application.models import ConfirmationStatus

CALLBACK_APPROVE = "approve"
CALLBACK_REVISE = "revise"
CALLBACK_REJECT = "reject"

REVISION_PROMPT = "✏️ Send your revision as a reply\\."


def render_draft_message(draft: DraftProposal) -> str:
    title = escape_markdown(draft.title, version=2)
    description = escape_markdown(draft.description, version=2)
    resource_type = escape_markdown(draft.resource_type, version=2)
    intent = escape_markdown(draft.intent, version=2)
    reasoning = escape_markdown(draft.reasoning, version=2)

    lines = [
        f"*Title:* {title}",
        f"*Type:* {resource_type}",
        f"*Intent:* {intent}",
    ]
    if draft.source_url:
        url_label = escape_markdown(draft.source_url, version=2)
        url_target = _escape_link_target(draft.source_url)
        lines.append(f"*Source:* [{url_label}]({url_target})")

    lines.extend(
        [
            "",
            "*Description:*",
            description,
            "",
            "*Reasoning:*",
            reasoning,
        ]
    )
    return "\n".join(lines)


def build_review_keyboard(confirmation_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Approve",
                    callback_data=f"{CALLBACK_APPROVE}:{confirmation_id}",
                ),
                InlineKeyboardButton(
                    "📝 Revise",
                    callback_data=f"{CALLBACK_REVISE}:{confirmation_id}",
                ),
                InlineKeyboardButton(
                    "❌ Reject",
                    callback_data=f"{CALLBACK_REJECT}:{confirmation_id}",
                ),
            ]
        ]
    )


def render_terminal_message(
    draft: DraftProposal,
    status: ConfirmationStatus,
) -> str:
    body = render_draft_message(draft)
    badge = _terminal_badge(status)
    return f"{badge}\n\n{body}"


def _terminal_badge(status: ConfirmationStatus) -> str:
    if status is ConfirmationStatus.APPROVED:
        return "✅ *Saved*"
    if status is ConfirmationStatus.REJECTED:
        return "❌ *Rejected*"
    return "⏳ *Pending*"


def _escape_link_target(url: str) -> str:
    return url.replace("\\", "\\\\").replace(")", "\\)")
