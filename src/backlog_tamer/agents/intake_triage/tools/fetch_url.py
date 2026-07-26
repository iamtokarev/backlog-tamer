from __future__ import annotations

import ipaddress
import re
import socket
from functools import lru_cache
from io import BytesIO
from typing import Final
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlparse, urlunparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from google.adk.tools import ToolContext

from ..schemas import FetchedUrl

REQUEST_TIMEOUT_SECONDS: Final[float] = 20.0
MAX_REDIRECTS: Final[int] = 5
MAX_RESPONSE_BYTES: Final[int] = 1_500_000
MAX_PREVIEW_CHARS: Final[int] = 1_200
MAX_KEY_POINTS: Final[int] = 5
MAX_PDF_PAGES: Final[int] = 5
MAX_CACHED_URLS: Final[int] = 128
USER_AGENT: Final[str] = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/135.0.0.0 Safari/537.36"
)
SKIP_TEXT_RE: Final[re.Pattern[str]] = re.compile(
    r"^(menu|search|share|home|skip to content|sign in|log in|subscribe|"
    r"privacy policy|terms of service|accept all|cookie settings|open app)$",
    re.IGNORECASE,
)
WHITESPACE_RE: Final[re.Pattern[str]] = re.compile(r"\s+")
PRIVATE_HOST_SUFFIXES: Final[tuple[str, ...]] = (
    ".local",
    ".internal",
    ".localhost",
    ".home",
)
X_STATUS_HOSTS: Final[set[str]] = {
    "x.com",
    "www.x.com",
    "mobile.x.com",
}
X_STATUS_PATH_RE: Final[re.Pattern[str]] = re.compile(
    r"^/(?:i/web/)?(?:[A-Za-z0-9_]+)/status/(\d+)(?:/)?$"
)
X_OEMBED_ENDPOINT: Final[str] = "https://publish.twitter.com/oembed"
FETCHED_CONTEXT_STATE_KEY: Final[str] = "fetched_context"


class RedirectLimitExceededError(ValueError):
    pass


class SafeRedirectHandler(HTTPRedirectHandler):
    def __init__(self, max_redirects: int):
        self.max_redirects = max_redirects
        self.redirect_count = 0

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.redirect_count += 1
        if self.redirect_count > self.max_redirects:
            raise RedirectLimitExceededError("Too many redirects")
        safe_url = _normalize_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, safe_url)


def fetch_url(url: str, tool_context: ToolContext | None = None) -> dict:
    """Fetch a user-provided public URL and return compact triage context.

    Use this when the intake item contains a link and you need enough signal to
    classify it. The tool returns compact metadata, key points, and a short
    preview instead of dumping the full page.

    Args:
        url: A user-provided HTTP or HTTPS URL.

    Returns:
        A structured result with fetch status, normalized URLs, metadata,
        reduced text context, and any failure reason.
    """

    try:
        normalized_url = _normalize_public_url(url)
    except ValueError as exc:
        result = FetchedUrl(
            status="error",
            requested_url=url,
            error=str(exc),
            notes=["Only public http/https URLs are allowed."],
        ).model_dump(mode="json")
        _persist_fetch_result(url, result, tool_context)
        return result

    result = _fetch_url_cached(normalized_url).model_dump(mode="json")
    _persist_fetch_result(normalized_url, result, tool_context)
    return result


def _persist_fetch_result(
    key: str,
    result: dict,
    tool_context: ToolContext | None,
) -> None:
    if tool_context is None:
        return

    existing = tool_context.state.get(FETCHED_CONTEXT_STATE_KEY, {})
    if not isinstance(existing, dict):
        existing = {}

    updated = dict(existing)
    updated[key] = result
    tool_context.state[FETCHED_CONTEXT_STATE_KEY] = updated


@lru_cache(maxsize=MAX_CACHED_URLS)
def _fetch_url_cached(normalized_url: str) -> FetchedUrl:
    parsed_url = urlparse(normalized_url)
    if _is_x_status_url(parsed_url):
        x_result = _fetch_x_status_oembed(normalized_url)
        if x_result is not None:
            return x_result

    request = Request(
        normalized_url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    opener = build_opener(SafeRedirectHandler(MAX_REDIRECTS))

    try:
        with opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            final_url = _normalize_public_url(response.geturl())
            status_code = getattr(response, "status", None)
            content_type = response.headers.get_content_type()
            charset = response.headers.get_content_charset() or "utf-8"
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as exc:
        return FetchedUrl(
            status="error",
            requested_url=normalized_url,
            final_url=_safe_error_url(exc),
            status_code=exc.code,
            error=f"HTTP {exc.code} while fetching URL.",
        )
    except RedirectLimitExceededError as exc:
        return FetchedUrl(
            status="error",
            requested_url=normalized_url,
            error=str(exc),
        )
    except URLError as exc:
        return FetchedUrl(
            status="error",
            requested_url=normalized_url,
            error=_stringify_exception(exc),
        )
    except Exception as exc:  # pragma: no cover - network/runtime variability
        return FetchedUrl(
            status="error",
            requested_url=normalized_url,
            error=_stringify_exception(exc),
        )

    notes: list[str] = []
    if len(body) > MAX_RESPONSE_BYTES:
        body = body[:MAX_RESPONSE_BYTES]
        notes.append("Response body was truncated to the configured byte limit.")

    if content_type in {"text/html", "application/xhtml+xml"}:
        return _build_html_result(
            requested_url=normalized_url,
            final_url=final_url,
            status_code=status_code,
            content_type=content_type,
            body=body,
            charset=charset,
            notes=notes,
        )

    if content_type == "application/pdf":
        return _build_pdf_result(
            requested_url=normalized_url,
            final_url=final_url,
            status_code=status_code,
            content_type=content_type,
            body=body,
            notes=notes,
        )

    if content_type.startswith("text/") or content_type in {
        "application/json",
        "application/xml",
        "application/javascript",
    }:
        return _build_text_result(
            requested_url=normalized_url,
            final_url=final_url,
            status_code=status_code,
            content_type=content_type,
            body=body,
            charset=charset,
            notes=notes,
        )

    notes.append("Content type is not readable text; returning metadata only.")
    return FetchedUrl(
        status="error",
        requested_url=normalized_url,
        final_url=final_url,
        domain=urlparse(final_url).hostname,
        page_kind="unknown",
        content_type=content_type,
        status_code=status_code,
        notes=notes,
        error="Unsupported content type for triage extraction.",
    )


def _fetch_x_status_oembed(normalized_url: str) -> FetchedUrl | None:
    oembed_url = f"{X_OEMBED_ENDPOINT}?" + urlencode(
        {
            "url": normalized_url,
            "omit_script": "1",
            "dnt": "true",
        }
    )
    request = Request(
        oembed_url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
        },
    )

    try:
        with build_opener().open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            if response.headers.get_content_type() != "application/json":
                return None
            charset = response.headers.get_content_charset() or "utf-8"
            payload = response.read(MAX_RESPONSE_BYTES).decode(
                charset,
                errors="replace",
            )
    except Exception:
        return None

    try:
        import json

        data = json.loads(payload)
    except Exception:
        return None

    tweet_text, published_at, embedded_url = _parse_x_oembed_html(data.get("html"))
    author_name = _clean_text(data.get("author_name"))
    author_url = _clean_text(data.get("author_url"))

    if not tweet_text and not author_name:
        return None

    display_title = _build_x_title(tweet_text, author_name)
    notes = ["Fetched via X oEmbed fallback."]
    if not tweet_text:
        notes.append("The embedded post did not expose readable body text.")

    final_url = _first_non_empty(
        _clean_text(data.get("url")),
        embedded_url,
        normalized_url,
    )
    domain = (
        urlparse(final_url).hostname if final_url else urlparse(normalized_url).hostname
    )
    return FetchedUrl(
        status="success",
        requested_url=normalized_url,
        final_url=final_url,
        canonical_url=final_url,
        domain=domain,
        page_kind="text",
        content_type="application/x-oembed+json",
        title=display_title,
        description=tweet_text,
        site_name="X",
        author=author_name,
        published_at=published_at,
        key_points=_build_key_points(
            description=tweet_text,
            headings=[],
            lines=_clean_lines(tweet_text or ""),
        ),
        content_preview=tweet_text[:MAX_PREVIEW_CHARS] if tweet_text else author_url,
        notes=notes,
    )


def _build_html_result(
    *,
    requested_url: str,
    final_url: str,
    status_code: int | None,
    content_type: str,
    body: bytes,
    charset: str,
    notes: list[str],
) -> FetchedUrl:
    html = body.decode(charset, errors="replace")
    canonical_url = None
    title = None
    description = None
    site_name = None
    author = None
    published_at = None
    headings: list[str] = []
    visible_text = ""

    soup = _build_soup(html)
    if soup is not None:
        title = _first_non_empty(
            _extract_meta_content(soup, property_name="og:title"),
            _extract_meta_content(soup, name="twitter:title"),
            soup.title.string if soup.title and soup.title.string else None,
        )
        description = _first_non_empty(
            _extract_meta_content(soup, name="description"),
            _extract_meta_content(soup, property_name="og:description"),
            _extract_meta_content(soup, name="twitter:description"),
        )
        site_name = _extract_meta_content(soup, property_name="og:site_name")
        author = _first_non_empty(
            _extract_meta_content(soup, name="author"),
            _extract_meta_content(soup, property_name="article:author"),
        )
        published_at = _first_non_empty(
            _extract_meta_content(soup, property_name="article:published_time"),
            _extract_meta_content(soup, name="date"),
            _extract_meta_content(soup, name="publish-date"),
        )
        canonical_url = _extract_canonical_url(soup, final_url)
        headings = _collect_headings(soup)
        visible_text = _extract_visible_html_text(soup)
    else:  # pragma: no cover - fallback path
        title = _extract_title_without_bs4(html)
        visible_text = _strip_html_tags(html)
        notes.append(
            "BeautifulSoup is not installed; HTML extraction used a simpler fallback."
        )

    lines = _clean_lines(visible_text)
    key_points = _build_key_points(
        description=description,
        headings=headings,
        lines=lines,
    )
    preview = _build_preview(lines)

    if not preview and description:
        preview = description[:MAX_PREVIEW_CHARS]

    if len(preview) < 120 and html.count("<script") >= 8:
        notes.append(
            "Page appears script-heavy; extracted text may be incomplete "
            "without a browser."
        )

    return FetchedUrl(
        status="success",
        requested_url=requested_url,
        final_url=final_url,
        canonical_url=canonical_url,
        domain=urlparse(final_url).hostname,
        page_kind="html",
        content_type=content_type,
        status_code=status_code,
        title=title,
        description=description,
        site_name=site_name,
        author=author,
        published_at=published_at,
        key_points=key_points,
        content_preview=preview or None,
        notes=notes,
    )


def _build_pdf_result(
    *,
    requested_url: str,
    final_url: str,
    status_code: int | None,
    content_type: str,
    body: bytes,
    notes: list[str],
) -> FetchedUrl:
    pdf_reader = _load_pdf_reader()
    if pdf_reader is None:
        return FetchedUrl(
            status="error",
            requested_url=requested_url,
            final_url=final_url,
            domain=urlparse(final_url).hostname,
            page_kind="pdf",
            content_type=content_type,
            status_code=status_code,
            notes=notes,
            error="PDF extraction is unavailable because pypdf is not installed.",
        )

    try:
        reader = pdf_reader(BytesIO(body))
    except Exception as exc:  # pragma: no cover - malformed PDFs vary
        return FetchedUrl(
            status="error",
            requested_url=requested_url,
            final_url=final_url,
            domain=urlparse(final_url).hostname,
            page_kind="pdf",
            content_type=content_type,
            status_code=status_code,
            notes=notes,
            error=f"Failed to parse PDF: {_stringify_exception(exc)}",
        )

    metadata = reader.metadata or {}
    page_text: list[str] = []
    for index, page in enumerate(reader.pages[:MAX_PDF_PAGES]):
        extracted = page.extract_text() or ""
        if extracted.strip():
            page_text.append(extracted)
        if index + 1 >= MAX_PDF_PAGES:
            break

    if len(reader.pages) > MAX_PDF_PAGES:
        notes.append(f"Only the first {MAX_PDF_PAGES} PDF pages were parsed.")

    lines = _clean_lines("\n".join(page_text))
    preview = _build_preview(lines)
    description = _first_non_empty(
        _clean_text(metadata.get("/Subject")),
        _clean_text(metadata.get("/Title")),
    )

    return FetchedUrl(
        status="success",
        requested_url=requested_url,
        final_url=final_url,
        domain=urlparse(final_url).hostname,
        page_kind="pdf",
        content_type=content_type,
        status_code=status_code,
        title=_clean_text(metadata.get("/Title")),
        description=description,
        author=_clean_text(metadata.get("/Author")),
        key_points=_build_key_points(
            description=description,
            headings=[],
            lines=lines,
        ),
        content_preview=preview or None,
        notes=notes,
    )


def _build_text_result(
    *,
    requested_url: str,
    final_url: str,
    status_code: int | None,
    content_type: str,
    body: bytes,
    charset: str,
    notes: list[str],
) -> FetchedUrl:
    text = body.decode(charset, errors="replace")
    lines = _clean_lines(text)
    title = lines[0] if lines and len(lines[0]) <= 120 else None
    preview = _build_preview(lines)
    return FetchedUrl(
        status="success",
        requested_url=requested_url,
        final_url=final_url,
        domain=urlparse(final_url).hostname,
        page_kind="text",
        content_type=content_type,
        status_code=status_code,
        title=title,
        key_points=_build_key_points(
            description=None,
            headings=[],
            lines=lines,
        ),
        content_preview=preview or None,
        notes=notes,
    )


def _normalize_public_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("URL must use http or https.")
    if not parsed.hostname:
        raise ValueError("URL must include a valid hostname.")
    if parsed.username or parsed.password:
        raise ValueError("URLs with embedded credentials are not allowed.")

    hostname = parsed.hostname.lower()
    if hostname == "localhost" or hostname.endswith(PRIVATE_HOST_SUFFIXES):
        raise ValueError("Private or local hostnames are not allowed.")

    _assert_public_host(hostname)

    path = parsed.path or "/"
    normalized = parsed._replace(
        scheme=parsed.scheme.lower(),
        netloc=parsed.netloc,
        path=path,
        fragment="",
    )
    return urlunparse(normalized)


def _is_x_status_url(parsed_url) -> bool:
    hostname = (parsed_url.hostname or "").lower()
    if hostname not in X_STATUS_HOSTS:
        return False
    return X_STATUS_PATH_RE.match(parsed_url.path) is not None


def _assert_public_host(hostname: str) -> None:
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        ip = None

    if ip is not None and not ip.is_global:
        raise ValueError("Private or non-global IP addresses are not allowed.")

    try:
        resolved = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return

    for _, _, _, _, sockaddr in resolved:
        resolved_ip = ipaddress.ip_address(sockaddr[0])
        if not resolved_ip.is_global:
            raise ValueError("URL resolves to a private or non-global IP address.")


def _extract_meta_content(
    soup,
    *,
    name: str | None = None,
    property_name: str | None = None,
) -> str | None:
    if property_name:
        tag = soup.find("meta", attrs={"property": property_name})
        if tag and tag.get("content"):
            return _clean_text(tag["content"])
    if name:
        tag = soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            return _clean_text(tag["content"])
    return None


def _extract_canonical_url(soup, final_url: str) -> str | None:
    link = soup.find("link", rel=lambda value: value and "canonical" in value)
    if not link or not link.get("href"):
        return None
    try:
        return _normalize_public_url(urljoin(final_url, link["href"]))
    except ValueError:
        return None


def _collect_headings(soup) -> list[str]:
    headings: list[str] = []
    seen: set[str] = set()
    for tag in soup.find_all(["h1", "h2", "h3"]):
        text = _clean_text(tag.get_text(" ", strip=True))
        if not text:
            continue
        lowered = text.casefold()
        if lowered in seen:
            continue
        seen.add(lowered)
        headings.append(text)
        if len(headings) >= 8:
            break
    return headings


def _extract_visible_html_text(soup) -> str:
    for tag_name in [
        "script",
        "style",
        "noscript",
        "svg",
        "canvas",
        "form",
        "header",
        "footer",
        "nav",
        "aside",
    ]:
        for tag in soup.find_all(tag_name):
            tag.decompose()

    root = (
        soup.find("article")
        or soup.find("main")
        or soup.find(attrs={"role": "main"})
        or soup.body
        or soup
    )
    return root.get_text("\n", strip=True)


def _extract_title_without_bs4(html: str) -> str | None:
    match = re.search(
        r"<title[^>]*>(.*?)</title>",
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return None
    return _clean_text(match.group(1))


def _strip_html_tags(html: str) -> str:
    stripped = re.sub(
        r"<script.*?</script>",
        " ",
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    stripped = re.sub(
        r"<style.*?</style>",
        " ",
        stripped,
        flags=re.IGNORECASE | re.DOTALL,
    )
    stripped = re.sub(r"<[^>]+>", " ", stripped)
    return _clean_text(stripped) or ""


def _build_soup(html: str):
    try:
        from bs4 import BeautifulSoup
    except ImportError:  # pragma: no cover - optional dependency
        return None
    return BeautifulSoup(html, "html.parser")


def _load_pdf_reader():
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover - optional dependency
        return None
    return PdfReader


def missing_optional_dependencies() -> list[str]:
    """Return the extraction dependencies that are absent from this environment.

    Both loaders above swallow ImportError and their callers fall back to a
    weaker extraction path, so a dependency missing from the deployed image
    degrades output quality without ever raising. The deploy healthcheck calls
    this so that failure mode surfaces instead of going unnoticed.
    """
    missing: list[str] = []
    if _build_soup("<html><title>probe</title></html>") is None:
        missing.append("beautifulsoup4")
    if _load_pdf_reader() is None:
        missing.append("pypdf")
    return missing


def _clean_lines(text: str) -> list[str]:
    lines: list[str] = []
    seen: set[str] = set()

    for raw_line in text.splitlines():
        line = _clean_text(raw_line)
        if not line or len(line) < 25:
            continue
        if SKIP_TEXT_RE.match(line):
            continue
        lowered = line.casefold()
        if lowered in seen:
            continue
        seen.add(lowered)
        lines.append(line)
        if len(lines) >= 80:
            break

    return lines


def _build_key_points(
    *,
    description: str | None,
    headings: list[str],
    lines: list[str],
) -> list[str]:
    candidates: list[str] = []
    if description:
        candidates.append(description)
    candidates.extend(headings)
    candidates.extend(lines)

    key_points: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        cleaned = _clean_text(candidate)
        if not cleaned or len(cleaned) < 20:
            continue
        if len(cleaned) > 220:
            cleaned = cleaned[:217].rstrip() + "..."
        lowered = cleaned.casefold()
        if lowered in seen:
            continue
        seen.add(lowered)
        key_points.append(cleaned)
        if len(key_points) >= MAX_KEY_POINTS:
            break
    return key_points


def _build_preview(lines: list[str]) -> str:
    preview_parts: list[str] = []
    total_length = 0
    for line in lines:
        if total_length >= MAX_PREVIEW_CHARS:
            break
        remaining = MAX_PREVIEW_CHARS - total_length
        chunk = line[:remaining].rstrip()
        if not chunk:
            continue
        preview_parts.append(chunk)
        total_length += len(chunk) + 1
    return "\n".join(preview_parts)


def _first_non_empty(*values: str | None) -> str | None:
    for value in values:
        cleaned = _clean_text(value)
        if cleaned:
            return cleaned
    return None


def _clean_text(value: object) -> str | None:
    if value is None:
        return None
    text = WHITESPACE_RE.sub(" ", str(value)).strip()
    return text or None


def _safe_error_url(exc: HTTPError) -> str | None:
    if not getattr(exc, "url", None):
        return None
    try:
        return _normalize_public_url(exc.url)
    except ValueError:
        return None


def _stringify_exception(exc: Exception) -> str:
    message = str(exc).strip()
    return message or exc.__class__.__name__


def _parse_x_oembed_html(fragment: object) -> tuple[str | None, str | None, str | None]:
    html = _clean_text(fragment)
    if not html:
        return None, None, None

    soup = _build_soup(html)
    if soup is None:
        return None, None, None

    blockquote = soup.find("blockquote")
    if blockquote is None:
        return None, None, None

    text_parts: list[str] = []
    paragraph = blockquote.find("p")
    if paragraph is not None:
        text = paragraph.get_text(" ", strip=True)
        cleaned = _clean_text(text)
        if cleaned:
            text_parts.append(cleaned)

    links: list[str] = []
    for link in blockquote.find_all("a", href=True):
        href = _clean_text(link["href"])
        if href:
            links.append(href)

    tweet_text = " ".join(text_parts).strip() or None
    published_at = None
    embedded_url = None
    if links:
        embedded_url = links[-1]
        last_link = blockquote.find_all("a")[-1]
        date_text = _clean_text(last_link.get_text(" ", strip=True))
        if date_text and not date_text.startswith("http"):
            published_at = date_text

    return tweet_text, published_at, embedded_url


def _build_x_title(tweet_text: str | None, author_name: str | None) -> str | None:
    if tweet_text:
        compact = (
            tweet_text if len(tweet_text) <= 80 else tweet_text[:77].rstrip() + "..."
        )
        if author_name:
            return f"{author_name} on X: {compact}"
        return compact
    if author_name:
        return f"Post by {author_name} on X"
    return "X post"
