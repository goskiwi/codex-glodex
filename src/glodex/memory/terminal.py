"""Narrow post-terminal hook for authenticated user-memory history and reflection."""

from __future__ import annotations

from typing import Protocol

from glodex.agent.contracts import AgentDemoResponse
from glodex.runtime.contracts import DurableRun


class MemoryTerminalWriterPort(Protocol):
    async def write_terminal(
        self,
        *,
        run: DurableRun,
        response: AgentDemoResponse,
    ) -> None: ...


__all__ = ["MemoryTerminalWriterPort"]
