"""Measure the production Triage on the golden set.

    python -m evals.run [--stage classify|revise|titles|all] [--model SLUG]
                        [--limit N]

Builds the real `Triage` over a cached `DecisionClient`, prints a table and
writes `evals/data/reports/<YYYY-MM-DD>-<model>.json`. Answers are cached in
`evals/data/cache/`, so re-running a stage costs nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import date
from pathlib import Path
from typing import Any

from backlog_tamer.config import get_settings
from backlog_tamer.domain import IncomingContext, ProjectDraft, SourceLink
from backlog_tamer.triage import DecisionClient, Triage
from backlog_tamer.triage.client import Usage

from . import metrics
from .caching import CachingClient, UsageLog
from .cases import RevisionCase, revision_cases
from .golden import DATA, GoldenItem, load_golden

STAGES = ("classify", "revise", "titles")
CACHE_DIR = DATA / "cache"
REPORTS_DIR = DATA / "reports"
CONCURRENCY = 4
CLASSIFIED_FIELDS = ("project_type", "intent", "priority")
TITLE_RUBRIC = (
    "A good backlog title is 4-10 words, keeps a recognizable proper name, says "
    "what the thing is and why it is worth the user's time, is specific and "
    "accurate to the page, and is not a bare repo/page name or hype."
)

type Measured[T] = tuple[T | None, list[Usage]]


async def evaluate(
    items: list[GoldenItem],
    client: CachingClient,
    usage: UsageLog,
    *,
    stages: set[str],
) -> dict[str, Any]:
    """The report for `stages` ("all" for every stage) over `items`."""
    wanted = set(STAGES) if "all" in stages else stages
    vocabulary = golden_vocabulary(items)
    triage = Triage(client)  # CachingClient has the same decide/title
    gate = asyncio.Semaphore(CONCURRENCY)
    report: dict[str, Any] = {
        "model": client.decision_model,
        "title_model": client.title_model,
        "items": len(items),
        "vocabulary": len(vocabulary),
    }

    drafts: list[Measured[ProjectDraft]] = []
    if wanted & {"classify", "titles"}:
        drafts = await asyncio.gather(
            *(
                _measure(
                    usage,
                    gate,
                    f"classify {item.id}",
                    lambda item=item: triage.classify(
                        _context(item), item.grounding, vocabulary
                    ),
                )
                for item in items
            )
        )
    if "classify" in wanted:
        report["classify"] = _classify_report(items, drafts)
    if "revise" in wanted:
        report["revise"] = await _revise_report(items, triage, usage, gate, vocabulary)
    if "titles" in wanted:
        report["titles"] = await _titles_report(items, drafts, client, usage, gate)
    return report


def golden_vocabulary(items: list[GoldenItem]) -> list[str]:
    """Every approved tag in the golden set, in first-seen order."""
    return list(dict.fromkeys(tag for item in items for tag in item.gold.topics))


def _classify_report(
    items: list[GoldenItem], drafts: list[Measured[ProjectDraft]]
) -> dict[str, Any]:
    done = [
        (item, d, calls) for item, (d, calls) in zip(items, drafts, strict=True) if d
    ]
    return {
        "n": len(done),
        "errors": len(items) - len(done),
        "accuracy": {
            field: metrics.accuracy(
                [getattr(d, field) for _, d, _ in done],
                [getattr(item.gold, field) for item, _, _ in done],
            )
            for field in CLASSIFIED_FIELDS
        },
        "tags": metrics.tag_scores(
            [d.topics for _, d, _ in done], [item.gold.topics for item, _, _ in done]
        ),
        "latency": metrics.latency([metrics.classify_seconds(c) for *_, c in done]),
        "cost_per_1000": metrics.cost_per_1000([c for *_, c in done]),
    }


async def _revise_report(items, triage, usage, gate, vocabulary) -> dict[str, Any]:
    by_id = {item.id: item for item in items}
    cases = revision_cases(items, vocabulary)

    async def revise(case: RevisionCase) -> ProjectDraft:
        item = by_id[case.item_id]
        return await triage.revise(
            _context(item), item.grounding, case.draft, case.feedback, vocabulary
        )

    results = await asyncio.gather(
        *(
            _measure(usage, gate, f"revise {c.item_id}", lambda c=c: revise(c))
            for c in cases
        )
    )
    done = [(c, d, calls) for c, (d, calls) in zip(cases, results, strict=True) if d]
    outcomes = [
        (c.field, *metrics.revision_outcome(c.field, c.target, c.draft, d))
        for c, d, _ in done
    ]
    asked = Counter(field for field, _, _ in outcomes)
    applied = Counter(field for field, ok, _ in outcomes if ok)
    return {
        "n": len(done),
        "errors": len(cases) - len(done),
        "applied": sum(ok for _, ok, _ in outcomes) / len(done) if done else 0.0,
        "collateral": sum(bad for *_, bad in outcomes) / len(done) if done else 0.0,
        "applied_by_field": {f: f"{applied[f]}/{asked[f]}" for f in asked},
        "latency": metrics.latency([metrics.revise_seconds(c) for *_, c in done]),
        "cost_per_1000": metrics.cost_per_1000([c for *_, c in done]),
    }


async def _titles_report(items, drafts, client, usage, gate) -> dict[str, Any]:
    pairs = [(item, d) for item, (d, _) in zip(items, drafts, strict=True) if d]
    results = await asyncio.gather(
        *(
            _measure(
                usage,
                gate,
                f"judge {item.id}",
                lambda item=item, d=d: judge_title(client, item, d.project_name),
            )
            for item, d in pairs
        )
    )
    winners = [winner for winner, _ in results if winner]
    return {
        "n": len(winners),
        "errors": len(items) - len(winners),
        **metrics.win_rate(winners),
    }


async def judge_title(client: CachingClient, item: GoldenItem, new: str) -> str:
    """ "new", "gold" or "tie": the decision model's pick between the new title
    and the approved one, shown in an order seeded by the item."""
    flip = random.Random(item.id).random() < 0.5
    first, second = (
        (item.gold.project_name, new) if flip else (new, item.gold.project_name)
    )
    state = {"page": _judge_page(item), "title_1": first, "title_2": second}
    questions = {
        "better": {
            "type": "choice",
            "instructions": "Which title is the better backlog title for this page? "
            + TITLE_RUBRIC,
            "criteria": {
                "title_1": "title_1 is better",
                "title_2": "title_2 is better",
                "tie": "They are equally good",
            },
        }
    }
    answers = await client.decide(state, questions)
    pick = (answers.get("better") or {}).get("choice")
    if pick not in ("title_1", "title_2"):
        return "tie"
    return "gold" if (pick == "title_1") == flip else "new"


def _judge_page(item: GoldenItem) -> dict[str, Any]:
    g = item.grounding
    page = {
        "url": item.url,
        "title": g.page_title,
        "site_name": g.site_name,
        "description": g.description,
        "key_points": g.key_points,
    }
    return {k: v for k, v in page.items() if v}


def _context(item: GoldenItem) -> IncomingContext:
    return IncomingContext(
        raw_text=item.note or item.url,
        note=item.note,
        links=[SourceLink(url=item.url)],
    )


async def _measure[T](
    usage: UsageLog,
    gate: asyncio.Semaphore,
    label: str,
    job: Callable[[], Awaitable[T]],
) -> Measured[T]:
    """The job's result and its calls; a failure is printed and counted."""
    async with gate:
        with usage.capture() as calls:
            try:
                return await job(), calls
            except Exception as exc:  # keep going; the report counts errors
                print(f"  ! {label}: {exc!r}")
                return None, calls


def print_table(report: dict[str, Any]) -> None:
    print(f"model {report['model']}  title {report['title_model']}")
    print(f"items {report['items']}  vocabulary {report['vocabulary']}")
    if c := report.get("classify"):
        acc, tags, lat = c["accuracy"], c["tags"], c["latency"]
        print(
            f"classify  n={c['n']} errors={c['errors']}  "
            f"type {acc['project_type']:.1%}  intent {acc['intent']:.1%}  "
            f"priority {acc['priority']:.1%}  "
            f"tags P/R/F1 {tags['precision']:.2f}/{tags['recall']:.2f}/"
            f"{tags['f1']:.2f}  "
            f"p50 {lat['p50']:.2f}s p95 {lat['p95']:.2f}s  "
            f"${c['cost_per_1000']:.2f}/1k"
        )
    if r := report.get("revise"):
        lat = r["latency"]
        by_field = " ".join(f"{f}={v}" for f, v in r["applied_by_field"].items())
        print(
            f"revise    n={r['n']} errors={r['errors']}  "
            f"applied {r['applied']:.1%}  collateral {r['collateral']:.1%}  "
            f"p50 {lat['p50']:.2f}s p95 {lat['p95']:.2f}s  "
            f"${r['cost_per_1000']:.2f}/1k  [{by_field}]"
        )
    if t := report.get("titles"):
        print(
            f"titles    n={t['n']} errors={t['errors']}  "
            f"{t['wins']} wins / {t['losses']} losses / {t['ties']} ties  "
            f"win rate {t['win_rate']:.1%}"
        )


def report_path(model: str, today: date | None = None) -> Path:
    slug = model.replace("/", "_").replace(":", "_")
    return REPORTS_DIR / f"{(today or date.today()):%Y-%m-%d}-{slug}.json"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--stage", choices=[*STAGES, "all"], default="all")
    parser.add_argument("--model", help="decision model slug (default: settings)")
    parser.add_argument("--limit", type=int, help="only the first N golden items")
    args = parser.parse_args()

    settings = get_settings()
    usage = UsageLog()
    inner = DecisionClient(
        settings.openrouter_api_key.get_secret_value(),
        decision_model=args.model or settings.triage.decision_model,
        title_model=settings.triage.title_model,
        on_usage=usage,
    )
    client = CachingClient(inner, CACHE_DIR, usage=usage)
    items = load_golden()[: args.limit]
    report = asyncio.run(evaluate(items, client, usage, stages={args.stage}))

    print_table(report)
    path = report_path(client.decision_model)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=1, ensure_ascii=False))
    print(f"report {path}")


if __name__ == "__main__":
    main()
