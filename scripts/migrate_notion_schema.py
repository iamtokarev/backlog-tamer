#!/usr/bin/env python
"""Add the columns the writer has been silently dropping, then backfill them.

`NotionWriter._fit_to_schema` removes properties the database does not have,
so `Project type`, `Intent`, `Source` and `Captured` were discarded on every
commit -- and `find_project_by_source` could never match, which left duplicate
detection permanently off.

The script only ever adds columns and fills them where they are empty. It
never edits `Tags`, `Summary`, `Status`, `Priority`, names, page bodies or
relations. Dry-run is the default; `--apply` writes, and always snapshots
every page to JSON first.

    uv run python scripts/migrate_notion_schema.py
    uv run python scripts/migrate_notion_schema.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx

from backlog_tamer.agents.intake_triage.tools.fetch_url import strip_tracking_params
from backlog_tamer.config import get_settings
from backlog_tamer.integrations.notion.writer import (
    PROJECT_CAPTURED_PROPERTY,
    PROJECT_INTENT_PROPERTY,
    PROJECT_SOURCE_PROPERTY,
    PROJECT_SUMMARY_PROPERTY,
    PROJECT_TAGS_PROPERTY,
    PROJECT_TYPE_PROPERTY,
    TASK_SOURCE_PROPERTY,
)

NOTION_API = "https://api.notion.com/v1"

PROJECT_TYPE_OPTIONS = [
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
INTENT_OPTIONS = ["learn", "build", "research", "explore", "reference", "unclear"]

# Values that used to live in Tags before Type and Intent were their own
# columns. They are the only record of how 34 older projects were classified.
LEGACY_TAG_TO_PROJECT_TYPE = {
    "paper": "paper",
    "article": "article",
    "video": "video",
    "course": "course",
    "repository": "repository",
    "documentation": "tool",
    "tool": "tool",
    "product": "product",
    "model": "model",
    "company": "company",
    "project": "product",
    "idea": "product",
}
LEGACY_TAG_TO_INTENT = {
    "learn": "learn",
    "build": "build",
    "research": "research",
    "explore": "explore",
    "exploration": "explore",
    "reference": "reference",
}

SOURCE_IN_SUMMARY_RE = re.compile(r"https?://\S+")


def _headers(settings) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.notion_token.get_secret_value()}",
        "Notion-Version": settings.notion_api_version,
        "Content-Type": "application/json",
    }


def _raise_for_notion(response: httpx.Response) -> None:
    if response.status_code < 400:
        return
    body = response.json() if response.content else {}
    message = body.get("message") if isinstance(body, dict) else None
    raise RuntimeError(f"Notion rejected the call ({response.status_code}): {message}")


def _canonical(url: str) -> str:
    """Drop campaign parameters so a re-capture can match this row."""
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return url.strip()
    return urlunparse(
        parsed._replace(query=strip_tracking_params(parsed.query), fragment="")
    )


async def _all_pages(
    client: httpx.AsyncClient, database_id: str
) -> list[dict[str, Any]]:
    pages: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        body: dict[str, Any] = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        response = await client.post(
            f"{NOTION_API}/databases/{database_id}/query", json=body
        )
        _raise_for_notion(response)
        payload = response.json()
        pages.extend(payload.get("results", []))
        cursor = payload.get("next_cursor")
        if not cursor:
            return pages


async def _bookmark_url(client: httpx.AsyncClient, page_id: str) -> str | None:
    """The source link the writer already puts at the top of every page body."""
    response = await client.get(
        f"{NOTION_API}/blocks/{page_id}/children", params={"page_size": 10}
    )
    if response.status_code >= 400:
        return None
    for block in response.json().get("results", []):
        if block.get("type") == "bookmark":
            url = (block.get("bookmark") or {}).get("url")
            if url:
                return str(url)
    return None


def _plain_text(prop: dict[str, Any] | None, key: str) -> str:
    if not prop:
        return ""
    return "".join(part.get("plain_text", "") for part in prop.get(key, []))


def _is_empty(prop: dict[str, Any] | None) -> bool:
    """Only fill a column that has nothing in it."""
    if not prop:
        return True
    kind = prop.get("type")
    value = prop.get(kind)
    return value in (None, [], "")


async def ensure_columns(
    client: httpx.AsyncClient,
    projects_db: str,
    tasks_db: str,
    apply: bool,
) -> None:
    plans = [
        (
            projects_db,
            {
                PROJECT_TYPE_PROPERTY: {
                    "select": {"options": [{"name": n} for n in PROJECT_TYPE_OPTIONS]}
                },
                PROJECT_INTENT_PROPERTY: {
                    "select": {"options": [{"name": n} for n in INTENT_OPTIONS]}
                },
                PROJECT_SOURCE_PROPERTY: {"url": {}},
                PROJECT_CAPTURED_PROPERTY: {"date": {}},
            },
        ),
        (tasks_db, {TASK_SOURCE_PROPERTY: {"url": {}}}),
    ]

    for database_id, wanted in plans:
        response = await client.get(f"{NOTION_API}/databases/{database_id}")
        _raise_for_notion(response)
        existing = set(response.json().get("properties", {}))
        missing = {name: spec for name, spec in wanted.items() if name not in existing}
        if not missing:
            print(f"  {database_id}: all columns already present")
            continue

        print(f"  {database_id}: adding {', '.join(sorted(missing))}")
        if not apply:
            continue
        patch = await client.patch(
            f"{NOTION_API}/databases/{database_id}", json={"properties": missing}
        )
        _raise_for_notion(patch)


async def backfill_projects(
    client: httpx.AsyncClient,
    projects_db: str,
    apply: bool,
    snapshot_path: Path,
) -> None:
    pages = await _all_pages(client, projects_db)
    print(f"  {len(pages)} projects found")

    snapshot_path.write_text(json.dumps(pages, indent=2, ensure_ascii=False))
    print(f"  snapshot written to {snapshot_path}")

    filled = {"Source": 0, "Captured": 0, "Project type": 0, "Intent": 0}
    for page in pages:
        properties = page.get("properties", {})
        updates: dict[str, Any] = {}

        if _is_empty(properties.get(PROJECT_SOURCE_PROPERTY)):
            url = await _bookmark_url(client, page["id"])
            if not url:
                summary = _plain_text(
                    properties.get(PROJECT_SUMMARY_PROPERTY), "rich_text"
                )
                match = SOURCE_IN_SUMMARY_RE.search(summary)
                url = match.group(0).rstrip(".,)") if match else None
            if url:
                updates[PROJECT_SOURCE_PROPERTY] = {"url": _canonical(url)}

        if _is_empty(properties.get(PROJECT_CAPTURED_PROPERTY)):
            created = page.get("created_time")
            if created:
                updates[PROJECT_CAPTURED_PROPERTY] = {"date": {"start": created[:10]}}

        tags = {
            option["name"].casefold()
            for option in (properties.get(PROJECT_TAGS_PROPERTY) or {}).get(
                "multi_select", []
            )
        }
        if _is_empty(properties.get(PROJECT_TYPE_PROPERTY)):
            for tag in tags:
                if tag in LEGACY_TAG_TO_PROJECT_TYPE:
                    updates[PROJECT_TYPE_PROPERTY] = {
                        "select": {"name": LEGACY_TAG_TO_PROJECT_TYPE[tag]}
                    }
                    break
        if _is_empty(properties.get(PROJECT_INTENT_PROPERTY)):
            for tag in tags:
                if tag in LEGACY_TAG_TO_INTENT:
                    updates[PROJECT_INTENT_PROPERTY] = {
                        "select": {"name": LEGACY_TAG_TO_INTENT[tag]}
                    }
                    break

        if not updates:
            continue

        title = _plain_text(properties.get("Project name"), "title")[:48]
        print(f"    {title!r}: {json.dumps(updates, ensure_ascii=False)[:150]}")
        for name in updates:
            filled[name] = filled.get(name, 0) + 1

        if apply:
            patch = await client.patch(
                f"{NOTION_API}/pages/{page['id']}", json={"properties": updates}
            )
            _raise_for_notion(patch)

    print(f"  filled: {filled}")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the changes (default: print what would change)",
    )
    args = parser.parse_args()

    settings = get_settings()
    if settings.notion_token is None:
        print("NOTION_TOKEN is not configured.", file=sys.stderr)
        return 1

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    snapshot_path = Path(f"notion-projects-snapshot-{stamp}.json")

    mode = "APPLY" if args.apply else "DRY RUN (nothing is written)"
    print(f"=== Notion schema migration — {mode} ===")

    async with httpx.AsyncClient(headers=_headers(settings), timeout=30.0) as client:
        print("\n[1/2] Columns")
        await ensure_columns(
            client,
            settings.notion_projects_database_id,
            settings.notion_tasks_database_id,
            args.apply,
        )

        print("\n[2/2] Backfill")
        if not args.apply:
            print("  note: new columns read as empty until --apply adds them")
        await backfill_projects(
            client,
            settings.notion_projects_database_id,
            args.apply,
            snapshot_path,
        )

    if not args.apply:
        print("\nRe-run with --apply to write these changes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
