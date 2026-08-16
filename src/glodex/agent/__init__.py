"""Native-tool shopping Agent surface."""

from typing import TYPE_CHECKING

from glodex.agent.contracts import BUSINESS_TOOL_SET, ToolName

if TYPE_CHECKING:
    from glodex.agent.graph import ReActAgentService
    from glodex.agent.tool_session import AgentRuntimeConfig

__all__ = [
    "BUSINESS_TOOL_SET",
    "AgentRuntimeConfig",
    "ReActAgentService",
    "ToolName",
]


def __getattr__(name: str) -> object:
    """Keep native graph imports out of the deterministic default CLI path."""

    if name == "AgentRuntimeConfig":
        from glodex.agent.tool_session import AgentRuntimeConfig

        return AgentRuntimeConfig
    if name == "ReActAgentService":
        from glodex.agent.graph import ReActAgentService

        return ReActAgentService
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
