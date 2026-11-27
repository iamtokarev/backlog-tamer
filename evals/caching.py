"""A disk cache in front of the decision client, so stages re-run for free.

An entry keeps the answer and the usage the first call reported. A run served
from cache therefore still reports the original call's cost and latency, and
a report is the same whether it was measured live or replayed.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict
from pathlib import Path
from typing import Any

from backlog_tamer.triage.client import DecisionClient, Usage


class UsageLog:
    """The `on_usage` callback for the eval: records into every open capture.

    Captures nest and follow asyncio tasks (they live in a ContextVar), so an
    Intake's capture sees its own calls even while other Intakes run.
    """

    def __init__(self) -> None:
        self._open: ContextVar[tuple[list[Usage], ...]] = ContextVar(
            "usage_captures", default=()
        )

    def __call__(self, usage: Usage) -> None:
        for capture in self._open.get():
            capture.append(usage)

    @contextmanager
    def capture(self) -> Iterator[list[Usage]]:
        seen: list[Usage] = []
        token = self._open.set((*self._open.get(), seen))
        try:
            yield seen
        finally:
            self._open.reset(token)


class CachingClient:
    """Same `decide` and `title` as DecisionClient, answered from disk if seen."""

    def __init__(self, inner: DecisionClient, cache_dir: Path, *, usage: UsageLog):
        self._inner = inner
        self._dir = Path(cache_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._usage = usage
        self.decision_model = inner.decision_model
        self.title_model = inner.title_model

    async def decide(self, state: Any, questions: dict[str, Any]) -> dict[str, Any]:
        body = {"state": state, "questions": questions}
        return await self._cached(
            self.decision_model,
            "decide",
            body,
            lambda: self._inner.decide(state, questions),
        )

    async def title(
        self,
        page: dict[str, Any],
        *,
        feedback: str | None = None,
        current: str | None = None,
    ) -> str:
        body = {"page": page, "feedback": feedback, "current": current}
        return await self._cached(
            self.title_model,
            "title",
            body,
            lambda: self._inner.title(page, feedback=feedback, current=current),
        )

    async def _cached(self, model, kind, body, call):
        path = self._dir / f"{cache_key(model, kind, body)}.json"
        if path.exists():
            entry = json.loads(path.read_text())
            for usage in entry["usage"]:
                self._usage(Usage(**usage))
            return entry["answer"]
        with self._usage.capture() as seen:
            answer = await call()
        entry = {"answer": answer, "usage": [asdict(u) for u in seen]}
        path.write_text(json.dumps(entry, ensure_ascii=False))
        return answer


def cache_key(model: str, kind: str, body: Any) -> str:
    canonical = json.dumps(
        {"model": model, "kind": kind, "body": body},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()
