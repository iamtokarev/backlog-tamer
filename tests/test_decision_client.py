"""DecisionClient against a stubbed OpenRouter: chunking, titles, usage."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from backlog_tamer.triage import DecisionClient
from backlog_tamer.triage import client as client_module
from backlog_tamer.triage.client import Usage


@pytest.fixture
def requests(monkeypatch) -> list[dict]:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append({"path": request.url.path, "body": body})
        if request.url.path.endswith("/decisions"):
            answers = {
                name: {"type": "noul", "noul": 1.0} for name in body["questions"]
            }
            return httpx.Response(
                200,
                json={"answers": answers, "usage": {"input_tokens": 10, "cost": 0.01}},
            )
        content = json.dumps({"project_name": "  LangGraph: stateful agents "})
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 7},
            },
        )

    real = httpx.AsyncClient
    monkeypatch.setattr(
        client_module.httpx,
        "AsyncClient",
        lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs),
    )
    return seen


def test_decide_sends_at_most_128_questions_per_request(requests):
    usages: list[Usage] = []
    client = DecisionClient(
        "key", decision_model="decider", title_model="titler", on_usage=usages.append
    )
    questions = {f"q_{i}": {"type": "noul"} for i in range(200)}

    answers = asyncio.run(client.decide({"url": "x"}, questions))

    assert sorted(len(r["body"]["questions"]) for r in requests) == [72, 128]
    assert set(answers) == set(questions)
    assert [(u.model, u.input_tokens, u.cost) for u in usages] == [
        ("decider", 10, 0.01),
        ("decider", 10, 0.01),
    ]


def test_title_returns_the_project_name_only(requests):
    usages: list[Usage] = []
    client = DecisionClient(
        "key", decision_model="decider", title_model="titler", on_usage=usages.append
    )

    title = asyncio.run(client.title({"url": "x"}, feedback="shorter", current="Old"))

    assert title == "LangGraph: stateful agents"
    schema = requests[0]["body"]["response_format"]["json_schema"]["schema"]
    assert schema["required"] == ["project_name"]
    assert "reviewer_feedback: shorter" in requests[0]["body"]["messages"][1]["content"]
    assert (usages[0].path, usages[0].model, usages[0].input_tokens) == (
        "/v1/chat/completions",
        "titler",
        7,
    )


def test_usage_is_logged_when_no_hook_is_given(requests, caplog):
    client = DecisionClient("key", decision_model="decider", title_model="titler")

    with caplog.at_level("INFO", logger=client_module.__name__):
        asyncio.run(client.decide({}, {"q": {"type": "noul"}}))

    assert "decision_triage_call path=/alpha/decisions model=decider" in caplog.text
