"""Run Triage on one URL without Telegram, a database or Notion writes.

    uv run python -m backlog_tamer.dev.run_triage URL [--note TEXT] \\
        [--revise FEEDBACK]...

Reads the source, classifies it, then applies each Revision in order,
printing every Draft as JSON with the seconds each step took. The tag
vocabulary comes from Notion when it is configured, otherwise it is empty.
"""

from __future__ import annotations

import argparse
import asyncio
from time import perf_counter

from backlog_tamer.config import get_settings
from backlog_tamer.domain import IncomingContext, ProjectDraft, SourceLink
from backlog_tamer.integrations.notion import NotionWriter
from backlog_tamer.integrations.source_reader import SourceReader
from backlog_tamer.triage import Triage


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Triage on one URL.")
    parser.add_argument("url", help="The public URL to triage.")
    parser.add_argument("--note", default=None, help="The user's note, if any.")
    parser.add_argument(
        "--revise",
        dest="revisions",
        action="append",
        default=[],
        metavar="FEEDBACK",
        help="Revision feedback, applied in order. Repeat for several.",
    )
    return parser.parse_args()


async def _vocabulary(settings) -> list[str]:
    if settings.notion_token is None:
        return []
    return await NotionWriter.from_settings(settings).list_tag_options()


def _print(step: str, started: float, draft: ProjectDraft | None = None) -> None:
    print(f"\n# {step} ({perf_counter() - started:.2f}s)")
    if draft is not None:
        print(draft.model_dump_json(indent=2))


async def main() -> None:
    args = parse_args()
    settings = get_settings()
    triage = Triage.from_settings(settings)
    reader = SourceReader.from_api_key(settings.browserbase_api_key.get_secret_value())
    context = IncomingContext(
        raw_text=args.url,
        note=args.note,
        links=[SourceLink(url=args.url)],
    )

    vocabulary = await _vocabulary(settings)

    started = perf_counter()
    fetched = await reader.read(args.url)
    grounding = fetched.to_grounding()
    _print(f"read: {grounding.fetch_status}", started)

    started = perf_counter()
    draft = await triage.classify(context, grounding, vocabulary)
    _print("classify", started, draft)

    for feedback in args.revisions:
        started = perf_counter()
        draft = await triage.revise(context, grounding, draft, feedback, vocabulary)
        _print(f"revise: {feedback}", started, draft)


if __name__ == "__main__":
    asyncio.run(main())
