"""The golden set: approved Notion Projects, each with its source's Grounding.

    python -m evals.golden [--refresh]

Exports the Projects database and keeps the rows that have a Source and full
labels (Project type, Intent, Priority). Each Source is read once by the
app's SourceReader so every eval run sees identical input; an item that
already has a Grounding keeps it unless `--refresh` is given. The Tags
options go to `vocabulary.json`, in Notion's order: that is the vocabulary
production offers Triage, so the eval offers the same one.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from backlog_tamer.config import get_settings
from backlog_tamer.domain import Grounding, ProjectDraft
from backlog_tamer.integrations.notion.writer import (
    PROJECT_INTENT_PROPERTY,
    PROJECT_NAME_PROPERTY,
    PROJECT_PRIORITY_PROPERTY,
    PROJECT_SOURCE_PROPERTY,
    PROJECT_TAGS_PROPERTY,
    PROJECT_TYPE_PROPERTY,
    NotionWriter,
)
from backlog_tamer.integrations.source_reader import SourceReader
from backlog_tamer.triage.questions import LEGACY_TAGS

DATA = Path(__file__).parent / "data"
GOLDEN_PATH = DATA / "golden.jsonl"
VOCABULARY_PATH = DATA / "vocabulary.json"
FETCH_CONCURRENCY = 4


class Gold(BaseModel):
    """The labels the user approved in Notion."""

    project_name: str
    project_type: str
    intent: str
    priority: str
    topics: list[str]


class GoldenItem(BaseModel):
    id: str
    url: str
    note: str | None = None
    gold: Gold
    grounding: Grounding

    def gold_draft(self) -> ProjectDraft:
        """The approved labels as a Draft, the starting point of a Revision case."""
        return ProjectDraft(
            project_name=self.gold.project_name,
            summary=self.grounding.description or self.gold.project_name,
            project_type=self.gold.project_type,
            intent=self.gold.intent,
            priority=self.gold.priority,
            source_url=self.url,
            topics=self.gold.topics,
        )


def load_golden(path: Path = GOLDEN_PATH) -> list[GoldenItem]:
    lines = path.read_text().splitlines()
    return [GoldenItem.model_validate_json(line) for line in lines if line.strip()]


def write_golden(items: list[GoldenItem], path: Path = GOLDEN_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(item.model_dump_json() + "\n" for item in items))


def labeled_row(page: dict[str, Any]) -> dict[str, Any] | None:
    """A Project page's Source and labels, or None when any is missing."""
    props = page.get("properties") or {}
    row = {
        "url": _plain(props.get(PROJECT_SOURCE_PROPERTY)),
        "project_name": _plain(props.get(PROJECT_NAME_PROPERTY)),
        "project_type": _plain(props.get(PROJECT_TYPE_PROPERTY)),
        "intent": _plain(props.get(PROJECT_INTENT_PROPERTY)),
        "priority": _plain(props.get(PROJECT_PRIORITY_PROPERTY)),
    }
    if not all(row.values()):
        return None
    tags = _plain(props.get(PROJECT_TAGS_PROPERTY)) or []
    row["topics"] = [t for t in tags if t not in LEGACY_TAGS]
    return row


def _plain(prop: dict[str, Any] | None) -> Any:
    if not prop:
        return None
    kind = prop.get("type")
    value = prop.get(kind)
    if kind in ("title", "rich_text"):
        return "".join(part.get("plain_text", "") for part in value or []) or None
    if kind in ("select", "status"):
        return value.get("name") if value else None
    if kind == "multi_select":
        return [option["name"] for option in value or []]
    if kind == "url":
        return value
    return None


def write_vocabulary(tags: list[str], path: Path = VOCABULARY_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(tags, ensure_ascii=False, indent=1) + "\n")


def load_vocabulary(path: Path = VOCABULARY_PATH) -> list[str]:
    """The Notion Tags options, in Notion's order, as production offers them."""
    if not path.exists():
        raise SystemExit(
            f"{path} is missing. Run `mise run eval:golden` to export the "
            "Notion Tags options with the golden set."
        )
    return json.loads(path.read_text())


async def build(refresh: bool) -> tuple[list[GoldenItem], list[str]]:
    settings = get_settings()
    writer = NotionWriter.from_settings(settings)
    reader = SourceReader.from_api_key(settings.browserbase_api_key.get_secret_value())
    known = {item.id: item for item in load_golden()} if GOLDEN_PATH.exists() else {}
    pages = await writer.list_projects()
    # Production offers every Tags option; the golden tags alone are fewer
    # and would make tagging look easier than it is.
    vocabulary = await writer.list_tag_options()
    if not vocabulary:
        raise SystemExit("Could not read the Tags options of the Projects database.")
    gate = asyncio.Semaphore(FETCH_CONCURRENCY)

    async def one(page: dict[str, Any]) -> GoldenItem | None:
        row = labeled_row(page)
        if row is None:
            return None
        url = row.pop("url")
        previous = known.get(page["id"])
        if previous and not refresh:
            grounding = previous.grounding
        else:
            async with gate:
                grounding = (await reader.read(url)).to_grounding()
            print(grounding.fetch_status, url)
        return GoldenItem(
            id=page["id"],
            url=url,
            note=previous.note if previous else None,
            gold=Gold(**row),
            grounding=grounding,
        )

    items = [item for item in await asyncio.gather(*map(one, pages)) if item]
    return items, vocabulary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="refetch sources that already have a Grounding",
    )
    args = parser.parse_args()
    items, vocabulary = asyncio.run(build(args.refresh))
    write_golden(items)
    write_vocabulary(vocabulary)
    fetched = sum(item.grounding.fetch_status == "success" for item in items)
    print(
        json.dumps(
            {
                "items": len(items),
                "fetched": fetched,
                "tags": len(vocabulary),
                "path": str(GOLDEN_PATH),
                "vocabulary": str(VOCABULARY_PATH),
            }
        )
    )


if __name__ == "__main__":
    main()
