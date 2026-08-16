"""Production LangGraph checkpoint ownership and namespace evidence."""

from __future__ import annotations

import asyncio
from contextlib import AbstractAsyncContextManager
from types import SimpleNamespace
from typing import Any, cast

import pytest

from glodex.agent.contracts import FULL_TOOL_SET
from glodex.agent.graph import _SYSTEM_PROMPT, AgentLoopDefinition, _stream_graph
from glodex.runtime.contracts import DurableCheckpointState
from glodex.runtime.graph_checkpoint import PostgresGraphCheckpointStore

pytestmark = [
    pytest.mark.unit,
    pytest.mark.spec(
        "GLO-DURABLE-P0-002",
        "GLO-DURABLE-P0-003",
        "GLO-DURABLE-P0-004",
        "GLO-DURABLE-NFR-001",
        "GLO-DURABLE-NFR-002",
        "DURABLE-AC-003",
        "DURABLE-AC-004",
    ),
]


def _definition(*, thread_id: str, checkpoint_namespace: str) -> AgentLoopDefinition:
    return AgentLoopDefinition(
        thread_id=thread_id,
        checkpoint_namespace=checkpoint_namespace,
        tool_names=FULL_TOOL_SET,
        system_prompt=_SYSTEM_PROMPT,
    )


class _Saver:
    def __init__(self) -> None:
        self.setup_calls = 0

    async def setup(self) -> None:
        self.setup_calls += 1


class _SaverContext(AbstractAsyncContextManager[Any]):
    def __init__(self, saver: _Saver) -> None:
        self.saver = saver
        self.entered = 0
        self.exited = 0

    async def __aenter__(self) -> Any:
        self.entered += 1
        return self.saver

    async def __aexit__(self, *_args: object) -> None:
        self.exited += 1


def test_postgres_graph_checkpoint_store_requires_open_and_owns_one_saver() -> None:
    saver = _Saver()
    context = _SaverContext(saver)
    store = PostgresGraphCheckpointStore(
        context_factory=cast(Any, lambda _dsn: context),
    )

    with pytest.raises(RuntimeError, match="LANGGRAPH_CHECKPOINT_STORE_NOT_OPEN"):
        store.require_saver()

    async def scenario() -> None:
        await store.open()
        await store.open()
        assert store.require_saver() is saver
        assert saver.setup_calls == 1
        assert context.entered == 1
        await store.close()
        await store.close()
        assert context.exited == 1

    asyncio.run(scenario())


def test_postgres_graph_checkpoint_store_has_no_unencrypted_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LANGGRAPH_AES_KEY", raising=False)
    store = PostgresGraphCheckpointStore()

    with pytest.raises(ValueError, match="LANGGRAPH_AES_KEY"):
        asyncio.run(store.open())
    with pytest.raises(RuntimeError, match="LANGGRAPH_CHECKPOINT_STORE_NOT_OPEN"):
        store.require_saver()


def test_stream_graph_uses_thread_and_recovery_attempt_namespace() -> None:
    captured: dict[str, object] = {}

    class _Graph:
        def astream_events(
            self,
            value: object,
            *,
            config: dict[str, object],
            version: str,
        ) -> Any:
            captured.update(value=value, config=config, version=version)

            async def events() -> Any:
                if False:
                    yield None

            return events()

    class _Session:
        failed = False

    asyncio.run(
        _stream_graph(
            graph=_Graph(),
            messages=["locked input"],
            run_id="run-checkpoint-test",
            definition=_definition(
                thread_id="thread-checkpoint-test",
                checkpoint_namespace="run-checkpoint-test.checkpoint-7",
            ),
            session=cast(Any, _Session()),
            checkpoint_writer=None,
        )
    )

    assert captured["value"] == {"messages": ["locked input"]}
    config = cast(dict[str, Any], captured["config"])
    assert config["configurable"] == {
        "thread_id": ("thread-checkpoint-test.run-checkpoint-test.checkpoint-7"),
    }


def test_stream_graph_uses_thread_identity_not_parent_depth_for_model_events() -> None:
    graph_thread_id = "thread-root.run-root.checkpoint-3"

    class _Graph:
        def astream_events(self, *_args: object, **_kwargs: object) -> Any:
            async def events() -> Any:
                yield {
                    "event": "on_chat_model_start",
                    "parent_ids": ["one"],
                    "metadata": {"thread_id": graph_thread_id},
                }
                yield {
                    "event": "on_chat_model_stream",
                    "parent_ids": ["one"],
                    "metadata": {"thread_id": graph_thread_id},
                    "data": {"chunk": "private token and partial tool arguments"},
                }
                yield {
                    "event": "on_chat_model_end",
                    "parent_ids": ["one", "two"],
                    "metadata": {"thread_id": "child-thread.child-checkpoint"},
                    "data": {"output": SimpleNamespace(tool_calls=[{"name": "child"}])},
                }
                yield {
                    "event": "on_chat_model_end",
                    "parent_ids": ["one", "two"],
                    "metadata": {"thread_id": graph_thread_id},
                    "data": {"output": SimpleNamespace(tool_calls=[{"name": "root"}])},
                }

            return events()

    class _Session:
        def __init__(self) -> None:
            self.failed = False
            self.terminal_response = None
            self.handoff_ready = False
            self.starts = 0
            self.streams = 0
            self.finishes: list[object] = []

        def model_started(self) -> None:
            self.starts += 1

        def model_streaming(self) -> None:
            self.streams += 1

        def model_finished(self, calls: object) -> None:
            self.finishes.append(calls)

    session = _Session()
    asyncio.run(
        _stream_graph(
            graph=_Graph(),
            messages=["locked input"],
            run_id="run-root",
            definition=_definition(
                thread_id="thread-root",
                checkpoint_namespace="run-root.checkpoint-3",
            ),
            session=cast(Any, session),
            checkpoint_writer=None,
        )
    )

    assert session.starts == 1
    assert session.streams == 1
    assert session.finishes == [[{"name": "root"}]]


def test_stream_graph_validates_the_same_serial_tool_projection_as_execution() -> None:
    graph_thread_id = "thread-root.run-root.checkpoint-serial"

    class _Graph:
        def astream_events(self, *_args: object, **_kwargs: object) -> Any:
            async def events() -> Any:
                yield {
                    "event": "on_chat_model_end",
                    "metadata": {"thread_id": graph_thread_id},
                    "data": {
                        "output": SimpleNamespace(
                            tool_calls=[
                                {"name": "item_search", "args": {"query": "手机"}},
                                {"name": "price_compare", "args": {}},
                            ]
                        )
                    },
                }

            return events()

    class _Session:
        failed = False
        terminal_response = None
        handoff_ready = False

        def __init__(self) -> None:
            self.finishes: list[object] = []

        def model_finished(self, calls: object) -> None:
            self.finishes.append(calls)

    session = _Session()
    asyncio.run(
        _stream_graph(
            graph=_Graph(),
            messages=["locked input"],
            run_id="run-root",
            definition=_definition(
                thread_id="thread-root",
                checkpoint_namespace="run-root.checkpoint-serial",
            ),
            session=cast(Any, session),
            checkpoint_writer=None,
        )
    )

    assert session.finishes == [[{"name": "item_search", "args": {"query": "手机"}}]]


def test_model_call_stays_remote_pending_until_the_response_arrives() -> None:
    graph_thread_id = "thread-root.run-root.checkpoint-4"

    class _Graph:
        def astream_events(self, *_args: object, **_kwargs: object) -> Any:
            async def events() -> Any:
                yield {
                    "event": "on_chat_model_start",
                    "metadata": {"thread_id": graph_thread_id},
                }
                yield {
                    "event": "on_chat_model_end",
                    "metadata": {"thread_id": graph_thread_id},
                    "data": {"output": SimpleNamespace(tool_calls=[{"name": "planner"}])},
                }

            return events()

    class _Session:
        run_id = "run-root"
        failed = False
        terminal_response = None
        handoff_ready = False
        events: tuple[()] = ()
        calls = 0

        def model_started(self) -> None:
            self.calls += 1

        def model_finished(self, _calls: object) -> None:
            return None

        def checkpoint_local_state(self) -> dict[str, object]:
            return {
                "agent_model_calls": self.calls,
                "agent_session_snapshot": {"schema": "test", "pages": [["state"]]},
            }

    class _Writer:
        def __init__(self) -> None:
            self.states: list[DurableCheckpointState] = []

        async def persist(self, **kwargs: object) -> None:
            self.states.append(cast(DurableCheckpointState, kwargs["state"]))

    writer = _Writer()
    asyncio.run(
        _stream_graph(
            graph=_Graph(),
            messages=["locked input"],
            run_id="run-root",
            definition=_definition(
                thread_id="thread-root",
                checkpoint_namespace="run-root.checkpoint-4",
            ),
            session=cast(Any, _Session()),
            checkpoint_writer=cast(Any, writer),
        )
    )

    assert writer.states == [
        DurableCheckpointState.REMOTE_PENDING,
        DurableCheckpointState.CONFIRMED,
    ]
