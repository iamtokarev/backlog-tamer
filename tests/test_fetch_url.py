from __future__ import annotations

import socket
from email.message import Message

import pytest

from backlog_tamer.agents.intake_triage.tools import fetch_url

PUBLIC_IP = "93.184.216.34"
PRIVATE_IP = "127.0.0.1"


def test_prepare_public_url_preserves_url_and_returns_public_addresses(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                (PUBLIC_IP, 0),
            )
        ],
    )

    normalized, addresses = fetch_url._prepare_public_url(
        "HTTPS://example.test/resource?q=1#fragment"
    )

    assert normalized == "https://example.test/resource?q=1"
    assert addresses == (PUBLIC_IP,)


def test_prepare_public_url_fails_closed_on_dns_error(monkeypatch):
    def fail_resolution(*_args, **_kwargs):
        raise socket.gaierror("resolution failed")

    monkeypatch.setattr(socket, "getaddrinfo", fail_resolution)

    with pytest.raises(ValueError, match="could not be resolved"):
        fetch_url._prepare_public_url("https://example.test/resource")


def test_request_connects_to_the_address_that_was_validated(monkeypatch):
    dns_calls: list[str] = []
    connection_attempts: list[tuple[str, int]] = []

    def resolve_once(hostname, *_args, **_kwargs):
        dns_calls.append(hostname)
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                (PUBLIC_IP, 0),
            )
        ]

    def stop_before_network(address, *_args, **_kwargs):
        connection_attempts.append(address)
        raise OSError("test stops before network access")

    monkeypatch.setattr(socket, "getaddrinfo", resolve_once)
    monkeypatch.setattr(socket, "create_connection", stop_before_network)

    with pytest.raises(OSError, match="stops before network"):
        fetch_url._request_public_url("http://rebind.test/resource")

    assert dns_calls == ["rebind.test"]
    assert connection_attempts == [(PUBLIC_IP, 80)]


def test_connection_rejects_a_peer_that_does_not_match_the_pinned_address(
    monkeypatch,
):
    class PrivatePeerSocket:
        closed = False

        def getpeername(self):
            return PRIVATE_IP, 80

        def close(self):
            self.closed = True

    peer_socket = PrivatePeerSocket()
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                (PUBLIC_IP, 0),
            )
        ],
    )
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *_args, **_kwargs: peer_socket,
    )

    with pytest.raises(ValueError, match="unexpected or non-global"):
        fetch_url._request_public_url("http://rebind.test/resource")

    assert peer_socket.closed is True


def test_redirect_to_private_address_is_rejected_before_second_request(monkeypatch):
    requested_addresses: list[str] = []

    def resolve(hostname, *_args, **_kwargs):
        ip = PUBLIC_IP if hostname == "public.test" else PRIVATE_IP
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                (ip, 0),
            )
        ]

    def redirect_response(url, pinned_ip):
        requested_addresses.append(pinned_ip)
        headers = Message()
        headers["location"] = "http://private.test/secret"
        return fetch_url.PublicHttpResponse(
            url=url,
            status=302,
            headers=headers,
            body=b"",
        )

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(fetch_url, "_request_public_address", redirect_response)

    with pytest.raises(ValueError, match="private or non-global"):
        fetch_url._request_public_url("http://public.test/start")

    assert requested_addresses == [PUBLIC_IP]


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        # The real capture that stored a course under an un-matchable URL.
        (
            "https://academy.langchain.com/courses/deepagents?_gl=1*174rmm0*_ga*NTY",
            "https://academy.langchain.com/courses/deepagents",
        ),
        (
            "https://huggingface.co/papers/2609.02749?utm_source=digest&utm_medium=email",
            "https://huggingface.co/papers/2609.02749",
        ),
        # Parameters that select content are not campaign noise.
        (
            "https://www.youtube.com/watch?v=abc123&si=xyz",
            "https://www.youtube.com/watch?v=abc123",
        ),
        (
            "https://example.com/search?q=agents&page=2",
            "https://example.com/search?q=agents&page=2",
        ),
    ],
)
def test_tracking_parameters_are_stripped_from_the_normalized_url(
    url: str,
    expected: str,
):
    assert fetch_url._normalize_url_syntax(url) == expected


def test_key_points_drop_page_furniture():
    """Real key points from last week's captures were nav and CTA text."""
    key_points = fetch_url._build_key_points(
        description="Add us as a preferred source on Google",
        headings=[
            "Join the discussion on this paper page",
            "Models citing this paper 0",
            "Repo-To-Skill: Distilling GitHub Repositories Into AI4AI Skills",
        ],
        lines=[
            "Subscribe to our newsletter for weekly updates",
            "AI coding strategies that work delivered to your inbox.",
            "DisCo distills operational knowledge into reusable agent skills.",
        ],
    )

    assert key_points == [
        "Repo-To-Skill: Distilling GitHub Repositories Into AI4AI Skills",
        "DisCo distills operational knowledge into reusable agent skills.",
    ]
