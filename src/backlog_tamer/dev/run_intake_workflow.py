from __future__ import annotations

import argparse
import asyncio
from typing import Any

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from backlog_tamer.agents.intake_triage.schemas import IncomingContext, SourceLink
from backlog_tamer.agents.intake_triage.workflow import build_triage_message
from backlog_tamer.config import get_settings

APP_NAME = "backlog_tamer"
USER_ID = "dev_user"
SESSION_ID = "dev_session"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the intake triage ADK workflow end to end.",
    )
    parser.add_argument(
        "raw_text",
        help="Primary learning item text sent to the workflow.",
    )
    parser.add_argument(
        "--note",
        default=None,
        help="Optional user note attached to the learning item.",
    )
    parser.add_argument(
        "--link",
        dest="links",
        action="append",
        default=[],
        help="Attach one or more public URLs. Repeat the flag for multiple links.",
    )
    parser.add_argument(
        "--review-reply",
        default=None,
        help="Optional review response to auto-resume the interrupt.",
    )
    parser.add_argument(
        "--user-id",
        default=USER_ID,
        help="Runner user id for the local session.",
    )
    parser.add_argument(
        "--session-id",
        default=SESSION_ID,
        help="Runner session id for the local session.",
    )
    return parser.parse_args()


def build_context(args: argparse.Namespace) -> IncomingContext:
    return IncomingContext(
        raw_text=args.raw_text,
        note=args.note,
        links=[SourceLink(url=url) for url in args.links],
    )


def render_event(event: Any) -> None:
    path = event.node_info.path if event.node_info else "unknown"
    print(f"\n[{path}]")

    if event.error_code or event.error_message:
        print(f"ERROR: {event.error_code} {event.error_message}")
        return

    if not event.content or not event.content.parts:
        print(event)
        return

    for part in event.content.parts:
        function_call = getattr(part, "function_call", None)
        if function_call is not None:
            print(f"Function call: {function_call.name}")
            print(f"Call id: {function_call.id}")
            print(f"Args: {function_call.args}")
            continue

        function_response = getattr(part, "function_response", None)
        if function_response is not None:
            print(f"Function response: {function_response.name}")
            print(f"Call id: {function_response.id}")
            print(f"Response: {function_response.response}")
            continue

        text = getattr(part, "text", None)
        if text:
            print(text)


def extract_request_input_handles(events: list[Any]) -> dict[str, str]:
    interrupt_event = next(
        event
        for event in events
        if event.content
        and event.content.parts
        and getattr(event.content.parts[0], "function_call", None)
        and event.content.parts[0].function_call.name == "adk_request_input"
    )

    function_call = interrupt_event.content.parts[0].function_call
    return {
        "invocation_id": interrupt_event.invocation_id,
        "request_input_call_id": function_call.id,
    }


async def run_turn(
    runner: Runner,
    *,
    user_id: str,
    session_id: str,
    message: types.Content,
    invocation_id: str | None = None,
) -> list[Any]:
    events: list[Any] = []
    async for event in runner.run_async(
        user_id=user_id,
        session_id=session_id,
        invocation_id=invocation_id,
        new_message=message,
    ):
        events.append(event)
        render_event(event)
    return events


def build_review_response(call_id: str, reply_text: str) -> types.Content:
    return types.Content(
        role="user",
        parts=[
            types.Part(
                function_response=types.FunctionResponse(
                    id=call_id,
                    name="adk_request_input",
                    response={"value": reply_text},
                )
            )
        ],
    )


async def main() -> None:
    args = parse_args()
    settings = get_settings()

    from langsmith.integrations.google_adk import configure_google_adk

    from backlog_tamer.agents.intake_triage.agent import root_agent

    if settings.langsmith_tracing:
        configure_google_adk()

    session_service = InMemorySessionService()
    runner = Runner(
        agent=root_agent,
        app_name=APP_NAME,
        session_service=session_service,
    )

    await session_service.create_session(
        app_name=APP_NAME,
        user_id=args.user_id,
        session_id=args.session_id,
    )

    context = build_context(args)
    first_message = build_triage_message(context)
    first_events = await run_turn(
        runner,
        user_id=args.user_id,
        session_id=args.session_id,
        message=first_message,
    )

    try:
        handles = extract_request_input_handles(first_events)
    except StopIteration:
        print("\nNo review interrupt was emitted.")
        return

    review_reply = args.review_reply
    if review_reply is None:
        review_reply = input("\nReview reply: ").strip()

    if not review_reply:
        print("\nNo review reply provided. Leaving the workflow paused.")
        return

    review_message = build_review_response(
        handles["request_input_call_id"],
        review_reply,
    )
    await run_turn(
        runner,
        user_id=args.user_id,
        session_id=args.session_id,
        invocation_id=handles["invocation_id"],
        message=review_message,
    )


if __name__ == "__main__":
    asyncio.run(main())
