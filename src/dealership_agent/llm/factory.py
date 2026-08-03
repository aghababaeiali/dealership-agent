"""Select an LLMProvider based on Settings.llm_provider."""

from dealership_agent.config import get_settings
from dealership_agent.llm.base import LLMProvider
from dealership_agent.llm.bedrock_provider import BedrockProvider
from dealership_agent.llm.groq_provider import GroqProvider


def get_llm_provider() -> LLMProvider:
    settings = get_settings()
    if settings.llm_provider == "groq":
        return GroqProvider(
            api_key=settings.groq_api_key,
            max_retries=settings.llm_retry_max_retries,
            base_delay_seconds=settings.llm_retry_base_delay_seconds,
            max_delay_seconds=settings.llm_retry_max_delay_seconds,
        )
    if settings.llm_provider == "bedrock":
        return BedrockProvider(region=settings.aws_region)
    raise ValueError(f"Unknown LLM_PROVIDER: {settings.llm_provider!r}")
