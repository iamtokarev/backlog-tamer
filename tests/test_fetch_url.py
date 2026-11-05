"""The ADK tool reuses and persists Grounding per Intake."""

import asyncio
from types import SimpleNamespace

from backlog_tamer.agents.intake_triage.tools.fetch_url import make_fetch_url_tool
from backlog_tamer.agents.intake_triage.workflow import FETCHED_CONTEXT_STATE_KEY
from backlog_tamer.integrations.source_reader import FetchedUrl

URL = "https://example.com/source"


class Reader:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    async def read(self, url):
        self.calls.append(url)
        return self.results.pop(0)


def success():
    return FetchedUrl(status="success", requested_url=URL, title="Source")


def test_success_is_reused_only_inside_the_same_intake():
    reader = Reader(success(), success())
    tool = make_fetch_url_tool(reader)
    context = SimpleNamespace(state={})
    first = asyncio.run(tool(URL + "?utm_source=email", context))
    assert asyncio.run(tool(URL, context)) == first
    assert context.state[FETCHED_CONTEXT_STATE_KEY][URL] == first
    asyncio.run(tool(URL, SimpleNamespace(state={})))
    assert len(reader.calls) == 2
    assert tool.__name__ == "fetch_url"


def test_failures_are_saved_but_retried():
    reader = Reader(
        FetchedUrl(status="error", requested_url=URL, error="Unavailable"), success()
    )
    tool = make_fetch_url_tool(reader)
    context = SimpleNamespace(state={})
    assert asyncio.run(tool(URL, context))["status"] == "error"
    assert asyncio.run(tool(URL, context))["status"] == "success"
    assert len(reader.calls) == 2


def test_old_checkpoint_grounding_remains_readable_without_provider_call():
    reader = Reader()
    old = {"status": "success", "requested_url": URL, "title": "Saved source"}
    context = SimpleNamespace(state={FETCHED_CONTEXT_STATE_KEY: {URL: old}})
    result = asyncio.run(make_fetch_url_tool(reader)(URL, context))
    assert result["title"] == "Saved source"
    assert result["key_points"] == []
    assert reader.calls == []


def test_parallel_reads_preserve_both_grounding_entries():
    reader = Reader(success(), success())
    tool = make_fetch_url_tool(reader)
    context = SimpleNamespace(state={})

    async def run():
        await asyncio.gather(tool(URL, context), tool(URL + "/other", context))

    asyncio.run(run())
    assert set(context.state[FETCHED_CONTEXT_STATE_KEY]) == {URL, URL + "/other"}
