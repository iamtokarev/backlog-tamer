"""The intake module driving a Review, with Triage and the source reader faked."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fakes import FakeNotionWriter, FakeReader, FakeTriage, build_settings

from backlog_tamer.application.confirmation_store import ConfirmationStore
from backlog_tamer.application.intake_service import IntakeService
from backlog_tamer.application.models import ConfirmationStatus
from backlog_tamer.domain import (
    Grounding,
    IncomingContext,
    ProjectDraft,
    SourceLink,
)
from backlog_tamer.integrations.source_reader import FetchedUrl

SOURCE_URL = "https://blog.langchain.com/langgraph/"
CONTEXT = IncomingContext(raw_text=SOURCE_URL, links=[SourceLink(url=SOURCE_URL)])


def _draft(**overrides) -> ProjectDraft:
    payload = {
        "project_name": "LangGraph: build stateful multi-agent workflows",
        "summary": "Graph of stateful nodes.",
        "project_type": "tool",
        "intent": "learn",
        "priority": "High",
        "source_url": SOURCE_URL,
    }
    payload.update(overrides)
    return ProjectDraft(**payload)


def _page(site_name: str = "LangChain Blog") -> FetchedUrl:
    return FetchedUrl(
        status="success",
        requested_url=SOURCE_URL,
        final_url=SOURCE_URL,
        site_name=site_name,
        title="LangGraph",
    )


FAILED_READ = FetchedUrl(
    status="error", requested_url=SOURCE_URL, error="Source returned HTTP 503."
)


class Harness:
    def __init__(self, tmp_path: Path, *drafts: ProjectDraft, reads=()):
        self.store = ConfirmationStore(f"sqlite:///{tmp_path / 'confirmations.db'}")
        self.triage = FakeTriage(*drafts)
        self.reader = FakeReader(*(reads or [_page()]))
        self.writer = FakeNotionWriter()
        self.service = IntakeService(
            settings=build_settings(),
            store=self.store,
            notion_writer=self.writer,
            triage=self.triage,
            reader=self.reader,
        )

    def start(self, context: IncomingContext = CONTEXT):
        return asyncio.run(
            self.service.start_intake(
                context,
                user_id="user-id",
                chat_id="chat-id",
                source_message_id="message-id",
            )
        )

    def quick_edit(self, confirmation_id: str, field: str, value: str):
        return self.service.apply_quick_edit(confirmation_id, field, value)


def test_start_persists_the_draft_and_grounding(tmp_path: Path):
    harness = Harness(tmp_path, _draft())
    harness.writer.tag_options = ["langgraph"]

    result = harness.start()

    grounding = _page().to_grounding()
    assert result.status == "needs_review"
    assert result.draft_proposal == _draft()
    assert result.grounding == grounding
    assert harness.reader.urls == [SOURCE_URL]
    [call] = harness.triage.calls
    assert call.operation == "classify"
    assert call.args["context"] == CONTEXT
    assert call.args["grounding"] == grounding
    assert call.args["vocabulary"] == ["langgraph"]
    stored = harness.store.get(result.confirmation_id)
    assert stored is not None
    assert stored.status is ConfirmationStatus.PENDING_REVIEW
    assert stored.draft_proposal == _draft()
    assert stored.grounding == grounding
    assert stored.chat_id == "chat-id"


def test_start_without_a_link_classifies_a_skipped_grounding_unread(
    tmp_path: Path,
):
    harness = Harness(tmp_path, _draft(source_url=None))

    result = harness.start(IncomingContext(raw_text="Learn Rust this winter"))

    assert harness.reader.reads == 0
    assert harness.triage.calls[0].args["grounding"] == Grounding(
        fetch_status="skipped"
    )
    assert result.grounding == Grounding(fetch_status="skipped")


def test_a_failed_read_still_drafts_from_an_error_grounding(tmp_path: Path):
    harness = Harness(tmp_path, _draft(), reads=[FAILED_READ])

    result = harness.start()

    assert result.status == "needs_review"
    assert result.grounding.fetch_status == "error"
    assert harness.triage.calls[0].args["grounding"].fetch_status == "error"


def test_a_revision_passes_the_stored_context_grounding_and_draft(tmp_path: Path):
    revised = _draft(intent="build")
    harness = Harness(tmp_path, _draft(), revised)
    harness.writer.tag_options = ["langgraph"]
    started = harness.start()

    result = asyncio.run(
        harness.service.revise(started.confirmation_id, "I want to build with it")
    )

    assert result.status == "needs_review"
    assert result.draft_proposal == revised
    call = harness.triage.calls[-1]
    assert call.operation == "revise"
    assert call.args["context"] == CONTEXT
    assert call.args["grounding"] == _page().to_grounding()
    assert call.args["draft"] == _draft()
    assert call.args["feedback"] == "I want to build with it"
    assert call.args["vocabulary"] == ["langgraph"]
    assert harness.reader.reads == 1
    review = harness.service.get_review(started.confirmation_id)
    assert review is not None
    assert review.draft_proposal == revised


def test_a_quick_edit_survives_revise_and_refetch(tmp_path: Path):
    # Triage proposes High every time; the user's Low must win throughout.
    harness = Harness(tmp_path, _draft(), _draft(intent="build"), _draft())
    started = harness.start()
    harness.quick_edit(started.confirmation_id, "priority", "Low")

    revised = asyncio.run(harness.service.revise(started.confirmation_id, "build"))
    refetched = asyncio.run(harness.service.refetch(started.confirmation_id))

    assert harness.triage.calls[1].args["draft"].priority == "Low"
    assert revised.draft_proposal == _draft(intent="build", priority="Low")
    assert refetched.draft_proposal == _draft(priority="Low")
    stored = harness.store.get(started.confirmation_id)
    assert stored is not None
    assert stored.draft_proposal.priority == "Low"
    assert stored.kept_fields == {"priority": "Low"}


def test_refetch_reads_again_and_stores_the_new_grounding(tmp_path: Path):
    regrounded = _page(site_name="LangChain")
    harness = Harness(
        tmp_path,
        _draft(),
        _draft(project_name="LangGraph overview"),
        reads=[FAILED_READ, regrounded],
    )
    started = harness.start()

    result = asyncio.run(harness.service.refetch(started.confirmation_id))

    assert harness.reader.reads == 2
    assert harness.triage.operations == ["classify", "classify"]
    assert harness.triage.calls[-1].args["grounding"] == regrounded.to_grounding()
    assert result.status == "needs_review"
    assert result.grounding == regrounded.to_grounding()
    assert result.draft_proposal == _draft(project_name="LangGraph overview")
    stored = harness.store.get(started.confirmation_id)
    assert stored is not None
    assert stored.grounding == regrounded.to_grounding()


def test_revision_text_reading_approve_is_sent_as_a_revision(tmp_path: Path):
    harness = Harness(tmp_path, _draft(), _draft())
    started = harness.start()

    result = asyncio.run(harness.service.revise(started.confirmation_id, "Approve"))

    assert result.status == "needs_review"
    assert harness.triage.operations == ["classify", "revise"]
    assert harness.writer.calls == []


def test_approve_commits_the_reviewed_draft_with_no_triage_call(tmp_path: Path):
    harness = Harness(tmp_path, _draft())
    started = harness.start()

    result = asyncio.run(harness.service.approve(started.confirmation_id))

    assert result.status == ConfirmationStatus.COMMITTED.value
    assert result.notion_project_url == "https://notion.so/project-id"
    assert harness.triage.operations == ["classify"]
    assert harness.writer.calls == [_draft()]


def test_reject_closes_the_review_without_touching_notion(tmp_path: Path):
    harness = Harness(tmp_path, _draft())
    started = harness.start()

    result = asyncio.run(harness.service.reject(started.confirmation_id))

    assert result.status == ConfirmationStatus.REJECTED.value
    assert result.draft_proposal == _draft()
    assert harness.triage.operations == ["classify"]
    assert harness.writer.calls == []
    stored = harness.store.get(started.confirmation_id)
    assert stored is not None
    assert stored.status is ConfirmationStatus.REJECTED


@pytest.mark.parametrize(
    "decide",
    [
        lambda service, cid: service.approve(cid),
        lambda service, cid: service.reject(cid),
        lambda service, cid: service.revise(cid, "shorter"),
        lambda service, cid: service.refetch(cid),
    ],
    ids=["approve", "reject", "revise", "refetch"],
)
def test_a_finished_review_ignores_late_decisions(tmp_path: Path, decide):
    harness = Harness(tmp_path, _draft())
    started = harness.start()
    asyncio.run(harness.service.reject(started.confirmation_id))

    result = asyncio.run(decide(harness.service, started.confirmation_id))

    assert result.status == ConfirmationStatus.REJECTED.value
    assert result.draft_proposal == _draft()
    assert harness.triage.operations == ["classify"]
    assert harness.reader.reads == 1
    assert harness.writer.calls == []


@pytest.mark.parametrize(
    "decide",
    [
        lambda service, cid: service.approve(cid),
        lambda service, cid: service.reject(cid),
        lambda service, cid: service.revise(cid, "shorter"),
        lambda service, cid: service.refetch(cid),
    ],
    ids=["approve", "reject", "revise", "refetch"],
)
def test_a_decision_on_an_unknown_confirmation_is_an_error(tmp_path: Path, decide):
    harness = Harness(tmp_path)

    with pytest.raises(ValueError):
        asyncio.run(decide(harness.service, "missing"))


def test_get_review_shows_the_draft_awaiting_review(tmp_path: Path):
    harness = Harness(tmp_path, _draft())
    started = harness.start()

    review = harness.service.get_review(started.confirmation_id)

    assert review is not None
    assert review.status == "needs_review"
    assert review.confirmation_id == started.confirmation_id
    assert review.draft_proposal == _draft()
    assert review.grounding == _page().to_grounding()


def test_get_review_of_an_unknown_confirmation_is_none(tmp_path: Path):
    harness = Harness(tmp_path)

    assert harness.service.get_review("missing") is None


def test_a_quick_edit_changes_the_draft_without_a_triage_call(tmp_path: Path):
    harness = Harness(tmp_path, _draft())
    started = harness.start()

    result = harness.quick_edit(started.confirmation_id, "priority", "Low")

    assert result.status == "needs_review"
    assert result.draft_proposal == _draft(priority="Low")
    assert harness.triage.operations == ["classify"]
    review = harness.service.get_review(started.confirmation_id)
    assert review is not None
    assert review.draft_proposal == _draft(priority="Low")


def test_a_quick_edit_after_the_review_ended_is_refused(tmp_path: Path):
    harness = Harness(tmp_path, _draft())
    started = harness.start()
    asyncio.run(harness.service.reject(started.confirmation_id))

    with pytest.raises(ValueError):
        harness.quick_edit(started.confirmation_id, "priority", "Low")
