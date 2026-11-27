"""DecisionClient against a stubbed OpenRouter: chunking, titles, usage, retries."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

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


def _stub(monkeypatch, responses) -> tuple[list[float], list[int]]:
    """Serve `responses` in order (a Response, or an exception to raise)."""
    sleeps: list[float] = []
    sent: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(1)
        outcome = responses[min(len(sent), len(responses)) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    real = httpx.AsyncClient
    monkeypatch.setattr(
        client_module.httpx,
        "AsyncClient",
        lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs),
    )
    monkeypatch.setattr(client_module.asyncio, "sleep", fake_sleep)
    return sleeps, sent


def _decide() -> dict:
    client = DecisionClient(
        "key", decision_model="d", title_model="t", on_usage=lambda _: None
    )
    return asyncio.run(client.decide({}, {"q": {"type": "noul"}}))


OK = httpx.Response(200, json={"answers": {"q": {"type": "noul", "noul": 1.0}}})


@pytest.mark.parametrize(
    ("header", "expected"),
    [("3", 3.0), ("1.5", 1.5), ("soon", 1.0), ("inf", 1.0), ("-4", 0.0)],
)
def test_retry_after_in_seconds_is_honoured_and_junk_falls_back(
    monkeypatch, header, expected
):
    sleeps, _ = _stub(
        monkeypatch, [httpx.Response(429, headers={"Retry-After": header}), OK]
    )

    assert _decide()["q"]["noul"] == 1.0
    assert sleeps == [expected]


def test_retry_after_as_an_http_date_waits_until_then(monkeypatch):
    when = format_datetime(datetime.now(UTC) + timedelta(seconds=30), usegmt=True)
    sleeps, _ = _stub(
        monkeypatch, [httpx.Response(503, headers={"Retry-After": when}), OK]
    )

    _decide()

    assert 20 < sleeps[0] <= 30


def test_timeouts_and_transport_errors_are_retried(monkeypatch):
    sleeps, sent = _stub(
        monkeypatch, [httpx.ConnectTimeout("slow"), httpx.ConnectError("reset"), OK]
    )

    assert _decide()["q"]["noul"] == 1.0
    assert (len(sent), sleeps) == (3, [1, 2])


def test_the_last_transport_error_surfaces(monkeypatch):
    _, sent = _stub(monkeypatch, [httpx.ReadTimeout("still slow")])

    with pytest.raises(httpx.ReadTimeout, match="still slow"):
        _decide()
    assert len(sent) == client_module.MAX_ATTEMPTS


def test_the_last_retry_status_surfaces(monkeypatch):
    _, sent = _stub(monkeypatch, [httpx.Response(503)])

    with pytest.raises(httpx.HTTPStatusError, match="503"):
        _decide()
    assert len(sent) == client_module.MAX_ATTEMPTS
