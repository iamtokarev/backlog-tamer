"""Source reading across its interface, with scripted external responses."""

import asyncio
import base64
import json
import socket
from collections import deque
from io import BytesIO
from types import SimpleNamespace

import httpx
import pytest
from browserbase import APIStatusError
from pypdf import PdfWriter

from backlog_tamer import source_urls
from backlog_tamer.integrations import source_reader as module
from backlog_tamer.integrations.source_reader import SourceReader, _Rendered, _Response

URL = "https://example.test/source"
PAGE = {
    "content_status": "complete",
    "title": "Stateful agents",
    "description": "A practical guide.",
    "site_name": "Example",
    "author": "",
    "published_at": "",
    "key_points": ["Persist state across agent turns."],
    "content_preview": "A guide to stateful agents and human review.",
}


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    def resolve(hostname, *_args, **_kwargs):
        address = "127.0.0.1" if hostname == "private.test" else "1.1.1.1"
        return [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 0))
        ]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)


def raw(content="<html><head></head><body>Source</body></html>", **kwargs):
    return _Response(200, content, "text/html", **kwargs)


def page(**changes):
    return _Response(200, {**PAGE, **changes}, "application/json")


class Provider:
    def __init__(self, *responses, rendered=None):
        self.responses = deque(responses)
        self.rendered = rendered or _Rendered(PAGE, URL, None, 200)
        self.calls = []
        self.renders = []
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        self.closed = True

    async def fetch(self, url, *, format, schema=None):
        self.calls.append((url, format))
        response = self.responses.popleft()
        if isinstance(response, Exception):
            raise response
        return response

    async def render(self, url):
        self.renders.append(url)
        return self.rendered


def read(provider, url=URL):
    return asyncio.run(SourceReader(lambda: provider).read(url))


def test_html_uses_structured_fetch_and_deterministic_canonical():
    provider = Provider(
        raw('<link rel="canonical" href="/canonical?utm_source=email">'), page()
    )
    result = read(provider)
    assert result.status == "success"
    assert result.canonical_url == "https://example.test/canonical"
    assert result.key_points == PAGE["key_points"]
    assert [format for _, format in provider.calls] == ["raw", "json"]
    assert provider.renders == []
    assert provider.closed


@pytest.mark.parametrize(
    "changes",
    [
        {"content_status": "partial"},
        {"content_status": "unavailable"},
        {"title": ""},
        {"key_points": [], "content_preview": ""},
    ],
)
def test_insufficient_http_200_grounding_escalates_once(changes):
    provider = Provider(raw(), page(**changes))
    result = read(provider)
    assert result.status == "success"
    assert provider.renders == [URL]


def test_malformed_extraction_escalates():
    provider = Provider(raw(), _Response(200, {}, "application/json"))
    assert read(provider).status == "success"
    assert provider.renders == [URL]


@pytest.mark.parametrize(
    "url",
    [
        "https://www.kaggle.com/learn/intro-to-machine-learning",
        "https://webgpu.github.io/webgpu-samples/",
    ],
)
def test_known_dynamic_sources_skip_structured_http_extraction(url):
    provider = Provider(raw(), rendered=_Rendered(PAGE, url, None, 200))
    assert read(provider, url).status == "success"
    assert len(provider.calls) == 1
    assert provider.renders == [url]


@pytest.mark.parametrize("status", [403, 429, 500, 503])
def test_target_errors_may_recover_in_the_browser(status):
    provider = Provider(_Response(status, "", "text/html"))
    assert read(provider).status == "success"
    assert provider.renders == [URL]


@pytest.mark.parametrize("status", [404, 410])
def test_missing_sources_do_not_launch_browsers(status):
    provider = Provider(_Response(status, "", "text/html"))
    assert read(provider).status == "error"
    assert provider.renders == []


@pytest.mark.parametrize("status", [401, 402, 403, 429])
def test_provider_account_errors_fail_directly(status):
    response = httpx.Response(status, request=httpx.Request("POST", "https://api.test"))
    error = APIStatusError("Provider failure", response=response, body={})
    provider = Provider(raw(), error)
    result = read(provider)
    assert result.status == "error"
    assert str(status) in result.error
    assert provider.renders == []


def test_incomplete_browser_content_is_a_structured_failure():
    provider = Provider(
        raw(),
        page(content_status="partial"),
        rendered=_Rendered({**PAGE, "content_status": "partial"}, URL, None, 200),
    )
    assert read(provider).status == "error"
    assert provider.closed


def test_redirect_identity_is_resolved_before_extraction():
    target = "https://destination.test/article"
    provider = Provider(
        _Response(302, "", "text/html", headers={"location": target}),
        raw(),
        page(),
    )
    result = read(provider)
    assert result.requested_url == URL
    assert result.final_url == target
    assert provider.calls == [(URL, "raw"), (target, "raw"), (target, "json")]


def test_private_redirect_is_rejected_before_provider_request():
    provider = Provider(
        _Response(
            302, "", "text/html", headers={"location": "http://private.test/secret"}
        )
    )
    result = read(provider)
    assert result.status == "error"
    assert len(provider.calls) == 1


def test_redirects_have_a_hard_limit():
    provider = Provider(
        *[
            _Response(302, "", "text/html", headers={"location": "/next"})
            for _ in range(6)
        ]
    )
    assert "Too many" in read(provider).error
    assert len(provider.calls) == 6


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://localhost/",
        "http://127.0.0.1/",
        "http://user:password@example.test/",
        "http://private.test/",
    ],
)
def test_private_or_invalid_inputs_never_reach_provider(url):
    provider = Provider()
    assert read(provider, url).status == "error"
    assert provider.calls == []


@pytest.mark.parametrize("href", ["undefined", "null", "none", "http://127.0.0.1/"])
def test_invalid_canonical_metadata_is_ignored(href):
    provider = Provider(raw(f'<link rel="canonical" href="{href}">'), page())
    assert read(provider).canonical_url is None


def test_output_bounds_and_duplicate_points():
    provider = Provider(
        raw(),
        page(
            title="Title " * 100,
            description="Details " * 500,
            key_points=[
                "Same fact",
                "Same fact",
                *["x" * 400 + str(i) for i in range(8)],
            ],
            content_preview="text " * 500,
        ),
    )
    result = read(provider)
    assert len(result.title) <= 300
    assert len(result.content_preview) <= 1200
    assert len(result.key_points) <= 5
    assert max(map(len, result.key_points)) <= 300


def test_text_and_unsupported_content():
    text = read(Provider(_Response(200, "Source\nUseful information.", "text/plain")))
    assert text.page_kind == "text"
    assert text.content_preview == "Source\nUseful information."
    unsupported = read(Provider(_Response(200, "", "image/png")))
    assert unsupported.status == "error"


def test_pdf_larger_than_former_truncation_limit_and_page_bound():
    writer = PdfWriter()
    for _ in range(6):
        writer.add_blank_page(width=100, height=100)
    writer.add_metadata({"/Title": "Test PDF", "/Subject": "details " * 200_000})
    stream = BytesIO()
    writer.write(stream)
    body = stream.getvalue()
    assert len(body) > 1_500_000
    result = read(
        Provider(
            _Response(
                200,
                base64.b64encode(body).decode(),
                "application/pdf",
                encoding="base64",
            )
        )
    )
    assert result.status == "success"
    assert result.title == "Test PDF"
    assert len(result.description) <= 1200
    assert result.notes == ["Only the first 5 PDF pages were parsed."]


def test_oversize_and_malformed_binary_are_structured_failures():
    assert "5 MB" in read(Provider(raw("x" * 5_000_001))).error
    response = _Response(200, "invalid!", "application/pdf", encoding="base64")
    assert read(Provider(response)).status == "error"
    assert read(Provider(_Response(200, "broken", "application/pdf"))).status == "error"


def test_youtube_oembed_preserves_title_author_and_video_identity():
    url = "https://youtu.be/L1OBRtfPS34?si=tracking"
    provider = Provider(
        _Response(
            200,
            json.dumps({"title": "Learning agents", "author_name": "Channel"}),
            "application/json",
        )
    )
    result = read(provider, url)
    assert result.title == "Learning agents"
    assert result.author == "Channel"
    assert result.canonical_url == "https://www.youtube.com/watch?v=L1OBRtfPS34"
    assert len(provider.calls) == 1


def test_youtube_fallback_keeps_watch_url_when_page_canonical_is_placeholder():
    provider = Provider(
        _Response(404, "", "application/json"),
        raw('<link rel="canonical" href="undefined">'),
        page(),
    )
    result = read(provider, "https://youtu.be/L1OBRtfPS34")
    assert result.status == "success"
    assert result.canonical_url == "https://www.youtube.com/watch?v=L1OBRtfPS34"


def test_x_oembed_parses_only_the_post_and_preserves_date_and_link():
    url = "https://x.com/author/status/123"
    data = {
        "author_name": "Author",
        "html": '<blockquote><p>Hello <a href="https://example.test">agents</a>'
        '</p><a href="https://x.com/author/status/123">November 5</a>'
        "</blockquote><script>untrusted()</script>",
    }
    result = read(Provider(_Response(200, json.dumps(data), "application/json")), url)
    assert result.description == "Hello agents"
    assert result.author == "Author"
    assert result.published_at == "November 5"
    assert result.canonical_url == url


@pytest.mark.parametrize(
    "url, expected",
    [
        (
            "https://example.test/path?q=agents&page=2&utm_source=email#part",
            "https://example.test/path?q=agents&page=2",
        ),
        (
            "https://m.youtube.com/watch?v=L1OBRtfPS34",
            "https://www.youtube.com/watch?v=L1OBRtfPS34",
        ),
        (
            "https://www.youtube.com/shorts/L1OBRtfPS34",
            "https://www.youtube.com/watch?v=L1OBRtfPS34",
        ),
    ],
)
def test_shared_source_identity_rules(url, expected):
    actual = source_urls.youtube_watch_url(url) or source_urls.normalize_source_url(url)
    assert actual == expected


def test_reader_timeout_closes_provider(monkeypatch):
    provider = Provider(raw(), page(content_status="partial"))

    async def stalled(_url):
        await asyncio.Future()

    provider.render = stalled
    monkeypatch.setattr(module, "READ_TIMEOUT_SECONDS", 0.02)
    assert "time budget" in read(provider).error
    assert provider.closed


def test_sdk_clients_are_created_and_closed_per_event_loop(monkeypatch):
    created = []
    closed = []

    class Client:
        def __init__(self, **_kwargs):
            self.loop = asyncio.get_running_loop()
            self.fetch_api = self
            created.append(self)

        async def create(self, *, format, **_kwargs):
            return SimpleNamespace(
                id="request",
                status_code=200,
                headers={},
                content_type="text/html",
                encoding="utf-8",
                content=raw().content if format == "raw" else PAGE,
            )

        async def close(self):
            assert asyncio.get_running_loop() is self.loop
            closed.append(self)

    monkeypatch.setattr(module, "AsyncBrowserbase", Client)
    reader = SourceReader.from_api_key("test-key")
    assert asyncio.run(reader.read(URL)).status == "success"
    assert asyncio.run(reader.read(URL)).status == "success"
    assert len(created) == 2 and closed == created
    assert created[0].loop is not created[1].loop


@pytest.mark.parametrize("failure", ["cancel", "error", "timeout"])
def test_browser_cleanup_on_cancellation_error_and_timeout(monkeypatch, failure):
    closed = []
    entered = None

    async def close_client():
        closed.append("client")

    class Client:
        def __init__(self, **_kwargs):
            self.fetch_api = self

        async def create(self, **_kwargs):
            return SimpleNamespace(
                id="request",
                status_code=200,
                headers={},
                content_type="text/html",
                encoding="utf-8",
                content=raw().content,
            )

        close = staticmethod(close_client)

    class Page:
        async def goto(self, *_args, **_kwargs):
            return SimpleNamespace(status=200)

        async def url(self):
            return "https://webgpu.github.io/webgpu-samples/"

        async def evaluate(self, *_args):
            return None

    class Browser:
        session_id = "session"

        def __init__(self):
            self.context = self

        async def new_page(self):
            return Page()

        async def close(self):
            closed.append("browser")

    class Stage:
        async def extract(self, _instruction, schema, **_kwargs):
            assert schema.model_json_schema()["additionalProperties"] is False
            entered.set()
            if failure == "error":
                raise RuntimeError("Credential-bearing SDK connection details")
            await asyncio.Future()

        async def close(self):
            closed.append("stagehand")

    async def launch(**_kwargs):
        return Browser()

    async def create(**_kwargs):
        return Stage()

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(module, "AsyncBrowserbase", Client)
    monkeypatch.setattr(module.browserbase, "launch", launch)
    monkeypatch.setattr(module.Stagehand, "create", create)
    monkeypatch.setattr(module.asyncio, "sleep", no_wait)
    if failure == "timeout":
        monkeypatch.setattr(module, "READ_TIMEOUT_SECONDS", 0.02)

    async def run():
        nonlocal entered
        entered = asyncio.Event()
        task = asyncio.create_task(
            SourceReader.from_api_key("test-key").read(
                "https://webgpu.github.io/webgpu-samples/"
            )
        )
        await entered.wait()
        if failure == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            result = await task
            assert result.status == "error"
            assert "Credential-bearing" not in result.error

    asyncio.run(run())
    assert closed == ["stagehand", "browser", "client"]
