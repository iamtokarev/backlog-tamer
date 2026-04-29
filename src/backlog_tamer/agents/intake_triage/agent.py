from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm

from backlog_tamer.config import get_settings

from .prompts import INTAKE_TRIAGE_INSTRUCTIONS
from .schemas import ProjectDraft
from .tools.fetch_url import fetch_url
from .workflow import build_intake_workflow

settings = get_settings()


def _get_model() -> LiteLlm:
    """Initalize the OpenAI model with the configured API key and model name."""
    model = LiteLlm(
        model=f"openai/{settings.agent.model}",
        api_key=settings.agent.openai_api_key.get_secret_value(),
    )
    return model


draft_agent = Agent(
    name="intake_triage",
    model=_get_model(),
    description="Turns messy learning inputs into grounded triage drafts.",
    instruction=INTAKE_TRIAGE_INSTRUCTIONS,
    output_schema=ProjectDraft,
    output_key="draft_proposal",
    tools=[fetch_url],
)

root_agent = build_intake_workflow(draft_agent)

intake_triage_agent = draft_agent
