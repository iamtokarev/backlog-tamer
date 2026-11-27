"""Pure scoring functions; every number in a report comes from here."""

from __future__ import annotations

import statistics
from collections import Counter
from collections.abc import Sequence
from typing import Any

from backlog_tamer.domain import ProjectDraft, fold_tag
from backlog_tamer.triage.client import Usage

# The fields a Revision can route; a change to any but the asked one is collateral.
REVISED_FIELDS = ("project_type", "intent", "priority", "project_name", "topics")
DECISIONS_PATH = "/alpha/decisions"
# `:free` variants report no cost; price them at the paid list price of the
# same model so a free window does not make a model look costless.
LIST_PRICE_PER_INPUT_TOKEN = {
    "perplexity/pplx-decider-v1.3:free": 0.02e-6,
    "perplexity/pplx-decider-flash-v1.3:free": 0.01e-6,
}


def accuracy(predicted: Sequence[Any], gold: Sequence[Any]) -> float:
    pairs = list(zip(predicted, gold, strict=True))
    return sum(p == g for p, g in pairs) / len(pairs) if pairs else 0.0


def percentile(values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile; 0.0 for no values."""
    ordered = sorted(values)
    if not ordered:
        return 0.0
    return ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))]


def latency(seconds: Sequence[float]) -> dict[str, float]:
    return {"p50": percentile(seconds, 0.5), "p95": percentile(seconds, 0.95)}


def call_cost(usage: Usage) -> float:
    if usage.model in LIST_PRICE_PER_INPUT_TOKEN:
        return (usage.input_tokens or 0) * LIST_PRICE_PER_INPUT_TOKEN[usage.model]
    return usage.cost or 0.0


def cost_per_1000(per_intake: Sequence[Sequence[Usage]]) -> float:
    """Dollars for 1,000 Intakes, from each Intake's calls."""
    costs = [sum(call_cost(u) for u in calls) for calls in per_intake]
    return 1000 * statistics.mean(costs) if costs else 0.0


def classify_seconds(calls: Sequence[Usage]) -> float:
    """Classify's model time: the two decision requests run in sequence while
    the title call runs beside them."""
    decisions = sum(u.seconds for u in calls if u.path == DECISIONS_PATH)
    titles = sum(u.seconds for u in calls if u.path != DECISIONS_PATH)
    return max(decisions, titles)


def revise_seconds(calls: Sequence[Usage]) -> float:
    """A Revision's model time: the decision request, then the title if routed."""
    return sum(u.seconds for u in calls)


def win_rate(winners: Sequence[str]) -> dict[str, Any]:
    """Wins of the new title over the approved one; ties count as no win."""
    counts = Counter(winners)
    return {
        "wins": counts["new"],
        "losses": counts["gold"],
        "ties": counts["tie"],
        "win_rate": counts["new"] / len(winners) if winners else 0.0,
    }


def revision_outcome(
    field: str, target: Any, before: ProjectDraft, after: ProjectDraft
) -> tuple[bool, bool]:
    """(applied, collateral) for one Revision case.

    Applied: a title comes back as the exact rename, topics hold every tag the
    feedback asked for, any other field equals the target. Collateral: some
    other field changed; tags compare as folded sets, so order and spelling
    variants are not a change.
    """
    applied = _matches(field, target, getattr(after, field))
    collateral = any(
        _value(other, getattr(before, other)) != _value(other, getattr(after, other))
        for other in REVISED_FIELDS
        if other != field
    )
    return applied, collateral


def _matches(field: str, target: Any, value: Any) -> bool:
    if field == "project_name":
        return value.strip().strip("\"'") == target
    if field == "topics":
        return _value(field, target) <= _value(field, value)
    return value == target


def _value(field: str, value: Any) -> Any:
    return frozenset(fold_tag(t) for t in value) if field == "topics" else value


def tag_scores(
    predicted: Sequence[Sequence[str]], gold: Sequence[Sequence[str]]
) -> dict[str, float]:
    """Micro-averaged precision, recall and F1 over folded tags."""
    hits = n_predicted = n_gold = 0
    for pred, want in zip(predicted, gold, strict=True):
        p, g = {fold_tag(t) for t in pred}, {fold_tag(t) for t in want}
        hits += len(p & g)
        n_predicted += len(p)
        n_gold += len(g)
    return {
        "precision": hits / n_predicted if n_predicted else 0.0,
        "recall": hits / n_gold if n_gold else 0.0,
        "f1": 2 * hits / (n_predicted + n_gold) if n_predicted + n_gold else 0.0,
    }
