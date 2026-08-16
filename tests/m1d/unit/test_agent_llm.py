"""Configuration and strict-native-tool evidence for the Agent model factory."""

from __future__ import annotations

import pytest
from langchain_core.tools import tool

from glodex.agent import llm as agent_llm
from glodex.agent.prompt_cache import PromptCacheCapability, PromptCacheTelemetry

pytestmark = pytest.mark.unit


def test_native_agent_requires_the_shared_llm_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("LLM_API_KEY", "retired-key")
    monkeypatch.setenv("LLM_BASE_URL", "https://retired.invalid")
    monkeypatch.setenv("LLM_MODEL_NAME", "retired-model")

    with pytest.raises(agent_llm.AgentModelConfigurationError) as captured:
        agent_llm.validate_llm_configuration()

    assert captured.value.code == "AGENT_LLM_CREDENTIALS_MISSING"


def test_native_agent_binds_chatopenai_tools_for_strict_serial_model_decisions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[_FakeChatOpenAI] = []

    class _FakeChatOpenAI:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs
            self.bound_tools: list[object] | None = None
            self.bind_kwargs: dict[str, object] | None = None
            created.append(self)

        def bind_tools(self, tools: list[object], **kwargs: object) -> _FakeChatOpenAI:
            self.bound_tools = tools
            self.bind_kwargs = kwargs
            return self

    @tool
    def planner() -> str:
        """Return a trusted planning receipt."""

        return "{}"

    monkeypatch.setenv("DEEPSEEK_API_KEY", "native-agent-test-key")
    monkeypatch.setattr(agent_llm, "ChatOpenAI", _FakeChatOpenAI)

    model = agent_llm.get_tool_calling_llm([planner])

    assert model is created[0]
    assert created[0].kwargs == {
        "model": "gpt-5.6-luna",
        "api_key": "native-agent-test-key",
        "base_url": "https://2btocken.xyz/v1",
        "temperature": 0,
        "timeout": 60.0,
        "max_retries": 1,
        "streaming": False,
        "metadata": {
            "glodex_prompt_cache": {
                "cache_supported": False,
                "cache_attempted": False,
                "cache_hit": False,
            }
        },
        "extra_body": {"thinking": {"type": "disabled"}},
    }
    assert created[0].bound_tools == [planner]
    assert created[0].bind_kwargs == {
        "strict": True,
        "parallel_tool_calls": False,
        "tool_choice": "auto",
    }


def test_unverified_prompt_cache_cannot_be_attempted_or_reported_as_hit() -> None:
    with pytest.raises(ValueError):
        PromptCacheCapability(provider="deepseek", cache_supported=True)
    with pytest.raises(ValueError):
        PromptCacheTelemetry(
            cache_supported=False,
            cache_attempted=True,
            cache_hit=False,
        )
    with pytest.raises(ValueError):
        PromptCacheTelemetry(
            cache_supported=True,
            cache_attempted=False,
            cache_hit=True,
        )
