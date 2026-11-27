"""The eval pipeline: metrics, and the run core over a fake decision client."""

from __future__ import annotations

import asyncio
import re
from typing import Any

import pytest

from backlog_tamer.domain import Grounding, ProjectDraft
from backlog_tamer.triage.client import Usage
from evals import metrics
from evals.caching import CachingClient, UsageLog
from evals.golden import GoldenItem, load_vocabulary, write_vocabulary
from evals.run import evaluate


def test_tag_scores_fold_spelling_variants_before_comparing():
    scores = metrics.tag_scores(
        predicted=[["LLM-Agents", "mcp"], ["rag"]],
        gold=[["llm agent", "langgraph"], []],
    )

    # 1 hit ("LLM-Agents" ~ "llm agent") out of 3 predicted and 2 gold.
    assert scores == {"precision": 1 / 3, "recall": 1 / 2, "f1": 2 / 5}


DRAFT = ProjectDraft(
    project_name="LangGraph: build stateful agents",
    summary="Build resilient agents as graphs.",
    project_type="repository",
    intent="learn",
    priority="Medium",
    topics=["langgraph", "multi-agent"],
)


def test_revision_that_fixes_only_the_asked_field_has_no_collateral():
    after = DRAFT.model_copy(
        update={"intent": "explore", "topics": ["multi agents", "LangGraph"]}
    )

    # Reordered or respelled tags are the same tags, not a change.
    assert metrics.revision_outcome("intent", "explore", DRAFT, after) == (
        True,
        False,
    )


def test_revision_that_changes_an_unrelated_field_is_collateral():
    after = DRAFT.model_copy(update={"priority": "High", "intent": "build"})

    assert metrics.revision_outcome("priority", "High", DRAFT, after) == (
        True,
        True,
    )


def test_revision_that_misses_the_asked_field_is_not_applied():
    assert metrics.revision_outcome("project_type", "tool", DRAFT, DRAFT) == (
        False,
        False,
    )


def test_title_revision_applies_when_the_exact_rename_comes_back():
    after = DRAFT.model_copy(update={"project_name": '"LangGraph course"'})

    assert metrics.revision_outcome("project_name", "LangGraph course", DRAFT, after)[0]


def test_topic_revision_applies_when_every_requested_tag_is_present():
    after = DRAFT.model_copy(update={"topics": ["MCP", "agent-evaluation", "rag"]})

    assert metrics.revision_outcome(
        "topics", ["mcp", "agent evaluation"], DRAFT, after
    ) == (True, False)


REPO = "https://github.com/langchain-ai/langgraph"
COURSE = "https://example.edu/agents-course"
GOLDEN = [
    GoldenItem.model_validate(
        {
            "id": "repo",
            "url": REPO,
            "note": None,
            "gold": {
                "project_name": "LangGraph: build stateful agents",
                "project_type": "repository",
                "intent": "explore",
                "priority": "Medium",
                "topics": ["langgraph", "multi-agent"],
            },
            "grounding": Grounding(
                fetch_status="success", page_title="langgraph", final_url=REPO
            ),
        }
    ),
    GoldenItem.model_validate(
        {
            "id": "course",
            "url": COURSE,
            "note": "for the agent I am building now",
            "gold": {
                "project_name": "Agents course: build tool-using agents",
                "project_type": "course",
                "intent": "learn",
                "priority": "High",
                "topics": ["mcp"],
            },
            "grounding": Grounding(fetch_status="error", fetch_error="timeout"),
        }
    ),
]
# What the fake decides per URL: right on the repo, wrong type on the course.
DECIDED = {
    REPO: {"project_type": "repository", "intent": "explore", "tags": {"langgraph"}},
    COURSE: {"project_type": "article", "intent": "learn", "tags": {"mcp"}},
}
EVERY_METRIC = {
    "classify": {"n", "errors", "accuracy", "tags", "latency", "cost_per_1000"},
    "revise": {
        "n",
        "errors",
        "applied",
        "collateral",
        "applied_by_field",
        "latency",
        "cost_per_1000",
    },
    "titles": {"n", "errors", "wins", "losses", "ties", "win_rate"},
}


class FakeClient:
    """Answers like the decision API, keyed by the item's URL; no network."""

    decision_model = "fake/decider"
    title_model = "fake/title"

    def __init__(self, on_usage):
        self._on_usage = on_usage

    async def decide(self, state: Any, questions: dict[str, Any]) -> dict[str, Any]:
        self._record("/alpha/decisions")
        url = state.get("url") or state["page"]["url"]
        decided = DECIDED[url]
        answers: dict[str, Any] = {}
        for name, question in questions.items():
            if name in ("project_type", "intent"):
                answers[name] = {
                    "type": "choice",
                    "choice": decided[name],
                    "confidence": 0.9,
                }
            elif name == "priority":
                answers[name] = {
                    "type": "score",
                    "confidence": 0.8,
                    "probabilities": {"0": 0.1, "1": 0.1, "2": 0.8},
                }
            elif name.startswith("tag_"):
                tag = re.search(r"'(.+?)'", question["instructions"]).group(1)
                answers[name] = {"type": "noul", "noul": float(tag in decided["tags"])}
            elif name == "better":
                answers[name] = {"type": "choice", "choice": "title_1"}
            else:  # routing and title acceptability
                answers[name] = {"type": "noul", "noul": 0.0}
        return answers

    async def title(self, page, *, feedback=None, current=None) -> str:
        self._record("/v1/chat/completions")
        return f"Title for {page['url']}"

    def _record(self, path: str) -> None:
        self._on_usage(
            Usage(path=path, model="fake", seconds=0.5, input_tokens=100, cost=0.001)
        )


class OfflineClient(FakeClient):
    async def decide(self, state, questions):
        raise AssertionError("served from cache, the client must not be called")

    async def title(self, page, *, feedback=None, current=None):
        raise AssertionError("served from cache, the client must not be called")


# Notion's Tags options: more than the golden tags, plus a legacy intent tag.
VOCABULARY = ["learn", "langgraph", "rag", "multi-agent", "evals", "mcp"]


def run_eval(client_class, cache_dir) -> dict[str, Any]:
    usage = UsageLog()
    client = CachingClient(client_class(on_usage=usage), cache_dir, usage=usage)
    return asyncio.run(evaluate(GOLDEN, VOCABULARY, client, usage, stages={"all"}))


def test_the_vocabulary_is_the_notion_tag_options_not_the_golden_tags(tmp_path):
    report = run_eval(FakeClient, tmp_path)

    # Every subject option is offered; the legacy "learn" is not a subject tag.
    assert report["vocabulary"] == 5


def test_vocabulary_round_trips_in_notion_order(tmp_path):
    path = tmp_path / "vocabulary.json"

    write_vocabulary(VOCABULARY, path)

    assert load_vocabulary(path) == VOCABULARY


def test_a_missing_vocabulary_says_how_to_build_it(tmp_path):
    with pytest.raises(SystemExit, match="mise run eval:golden"):
        load_vocabulary(tmp_path / "vocabulary.json")


def test_report_has_every_metric_and_classify_accuracy_matches_the_answers(
    tmp_path,
):
    report = run_eval(FakeClient, tmp_path)

    for stage, keys in EVERY_METRIC.items():
        assert keys <= set(report[stage]), stage
    assert report["model"] == "fake/decider"
    assert report["classify"]["n"] == 2
    assert report["classify"]["errors"] == 0
    assert report["classify"]["accuracy"]["project_type"] == 0.5
    assert report["classify"]["accuracy"]["intent"] == 1.0
    # Priority is only asked with a note (High); the repo falls back to Medium.
    assert report["classify"]["accuracy"]["priority"] == 1.0
    assert report["classify"]["tags"]["precision"] == 1.0
    assert report["classify"]["tags"]["recall"] == pytest.approx(2 / 3)


def test_a_cached_run_reports_the_original_cost_and_latency(tmp_path):
    first = run_eval(FakeClient, tmp_path)
    second = run_eval(OfflineClient, tmp_path)

    assert first["classify"]["cost_per_1000"] > 0
    assert second == first
