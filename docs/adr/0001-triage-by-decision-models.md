---
status: accepted
---

# Triage by decision models, not a drafting agent

Triage used to be a Google ADK drafting agent: 15–22 s per Intake, prose nobody used (a long summary, Tasks), and Revisions that rewrote fields the feedback never mentioned. We replaced it with decision models answering typed questions over a fixed **Label set**, plus one small LLM call for the title, because that matched the agent's quality at a fraction of the latency and stopped collateral edits. There is no engine switch and no fallback to the agent.

## Considered options

Evaluated on 52 Notion Projects you had already approved (n = 52, so one item is about 1.9 points; differences under ~5 points are noise). Fetch time is excluded.

| Option | Type | Intent | Priority | Tag F1 | $/1k Intakes | p50 |
|---|---|---|---|---|---|---|
| Drafting agent (gpt-6-luna) | 78.8% | 78.8% | 55.8% | 0.44 | $0.32 | 3.3 s |
| Jev 1.14, tuned | 84.6% | 73.1% | 53.8% | 0.32 | $0.42 | 0.24 s |
| **Decider v1.3, tuned, two-stage** | 78.8% | 78.8% | 61.5%¹ | 0.36 | $0.19 | 1.1 s |
| Decider Flash v1.3, tuned, two-stage | 78.8% | 73.1% | 53.8% | 0.29 | $0.09 | 0.5 s |

¹ With priority defaulting to Medium unless a note or Revision speaks to it.

Revision with a reviewer comment:

| Option | Comment applied | Unrelated field changed |
|---|---|---|
| Drafting agent | 94.2% | 34.6% |
| **Decider v1.3, route then re-decide** | 94.2% | 9.6% |
| Jev 1.14, route then re-decide | 86.5% | 3.8% |

A title-only call (gpt-6-luna, no reasoning) beat the agent's titles 29–20 with 3 ties.

Decider v1.3 won: it tied the agent on type and intent, kept Revisions to the fields they were about, and was cheaper. Jev had the best type accuracy but weaker intent and Revision.

The production pipeline, run by `mise run eval` without past Projects as examples, scored type 78.8%, intent 75.0%, priority 61.5%, tag F1 0.29 (P 0.22 / R 0.44), Revision applied 89.4% with 0.0% collateral changes, titles preferred over the approved Notion title 86.5%, p50 1.0 s, about $0.21 per 1,000 Intakes. Tag F1 is lower than the 0.36 above because that run included labelled examples; without them the prototype also scored 0.27–0.30. Tags remain the weakest field.

## Decisions that come with it

- **No Tasks.** A Draft is a Project only; you create Tasks yourself.
- **Priority defaults to Medium** and is decided only when a note or Revision says something about it.
- **Quick edits win over a Revision.** They last for the whole Review and are re-applied after every Revision and refetch.
- **A Revision is routed first:** only the fields it is about are decided again. The title is rewritten only when the name is routed.
- **LangSmith is dropped.** Each decision call logs its latency, tokens and confidence instead.
- **Only the first link of a message is read,** matching how items are captured.

## Consequences

- **The free model is temporary.** `perplexity/pplx-decider-v1.3:free` ends around mid-December 2026. The deploy healthcheck probes both models, so a dead slug fails the deploy; switching is one setting (`TRIAGE__DECISION_MODEL`), and `mise run eval -- --model <slug>` measures a replacement first.
- **Rolling back to v0.8.0 strands new Reviews.** Reviews created after the cutover cannot be revised or approved by v0.8.0, which resumes agent sessions they never had. Older Reviews keep working. Accepted for a one-user bot; the legacy columns and secrets stay until the rollback window closes.
