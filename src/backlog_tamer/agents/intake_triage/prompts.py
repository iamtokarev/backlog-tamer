from backlog_tamer.agents.intake_triage.schemas import IncomingContext, ProjectDraft

# The existing tag vocabulary is a hint, not the subject of the prompt.
MAX_KNOWN_TOPICS = 120

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
- Infer the best possible project_name, short_name, summary, project_type,
  intent, priority, source_url, and tasks.
- project_name is the backlog title the user will scan weeks later, so make
  it self-explanatory on its own.
- Never use a bare repository name, package name, product name, domain, or
  page title as project_name.
- Write project_name as a short descriptive phrase of roughly 4 to 10 words
  that says what the thing is and why it is worth the user's time.
- When a proper name is recognizable, keep it and add the payoff after it,
  e.g. "LangGraph: build stateful multi-agent workflows" instead of
  "langgraph".
- When the item has no recognizable name, describe its subject instead,
  e.g. "Vector index tradeoffs for hybrid search" instead of "blog post".
- Make project_name concrete and specific: prefer the actual topic,
  technique, or benefit over generic words like "tool", "guide", or
  "resource".
- Do not pad project_name with marketing hype or claims the source does not
  support.
- short_name is the bare handle the project is known by, 2 to 4 words, with no
  payoff clause: "SKILL.state", "NVIDIA PAIR", "Stanford CS146S". It is the
  part of project_name before the colon.
- When the item has no proper name, make short_name a 2-4 word subject phrase,
  e.g. "Hybrid search tradeoffs".
- project_type describes the thing the user wants to explore, not the webpage,
  document, changelog entry, or URL that introduced it.
- Choose project_type from this compact vocabulary: paper, article, video,
  course, repository, product, company, model, tool.
- Classify the target itself. A Cursor changelog or documentation page that
  introduces Origin is project_type "product", not documentation. A GitHub page
  whose target is an open-source repository is "repository". A research paper is
  "paper" even when it is linked through a publisher or index page.
- Use "paper", "article", "video", or "course" only when the item itself is the
  thing to consume. A Tom's Hardware article about NVIDIA PAIR is "product",
  because the target is PAIR; a standalone essay worth reading on its own is
  "article".
- A course is "course" even when it teaches a specific tool, and even when it
  is hosted by the company that makes that tool.
- intent describes what the user likely wants to do with it.
- Use intent "reference" when the item should be kept mainly for lookup.
- Use intent "explore" when the next step is lightweight investigation.
- Use intent "research" when the item needs deeper analysis or comparison.
- Use intent "build" when the item should become implementation work.
- Use intent "learn" when the item is mainly study material.
- topics are up to 3 lowercase subject tags naming the technology or
  subject, e.g. ["langgraph", "multi-agent", "orchestration"].
- Choose topics the user would search for later, not restatements of
  project_type or intent, and never generic words like "tool" or "guide".
- When a topic already used in the backlog fits, reuse it exactly as spelled
  rather than minting a near-duplicate that differs only by hyphens, case, or
  a plural.
- Use fewer topics, or none, rather than inventing ones the source does
  not support.
- Choose priority deliberately; do not fall back to "Medium" as a default.
- Use priority "High" when the note signals urgency, when the item plugs into
  something the user is actively building, or when it is time-sensitive.
- Use priority "Low" when the item is tangential, long-form with no near-term
  use, or saved mainly for completeness.
- Use priority "Medium" only when the item is genuinely between those two.
- Default to exactly one task, named "<verb>: <short_name>" where the verb
  follows intent: "Read" for learn, "Explore" for explore, "Build" for build,
  "Research" for research, "Skim and file" for reference.
- Use "Work through" instead for a course, and "Watch" instead for a video.
- Example: intent "learn" with short_name "SKILL.state" gives the single task
  "Read: SKILL.state".
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

<existing_topics>
Tags already used in the backlog. Reuse one whenever it fits rather than
minting a near-duplicate; only invent a topic when none of these apply.
{known_topics}
</existing_topics>

<requirements>
- infer project_name, short_name, summary, project_type, intent, priority,
  source_url, and tasks
- make project_name a descriptive 4-10 word phrase, never a bare repo or page name
- make short_name the bare 2-4 word handle, no payoff clause
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
short_name: {short_name}
summary: {summary}
project_type: {project_type}
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


def build_triage_prompt(
    context: IncomingContext,
    known_topics: list[str] | None = None,
) -> str:
    note = context.note or "none"
    links = _format_links(context)
    return INITIAL_TRIAGE_PROMPT_TEMPLATE.format(
        raw_text=context.raw_text,
        note=note,
        links=links,
        known_topics=_format_known_topics(known_topics),
    )


def _format_known_topics(known_topics: list[str] | None) -> str:
    """The existing tag vocabulary, or a marker when it could not be read.

    Kept to a bounded list: the prompt is a hint, and a workspace with
    hundreds of tags should not crowd out the item being classified.
    """
    if not known_topics:
        return "- none known yet"
    return "\n".join(f"- {topic}" for topic in known_topics[:MAX_KNOWN_TOPICS])


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
    short_name: str,
    summary: str,
    project_type: str,
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
        short_name=short_name or "none",
        summary=summary,
        project_type=project_type,
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
        short_name=draft.effective_short_name,
        summary=draft.summary,
        project_type=draft.project_type,
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
