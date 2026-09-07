from __future__ import annotations

import json

import pytest

from backlog_tamer.agents.intake_triage.prompts import INTAKE_TRIAGE_INSTRUCTIONS
from backlog_tamer.agents.intake_triage.schemas import ProjectDraft

PROJECT_TYPES = [
    "paper",
    "article",
    "video",
    "course",
    "repository",
    "product",
    "company",
    "model",
    "tool",
]


def _draft_payload(**overrides) -> dict[str, object]:
    payload: dict[str, object] = {
        "project_name": "Example target",
        "summary": "A target worth exploring.",
        "project_type": "product",
        "intent": "explore",
        "priority": "Medium",
        "tasks": ["Explore"],
    }
    payload.update(overrides)
    return payload


def test_project_type_schema_exposes_only_the_compact_vocabulary():
    schema = ProjectDraft.model_json_schema()

    assert schema["properties"]["project_type"]["enum"] == PROJECT_TYPES
    assert "resource_type" not in schema["properties"]


@pytest.mark.parametrize(
    ("target", "expected_guidance"),
    [
        (
            "a Cursor changelog or documentation page introducing Origin",
            'introduces Origin is project_type "product", not documentation',
        ),
        (
            "a GitHub page for an open-source target",
            'open-source repository is "repository"',
        ),
        (
            "a research paper linked through a publisher page",
            'research paper is\n  "paper"',
        ),
    ],
)
def test_classifier_prompt_keeps_project_focused_regression_cases(
    target: str,
    expected_guidance: str,
):
    assert target
    assert expected_guidance in INTAKE_TRIAGE_INSTRUCTIONS


@pytest.mark.parametrize(
    ("target", "expected_guidance"),
    [
        (
            "a course hosted by the vendor whose tool it teaches",
            'A course is "course" even when it teaches a specific tool',
        ),
        (
            "a news article covering a product launch",
            'about NVIDIA PAIR is "product"',
        ),
    ],
)
def test_study_material_has_its_own_vocabulary(
    target: str,
    expected_guidance: str,
):
    """Two courses in one week were classified "tool" for want of a value."""
    assert target
    assert expected_guidance in INTAKE_TRIAGE_INSTRUCTIONS


@pytest.mark.parametrize(
    ("legacy_type", "project_type"),
    [
        ("paper", "paper"),
        ("repository", "repository"),
        ("documentation", "tool"),
        ("article", "article"),
        ("video", "video"),
        ("course", "course"),
        ("idea", "product"),
    ],
)
def test_persisted_resource_type_drafts_are_migrated_on_load(
    legacy_type: str,
    project_type: str,
):
    payload = _draft_payload()
    payload.pop("project_type")
    payload["resource_type"] = legacy_type

    draft = ProjectDraft.model_validate_json(json.dumps(payload))

    assert draft.project_type == project_type
    assert "resource_type" not in draft.model_dump()
