from __future__ import annotations

import argparse
import asyncio

from google.adk.sessions import InMemorySessionService

from backlog_tamer.agents.intake_triage.schemas import IncomingContext, SourceLink
from backlog_tamer.agents.intake_triage.triage_workflow import (
    Approved,
    NeedsReview,
    Rejected,
    TriageWorkflow,
    TurnOutcome,
)
from backlog_tamer.config import get_settings

USER_ID = "dev_user"


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
    review = parser.add_mutually_exclusive_group()
    review.add_argument(
        "--approve",
        action="store_true",
        help="Approve the first draft instead of prompting.",
    )
    review.add_argument(
        "--reject",
        action="store_true",
        help="Reject the first draft instead of prompting.",
    )
    review.add_argument(
        "--revise",
        default=None,
        metavar="FEEDBACK",
        help="Send one Revision with this feedback instead of prompting.",
    )
    parser.add_argument(
        "--user-id",
        default=USER_ID,
        help="Runner user id for the local session.",
    )
    return parser.parse_args()


def build_context(args: argparse.Namespace) -> IncomingContext:
    return IncomingContext(
        raw_text=args.raw_text,
        note=args.note,
        links=[SourceLink(url=url) for url in args.links],
    )


def render_outcome(outcome: TurnOutcome) -> None:
    match outcome:
        case NeedsReview():
            print(f"\n{outcome.review_message}")
            print(f"\nGrounding: {outcome.grounding.model_dump_json()}")
        case Approved():
            print("\nApproved.")
        case Rejected():
            print(f"\nRejected: {outcome.draft.project_name}")


async def decide(
    workflow: TriageWorkflow,
    outcome: NeedsReview,
    args: argparse.Namespace,
) -> TurnOutcome | None:
    if args.approve:
        return await workflow.approve(outcome.checkpoint)
    if args.reject:
        return await workflow.reject(outcome.checkpoint)
    if args.revise is not None:
        return await workflow.revise(outcome.checkpoint, args.revise, {})

    choice = input("\n[a]pprove, [r]eject, or type revision feedback: ").strip()
    if not choice:
        return None
    if choice.lower() in {"a", "approve"}:
        return await workflow.approve(outcome.checkpoint)
    if choice.lower() in {"r", "reject"}:
        return await workflow.reject(outcome.checkpoint)
    return await workflow.revise(outcome.checkpoint, choice, {})


async def main() -> None:
    args = parse_args()
    workflow = TriageWorkflow.from_settings(
        get_settings(),
        session_service=InMemorySessionService(),
    )
    outcome = await workflow.start(build_context(args), args.user_id, [])
    render_outcome(outcome)

    if not isinstance(outcome, NeedsReview):
        return

    next_outcome = await decide(workflow, outcome, args)
    if next_outcome is None:
        print("\nNo review decision provided. Leaving the workflow paused.")
        return
    render_outcome(next_outcome)


if __name__ == "__main__":
    asyncio.run(main())
