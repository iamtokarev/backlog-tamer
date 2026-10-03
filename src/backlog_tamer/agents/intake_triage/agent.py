from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm
from pydantic import SecretStr

from backlog_tamer.config import AgentConfig, get_settings

from .prompts import INTAKE_TRIAGE_INSTRUCTIONS
from .schemas import ProjectDraft
from .tools.fetch_url import fetch_url
from .workflow import build_intake_workflow

settings = get_settings()


def _get_model(config: AgentConfig, api_key: SecretStr) -> LiteLlm:
    """Configure the model through OpenRouter with explicit credentials."""
    return LiteLlm(
        model=f"openrouter/{config.model}",
        api_key=api_key.get_secret_value(),
        api_base="https://openrouter.ai/api/v1",
        # Native reasoning avoids LiteLLM's model-metadata parameter whitelist.
        extra_body={
            "reasoning": {"effort": config.reasoning_effort},
            "provider": {"require_parameters": True},
        },
    )


draft_agent = Agent(
    name="intake_triage",
    model=_get_model(settings.agent, settings.openrouter_api_key),
    description="Turns messy learning inputs into grounded triage drafts.",
    instruction=INTAKE_TRIAGE_INSTRUCTIONS,
    output_schema=ProjectDraft,
    output_key="draft_proposal",
    tools=[fetch_url],
)

root_agent = build_intake_workflow(draft_agent)

intake_triage_agent = draft_agent
