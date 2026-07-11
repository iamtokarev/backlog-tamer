from backlog_tamer.agents.intake_triage.schemas import IncomingContext, ProjectDraft

INTAKE_TRIAGE_INSTRUCTIONS = """
You produce a structured ProjectDraft for captured learning items.

You may receive either:
1. a new captured item that needs an initial draft, or
2. a revision request for an existing draft.

Always return a valid ProjectDraft grounded only in the provided input and
tool results.

Rules:
- If a URL is present and more context is needed, call `fetch_url`.
- Use `fetch_url` to understand the page, not to copy it.
- Infer the best possible project_name, summary, resource_type, intent,
  priority, source_url, and tasks.
- resource_type describes what the item is.
- intent describes what the user likely wants to do with it.
- Use intent "reference" when the item should be kept mainly for lookup.
- Use intent "explore" when the next step is lightweight investigation.
- Use intent "research" when the item needs deeper analysis or comparison.
- Use intent "build" when the item should become implementation work.
- Use intent "learn" when the item is mainly study material.
- Choose priority based on likely urgency and usefulness. Default to
  "Medium" when unclear.
- Default to exactly one task, named by intent: use "Read" for articles,
  papers, and other study material; use "Explore" for repositories and
  tools.
- Only produce multiple tasks (up to 5) when the user's note explicitly
  asks for a breakdown into steps. Prefer short actionable task names.
- In revision mode, treat the current ProjectDraft as the baseline.
- In revision mode, preserve fields that are already correct.
- In revision mode, apply the latest feedback directly and concretely.
- Prefer clear, practical classifications over nuanced ones.
- If the item is ambiguous, choose the most reasonable project draft.
- Do not invent facts that are not present in the input or tool results.
- If `fetch_url` fails, rely on the raw URL and user note instead of
  inventing page details.
- If revision context already includes fetched results, use that context and
  only refetch if more grounding is genuinely needed.
- Keep summary brief and useful.
- If a source URL is available, include it.
""".strip()

INITIAL_TRIAGE_PROMPT_TEMPLATE = """
<task>
Create a new ProjectDraft for this captured learning item.
</task>

<intake>
raw_text: {raw_text}
note: {note}
links:
{links}
</intake>

<requirements>
- infer project_name, summary, resource_type, intent, priority, source_url, and tasks
- use fetch_url if the links are useful for grounding
- keep the result concise and practical
</requirements>
""".strip()

REVIEW_MESSAGE_TEMPLATE = """
Review the proposed draft:

{draft_snapshot}

Reply with one of:
- approve
- reject
- free-form revision feedback
""".strip()

REVIEW_DRAFT_SNAPSHOT_TEMPLATE = """
project_name: {project_name}
summary: {summary}
resource_type: {resource_type}
intent: {intent}
priority: {priority}
source_url: {source_url}
tasks:
{tasks}{fetched_block}
""".strip()

FETCHED_CONTEXT_BLOCK_TEMPLATE = """

Relevant fetched context:
{fetched_summary}
""".rstrip()

FETCHED_CONTEXT_ITEM_TEMPLATE = """- url: {url}
  title: {title}
  description: {description}
  key_points: {key_points}"""


REVISION_PROMPT_TEMPLATE = """
<task>
Revise the existing ProjectDraft using the latest human feedback.
</task>

<latest_feedback>
{latest_feedback}
</latest_feedback>

<current_draft>
{current_draft_snapshot}
</current_draft>

<original_intake>
{original_intake}
</original_intake>

<previous_review_feedback>
{prior_feedback}
</previous_review_feedback>

<revision_rules>
- preserve fields that are already correct
- change only what the latest feedback requires
- stay grounded in the original intake and fetched context already provided
- do not restart from scratch unless the feedback clearly requires it
</revision_rules>
""".strip()


def build_revision_prompt(
    *,
    draft_snapshot: str = "",
    triage_input: str = "",
    review_feedback: str = "",
    review_history: list[str] | None = None,
) -> str:
    original_intake = triage_input or "none"
    history = review_history or []
    prior_feedback = "\n".join(f"- {item}" for item in history[:-1]) or "none"
    current_draft_snapshot = draft_snapshot or "none"
    latest_feedback = review_feedback or "none"

    return REVISION_PROMPT_TEMPLATE.format(
        latest_feedback=latest_feedback,
        current_draft_snapshot=current_draft_snapshot,
        original_intake=original_intake,
        prior_feedback=prior_feedback,
    )


def build_triage_prompt(context: IncomingContext) -> str:
    note = context.note or "none"
    links = _format_links(context)
    return INITIAL_TRIAGE_PROMPT_TEMPLATE.format(
        raw_text=context.raw_text,
        note=note,
        links=links,
    )


def build_review_message(*, draft_snapshot: str) -> str:
    return REVIEW_MESSAGE_TEMPLATE.format(draft_snapshot=draft_snapshot)


def build_fetched_context_summary(
    fetched_context: dict[str, object] | None,
) -> str:
    if not fetched_context:
        return "none"

    sections: list[str] = []
    for index, (url, payload) in enumerate(fetched_context.items(), start=1):
        if index > 3:
            sections.append("- additional fetched results omitted for brevity")
            break

        if not isinstance(payload, dict):
            sections.append(f"- url: {url}")
            continue

        title = payload.get("title") or "none"
        description = payload.get("description") or "none"
        key_points = payload.get("key_points") or []
        key_points_text = (
            "; ".join(str(point) for point in key_points[:3]) if key_points else "none"
        )
        sections.append(
            FETCHED_CONTEXT_ITEM_TEMPLATE.format(
                url=url,
                title=title,
                description=description,
                key_points=key_points_text,
            )
        )

    return "\n".join(sections)


def build_review_draft_snapshot(
    *,
    project_name: str,
    summary: str,
    resource_type: str,
    intent: str,
    priority: str,
    source_url: str,
    tasks: list[str],
    fetched_summary: str,
) -> str:
    fetched_block = ""
    if fetched_summary != "none":
        fetched_block = FETCHED_CONTEXT_BLOCK_TEMPLATE.format(
            fetched_summary=fetched_summary
        )

    tasks_text = "\n".join(f"- {task}" for task in tasks) or "- none"
    return REVIEW_DRAFT_SNAPSHOT_TEMPLATE.format(
        project_name=project_name,
        summary=summary,
        resource_type=resource_type,
        intent=intent,
        priority=priority,
        source_url=source_url,
        tasks=tasks_text,
        fetched_block=fetched_block,
    )


def build_review_snapshot(
    draft: ProjectDraft,
    fetched_context: dict[str, object] | None = None,
) -> str:
    source_url = draft.source_url or "none"
    fetched_summary = build_fetched_context_summary(fetched_context)
    return build_review_draft_snapshot(
        project_name=draft.project_name,
        summary=draft.summary,
        resource_type=draft.resource_type,
        intent=draft.intent,
        priority=draft.priority,
        source_url=source_url,
        tasks=draft.tasks,
        fetched_summary=fetched_summary,
    )


def _format_links(context: IncomingContext) -> str:
    if not context.links:
        return "- none"

    return "\n".join(f"- {link.url}" for link in context.links)
