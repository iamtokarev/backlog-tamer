"""Turn the agent's default task into one that names its subject.

Every backlog task the agent produced was called either "Read" or "Explore",
so the Tasks database read as a wall of identical rows. The verb is a
mechanical function of intent and project type, so a table decides it here
rather than an instruction the model reinterprets each run — last week the
same rule produced "Explore" for one course and "Read" for another.
"""

from __future__ import annotations

from backlog_tamer.agents.intake_triage.schemas import ProjectDraft

# Intent says what the user wants to do with the item, so it picks the verb.
INTENT_VERBS = {
    "learn": "Read",
    "explore": "Explore",
    "build": "Build",
    "research": "Research",
    "reference": "Skim and file",
    "unclear": "Explore",
}

# ...except where the medium makes the intent verb read wrong.
PROJECT_TYPE_VERBS = {
    "course": "Work through",
    "video": "Watch",
}

DEFAULT_VERB = "Explore"

# A single task carrying one of these and nothing else is the agent's
# generated default, so it is safe to rewrite. Anything else was authored
# deliberately — by the model on an explicit breakdown, or by the user asking
# for a rename — and is left alone.
_BARE_VERBS = {verb.casefold() for verb in INTENT_VERBS.values()} | {
    verb.casefold() for verb in PROJECT_TYPE_VERBS.values()
}


def task_verb(draft: ProjectDraft) -> str:
    """The verb that fits this draft."""
    override = PROJECT_TYPE_VERBS.get(draft.project_type)
    if override is not None:
        return override
    return INTENT_VERBS.get(draft.intent, DEFAULT_VERB)


def compose_task_names(draft: ProjectDraft) -> list[str]:
    """Name the draft's default task after its subject.

    Idempotent: a name that already reads "<verb>: <subject>" is returned
    unchanged, so replaying this over a revised draft does not stack prefixes.
    """
    if len(draft.tasks) != 1:
        return list(draft.tasks)

    existing = draft.tasks[0].strip()
    if existing.casefold() not in _BARE_VERBS:
        return [existing] if existing else list(draft.tasks)

    subject = draft.effective_short_name
    if not subject:
        return [existing]

    return [f"{task_verb(draft)}: {subject}"]


def with_composed_task_names(draft: ProjectDraft) -> ProjectDraft:
    """A copy of the draft whose default task names its subject."""
    composed = compose_task_names(draft)
    if composed == draft.tasks:
        return draft
    return draft.model_copy(update={"tasks": composed})
