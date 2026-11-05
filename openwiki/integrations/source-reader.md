---
type: Integration
title: Source Reader (Browserbase + Stagehand)
description: >-
  How Backlog Tamer reads public web sources into compact Grounding through
  Browserbase Fetch API and hosted Stagehand rendering, with local SSRF
  validation, an extraction escalation ladder, YouTube/X oEmbed and PDF
  specialization, and a per-invocation client lifecycle for AWS Lambda.
tags: [integrations, source-reader, browserbase, stagehand, ssrf, grounding]
timestamp: 2026-11-05T23:00:00Z
openwiki:
  roles: [integration, architecture, operations]
  change_kinds: [lifecycle, public-api]
  source_paths:
    - src/backlog_tamer/integrations/source_reader.py
    - src/backlog_tamer/agents/intake_triage/tools/fetch_url.py
    - src/backlog_tamer/source_urls.py
  symbols:
    - SourceReader
    - SourceReader.read
    - SourceReader.from_api_key
    - FetchedUrl
    - _BrowserbaseProvider
    - make_fetch_url_tool
  test_paths:
    - tests/test_source_reader.py
    - tests/test_fetch_url.py
  invariants:
    - Private or non-global IP inputs and redirects never reach the Browserbase provider.
    - Failed page fetches are never cached as success by the ADK tool; refetch clears saved Grounding.
    - One Browserbase client and browser session per read; clients are closed even on cancel/error/timeout.
    - Source facts are never inferred from a URL alone; extraction returns structured failure when content is unavailable.
  validation_commands:
    - uv run pytest tests/test_source_reader.py tests/test_fetch_url.py -q
---

# Source Reader (Browserbase + Stagehand)

The agent needs grounded evidence about a URL before it drafts a project — a title, key points, a canonical identity — but it must not reach private network addresses, and many sources are JavaScript-rendered or behind soft paywalls. `SourceReader` (`src/backlog_tamer/integrations/source_reader.py`) is the component that turns one public URL into a compact `FetchedUrl` Grounding result, or a structured failure. It is the single place that owns remote network egress for source reading.

`BROWSERBASE_API_KEY` is a required setting (`Settings.browserbase_api_key`, validated non-blank in `config.py`). The agent is wired to it in `agent.py`: `SourceReader.from_api_key(settings.browserbase_api_key.get_secret_value())` is passed into `make_fetch_url_tool`, which adapts it to ADK's tool interface. Source reading is surfaced to the model as a tool named `fetch_url`; see [Intake Workflow](../workflows/intake-flow.md) for how the agent calls it and how Grounding flows into the draft.

## Why Browserbase

Earlier versions fetched sources directly from the Lambda with `httpx` and parsed HTML with `beautifulsoup4`, pinning the remote peer's IP to prevent SSRF and DNS rebinding. That worked for static HTML but could not render JavaScript-heavy sources (Kaggle Learn, WebGPU samples) or recover from soft 403/429 responses that a real browser satisfies. Source reading now delegates all remote DNS resolution and network egress to Browserbase, a managed browser infrastructure service:

- **Fetch API** (`AsyncBrowserbase.fetch_api.create`) — resolves redirects and content types server-side without launching a browser. Used for raw body retrieval and structured HTML extraction (Fetch JSON with a Pydantic schema).
- **Hosted Stagehand** (`browserbase.launch` + `Stagehand.create`) — launches a real browser session and uses Stagehand's `extract` to pull structured `_PageGrounding` from a rendered page. Used when structured Fetch returns insufficient content, when a source is known to be dynamic, or when the target returned a recoverable HTTP error.

Browserbase owns remote DNS and egress. Local validation still rejects private inputs and Fetch redirects **before** they reach the provider, but it does not pin the remote peer — that responsibility moved to the managed service.

## The Escalation Ladder

`SourceReader.read` runs under a 60-second timeout (`READ_TIMEOUT_SECONDS`) and dispatches to `_read`, which selects an extraction path by URL shape and content type. The ladder, from cheapest to most expensive:

<!-- openwiki: mermaid parse failed and this diagram was converted to a text fence so it does not break rendering. Fix the diagram source and restore the mermaid fence. Parser error: Heuristic: an unescaped angle bracket inside a label breaks rendering; rephrase the label. -->
<!-- openwiki: mermaid parse failed and this diagram was converted to a text fence so it does not break rendering. Fix the diagram source and restore the mermaid fence. Parser error: Heuristic: an unescaped angle bracket inside a label breaks rendering; rephrase the label. -->
```text
flowchart TD
    IN["read(url)"] --> VALID["_validate_public_url (local DNS)"]
    VALID -->|private/non-global| ERR["FetchedUrl(error)"]
    VALID --> OE{"YouTube or X status?"}
    OE -->|yes| OEMBED["_oembed: Fetch raw, json.loads"]
    OE -->|no| REQ["_request: Fetch raw, follow redirects (max 5)"]
    REQ -->|HTTP at least 400| RECOV{"403, 429, or 5xx?"}
    RECOV -->|yes| RENDER["_render: Stagehand browser"]
    RECOV -->|no| ERR
    REQ -->|200| CT{"content-type?"}
    CT -->|pdf| PDF["_pdf: pypdf, first 5 pages"]
    CT -->|html| DYN{"_browser_required? (Kaggle Learn, WebGPU samples)"}
    DYN -->|yes| RENDER
    DYN -->|no| JSON["Fetch JSON with _PageGrounding schema"]
    JSON -->|insufficient/partial| RENDER
    JSON -->|complete| HTML["_html -> FetchedUrl"]
    CT -->|text/json/xml| TEXT["text lines -> FetchedUrl"]
    RENDER -->|rendered complete| HTML
    RENDER -->|incomplete| ERR
    OEMBED -->|parsed| OUT["FetchedUrl(success)"]
    OEMBED -->|missing| REQ
    HTML --> OUT
    PDF --> OUT
    TEXT --> OUT
    OUT --> YT["youtube_watch_url pinned as canonical (if YouTube)"]
```

*Extraction path selection in `SourceReader._read`. A source escalates to a hosted browser only when cheaper Fetch-based extraction is insufficient, known-dynamic, or recovering from a target HTTP error.*

Key escalation rules:

- **YouTube and X status URLs** go through oEmbed (`_oembed`) first, which calls `_request` against `youtube.com/oembed` or `publish.twitter.com/oembed` with the default `format="raw"` (a plain Fetch raw retrieval, not Browserbase's model-powered Fetch JSON format) and then parses the response body with `json.loads(_body(response))`. The oEmbed endpoints return JSON as raw bytes, so this path uses raw Fetch plus local JSON decoding rather than the structured Fetch JSON schema extraction used for HTML. YouTube oEmbed supplies title and channel only (not a transcript); X oEmbed parses the post text, author, date, and link out of the returned HTML. If oEmbed is missing, the source falls through to the normal Fetch path.
- **HTML** is fetched raw to resolve redirects and read the `<link rel="canonical">`, then re-fetched as Fetch JSON with the `_PageGrounding` schema so the extraction model (not local parsing) selects title, description, site name, author, published date, key points, and a content preview. `_PageGrounding` has `extra="forbid"` and a `content_status` of `complete`/`partial`/`unavailable`. The acceptance rule in `SourceReader._html` requires all of: `content_status == "complete"`, a nonblank `title`, and a nonblank `key_points` or `content_preview` (after local trimming). Any miss returns `None`, which the caller treats as insufficient and escalates to a browser render.
- **Known dynamic sources** (`_browser_required`: `kaggle.com/learn/*`, `webgpu.github.io/webgpu-samples*`) skip structured Fetch and go straight to a Stagehand render.
- **Target HTTP errors** (403, 429, 5xx) are treated as recoverable in a browser — the source escalates to `_render("target_http")`. Hard 404/410 and 4xx (except the recoverable set) fail directly without launching a browser.
- **PDF** is parsed locally with `pypdf` (first 5 pages, `MAX_PDF_PAGES`); a body over 5 MB (`MAX_RESPONSE_BYTES`) is rejected before parsing.
- **Text, JSON, XML, JavaScript** content types are read as line-delimited text points.

Browser renders themselves can fail: if the rendered `_PageGrounding` is still `partial`/`unavailable`, `read` returns `FetchedUrl(status="error")` rather than passing off a guess.

## Local SSRF Validation

`_validate_public_url` (in `source_reader.py`) and `normalize_source_url` (in `source_urls.py`) are the local guards run **before** any Browserbase call and on every redirect `Location`:

- Scheme must be `http`/`https`; embedded credentials are rejected.
- Hostnames ending in `.local`, `.internal`, `.localhost`, `.home`, or equal to `localhost` are rejected.
- Literal IP addresses must be `is_global` (rejects private, loopback, link-local).
- The hostname is resolved via `socket.getaddrinfo`; if any resolved address is not global, the URL is rejected. This rejects private results observed locally at validation time — for example a hostname that resolves to a private IP at the moment validation runs. It does **not** pin or guarantee remote resolution: Browserbase owns remote DNS and network egress, and a hostname that resolves to a public address locally may resolve differently from the managed service.
- **Explicit Fetch redirects** are followed manually (`_request`, max `MAX_REDIRECTS = 5`), and each `Location` is re-validated via `_validate_public_url` **before** the next request is issued — a public URL that redirects to a private address is stopped before it reaches the provider.
- **Browser navigation destinations** are validated **after** navigation: `_BrowserbaseProvider.render` calls `page.goto(url)` first, then `_validate_public_url(final_url)` against the landed URL. Local code therefore does not block every browser subresource or redirect before it reaches the remote service; it rejects the post-navigation destination when it is observed to be private.

`_canonical` ignores canonical `<link>` values that resolve to `undefined`/`null`/`none` or to a private IP, which is how YouTube's JS-templated canonical can render as `youtube.com/undefined` to datacenter IPs.

## Output Bounds

`_points` and `_text` enforce output bounds regardless of what the extraction model returns: titles ≤ 300 chars, descriptions ≤ 1200, key points deduplicated and capped at 5 (`MAX_KEY_POINTS`) with each ≤ 300 chars, content preview ≤ 1200 (`MAX_PREVIEW_CHARS`, though the `FetchedUrl.content_preview` field allows 1600). These are defense-in-depth on top of the schema, not the primary content selection.

## FetchedUrl

The Grounding result. Defined in `source_reader.py`. Stored in ADK session state under `fetched_context` and summarized into `DraftGrounding` for the review card and Notion page body — see [Data Model](../domain/data-model.md).

| Field | Type | Notes |
|-------|------|-------|
| `status` | `Literal["success", "error"]` | |
| `requested_url` | `str` | Normalized URL passed in |
| `final_url` | `str \| None` | After redirects |
| `canonical_url` | `str \| None` | From `<link rel="canonical">`, or the pinned YouTube watch URL |
| `domain`, `page_kind`, `content_type`, `status_code` | various | `page_kind`: `html`, `pdf`, `text`, `unknown` |
| `title`, `description`, `site_name`, `author`, `published_at` | `str \| None` | Extracted metadata |
| `key_points` | `list[str]` | Up to 5 |
| `content_preview` | `str \| None` | Max 1600 chars |
| `notes` | `list[str]` | e.g. `"Only the first 5 PDF pages were parsed."` |
| `error` | `str \| None` | Structured failure reason |

On error, the `error` string is deliberately generic for SDK connection failures: the SDK may embed credential-bearing connection URLs in exception messages, so those are collapsed to `Source reading failed (<ExceptionType>).`. Provider HTTP errors (`APIStatusError`) surface the status code; provider account errors (401/402/403/429) fail directly without a browser fallback.

## The ADK `fetch_url` Tool Adapter

`src/backlog_tamer/agents/intake_triage/tools/fetch_url.py` is now a thin adapter. `make_fetch_url_tool(reader)` returns an async `fetch_url(url, tool_context)` function that:

1. Normalizes the URL with `normalize_source_url` to derive a stable cache key.
2. Reuses a previously-saved **success** result from `tool_context.state[FETCHED_CONTEXT_STATE_KEY]` without calling the provider — so the same URL is not re-fetched within one Intake. Failed results are saved too, but a failure is **not** reused (the cache check requires `status == "success"`); the next call re-reads.
3. On a miss, calls `reader.read(url)`, persists the payload back into session state (reading state again after the await so concurrent reads both persist), and returns the JSON payload.

Clearing `FETCHED_CONTEXT_STATE_KEY` is how a **Refetch** works: `TriageWorkflow.refetch` resumes the paused turn with `state_delta={FETCHED_CONTEXT_STATE_KEY: {}}`, so the saved failure is discarded and the agent re-reads the source. See [Intake Workflow](../workflows/intake-flow.md#refetch-after-failed-page-fetch).

## Per-Invocation Client Lifecycle

`_BrowserbaseProvider` creates a fresh `AsyncBrowserbase` client per `__aenter__`, and each `render` launches its own browser session and `Stagehand` instance, closing them in a `finally` block (Stagehand first, then the browser) even on cancellation, error, or timeout. This is deliberate for AWS Lambda: each invocation gets a new event loop, and a client bound to a prior loop would be unusable. `tests/test_source_reader.py::test_sdk_clients_are_created_and_closed_per_event_loop` pins that two sequential `read` calls in separate `asyncio.run` loops create and close two distinct clients on two distinct loops.

## Source URL Identity

`src/backlog_tamer/source_urls.py` holds the shared URL identity helpers used by source reading, the Notion writer's duplicate detection, and the schema migration script (`scripts/migrate_notion_schema.py`, which imports `strip_tracking_params` to campaign-canonicalize backfilled Source values):

- `normalize_source_url` — scheme/host validation, private-host rejection, and `strip_tracking_params`.
- `strip_tracking_params` — drops campaign parameters (`utm_*`, `_ga*`, `mc_*`, `pk_*`, `fbclid`, `gclid`, `dclid`, `msclkid`, `igshid`, `si`, `ref`, `ref_src`, `spm`, `yclid`, `_gl`) so a re-capture of the same page through a different tracking link matches the earlier Notion row. See [Duplicate Detection](../workflows/intake-flow.md#duplicate-detection).
- `youtube_watch_url` — collapses `youtu.be`, `m.youtube.com`, `/shorts/`, `/live/`, `/embed/`, and `/v/` variants to a single `https://www.youtube.com/watch?v={id}`, pinned as the canonical URL so the page's own JS-templated canonical (which can render as `youtube.com/undefined` to datacenter IPs) does not win.

## Dependencies and Lockfile

`pyproject.toml` declares `browserbase>=1.24,<2`, `stagehand>=4.4,<5`, and `pypdf>=6.16.1` (the `beautifulsoup4` dependency is gone). Stagehand v4 conflicts with ADK's `websockets` pin, so `[tool.uv]` overrides `websockets>=16.1.1,<17` to keep the shared version within GenAI's `<17` range. This override has been validated for the HTTP-backed `run_async` application path that Intake actually uses, via the locked suite (`uv lock --check` / `make test`) and live source-backed Triage turns through the dev runner; it is not an unconditional compatibility guarantee for other ADK features (e.g. Gemini Live), which may need separate verification when changed. When changing those dependencies, re-run that focused validation.

## When to Consult This Page

- Adding a new content-type extraction path or changing the escalation ladder.
- Changing the `_browser_required` known-dynamic source list.
- Touching SSRF validation or redirect handling.
- Wiring a different Browserbase region, timeout, or client configuration.
- Debugging a "Source reading failed" error or a degraded Grounding result.
- Adding or changing a source URL identity rule that duplicate detection depends on.

## Change Surface

To add a new known-dynamic source (skip structured Fetch, go straight to render):

1. Add a host/path condition to `_browser_required` in `source_reader.py`.
2. Add a `test_known_dynamic_sources_skip_structured_http_extraction` parametrize case in `tests/test_source_reader.py`.
3. Confirm the source renders completely via Stagehand; otherwise it returns a structured failure (which is the correct, safe behavior).

To add a new specialized extraction path (e.g. a new oEmbed provider):

1. Add the URL-shape detection in `SourceReader._read` before the generic Fetch path.
2. Implement the extractor returning a `FetchedUrl`.
3. Cover it in `tests/test_source_reader.py` with a scripted `Provider`.

## Source References

| File | Purpose |
|------|---------|
| `src/backlog_tamer/integrations/source_reader.py` | `SourceReader`, `_BrowserbaseProvider`, `FetchedUrl`, `_PageGrounding`, escalation ladder, SSRF validation |
| `src/backlog_tamer/agents/intake_triage/tools/fetch_url.py` | `make_fetch_url_tool` — ADK tool adapter, success-only caching, failure retry |
| `src/backlog_tamer/source_urls.py` | `normalize_source_url`, `strip_tracking_params`, `youtube_watch_url` |
| `src/backlog_tamer/agents/intake_triage/agent.py` | Wires `SourceReader.from_api_key` into the `fetch_url` tool |
| `tests/test_source_reader.py` | Interface, escalation, SSRF, redirect, oEmbed, PDF, bounds, cleanup, per-loop lifecycle |
| `tests/test_fetch_url.py` | ADK tool caching, success reuse, failure retry, parallel reads |
