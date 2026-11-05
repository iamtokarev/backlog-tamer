"""Stable public source identities shared by intake and maintenance scripts."""

import ipaddress
import re
from typing import Final
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

TRACKING_PARAM_PREFIXES: Final[tuple[str, ...]] = ("utm_", "_ga", "mc_", "pk_")
TRACKING_PARAMS: Final[frozenset[str]] = frozenset(
    {
        "_gl",
        "fbclid",
        "gclid",
        "dclid",
        "msclkid",
        "igshid",
        "si",
        "ref",
        "ref_src",
        "spm",
        "yclid",
    }
)

PRIVATE_HOST_SUFFIXES: Final[tuple[str, ...]] = (
    ".local",
    ".internal",
    ".localhost",
    ".home",
)
YOUTUBE_HOSTS: Final[frozenset[str]] = frozenset(
    {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
)
YOUTUBE_SHORT_HOSTS: Final[frozenset[str]] = frozenset({"youtu.be", "www.youtu.be"})
YOUTUBE_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_-]{11}$")
YOUTUBE_PATH_ID_RE: Final[re.Pattern[str]] = re.compile(
    r"^/(?:shorts|live|embed|v)/([^/]+)/?$"
)


def normalize_source_url(url: str) -> str:
    try:
        parsed = urlparse(url.strip())
        hostname = parsed.hostname
        _parsed_port = parsed.port
    except ValueError as exc:
        raise ValueError("URL must include a valid hostname and port.") from exc

    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("URL must use http or https.")
    if not hostname:
        raise ValueError("URL must include a valid hostname.")
    if parsed.username or parsed.password:
        raise ValueError("URLs with embedded credentials are not allowed.")

    hostname = hostname.lower()
    if hostname == "localhost" or hostname.endswith(PRIVATE_HOST_SUFFIXES):
        raise ValueError("Private or local hostnames are not allowed.")

    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("Private or non-global IP addresses are not allowed.")

    path = parsed.path or "/"
    normalized = parsed._replace(
        scheme=parsed.scheme.lower(),
        netloc=parsed.netloc,
        path=path,
        query=strip_tracking_params(parsed.query),
        fragment="",
    )
    return urlunparse(normalized)


def strip_tracking_params(query: str) -> str:
    """Drop campaign parameters, keeping the ones that select content."""
    if not query:
        return query

    kept = [
        (name, value)
        for name, value in parse_qsl(query, keep_blank_values=True)
        if not _is_tracking_param(name)
    ]
    return urlencode(kept)


def _is_tracking_param(name: str) -> bool:
    lowered = name.lower()
    if lowered in TRACKING_PARAMS:
        return True
    return lowered.startswith(TRACKING_PARAM_PREFIXES)


def youtube_watch_url(url: str) -> str | None:
    """The one URL a YouTube video is stored under, or None if not a video.

    youtu.be, m., /shorts/, /live/ and /embed/ links, with or without a
    timestamp or playlist, all name the same video; storing them as one URL
    is what lets the duplicate check recognise a re-send.
    """
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    video_id = None
    if hostname in YOUTUBE_SHORT_HOSTS:
        video_id = parsed.path.strip("/")
    elif hostname in YOUTUBE_HOSTS:
        if parsed.path.rstrip("/") == "/watch":
            video_id = dict(parse_qsl(parsed.query)).get("v")
        else:
            match = YOUTUBE_PATH_ID_RE.match(parsed.path)
            video_id = match.group(1) if match else None

    if not video_id or not YOUTUBE_ID_RE.match(video_id):
        return None
    return f"https://www.youtube.com/watch?v={video_id}"
