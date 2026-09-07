from __future__ import annotations

import pytest

from backlog_tamer.agents.intake_triage.schemas import ProjectDraft
from backlog_tamer.agents.intake_triage.task_names import (
    compose_task_names,
    with_composed_task_names,
)


def _draft(**overrides) -> ProjectDraft:
    payload: dict[str, object] = {
        "project_name": "SKILL.state: scalable long-horizon agent execution",
        "short_name": "SKILL.state",
        "summary": "Explicit mutable execution state for long-running agents.",
        "project_type": "paper",
        "intent": "learn",
        "priority": "Medium",
        "tasks": ["Read"],
    }
    payload.update(overrides)
    return ProjectDraft.model_validate(payload)


@pytest.mark.parametrize(
    ("intent", "project_type", "expected"),
    [
        ("learn", "paper", "Read: SKILL.state"),
        ("explore", "repository", "Explore: SKILL.state"),
        ("build", "tool", "Build: SKILL.state"),
        ("research", "product", "Research: SKILL.state"),
        ("reference", "tool", "Skim and file: SKILL.state"),
        ("unclear", "product", "Explore: SKILL.state"),
        # The medium overrides the intent verb where it would read wrong.
        ("learn", "course", "Work through: SKILL.state"),
        ("learn", "video", "Watch: SKILL.state"),
    ],
)
def test_the_default_task_is_named_after_its_subject(
    intent: str,
    project_type: str,
    expected: str,
):
    """Twenty tasks called "Read" and twenty called "Explore" is not a backlog."""
    draft = _draft(intent=intent, project_type=project_type)

    assert compose_task_names(draft) == [expected]


def test_an_authored_task_name_is_left_alone():
    draft = _draft(tasks=["Reproduce the benchmark on a laptop"])

    assert compose_task_names(draft) == ["Reproduce the benchmark on a laptop"]


def test_an_explicit_breakdown_is_left_alone():
    draft = _draft(tasks=["Read", "Explore", "Build"])

    assert compose_task_names(draft) == ["Read", "Explore", "Build"]


def test_composing_twice_does_not_stack_prefixes():
    once = with_composed_task_names(_draft())

    assert with_composed_task_names(once).tasks == ["Read: SKILL.state"]


def test_a_draft_saved_before_short_name_falls_back_to_the_project_name():
    """Older rows carry the handle in front of the colon."""
    draft = _draft(short_name="")

    assert compose_task_names(draft) == ["Read: SKILL.state"]


def test_a_project_name_without_a_handle_uses_the_whole_name():
    draft = _draft(
        project_name="Vector index tradeoffs for hybrid search",
        short_name="",
    )

    assert compose_task_names(draft) == [
        "Read: Vector index tradeoffs for hybrid search"
    ]
