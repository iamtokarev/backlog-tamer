"""OpenRouter calls for Triage: typed decisions and the title LLM.

A fresh httpx client per call: Lambda creates a new event loop for each
invocation, so nothing async may outlive one.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from time import perf_counter
from typing import Any

import httpx

logger = logging.getLogger(__name__)

OPENROUTER_API = "https://openrouter.ai/api"
RETRY_STATUSES = frozenset({429, 502, 503, 524, 529})
MAX_ATTEMPTS = 4
TIMEOUT_SECONDS = 60
# Decider rejects requests with more questions than this.
MAX_QUESTIONS_PER_REQUEST = 128

TITLE_SYSTEM = """
You write one backlog title for a saved learning item. Return JSON only.
- project_name: what the user scans weeks later. Roughly 4 to 10 words that
  say what the thing is and why it is worth the user's time.
- Keep a recognizable proper name and add the payoff after a colon, e.g.
  "LangGraph: build stateful multi-agent workflows". Never a bare repo,
  package, product, domain or page title.
- With no proper name, describe the subject, e.g. "Vector index tradeoffs
  for hybrid search".
- Concrete and specific; no hype or claims the source does not support.
- If reviewer feedback is given, apply it directly. When the feedback gives an
  exact title, use it verbatim even if it breaks the rules above.
""".strip()

TITLE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "Title",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["project_name"],
            "properties": {
                "project_name": {"type": "string"},
            },
        },
    },
}


@dataclass(frozen=True)
class Usage:
    """One model call, for logs and the eval's cost numbers."""

    path: str
    model: str | None
    seconds: float
    input_tokens: int | None
    cost: float | None


def log_usage(usage: Usage) -> None:
    logger.info(
        "decision_triage_call path=%s model=%s seconds=%.2f input_tokens=%s",
        usage.path,
        usage.model,
        usage.seconds,
        usage.input_tokens,
    )


class DecisionClient:
    def __init__(
        self,
        api_key: str,
        *,
        decision_model: str,
        title_model: str,
        on_usage: Callable[[Usage], None] | None = None,
    ):
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        self.decision_model = decision_model
        self.title_model = title_model
        self._on_usage = on_usage or log_usage

    async def decide(self, state: Any, questions: dict[str, Any]) -> dict[str, Any]:
        """Answers keyed like `questions`; providers that cap the count get chunks."""
        names = list(questions)
        step = MAX_QUESTIONS_PER_REQUEST
        chunks = [names[i : i + step] for i in range(0, len(names), step)]
        bodies = await asyncio.gather(
            *(
                self._post(
                    "/alpha/decisions",
                    {
                        "model": self.decision_model,
                        "state": state,
                        "questions": {n: questions[n] for n in chunk},
                    },
                )
                for chunk in chunks
            )
        )
        answers: dict[str, Any] = {}
        for body in bodies:
            answers.update(body.get("answers") or {})
        return answers

    async def title(
        self,
        page: dict[str, Any],
        *,
        feedback: str | None = None,
        current: str | None = None,
    ) -> str:
        """The project_name for `page`, applying reviewer feedback if given."""
        lines = [f"page: {json.dumps(page, ensure_ascii=False)}"]
        if current:
            lines.append(f"current_title: {current}")
        if feedback:
            lines.append(f"reviewer_feedback: {feedback}")
        body = await self._post(
            "/v1/chat/completions",
            {
                "model": self.title_model,
                "messages": [
                    {"role": "system", "content": TITLE_SYSTEM},
                    {"role": "user", "content": "\n".join(lines)},
                ],
                "response_format": TITLE_SCHEMA,
                "reasoning": {"effort": "none"},
                "provider": {"require_parameters": True},
            },
        )
        payload = json.loads(body["choices"][0]["message"]["content"])
        return payload["project_name"].strip()

    async def probe(self) -> None:
        """Call both models once; raise if either can't answer.

        Catches a retired slug, a typo or a bad key at deploy time instead of
        on the first real capture.
        """
        answers = await self.decide(
            {"text": "healthcheck"},
            {
                "ok": {
                    "type": "noul",
                    "instructions": "Is this a healthcheck?",
                    "criteria": {"true": "yes", "false": "no"},
                }
            },
        )
        if (answers.get("ok") or {}).get("type") != "noul":
            raise RuntimeError(
                f"Decision model {self.decision_model} returned no 'ok' answer: "
                f"{answers!r}"
            )
        if not await self.title({"title": "healthcheck"}):
            raise RuntimeError(f"Title model {self.title_model} returned no title.")

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """POST with retries; the final attempt's error, if any, propagates."""
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            for attempt in range(MAX_ATTEMPTS - 1):
                start = perf_counter()
                try:
                    response = await client.post(
                        f"{OPENROUTER_API}{path}", json=body, headers=self._headers
                    )
                except (httpx.TimeoutException, httpx.TransportError):
                    await asyncio.sleep(2**attempt)
                    continue
                if response.status_code in RETRY_STATUSES:
                    await asyncio.sleep(_retry_delay(response, attempt))
                    continue
                return self._read(response, path, body, start)
            start = perf_counter()
            response = await client.post(
                f"{OPENROUTER_API}{path}", json=body, headers=self._headers
            )
            return self._read(response, path, body, start)

    def _read(
        self, response: httpx.Response, path: str, body: dict[str, Any], start: float
    ) -> dict[str, Any]:
        response.raise_for_status()
        payload = response.json()
        usage = payload.get("usage") or {}
        self._on_usage(
            Usage(
                path=path,
                model=body.get("model"),
                seconds=perf_counter() - start,
                input_tokens=usage.get("input_tokens", usage.get("prompt_tokens")),
                cost=usage.get("cost"),
            )
        )
        return payload


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    """Seconds from Retry-After (a number or an HTTP-date), else backoff."""
    header = (response.headers.get("Retry-After") or "").strip()
    try:
        seconds = float(header)
    except ValueError:
        pass
    else:
        return max(0.0, seconds) if math.isfinite(seconds) else float(2**attempt)
    try:
        when = parsedate_to_datetime(header)
    except (TypeError, ValueError):
        return float(2**attempt)
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())
