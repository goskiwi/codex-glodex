"""Explicit live smoke for Reflect persistence and subsequent memory-aware retrieval."""

from __future__ import annotations

import argparse
import asyncio
import secrets
import time
from typing import Any

import httpx

_BASE_URL = "http://127.0.0.1:8766"
_TERMINAL = {"COMPLETED", "NO_MATCH"}


async def _wait_terminal(client: httpx.AsyncClient, run_id: str) -> str:
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        response = await client.get(f"/api/v1/durable-agent-runs/{run_id}")
        response.raise_for_status()
        state = response.json().get("state")
        if state in _TERMINAL:
            return str(state)
        if state in {"FAILED", "ABORTED"}:
            raise RuntimeError("live Agent run did not reach a trusted terminal")
        await asyncio.sleep(0.5)
    raise TimeoutError("live Agent run timed out")


async def _run(client: httpx.AsyncClient, query: str) -> str:
    response = await client.post(
        "/api/v1/durable-agent-runs",
        json={
            "request": {
                "query": query,
                "locale": "zh-CN",
                "displayCurrency": "CNY",
                "topK": 3,
                "snapshotVersion": "synthetic-interview-commerce-v1",
            }
        },
    )
    response.raise_for_status()
    run_id = response.json().get("runId")
    if type(run_id) is not str or not run_id:
        raise RuntimeError("live Agent acknowledgement is invalid")
    return await _wait_terminal(client, run_id)


async def _memory_ids(client: httpx.AsyncClient) -> frozenset[str]:
    response = await client.get("/api/v1/memory")
    response.raise_for_status()
    payload: Any = response.json()
    if type(payload) is not dict or payload.get("schemaVersion") != (
        "glodex.user-memory.memory-list.v3"
    ):
        raise RuntimeError("live memory response is invalid")
    entries = payload.get("activeEntries")
    if type(entries) is not list:
        raise RuntimeError("live memory response is invalid")
    return frozenset(
        entry["entryId"]
        for entry in entries
        if type(entry) is dict and type(entry.get("entryId")) is str
    )


async def _accept() -> None:
    suffix = secrets.token_hex(6)
    credentials = {"username": f"m5b.{suffix}", "password": secrets.token_urlsafe(24)}
    async with httpx.AsyncClient(base_url=_BASE_URL, timeout=15.0) as client:
        registered = await client.post("/api/v1/local-auth/register", json=credentials)
        registered.raise_for_status()
        try:
            before = await _memory_ids(client)
            first_state = await _run(
                client,
                "我长期偏好轻薄设计,请帮我选择一台适合出差的笔记本电脑",
            )
            deadline = time.monotonic() + 30
            after = before
            while time.monotonic() < deadline and after == before:
                await asyncio.sleep(0.5)
                after = await _memory_ids(client)
            if len(after - before) != 1:
                raise RuntimeError("live Reflect did not create exactly one active memory")
            second_state = await _run(client, "帮我选择一台适合出差的笔记本电脑")
            print(
                {
                    "valid": True,
                    "reflect_terminal": first_state,
                    "subsequent_terminal": second_state,
                    "new_active_memory_count": 1,
                }
            )
        finally:
            await client.delete("/api/v1/local-auth/me")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    if not args.live:
        parser.error("live acceptance requires --live")
    asyncio.run(_accept())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
