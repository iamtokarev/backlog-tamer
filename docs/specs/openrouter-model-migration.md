# OpenRouter model migration

Status: agreed and implemented; verification results recorded below.
Explored: 2026-10-03. No application code, configuration, dependencies, or runtime secrets changed during exploration.

## Outcome

Route every intake model request through OpenRouter using the existing Google ADK `LiteLlm` adapter. Default to GPT-6 Luna with medium reasoning effort. Changing `AGENT__MODEL` selects another compatible model available through OpenRouter.

Interpret “switch” as replacing the direct OpenAI connection. OpenRouter becomes the sole model gateway. Remove the old credential requirement and routing implementation; do not add a provider toggle, compatibility aliases, or direct OpenAI fallback.

## Original implementation and findings

- [`config.py`](../../src/backlog_tamer/config.py) requires `AgentConfig.openai_api_key`, defaults to `gpt-5.6-luna`, and defaults reasoning effort to `medium`. `Settings.agent` is currently required. Root settings read `.env` with the `__` nested delimiter.
- [`agent.py`](../../src/backlog_tamer/agents/intake_triage/agent.py) is the only model-construction location. It builds `LiteLlm(model=f"openai/{settings.agent.model}", api_key=..., reasoning_effort=...)`. The drafting agent uses `fetch_url`, `output_schema=ProjectDraft`, and `output_key="draft_proposal"`.
- [`workflow.py`](../../src/backlog_tamer/agents/intake_triage/workflow.py) wraps that drafting agent with human review, approve/reject routes, and a revision loop. [`intake_service.py`](../../src/backlog_tamer/application/intake_service.py) lazily imports the root agent and configures LangSmith tracing.
- Polling, local webhook, the standalone development runner, and the deployed worker share this agent module. No independent model clients were found in application source.
- Local `.env` contains a nonempty `OPENROUTER_API_KEY`; no model or reasoning overrides were found there or in the inspected process environment. Key values were not displayed. `.env.example` already has an uncommitted OpenRouter-key addition, which implementation must preserve.
- [`lambda_handlers.py`](../../src/backlog_tamer/integrations/telegram/lambda_handlers.py) loads an arbitrary JSON secret into environment variables before importing the agent. Terraform manages the secret container, not its contents. Its existing loader already accommodates `OPENROUTER_API_KEY`.
- The deployed healthcheck imports the agent and checks extraction dependencies, Notion schema, and installed version. It does **not** make a model request or prove the model credential works.
- The lockfile pins Google ADK 2.4.0, LiteLLM 1.84.10, OpenAI SDK 2.24.0, and pydantic-settings 2.15.0. Application code does not import the OpenAI SDK directly; LiteLLM requires it transitively.

OpenRouter currently lists [`openai/gpt-6-luna`](https://openrouter.ai/openai/gpt-6-luna), including tools, structured outputs, and reasoning support in its [model catalog](https://openrouter.ai/api/v1/models). This verifies model availability and advertised capabilities, not an authenticated end-to-end workflow.

## Design and interface

Keep the seam at the drafting agent's existing `model` slot. ADK's `LiteLlm` adapter already hides transport and request/response conversion behind its model interface. Adding a provider registry or another pass-through module would reduce locality without adding depth.

Keep model construction in `agent.py`. Make the existing private helper accept its dependencies rather than reading global settings internally:

```python
_get_model(config: AgentConfig, api_key: SecretStr) -> LiteLlm
```

The module's composition code loads settings and passes `settings.agent` and `settings.openrouter_api_key`. The helper returns the configured adapter directly. Existing agent/workflow exports and callers remain as they are.

### Configuration contract

| Input | Proposed meaning | Default / requirement |
| --- | --- | --- |
| `OPENROUTER_API_KEY` | Root `Settings.openrouter_api_key: SecretStr` | Required; reject empty or whitespace-only values |
| `AGENT__MODEL` | OpenRouter model slug, including its publisher | `openai/gpt-6-luna` |
| `AGENT__REASONING_EFFORT` | Existing `none`, `low`, `medium`, `high` setting | `medium` |

Remove `AgentConfig.openai_api_key`. Default `Settings.agent` with `Field(default_factory=AgentConfig)` so a nested agent override is no longer needed to instantiate it. Retain the existing `.env` and environment-variable precedence, caching, and tracing configuration.

Model values use OpenRouter slugs, not bare OpenAI names or LiteLLM transport prefixes. Add `openrouter/` exactly once during adapter construction. The key is passed explicitly, unwrapped only at model construction; model authentication does not depend on exporting `.env` contents into `os.environ`.

### Request contract

Proposed constructor shape, shown for agreement only:

```python
LiteLlm(
    model=f"openrouter/{config.model}",
    api_key=api_key.get_secret_value(),
    api_base="https://openrouter.ai/api/v1",
    extra_body={
        "reasoning": {"effort": config.reasoning_effort},
        "provider": {"require_parameters": True},
    },
)
```

Use LiteLLM's [native OpenRouter routing](https://docs.litellm.ai/docs/providers/openrouter). The fixed endpoint belongs to the implementation; it does not add another configuration setting.

Translate the existing reasoning setting to OpenRouter's [native reasoning object](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens). During an offline check, LiteLLM 1.84.10 using bundled metadata rejected the old top-level `reasoning_effort` parameter for this model. Native `reasoning.effort` forwarded successfully. This avoids relying on remotely refreshed model metadata or dropping the setting silently.

Set `provider.require_parameters=true` so routing respects supplied capabilities, as recommended by OpenRouter's [structured-output guidance](https://openrouter.ai/docs/guides/features/structured-outputs). Leave ADK responsible for generating tool declarations and structured-output requests. Do not manually duplicate `ProjectDraft` as a second response schema or enable `drop_params`.

Missing configuration fails when settings load. Authentication, unavailable-model, and unsupported-capability errors follow existing error handling. The application does not retry against direct OpenAI or substitute a different model. OpenRouter may route between supporting endpoints for the selected model.

Changing the model later requires a compatible OpenRouter slug with the capabilities the workflow uses. This migration does not promise that every catalog model supports tools, the selected reasoning effort, and the draft schema.

## Surgical change set

| File | Intended change |
| --- | --- |
| `src/backlog_tamer/config.py` | Replace the old credential field with the root OpenRouter field; default agent configuration; change the model default to its OpenRouter slug |
| `src/backlog_tamer/agents/intake_triage/agent.py` | Update the existing helper and its call; replace routing and reasoning serialization; update its provider-specific docstring/comment |
| `.env.example` | Keep the existing OpenRouter-key addition; remove the old OpenAI key; document model and reasoning settings |
| `pyproject.toml`, `uv.lock` | Remove the unused direct `openai` requirement and relock narrowly; retain the SDK transitively through LiteLLM; no version bump or dependency upgrade |
| `tests/test_telegram_lambda_handlers.py` | Replace the old credential fixture; isolate healthcheck configuration from local `.env` |
| Focused configuration/model tests | Verify settings loading and the actual adapter request/response contract with dummy credentials |
| `README.md`, `CLAUDE.md` | Update model setup and architecture references; `AGENTS.md` is a symlink to `CLAUDE.md`, so edit its target once |

The approval workflow, prompts, draft schema, URL extraction, persistence, Telegram handling, Notion writes, tracing, Terraform, and deployment workflow need no implementation changes for this migration. Do not hand-edit generated OpenWiki pages. Remove obsolete direct-provider references from maintained setup docs and tests, while retaining historical changelog entries and valid references to OpenAI as the model publisher or transitive SDK.

## Validation and acceptance criteria

1. Settings load from an isolated `.env` containing `OPENROUTER_API_KEY` plus existing non-model requirements, with no OpenAI credential. Test environment-variable precedence, the default agent settings, and nested model overrides. Missing or blank OpenRouter credentials fail even if an old OpenAI credential exists.
2. Exercise the configured adapter through its generation interface with a mocked HTTP transport. Assert the destination is OpenRouter, the request model is `openai/gpt-6-luna`, authentication uses the dummy OpenRouter key, reasoning remains `medium`, and capability-aware routing is present. Check that a different configured publisher slug is prefixed correctly.
3. Cover a model tool call and its tool-result continuation, plus a structured response that validates as `ProjectDraft`. Preserve ADK's draft state and human-review interrupt behavior. Error responses must propagate through existing handling without a direct-provider fallback.
4. Update and run Lambda healthcheck tests with only the new credential contract. They must not make model calls.
5. Run the repository's required checks: `uv run --locked ruff format --check`, `uv run --locked ruff check`, and `uv run --locked pytest -q`. Review the lockfile for unrelated churn and search maintained source/config/tests/docs for obsolete credential and routing references.
6. After implementation, run a small authenticated smoke check through the standalone development runner: a text intake, a URL intake that exercises `fetch_url`, and a revision returning to review. Use in-memory sessions and synthetic inputs; the runner does not write to Notion. This check proves credentials, model access, and actual workflow compatibility, which mocked checks and the deployment healthcheck cannot prove.

Exploration already verified the proposed settings shape with dummy credentials and pydantic-settings 2.15.0. A mocked LiteLLM HTTP request also verified the native reasoning object, capability preference, tool declaration, schema request, model slug, and OpenRouter destination. It did not exercise real model responses or the full ADK workflow. No authenticated model call was made during this spec phase.

## Rollout

1. Implement only after agreement on this spec. The existing local OpenRouter key needs no rename.
2. Before releasing, add `OPENROUTER_API_KEY` to the production runtime secret. If production has `AGENT__MODEL`, convert its value to an OpenRouter slug; otherwise the new default applies. Production secret contents have not been inspected in this phase.
3. Release through the repository's existing release/deploy process. Verify the worker version/healthcheck and one real Telegram intake/review before treating production migration as complete.
4. Remove the obsolete OpenAI entries from local/production secrets after the rollback window. Previous images require the old credential and any old-style model setting, so retain or restore their configuration if rolling back. This operational rollback does not require legacy provider code in the new implementation.

## Agreed scope

OpenRouter is the sole model gateway, using GPT-6 Luna and medium reasoning, with the flat existing key spelling and publisher-qualified `AGENT__MODEL` contract above. Implementation removes direct OpenAI support rather than keeping a selectable legacy path.

## Implementation verification

- Authenticated text intake reached human review and the approved route through the standalone runner.
- Authenticated public-URL intake successfully called `fetch_url`, produced a structured draft, and returned to human review after revision changed priority to Low.
- Both smoke checks used synthetic inputs, in-memory sessions, and disabled tracing; neither wrote to Telegram or Notion.
- Relocking removed only the two direct OpenAI dependency references from the project lock entry; all package versions remained unchanged.
- Focused checks cover configuration, model HTTP requests and responses, tool-result continuation, the revision workflow, and Lambda healthchecks. Full-suite and review results are reported with the implementation PR.

The approved scope was extended to default to GPT-6 Luna (`openai/gpt-6-luna`) at medium reasoning and regenerate the repository wiki by running OpenWiki.

An authenticated GPT-6 Luna smoke check also passed public-URL fetching, structured drafting, and revision to a low-priority single-task proposal at medium reasoning. It used in-memory sessions and disabled tracing, with no Telegram or Notion writes.
