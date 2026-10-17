"""The Telegram handlers driven the way the user drives them: send, then press.

Telegram is faked down to the few calls the handlers make; the intake module
behind them is real, with the Triage turns scripted and Notion faked.
"""

from __future__ import annotations

import asyncio
from itertools import count
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fakes import FakeNotionWriter, ScriptedTriageWorkflow, build_settings
from telegram import ForceReply, MessageEntity

from backlog_tamer.agents.intake_triage.schemas import DraftGrounding, ProjectDraft
from backlog_tamer.agents.intake_triage.triage_workflow import (
    Approved,
    Checkpoint,
    NeedsReview,
    Rejected,
)
from backlog_tamer.application.confirmation_store import ConfirmationStore
from backlog_tamer.application.intake_service import IntakeService
from backlog_tamer.integrations.notion import ExistingProject
from backlog_tamer.integrations.telegram.callbacks import (
    Action,
    ConfirmationAction,
    PickValue,
    decode_callback,
    encode_callback,
)
from backlog_tamer.integrations.telegram.handlers import (
    ALLOWED_USER_ID_KEY,
    DRAFT_ERROR_REPLY,
    INTAKE_SERVICE_KEY,
    REVIEW_ERROR_REPLY,
    SUPERSEDED_NOTE,
    UNKNOWN_CONFIRMATION_REPLY,
    handle_callback,
    handle_message,
)

ALLOWED_USER_ID = 42
CHAT_ID = 100
SOURCE_URL = "https://blog.langchain.com/langgraph/"

_message_ids = count(1)


def _draft(**overrides) -> ProjectDraft:
    payload = {
        "project_name": "LangGraph: build stateful multi-agent workflows",
        "summary": "Graph of stateful nodes.",
        "project_type": "tool",
        "intent": "learn",
        "priority": "High",
        "source_url": SOURCE_URL,
        "tasks": ["Read: LangGraph"],
    }
    payload.update(overrides)
    return ProjectDraft(**payload)


def _needs_review(
    draft: ProjectDraft | None = None,
    grounding: DraftGrounding | None = None,
    turn: int = 1,
) -> NeedsReview:
    return NeedsReview(
        draft=draft or _draft(),
        grounding=grounding or DraftGrounding(fetch_status="success"),
        review_message="Review the proposed draft.",
        checkpoint=Checkpoint(
            user_id=str(ALLOWED_USER_ID),
            session_id="session-1",
            invocation_id=f"invocation-{turn}",
            request_input_call_id=f"call-{turn}",
        ),
    )


class FakeMessage:
    """A message in the chat; the bot's own messages can be edited in place."""

    def __init__(self, chat: Chat, text: str | None = None, entities=(), markup=None):
        self.chat = chat
        self.message_id = next(_message_ids)
        self.text = text
        self.caption = None
        self.entities = tuple(entities)
        self.caption_entities = ()
        self.reply_markup = markup
        # Every text this message has shown, oldest first.
        self.shown: list[str] = [text] if text is not None else []

    async def reply_text(self, text, parse_mode=None, reply_markup=None):
        return self.chat.bot_sends(text, reply_markup)

    async def edit_text(self, text, parse_mode=None, reply_markup=None):
        # Like Telegram, editing the text without a keyboard removes it.
        self.text = text
        self.reply_markup = reply_markup
        self.shown.append(text)

    @property
    def buttons(self) -> list[str]:
        if self.reply_markup is None or isinstance(self.reply_markup, ForceReply):
            return []
        return [
            button.text for row in self.reply_markup.inline_keyboard for button in row
        ]

    def callback_data(self, label: str) -> str:
        """The callback_data of the one button whose label contains `label`."""
        matches = [
            button.callback_data
            for row in self.reply_markup.inline_keyboard
            for button in row
            if label in button.text
        ]
        assert len(matches) == 1, f"{label!r} not in {self.buttons}"
        return matches[0]


class FakeCallbackQuery:
    def __init__(self, data: str, message: FakeMessage):
        self.data = data
        self.message = message
        self.answers: list[str | None] = []

    async def answer(self, text=None, **kwargs):
        self.answers.append(text)

    async def edit_message_text(self, text, parse_mode=None, reply_markup=None):
        await self.message.edit_text(text, reply_markup=reply_markup)

    async def edit_message_reply_markup(self, reply_markup=None):
        self.message.reply_markup = reply_markup


class FakeBot:
    """Edits a message by id, the way a handler reaches one it is not replying to."""

    def __init__(self, chat: Chat):
        self.chat = chat

    async def edit_message_text(
        self, text, chat_id, message_id, parse_mode=None, reply_markup=None
    ):
        assert chat_id == CHAT_ID
        [message] = [m for m in self.chat.bot_messages if m.message_id == message_id]
        await message.edit_text(text, reply_markup=reply_markup)


class Chat:
    """One chat between the allowed user and the bot."""

    def __init__(
        self, tmp_path: Path, *outcomes, writer: FakeNotionWriter | None = None
    ):
        self.workflow = ScriptedTriageWorkflow(*outcomes)
        self.writer = writer or FakeNotionWriter()
        self.service = IntakeService(
            settings=build_settings(),
            store=ConfirmationStore(f"sqlite:///{tmp_path / 'confirmations.db'}"),
            notion_writer=self.writer,
            workflow=self.workflow,
        )
        self.context = SimpleNamespace(
            bot_data={
                INTAKE_SERVICE_KEY: self.service,
                ALLOWED_USER_ID_KEY: ALLOWED_USER_ID,
            },
            user_data={},
            bot=FakeBot(self),
        )
        self.bot_messages: list[FakeMessage] = []

    def bot_sends(self, text: str, markup=None) -> FakeMessage:
        message = FakeMessage(self, text, markup=markup)
        self.bot_messages.append(message)
        return message

    @property
    def last(self) -> FakeMessage:
        return self.bot_messages[-1]

    def send(self, text: str, entities=()) -> FakeMessage:
        message = FakeMessage(self, text, entities=entities)
        asyncio.run(handle_message(self._update(message), self.context))
        return message

    def send_link(self, url: str = SOURCE_URL) -> FakeMessage:
        return self.send(
            url,
            entities=[MessageEntity(MessageEntity.URL, offset=0, length=len(url))],
        )

    def press(
        self, card: FakeMessage, label: str, user_id: int = ALLOWED_USER_ID
    ) -> FakeCallbackQuery:
        return self.press_data(card, card.callback_data(label), user_id=user_id)

    def press_data(
        self,
        card: FakeMessage,
        data: str,
        user_id: int = ALLOWED_USER_ID,
        chat_id: int | None = CHAT_ID,
    ) -> FakeCallbackQuery:
        query = FakeCallbackQuery(data, card)
        update = self._update(card, user_id, chat_id, callback_query=query)
        asyncio.run(handle_callback(update, self.context))
        return query

    @staticmethod
    def _update(
        message,
        user_id: int = ALLOWED_USER_ID,
        chat_id: int | None = CHAT_ID,
        callback_query=None,
    ):
        # A button on a message sent in inline mode arrives without a chat.
        return SimpleNamespace(
            effective_message=message,
            effective_chat=None if chat_id is None else SimpleNamespace(id=chat_id),
            effective_user=SimpleNamespace(id=user_id),
            callback_query=callback_query,
        )


def test_a_sent_link_becomes_a_review_card(tmp_path: Path):
    chat = Chat(tmp_path, _needs_review())

    chat.send_link()

    card = chat.last
    assert card.shown[0] == "🔎 Reading blog.langchain.com…"
    assert "<b>LangGraph: build stateful multi-agent workflows</b>" in card.text
    assert card.buttons == [
        "✅ Approve & save",
        "❌ Reject",
        "🔺 High ▸",
        "📚 learn ▸",
        "🛠️ tool ▸",
        "📝 Revise with a note",
    ]
    assert chat.workflow.operations == ["start"]


def test_revision_feedback_redrafts_and_names_what_changed(tmp_path: Path):
    chat = Chat(
        tmp_path,
        _needs_review(),
        _needs_review(_draft(priority="Low", tasks=["Read", "Build"]), turn=2),
    )
    chat.send_link()
    card = chat.last

    chat.press(card, "Revise")

    assert card.buttons == ["✕ Cancel revision"]
    prompt = chat.last
    assert prompt.text.startswith("✏️ What should I change about <b>LangGraph")
    assert isinstance(prompt.reply_markup, ForceReply)

    chat.send("not urgent, and add a build task")

    revised = chat.last
    assert revised.shown[0] == "✍️ Revising the draft…"
    assert revised.text.startswith(
        "✏️ <i>Changed: priority High → Low · tasks 1 → 2</i>"
    )
    assert "🔻 Low ▸" in revised.buttons
    assert chat.workflow.operations == ["start", "revise"]
    assert (
        chat.workflow.calls[-1].args["feedback"] == "not urgent, and add a build task"
    )


def test_a_revision_retires_the_card_it_started_from(tmp_path: Path):
    chat = Chat(
        tmp_path,
        _needs_review(),
        _needs_review(_draft(priority="Low"), turn=2),
    )
    chat.send_link()
    card = chat.last
    chat.press(card, "Revise")

    chat.send("not urgent")

    assert card.buttons == []
    assert card.text.startswith(SUPERSEDED_NOTE)
    assert "<b>LangGraph: build stateful multi-agent workflows</b>" in card.text
    assert "🔺 High" in card.text
    assert "🔻 Low ▸" in chat.last.buttons


def test_empty_revision_feedback_leaves_the_card_waiting(tmp_path: Path):
    chat = Chat(tmp_path, _needs_review())
    chat.send_link()
    card = chat.last
    chat.press(card, "Revise")

    chat.send("   ")

    assert card.buttons == ["✕ Cancel revision"]


def test_empty_revision_feedback_asks_again_and_keeps_waiting(tmp_path: Path):
    chat = Chat(
        tmp_path,
        _needs_review(),
        _needs_review(_draft(priority="Low"), turn=2),
    )
    chat.send_link()
    chat.press(chat.last, "Revise")

    chat.send("   ")

    assert chat.last.text == "Please send the revision feedback as text."
    chat.send("lower priority")
    assert chat.workflow.operations == ["start", "revise"]


def test_cancelled_revision_restores_the_card_and_frees_the_next_message(
    tmp_path: Path,
):
    chat = Chat(tmp_path, _needs_review(), _needs_review(_draft(), turn=2))
    chat.send_link()
    card = chat.last
    chat.press(card, "Revise")

    query = chat.press(card, "Cancel revision")

    assert query.answers == ["Revision cancelled."]
    assert "✅ Approve & save" in card.buttons
    chat.send_link("https://example.com/another")
    assert chat.workflow.operations == ["start", "start"]


def test_a_redraft_that_changes_nothing_has_no_change_line(tmp_path: Path):
    chat = Chat(tmp_path, _needs_review(), _needs_review(turn=2))
    chat.send_link()
    chat.press(chat.last, "Revise")

    chat.send("looks fine actually")

    assert chat.last.text.startswith("<b>LangGraph")


def test_a_revision_the_agent_ends_with_approval_saves_it(tmp_path: Path):
    chat = Chat(tmp_path, _needs_review(), Approved())
    chat.send_link()
    chat.press(chat.last, "Revise")

    chat.send("perfect, save it")

    assert chat.last.text.startswith("✅ <b>Saved to Notion</b>")
    assert chat.last.buttons == ["↗️ Open in Notion", "↩️ Undo"]
    assert len(chat.writer.calls) == 1


def test_cancel_without_a_chat_still_restores_the_card(tmp_path: Path):
    chat = Chat(tmp_path, _needs_review())
    chat.send_link()
    card = chat.last
    chat.press(card, "Revise")

    chat.press_data(card, card.callback_data("Cancel revision"), chat_id=None)

    assert "✅ Approve & save" in card.buttons


def test_quick_edit_sets_a_field_without_the_agent(tmp_path: Path):
    chat = Chat(tmp_path, _needs_review())
    chat.send_link()
    card = chat.last

    chat.press(card, "High ▸")

    assert card.buttons == ["🔺 High", "▪️ Medium", "🔻 Low", "↩︎ Back"]

    chat.press(card, "Low")

    assert "🔻 Low" in card.text
    assert "🔻 Low ▸" in card.buttons
    assert chat.service.get_review(_confirmation_id(card)).draft_proposal.priority == (
        "Low"
    )
    assert chat.workflow.operations == ["start"]


def test_back_from_a_picker_restores_the_review_keyboard(tmp_path: Path):
    chat = Chat(tmp_path, _needs_review())
    chat.send_link()
    card = chat.last
    text_before = card.text

    chat.press(card, "tool ▸")
    assert "📦 repository" in card.buttons

    chat.press(card, "Back")

    assert card.text == text_before
    assert "🛠️ tool ▸" in card.buttons
    assert "✅ Approve & save" in card.buttons


def test_approve_saves_to_notion_and_leaves_a_terminal_card(tmp_path: Path):
    chat = Chat(tmp_path, _needs_review(), Approved())
    chat.send_link()
    card = chat.last

    chat.press(card, "Approve")

    assert card.shown[-2] == "⏳ Saving to Notion…"
    assert card.text.startswith("✅ <b>Saved to Notion</b>")
    assert card.buttons == ["↗️ Open in Notion", "↩️ Undo"]
    assert [draft.project_name for draft in chat.writer.calls] == [
        "LangGraph: build stateful multi-agent workflows"
    ]


def test_undo_archives_what_the_commit_created(tmp_path: Path):
    chat = Chat(tmp_path, _needs_review(), Approved())
    chat.send_link()
    card = chat.last
    chat.press(card, "Approve")

    chat.press(card, "Undo")

    assert card.shown[-2] == "↩️ Removing it from Notion…"
    assert card.text.startswith("↩️ <b>Removed from Notion</b>")
    assert card.buttons == []
    assert chat.writer.archived == ["project-id", "task-id"]


def test_reject_closes_the_review_without_writing(tmp_path: Path):
    chat = Chat(tmp_path, _needs_review(), Rejected(draft=None))
    chat.send_link()
    card = chat.last

    chat.press(card, "Reject")

    assert card.text.startswith("❌ <b>Rejected</b>")
    assert card.buttons == []
    assert chat.writer.calls == []


def test_a_failed_save_can_be_retried(tmp_path: Path):
    chat = Chat(
        tmp_path, _needs_review(), Approved(), writer=FakeNotionWriter(fail=True)
    )
    chat.send_link()
    card = chat.last
    chat.press(card, "Approve")

    assert card.text.startswith("⚠️ <b>Save failed</b>\nnotion unavailable")
    assert card.buttons == ["🔁 Retry save"]

    chat.writer.fail = False
    chat.press(card, "Retry save")

    assert card.shown[-2] == "⏳ Saving to Notion…"
    assert card.text.startswith("✅ <b>Saved to Notion</b>")
    assert chat.workflow.operations == ["start", "approve"]


def test_a_duplicate_link_can_add_its_task_to_the_existing_project(tmp_path: Path):
    writer = FakeNotionWriter()
    writer.existing_project = ExistingProject(
        page_id="existing-id",
        page_url="https://notion.so/existing-id",
        created_time="2026-09-01T10:00:00.000Z",
    )
    chat = Chat(tmp_path, _needs_review(), Approved(), writer=writer)
    chat.send_link()
    card = chat.last
    chat.press(card, "Approve")

    assert card.text.startswith("🔁 <b>Already in your backlog</b>")
    assert "since 01 Sep 2026" in card.text
    assert card.buttons == ["↗️ Open existing", "＋ Add task there"]

    chat.press(card, "Add task there")

    assert card.shown[-2] == "＋ Adding the task to the existing project…"
    assert card.text.startswith("✅ <b>Saved to Notion</b>")
    assert writer.added_task_project_ids == ["existing-id"]
    assert writer.calls == []


def test_a_failed_draft_says_so(tmp_path: Path):
    chat = Chat(tmp_path)  # no Turn outcome queued: the Triage turn fails

    chat.send_link()

    assert chat.last.text == DRAFT_ERROR_REPLY


def test_retry_fetch_redrafts_from_a_fresh_read_of_the_page(tmp_path: Path):
    failed_fetch = DraftGrounding(fetch_status="error", fetch_error="HTTP 503")
    chat = Chat(
        tmp_path,
        _needs_review(grounding=failed_fetch),
        _needs_review(_draft(project_name="LangGraph overview"), turn=2),
    )
    chat.send_link()
    card = chat.last
    assert "⚠️ <i>Couldn't open the page (HTTP 503)" in card.text

    chat.press(card, "Retry fetch")

    assert card.shown[-2] == "🔎 Opening the page again…"
    assert card.text.startswith("<b>LangGraph overview</b>")
    assert "Couldn't open the page" not in card.text
    assert "🔁 Retry fetch" not in card.buttons
    assert chat.workflow.operations == ["start", "refetch"]


def test_a_stale_button_after_the_review_closed_shows_where_it_ended(
    tmp_path: Path,
):
    failed_fetch = DraftGrounding(fetch_status="error", fetch_error="HTTP 503")
    chat = Chat(tmp_path, _needs_review(grounding=failed_fetch), Rejected(draft=None))
    chat.send_link()
    card = chat.last
    retry_fetch = card.callback_data("Retry fetch")
    chat.press(card, "Reject")

    chat.press_data(card, retry_fetch)

    assert card.text.startswith("❌ <b>Rejected</b>")
    assert chat.workflow.operations == ["start", "reject"]


@pytest.mark.parametrize("action", list(Action))
def test_an_unknown_confirmation_asks_for_the_item_again(tmp_path: Path, action):
    chat = Chat(tmp_path)
    card = chat.bot_sends("An old card")

    chat.press_data(card, encode_callback(ConfirmationAction(action, str(uuid4()))))

    assert card.text == UNKNOWN_CONFIRMATION_REPLY
    assert chat.workflow.operations == []


def test_a_quick_edit_on_an_unknown_confirmation_asks_for_the_item_again(
    tmp_path: Path,
):
    chat = Chat(tmp_path)
    card = chat.bot_sends("An old card")

    chat.press_data(card, encode_callback(PickValue("priority", "Low", str(uuid4()))))

    assert card.text == UNKNOWN_CONFIRMATION_REPLY


def test_an_unexpected_failure_keeps_the_draft_open(tmp_path: Path):
    chat = Chat(tmp_path, _needs_review())  # nothing queued for the approval
    chat.send_link()
    card = chat.last

    chat.press(card, "Approve")

    assert card.text.startswith(REVIEW_ERROR_REPLY)
    assert "<b>LangGraph: build stateful multi-agent workflows</b>" in card.text
    assert "✅ Approve & save" in card.buttons
    chat.workflow.queue(Approved())
    chat.press(card, "Approve")
    assert card.text.startswith("✅ <b>Saved to Notion</b>")


def test_an_unexpected_refetch_failure_keeps_the_draft_open(tmp_path: Path):
    failed_fetch = DraftGrounding(fetch_status="error", fetch_error="HTTP 503")
    chat = Chat(tmp_path, _needs_review(grounding=failed_fetch))
    chat.send_link()
    card = chat.last

    chat.press(card, "Retry fetch")

    assert card.text.startswith(REVIEW_ERROR_REPLY)
    assert "🔁 Retry fetch" in card.buttons
    assert "✅ Approve & save" in card.buttons


def test_an_unexpected_revision_failure_keeps_the_draft_open(tmp_path: Path):
    chat = Chat(tmp_path, _needs_review())  # nothing queued for the Revision
    chat.send_link()
    chat.press(chat.last, "Revise")

    chat.send("lower priority")

    reply = chat.last
    assert reply.text.startswith(REVIEW_ERROR_REPLY)
    assert "📝 Revise with a note" in reply.buttons
    chat.workflow.queue(Approved())
    chat.press(reply, "Approve")
    assert reply.text.startswith("✅ <b>Saved to Notion</b>")


def test_another_user_cannot_press_the_buttons(tmp_path: Path):
    chat = Chat(tmp_path, _needs_review(), Approved())
    chat.send_link()
    card = chat.last
    text_before, buttons_before = card.text, card.buttons

    query = chat.press(card, "Approve", user_id=7)

    assert query.answers == [None]
    assert (card.text, card.buttons) == (text_before, buttons_before)
    assert chat.workflow.operations == ["start"]
    assert chat.writer.calls == []


@pytest.mark.parametrize(
    "data",
    ["launch:0b6f1c1e", "approve", "edit:x:0b6f1c1e", "pick:p:0b6f1c1e"],
)
def test_malformed_callback_data_is_answered_and_ignored(tmp_path: Path, data):
    chat = Chat(tmp_path, _needs_review())
    chat.send_link()
    card = chat.last
    text_before, buttons_before = card.text, card.buttons

    query = chat.press_data(card, data)

    assert query.answers == [None]
    assert (card.text, card.buttons) == (text_before, buttons_before)


def test_a_callback_without_data_is_left_alone(tmp_path: Path):
    chat = Chat(tmp_path)
    card = chat.bot_sends("A card")

    query = chat.press_data(card, "")

    assert query.answers == []
    assert card.shown == ["A card"]


def test_revise_needs_a_chat_to_wait_in(tmp_path: Path):
    chat = Chat(tmp_path, _needs_review())
    chat.send_link()
    card = chat.last
    buttons_before = card.buttons

    chat.press_data(card, card.callback_data("Revise"), chat_id=None)

    assert card.buttons == buttons_before
    assert chat.last is card


def _confirmation_id(card: FakeMessage) -> str:
    return decode_callback(card.callback_data("Approve")).confirmation_id
