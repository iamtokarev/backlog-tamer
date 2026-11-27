# Evals

Numbers for the production `Triage` before a model swap or a threshold change
ships. The pipeline runs the real `backlog_tamer.triage.Triage` against
Projects you already approved in Notion. It lives outside `src/` and is not
in the Lambda image.

Everything under `evals/data/` is gitignored, because the golden set is your
own Notion data and the repo is public. That covers the golden set, the tag
vocabulary, the answer cache and the reports.

## Commands

```sh
mise run eval:golden                  # build evals/data/golden.jsonl
mise run eval:golden -- --refresh     # also refetch sources already read
mise run eval                         # every stage, default decision model
mise run eval -- --stage revise --model typesafe/jev-1.14 --limit 5
```

They need a `.env` with `OPENROUTER_API_KEY`. `eval:golden` also needs
`NOTION_TOKEN` and `BROWSERBASE_API_KEY`. `--model` swaps the decision model
only; the title model comes from `TRIAGE__TITLE_MODEL`.

## The golden set

`python -m evals.golden` exports the Projects database. It keeps every row
that has a Source, Project type, Intent and Priority, and reads each Source
once with the app's `SourceReader`. Each line of `golden.jsonl` looks like
this:

```json
{"id": "...", "url": "...", "note": null,
 "gold": {"project_name": "...", "project_type": "...", "intent": "...",
          "priority": "...", "topics": ["..."]},
 "grounding": {"fetch_status": "success", "page_title": "...", "...": "..."}}
```

`topics` are the Notion Tags minus the legacy type and intent values. Every
run sees the same Grounding, so fetch time never counts toward latency.

The export also writes `vocabulary.json`, every Tags option of the Projects
database in Notion's order (`NotionWriter.list_tag_options()`). That is the
vocabulary production offers Triage, so the eval offers the same one; the
tags that happen to appear in the golden set are fewer and would make
tagging look easier. `mise run eval` stops with a pointer to
`mise run eval:golden` when the file is missing.
Notes are not kept in Notion, so `note` is empty and the priority question,
which Triage asks only when there is a note, falls back to Medium.

## Stages

**classify**: `Triage.classify` on every item, compared with the approved
labels.

- `accuracy` per field: project type, intent and priority. Priority mostly
  measures the Medium default, because there are no notes.
- `tags`: micro precision, recall and F1. Tags are compared after
  `domain.fold_tag`, so `llm-agents` matches `llm agent`.
- `latency` p50 and p95 of model time per Intake: the two decision requests
  run in sequence, while the title call runs beside them.
- `cost_per_1000` Intakes, in dollars. Free (`:free`) decision models are
  priced at the paid list price of the same model.

**revise**: `Triage.revise` on synthetic Revision cases (`cases.py`). Each
case takes an approved Draft, sets one field wrong and adds a comment that
asks for the approved value. The fields rotate through type, intent,
priority, title (an exact rename) and topics (the approved tags named). Half
the comments are terse and half are indirect.

- `applied`: the share of cases where the asked field ended at the expected
  value. For topics, every named tag has to be present.
- `collateral`: the share of cases where any other field changed. Tag order
  and spelling variants do not count as changes.
- `applied_by_field` breaks `applied` down per field. Check it first when
  `applied` drops.

**titles**: the decision model judges each new title against your approved
Notion title, in an order seeded by the item. `win_rate` is the new title's
wins over all judged items; ties count as no win. The judge is itself a
decision model, so treat this as a coarse signal and spot-check titles.

## Reading the report

Each run prints one line per stage and writes
`evals/data/reports/<YYYY-MM-DD>-<model>.json`. `n` counts the items scored,
and `errors` counts the calls that failed (they are printed with `!` and left
out of the numbers). With about 52 items, one item is about 2 points, so
treat differences under 5 points as noise.

Reference numbers from the prototype run (`perplexity/pplx-decider-v1.3`,
tuned, two-stage): type 78.8%, intent 78.8%, tag F1 0.36, revision applied
94%, collateral 10%. The prototype's revision cases had no topics field, so
its revision numbers are not exactly comparable.

## Cost and time

A full run is about 52 classify Intakes, about 47 Revision cases and 52
title judgements, roughly 250 model calls. It costs a few cents and takes
about a minute at the default concurrency of 4. Answers are cached in
`evals/data/cache/`, keyed by model, call kind and request body. A repeat
run is free and instant, and it reports the cost and latency of the original
calls. Delete the cache to measure again, for example after a provider
change.
