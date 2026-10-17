"""The intake module driving a Review, with the Triage turns scripted."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fakes import FakeNotionWriter, ScriptedTriageWorkflow, build_settings

from backlog_tamer.agents.intake_triage.schemas import (
    DraftGrounding,
    IncomingContext,
    ProjectDraft,
)
from backlog_tamer.agents.intake_triage.triage_workflow import (
    Approved,
    Checkpoint,
    NeedsReview,
    Rejected,
)
from backlog_tamer.application.confirmation_store import ConfirmationStore
from backlog_tamer.application.intake_service import IntakeService
from backlog_tamer.application.models import ConfirmationStatus

FIRST_CHECKPOINT = Checkpoint(
    user_id="user-id",
    session_id="session-1",
    invocation_id="invocation-1",
    request_input_call_id="call-1",
)


def _draft(**overrides) -> ProjectDraft:
    payload = {
        "project_name": "LangGraph: build stateful multi-agent workflows",
        "summary": "Graph of stateful nodes.",
        "project_type": "tool",
        "intent": "learn",
        "priority": "High",
        "source_url": "https://blog.langchain.com/langgraph/",
        "tasks": ["Read: LangGraph"],
    }
    payload.update(overrides)
    return ProjectDraft(**payload)


def _needs_review(
    draft: ProjectDraft | None = None,
    checkpoint: Checkpoint = FIRST_CHECKPOINT,
    grounding: DraftGrounding | None = None,
) -> NeedsReview:
    return NeedsReview(
        draft=draft or _draft(),
        grounding=grounding or DraftGrounding(fetch_status="skipped"),
        review_message="Review the proposed draft.",
        checkpoint=checkpoint,
    )


class Harness:
    def __init__(self, tmp_path: Path, *outcomes):
        self.store = ConfirmationStore(f"sqlite:///{tmp_path / 'confirmations.db'}")
        self.workflow = ScriptedTriageWorkflow(*outcomes)
        self.writer = FakeNotionWriter()
        self.service = IntakeService(
            settings=build_settings(),
            store=self.store,
            notion_writer=self.writer,
            workflow=self.workflow,
        )

    def start(self):
        return asyncio.run(
            self.service.start_intake(
                IncomingContext(raw_text="https://blog.langchain.com/langgraph/"),
                user_id="user-id",
                chat_id="chat-id",
                source_message_id="message-id",
            )
        )


def test_start_persists_a_pending_confirmation_for_the_draft(tmp_path: Path):
    grounding = DraftGrounding(fetch_status="success", site_name="LangChain Blog")
    harness = Harness(tmp_path, _needs_review(grounding=grounding))
    harness.writer.tag_options = ["langgraph"]

    result = harness.start()

    assert result.status == "needs_review"
    assert result.draft_proposal == _draft()
    assert result.grounding == grounding
    assert harness.workflow.calls[0].args["known_topics"] == ["langgraph"]
    stored = harness.store.get(result.confirmation_id)
    assert stored is not None
    assert stored.status is ConfirmationStatus.PENDING_REVIEW
    assert stored.draft_proposal == _draft()
    assert stored.chat_id == "chat-id"


SECOND_CHECKPOINT = Checkpoint(
    user_id="user-id",
    session_id="session-1",
    invocation_id="invocation-2",
    request_input_call_id="call-2",
)


def _revise_calls(harness: Harness):
    return [call for call in harness.workflow.calls if call.operation == "revise"]


def test_a_revision_replaces_the_draft_and_resumes_from_the_new_checkpoint(
    tmp_path: Path,
):
    revised = _draft(project_name="LangGraph: stateful agents")
    harness = Harness(
        tmp_path,
        _needs_review(),
        _needs_review(revised, SECOND_CHECKPOINT),
        _needs_review(revised, SECOND_CHECKPOINT),
    )
    started = harness.start()

    result = asyncio.run(harness.service.revise(started.confirmation_id, "shorter"))
    asyncio.run(harness.service.revise(started.confirmation_id, "again"))

    assert result.status == "needs_review"
    assert result.draft_proposal == revised
    first, second = _revise_calls(harness)
    assert first.args["checkpoint"] == FIRST_CHECKPOINT
    assert first.args["feedback"] == "shorter"
    assert second.args["checkpoint"] == SECOND_CHECKPOINT


def test_quick_edits_reach_the_next_revision_and_then_clear(tmp_path: Path):
    harness = Harness(
        tmp_path,
        _needs_review(),
        _needs_review(checkpoint=SECOND_CHECKPOINT),
        _needs_review(checkpoint=SECOND_CHECKPOINT),
    )
    started = harness.start()
    harness.store.apply_manual_edit(
        confirmation_id=started.confirmation_id,
        field="priority",
        value="Low",
    )

    asyncio.run(harness.service.revise(started.confirmation_id, "shorter"))
    asyncio.run(harness.service.revise(started.confirmation_id, "again"))

    first, second = _revise_calls(harness)
    assert first.args["kept_fields"] == {"priority": "Low"}
    assert second.args["kept_fields"] == {}


def test_revision_text_reading_approve_is_sent_as_a_revision(tmp_path: Path):
    harness = Harness(
        tmp_path,
        _needs_review(),
        _needs_review(checkpoint=SECOND_CHECKPOINT),
    )
    started = harness.start()

    result = asyncio.run(harness.service.revise(started.confirmation_id, "Approve"))

    assert result.status == "needs_review"
    assert harness.workflow.operations == ["start", "revise"]
    assert harness.writer.calls == []


def test_approve_commits_the_reviewed_draft_to_notion(tmp_path: Path):
    harness = Harness(tmp_path, _needs_review(), Approved())
    started = harness.start()

    result = asyncio.run(harness.service.approve(started.confirmation_id))

    assert result.status == ConfirmationStatus.COMMITTED.value
    assert result.notion_project_url == "https://notion.so/project-id"
    assert harness.workflow.calls[-1].args["checkpoint"] == FIRST_CHECKPOINT
    assert harness.writer.calls == [_draft()]


def test_reject_closes_the_review_without_touching_notion(tmp_path: Path):
    harness = Harness(tmp_path, _needs_review(), Rejected(draft=_draft()))
    started = harness.start()

    result = asyncio.run(harness.service.reject(started.confirmation_id))

    assert result.status == ConfirmationStatus.REJECTED.value
    assert result.draft_proposal == _draft()
    assert harness.writer.calls == []
    stored = harness.store.get(started.confirmation_id)
    assert stored is not None
    assert stored.status is ConfirmationStatus.REJECTED


def test_a_rejection_without_a_draft_reports_the_stored_one(tmp_path: Path):
    harness = Harness(tmp_path, _needs_review(), Rejected(draft=None))
    started = harness.start()

    result = asyncio.run(harness.service.reject(started.confirmation_id))

    assert result.status == ConfirmationStatus.REJECTED.value
    assert result.draft_proposal == _draft()


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
    harness = Harness(tmp_path, _needs_review(), Rejected(draft=_draft()))
    started = harness.start()
    asyncio.run(harness.service.reject(started.confirmation_id))

    result = asyncio.run(decide(harness.service, started.confirmation_id))

    assert result.status == ConfirmationStatus.REJECTED.value
    assert harness.workflow.operations == ["start", "reject"]
    assert harness.writer.calls == []


def test_a_decision_on_an_unknown_confirmation_is_an_error(tmp_path: Path):
    harness = Harness(tmp_path)

    with pytest.raises(ValueError):
        asyncio.run(harness.service.approve("missing"))


def test_get_review_shows_the_draft_awaiting_review(tmp_path: Path):
    grounding = DraftGrounding(fetch_status="success", site_name="LangChain Blog")
    harness = Harness(tmp_path, _needs_review(grounding=grounding))
    started = harness.start()

    review = harness.service.get_review(started.confirmation_id)

    assert review is not None
    assert review.status == "needs_review"
    assert review.confirmation_id == started.confirmation_id
    assert review.draft_proposal == _draft()
    assert review.grounding == grounding


def test_get_review_of_an_unknown_confirmation_is_none(tmp_path: Path):
    harness = Harness(tmp_path)

    assert harness.service.get_review("missing") is None


def test_a_quick_edit_changes_the_draft_without_a_triage_turn(tmp_path: Path):
    harness = Harness(tmp_path, _needs_review())
    started = harness.start()

    result = harness.service.apply_quick_edit(
        started.confirmation_id,
        "priority",
        "Low",
    )

    assert result.status == "needs_review"
    assert result.draft_proposal == _draft(priority="Low")
    assert harness.workflow.operations == ["start"]
    review = harness.service.get_review(started.confirmation_id)
    assert review is not None
    assert review.draft_proposal == _draft(priority="Low")


def test_a_quick_edit_after_the_review_ended_is_refused(tmp_path: Path):
    harness = Harness(tmp_path, _needs_review(), Rejected(draft=_draft()))
    started = harness.start()
    asyncio.run(harness.service.reject(started.confirmation_id))

    with pytest.raises(ValueError):
        harness.service.apply_quick_edit(started.confirmation_id, "priority", "Low")


def test_refetch_redrafts_from_the_checkpoint_keeping_quick_edits(tmp_path: Path):
    regrounded = DraftGrounding(fetch_status="success", site_name="LangChain Blog")
    harness = Harness(
        tmp_path,
        _needs_review(grounding=DraftGrounding(fetch_status="error")),
        _needs_review(checkpoint=SECOND_CHECKPOINT, grounding=regrounded),
    )
    started = harness.start()
    harness.store.apply_manual_edit(
        confirmation_id=started.confirmation_id,
        field="priority",
        value="Low",
    )

    result = asyncio.run(harness.service.refetch(started.confirmation_id))

    assert result.status == "needs_review"
    assert result.grounding == regrounded
    call = harness.workflow.calls[-1]
    assert call.operation == "refetch"
    assert call.args["checkpoint"] == FIRST_CHECKPOINT
    assert call.args["kept_fields"] == {"priority": "Low"}
    stored = harness.store.get(started.confirmation_id)
    assert stored is not None
    assert stored.grounding == regrounded
