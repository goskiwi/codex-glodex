"""Production PostgreSQL ownership for LangGraph AgentLoop checkpoints."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.encrypted import EncryptedSerializer

from glodex.infrastructure.postgres import validate_postgres_dsn

_DEFAULT_DSN = "postgresql://glodex@127.0.0.1:5433/glodex"

type SaverContextFactory = Callable[[str], AbstractAsyncContextManager[AsyncPostgresSaver]]


class PostgresGraphCheckpointStore:
    """Own one async PostgreSQL saver for every root and child AgentLoop."""

    def __init__(
        self,
        *,
        dsn: str = _DEFAULT_DSN,
        context_factory: SaverContextFactory | None = None,
    ) -> None:
        self._dsn = validate_postgres_dsn(dsn)
        self._context_factory = context_factory
        self._context: AbstractAsyncContextManager[AsyncPostgresSaver] | None = None
        self._saver: AsyncPostgresSaver | None = None

    async def open(self) -> None:
        """Open the saver and idempotently install its private schema."""

        if self._saver is not None:
            return
        context: AbstractAsyncContextManager[AsyncPostgresSaver]
        if self._context_factory is None:
            serde = EncryptedSerializer.from_pycryptodome_aes()
            context = AsyncPostgresSaver.from_conn_string(self._dsn, serde=serde)
        else:
            context = self._context_factory(self._dsn)
        saver = await context.__aenter__()
        try:
            await saver.setup()
        except BaseException as error:
            await context.__aexit__(type(error), error, error.__traceback__)
            raise
        self._context = context
        self._saver = saver

    async def close(self) -> None:
        """Close the saver without retaining a connection across event loops."""

        context, self._context = self._context, None
        self._saver = None
        if context is not None:
            await context.__aexit__(None, None, None)

    def require_saver(self) -> AsyncPostgresSaver:
        """Return the active production saver or reject execution before graph build."""

        if self._saver is None:
            raise RuntimeError("LANGGRAPH_CHECKPOINT_STORE_NOT_OPEN")
        return self._saver


__all__ = ["PostgresGraphCheckpointStore"]
