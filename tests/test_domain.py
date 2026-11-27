from __future__ import annotations

import pytest
from pydantic import ValidationError

from backlog_tamer.domain import Grounding, ProjectDraft, fold_tag


def _draft() -> ProjectDraft:
    return ProjectDraft(
        project_name="LangGraph: build stateful agents",
        summary="Graph of stateful nodes.",
        project_type="repository",
        intent="explore",
        priority="Medium",
        topics=["langgraph"],
    )


def test_tag_spelling_variants_fold_to_one_key():
    assert fold_tag("LLM-Agents") == fold_tag("llm agent") == "llm agent"
    assert fold_tag("agent_evaluation") == "agent evaluation"


def test_a_trailing_s_stays_on_short_words():
    assert fold_tag("gas") == "gas"
    assert fold_tag("ops tools") == "ops tool"


def test_with_edits_applies_fields_and_keeps_the_rest():
    edited = _draft().with_edits({"project_type": "tool", "priority": "High"})

    assert (edited.project_type, edited.priority) == ("tool", "High")
    assert edited.project_name == "LangGraph: build stateful agents"


def test_with_edits_rejects_a_value_outside_the_label_set():
    with pytest.raises(ValidationError):
        _draft().with_edits({"intent": "binge"})


def test_grounding_rows_stored_before_the_new_fields_still_load():
    stored = '{"fetch_status": "success", "site_name": "GitHub"}'

    grounding = Grounding.model_validate_json(stored)

    assert grounding.site_name == "GitHub"
    assert grounding.description is None
    assert grounding.content_preview is None


def test_draft_rows_stored_with_tasks_still_load_and_drop_them():
    stored = (
        '{"project_name": "LangGraph", "short_name": "LangGraph",'
        ' "summary": "Graph of stateful nodes.", "project_type": "repository",'
        ' "intent": "explore", "priority": "Medium",'
        ' "tasks": ["Read the README"]}'
    )

    draft = ProjectDraft.model_validate_json(stored)

    assert draft.project_name == "LangGraph"
    assert "tasks" not in draft.model_dump()
    assert "short_name" not in draft.model_dump()
