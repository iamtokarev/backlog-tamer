"""Adapt source reading to ADK's tool and saved Grounding interface."""

from collections.abc import Callable

from google.adk.tools import ToolContext
from pydantic import ValidationError

from backlog_tamer.integrations.source_reader import FetchedUrl, SourceReader
from backlog_tamer.source_urls import normalize_source_url

from ..workflow import FETCHED_CONTEXT_STATE_KEY


def make_fetch_url_tool(reader: SourceReader) -> Callable:
    async def fetch_url(url: str, tool_context: ToolContext) -> dict:
        """Read a public HTTP(S) source into compact metadata and key points."""
        try:
            key = normalize_source_url(url)
        except ValueError:
            key = url
        existing = tool_context.state.get(FETCHED_CONTEXT_STATE_KEY, {})
        if not isinstance(existing, dict):
            existing = {}
        cached = existing.get(key)
        if isinstance(cached, dict) and cached.get("status") == "success":
            try:
                return FetchedUrl.model_validate(cached).model_dump(mode="json")
            except ValidationError:
                pass

        result = await reader.read(url)
        payload = result.model_dump(mode="json")
        # Read state again after awaiting, so simultaneous reads can both persist.
        current = tool_context.state.get(FETCHED_CONTEXT_STATE_KEY, {})
        updated = dict(current) if isinstance(current, dict) else {}
        updated[key] = payload
        tool_context.state[FETCHED_CONTEXT_STATE_KEY] = updated
        return payload

    return fetch_url
