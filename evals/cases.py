"""Revision cases: a correct Draft with one field set wrong, and a comment.

Each case starts from an item's approved labels, sets one routed field to a
wrong value, and gives the reviewer comment that asks for the approved value
back. Half the comments are terse (`intent: build`), half indirect ("I
actually want to build something with this"). A title case asks for an exact
rename; a topics case names the approved tags.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

from backlog_tamer.domain import ProjectDraft, fold_tag
from backlog_tamer.triage.questions import INTENTS, PRIORITY_LEVELS, PROJECT_TYPES

from .golden import GoldenItem

FEEDBACK: dict[str, dict[str, list[str]]] = {
    "project_type": {
        "course": [
            "It's a course, I'm going to work through it.",
            "type should be course",
        ],
        "repository": ["This is a GitHub repo, not an article.", "type: repository"],
        "paper": ["It's a research paper.", "this is a paper"],
        "article": ["It's just a blog post to read.", "type should be article"],
        "video": ["It's a recorded talk, so video.", "type = video"],
        "product": [
            "The thing here is the product, not the article about it.",
            "type product",
        ],
        "tool": ["It's a dev tool I'd use.", "type should be tool"],
        "model": ["It's a model release.", "type: model"],
    },
    "intent": {
        "learn": ["I want to study this properly.", "intent should be learn"],
        "explore": ["Just want to poke at it quickly.", "intent: explore"],
        "build": ["I actually want to build something with this.", "intent build"],
        "research": [
            "This needs a proper deep dive and comparison.",
            "intent should be research",
        ],
        "reference": ["Only keeping it to look things up later.", "intent: reference"],
    },
    "priority": {
        "High": [
            "This is urgent, I need it for what I'm building right now.",
            "priority high",
        ],
        "Medium": ["Not urgent but fairly relevant.", "make it medium priority"],
        "Low": ["Not important, someday maybe.", "priority low"],
    },
}
TITLE_FEEDBACK = 'Rename it to "{title}"'
TITLE_PLACEHOLDER = "Interesting link to check out"
TOPIC_FEEDBACK = [
    "It's really about {tags}, not those tags.",
    "tags: {tags}",
]
FIELD_VALUES: dict[str, list[str]] = {
    "project_type": list(PROJECT_TYPES),
    "intent": [i for i in INTENTS if i != "unclear"],
    "priority": list(PRIORITY_LEVELS),
}
FIELDS = ("project_type", "intent", "priority", "project_name", "topics")
SEED = 7


@dataclass(frozen=True)
class RevisionCase:
    item_id: str
    field: str
    draft: ProjectDraft
    feedback: str
    target: Any


def revision_cases(
    items: list[GoldenItem], vocabulary: list[str]
) -> list[RevisionCase]:
    """One case per item, cycling through the routed fields.

    Items whose approved value has no feedback template (a `company`, empty
    tags) are skipped, so every case has an unambiguous expected value.
    """
    rng = random.Random(SEED)
    cases = []
    for index, item in enumerate(items):
        field = FIELDS[index % len(FIELDS)]
        gold = item.gold_draft()
        phrasing = index % 2
        if field == "project_name":
            target: Any = gold.project_name
            wrong: Any = TITLE_PLACEHOLDER
            feedback = TITLE_FEEDBACK.format(title=target)
        elif field == "topics":
            target = gold.topics
            wanted = {fold_tag(t) for t in target}
            others = [t for t in vocabulary if fold_tag(t) not in wanted]
            if not target or len(others) < 2:
                continue
            wrong = rng.sample(others, 2)
            tags = ", ".join(target)
            feedback = TOPIC_FEEDBACK[phrasing].format(tags=tags)
        else:
            target = getattr(gold, field)
            if target not in FEEDBACK[field]:
                continue
            wrong = rng.choice([v for v in FIELD_VALUES[field] if v != target])
            feedback = FEEDBACK[field][target][phrasing]
        cases.append(
            RevisionCase(
                item_id=item.id,
                field=field,
                draft=gold.model_copy(update={field: wrong}),
                feedback=feedback,
                target=target,
            )
        )
    return cases
