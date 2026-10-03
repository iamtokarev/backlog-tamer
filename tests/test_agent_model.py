from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from google.adk.models.llm_request import LlmRequest
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import SecretStr

from backlog_tamer.agents.intake_triage.schemas import ProjectDraft
from backlog_tamer.config import AgentConfig, get_settings

DRAFT = {
    "project_name": "Example: learn tool calling",
    "summary": "Explore a small tool-calling workflow.",
    "project_type": "tool",
    "intent": "learn",
    "priority": "Medium",
    "tasks": ["Explore Example"],
}


@pytest.fixture
def model_factory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for key in ("AGENT__MODEL", "AGENT__REASONING_EFFORT", "AGENT__OPENAI_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-openrouter-key")
    monkeypatch.setenv("TELEGRAM__BOT_TOKEN", "test-bot-token")
    monkeypatch.setenv("TELEGRAM__ALLOWED_USER_ID", "42")
    monkeypatch.setenv("NOTION_PROJECTS_DATABASE_ID", "test-projects-db")
    monkeypatch.setenv("NOTION_TASKS_DATABASE_ID", "test-tasks-db")
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    get_settings.cache_clear()
    from backlog_tamer.agents.intake_triage.agent import _get_model

    yield _get_model
    get_settings.cache_clear()


@pytest.fixture
def model_http(monkeypatch):
    import litellm
    from litellm.caching.llm_caching_handler import LLMClientCache
    from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler

    # Cached clients must not retain a previous test's mocked transport.
    monkeypatch.setattr(litellm, "in_memory_llm_clients_cache", LLMClientCache())
    requests = []
    responses = []

    def handle(request):
        requests.append(request)
        status, body = responses.pop(0)
        return httpx.Response(status, json=body)

    monkeypatch.setattr(
        AsyncHTTPHandler,
        "create_client",
        lambda *args, **kwargs: httpx.AsyncClient(
            transport=httpx.MockTransport(handle)
        ),
    )
    return requests, responses


def _completion(message, finish_reason="stop"):
    return {
        "id": "test-completion",
        "object": "chat.completion",
        "created": 0,
        "model": "openai/gpt-5.6-luna",
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def _request():
    return LlmRequest(
        contents=[
            types.Content(role="user", parts=[types.Part(text="Explore Example")])
        ],
        config=types.GenerateContentConfig(
            response_schema=ProjectDraft,
            tools=[
                types.Tool(
                    function_declarations=[
                        types.FunctionDeclaration(
                            name="fetch_url",
                            parameters_json_schema={
                                "type": "object",
                                "properties": {"url": {"type": "string"}},
                                "required": ["url"],
                            },
                        )
                    ]
                )
            ],
        ),
    )


def _generate(model, request):
    async def run():
        return [response async for response in model.generate_content_async(request)]

    return asyncio.run(run())


def test_model_routes_tools_and_structured_draft_through_openrouter(
    model_factory, model_http
):
    requests, responses = model_http
    responses.append(
        (200, _completion({"role": "assistant", "content": json.dumps(DRAFT)}))
    )
    model = model_factory(AgentConfig(), SecretStr("test-openrouter-key"))

    result = _generate(model, _request())

    request = requests[0]
    body = json.loads(request.content)
    assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer test-openrouter-key"
    assert body["model"] == "openai/gpt-5.6-luna"
    assert body["reasoning"] == {"effort": "medium"}
    assert body["provider"] == {"require_parameters": True}
    assert body["tools"][0]["function"]["name"] == "fetch_url"
    assert body["response_format"]["type"] == "json_schema"
    schema = body["response_format"]["json_schema"]
    assert schema["name"] == "ProjectDraft"
    assert schema["strict"] is True
    draft = ProjectDraft.model_validate_json(result[0].content.parts[0].text)
    assert draft.project_name == "Example: learn tool calling"


def test_model_keeps_tool_call_ids_and_sends_tool_results(model_factory, model_http):
    requests, responses = model_http
    responses.extend(
        [
            (
                200,
                _completion(
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call-fetch",
                                "type": "function",
                                "function": {
                                    "name": "fetch_url",
                                    "arguments": '{"url":"https://example.com/"}',
                                },
                            }
                        ],
                    },
                    "tool_calls",
                ),
            ),
            (200, _completion({"role": "assistant", "content": json.dumps(DRAFT)})),
        ]
    )
    model = model_factory(AgentConfig(), SecretStr("test-openrouter-key"))
    request = _request()

    tool_response = _generate(model, request)[0]
    call = tool_response.content.parts[0].function_call
    assert call.id == "call-fetch"
    assert call.name == "fetch_url"
    assert call.args == {"url": "https://example.com/"}
    request.contents.extend(
        [
            tool_response.content,
            types.Content(
                role="user",
                parts=[
                    types.Part(
                        function_response=types.FunctionResponse(
                            id=call.id,
                            name=call.name,
                            response={"status": "success", "title": "Example"},
                        )
                    )
                ],
            ),
        ]
    )
    result = _generate(model, request)

    messages = json.loads(requests[1].content)["messages"]
    tool_result = next(message for message in messages if message["role"] == "tool")
    assert tool_result["tool_call_id"] == "call-fetch"
    assert json.loads(tool_result["content"])["title"] == "Example"
    assert ProjectDraft.model_validate_json(result[0].content.parts[0].text).tasks == [
        "Explore Example"
    ]


def test_model_accepts_another_publisher_and_reasoning_effort(
    model_factory, model_http
):
    requests, responses = model_http
    responses.append(
        (200, _completion({"role": "assistant", "content": json.dumps(DRAFT)}))
    )
    model = model_factory(
        AgentConfig(model="anthropic/claude-sonnet-5", reasoning_effort="low"),
        SecretStr("test-openrouter-key"),
    )

    _generate(model, _request())

    body = json.loads(requests[0].content)
    assert body["model"] == "anthropic/claude-sonnet-5"
    assert body["reasoning"] == {"effort": "low"}
    assert str(requests[0].url) == "https://openrouter.ai/api/v1/chat/completions"


def test_model_authentication_error_propagates_without_fallback(
    model_factory, model_http
):
    from litellm.exceptions import AuthenticationError

    requests, responses = model_http
    responses.append((401, {"error": {"message": "Invalid credentials", "code": 401}}))
    model = model_factory(AgentConfig(), SecretStr("test-openrouter-key"))

    with pytest.raises(AuthenticationError):
        _generate(model, _request())

    assert len(requests) == 1
    assert requests[0].url.host == "openrouter.ai"


def test_workflow_persists_draft_and_returns_to_review_after_revision(
    model_factory, model_http
):
    from backlog_tamer.agents.intake_triage.agent import root_agent
    from backlog_tamer.agents.intake_triage.schemas import IncomingContext
    from backlog_tamer.agents.intake_triage.workflow import (
        build_triage_message,
        build_triage_state_delta,
    )
    from backlog_tamer.dev.run_intake_workflow import (
        build_review_response,
        extract_request_input_handles,
    )

    requests, responses = model_http
    revised = {**DRAFT, "priority": "High"}
    responses.extend(
        (200, _completion({"role": "assistant", "content": json.dumps(draft)}))
        for draft in (DRAFT, revised)
    )

    async def run():
        sessions = InMemorySessionService()
        runner = Runner(agent=root_agent, app_name="test", session_service=sessions)
        await sessions.create_session(
            app_name="test", user_id="user", session_id="test"
        )
        context = IncomingContext(raw_text="Explore Example")
        events = [
            event
            async for event in runner.run_async(
                user_id="user",
                session_id="test",
                new_message=build_triage_message(context),
                state_delta=build_triage_state_delta(context),
            )
        ]
        handles = extract_request_input_handles(events)
        session = await sessions.get_session(
            app_name="test", user_id="user", session_id="test"
        )
        assert (
            ProjectDraft.model_validate(session.state["draft_proposal"]).priority
            == "Medium"
        )
        revised_events = [
            event
            async for event in runner.run_async(
                user_id="user",
                session_id="test",
                invocation_id=handles["invocation_id"],
                new_message=build_review_response(
                    handles["request_input_call_id"], "Make it high priority"
                ),
            )
        ]
        assert extract_request_input_handles(revised_events)["request_input_call_id"]
        session = await sessions.get_session(
            app_name="test", user_id="user", session_id="test"
        )
        assert (
            ProjectDraft.model_validate(session.state["draft_proposal"]).priority
            == "High"
        )

    asyncio.run(run())
    assert len(requests) == 2
