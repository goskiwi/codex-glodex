"""The fixed OpenAI-compatible native tool-calling shopping model."""

from __future__ import annotations

from collections.abc import Sequence

from langchain_core.tools import BaseTool
from langchain_openai import ChatOpenAI

from glodex.agent.prompt_cache import DEEPSEEK_PROMPT_CACHE
from glodex.llm.config import LlmConfiguration, LlmConfigurationError, load_llm_configuration

_DEFAULT_TIMEOUT_SECONDS = 60.0
_DEFAULT_MAX_RETRIES = 1


class AgentModelConfigurationError(RuntimeError):
    """A stable, secret-free failure raised before a graph is started."""

    def __init__(self, code: str) -> None:
        if code not in {"AGENT_LLM_CREDENTIALS_MISSING", "AGENT_LLM_CONFIG_INVALID"}:
            raise ValueError("unsupported Agent model configuration error")
        self.code = code
        super().__init__(code)


def validate_llm_configuration() -> None:
    """Validate the model credential without exposing it."""

    _load_agent_configuration()


def get_llm() -> ChatOpenAI:
    """Create the fixed ``ChatOpenAI`` model with one bounded retry.

    Provider and model are fixed by the application, not operator-controlled.
    """

    configuration = _load_agent_configuration()
    cache_telemetry = DEEPSEEK_PROMPT_CACHE.telemetry()
    return ChatOpenAI(
        model=configuration.model_name,
        api_key=configuration.api_key,
        base_url=configuration.base_url,
        temperature=0,
        timeout=_DEFAULT_TIMEOUT_SECONDS,
        max_retries=_DEFAULT_MAX_RETRIES,
        streaming=False,
        metadata={"glodex_prompt_cache": cache_telemetry.as_metadata()},
        extra_body={"thinking": {"type": "disabled"}},
    )


def get_tool_calling_llm(tools: Sequence[BaseTool]) -> object:
    """Bind strict, serial native tools while leaving the next action to the model.

    ``tool_choice='auto'`` is deliberate: a native ReAct graph needs the model
    to decide whether another observation is useful. The quickstart may end
    with a direct assistant answer; the production shopping session accepts a
    user-visible answer only through a trusted terminal tool, rather than
    coercing every provider turn into a tool call.
    """

    if not tools:
        raise ValueError("the ReAct Agent requires at least one tool")
    return get_llm().bind_tools(
        list(tools),
        strict=True,
        parallel_tool_calls=False,
        tool_choice="auto",
    )


def _load_agent_configuration() -> LlmConfiguration:
    try:
        return load_llm_configuration()
    except LlmConfigurationError as error:
        code = (
            "AGENT_LLM_CREDENTIALS_MISSING"
            if error.code == "LLM_CREDENTIALS_MISSING"
            else "AGENT_LLM_CONFIG_INVALID"
        )
        raise AgentModelConfigurationError(code) from None


__all__ = [
    "AgentModelConfigurationError",
    "get_llm",
    "get_tool_calling_llm",
    "validate_llm_configuration",
]
