"""Offline, privacy, and Git inventory closure for M1c."""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

from glodex import cli
from glodex.config import load_config

pytestmark = pytest.mark.nfr

PROJECT_ROOT = Path(__file__).parents[3]
ARCHITECTURE_IMAGE_ROOT = PROJECT_ROOT / "项目架构"


@dataclass
class _SensitiveFailingTransport:
    detail: str
    calls: list[bytes] = field(default_factory=list)

    async def __call__(self, payload: bytes) -> bytes:
        self.calls.append(payload)
        raise RuntimeError(self.detail)


class _SingleRunId:
    def __init__(self) -> None:
        self.calls = 0

    def next_run_id(self) -> str:
        self.calls += 1
        return "run-nfr-security-001"


def _api_payload(query: str) -> dict[str, object]:
    return {
        "thread_id": "thread-nfr-security",
        "request": {
            "query": query,
            "locale": "zh-CN",
            "display_currency": "USD",
            "top_k": 3,
            "snapshot_version": "m0-v1",
        },
    }


def _git_lines(*arguments: str) -> tuple[str, ...]:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return tuple(line for line in completed.stdout.splitlines() if line)


@pytest.mark.spec("GLO-M1C-NFR-002")
def test_cli_api_sse_and_logs_never_expose_live_provider_data(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    install_live_transport: Callable[[object], list[object]],
) -> None:
    from glodex.api import live_app

    credential = "nfr-secret-sentinel"
    query = cli.DEMO_QUERY + ";private-query-sentinel"
    provider_detail = (
        "system-prompt-sentinel raw-response-sentinel "
        "https://api.deepseek.com raw-provider-error-sentinel"
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", credential)
    transport = _SensitiveFailingTransport(provider_detail)
    install_live_transport(transport)

    assert cli.main(("search", "--live-intent", "--query", query)) == 1
    cli_output = capsys.readouterr()

    app = live_app.create_live_app(
        config=load_config(environ={}),
        run_id_provider=_SingleRunId(),
    )

    async def exercise() -> tuple[str, str]:
        async with app.router.lifespan_context(app):
            asgi_transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(
                transport=asgi_transport,
                base_url="http://testserver",
            ) as client:
                accepted = await client.post(
                    "/api/v1/runs",
                    json=_api_payload(query),
                )
                assert accepted.status_code == 202
                await app.state.coordinator.wait_idle()
                run_id = accepted.json()["run_id"]
                status = await client.get(f"/api/v1/runs/{run_id}")
                events = await client.get(
                    f"/api/v1/runs/{run_id}/events",
                    headers={"accept": "text/event-stream"},
                )
        return status.text, events.text

    status_text, event_text = asyncio.run(exercise())
    public_text = "\n".join(
        (
            cli_output.out,
            cli_output.err,
            status_text,
            event_text,
            caplog.text,
        )
    )

    assert len(transport.calls) == 2
    for sentinel in (
        credential,
        query,
        "private-query-sentinel",
        "system-prompt-sentinel",
        "raw-response-sentinel",
        "raw-provider-error-sentinel",
        "https://api.deepseek.com",
    ):
        assert sentinel not in public_text
    status = json.loads(status_text)
    assert status["state"] == "FAILED"
    assert status["response"]["diagnostics"]["issues"][0]["code"] == ("intent.provider-unavailable")
    assert "RUN_ABORTED" not in event_text


@pytest.mark.spec("GLO-M1C-P0-006")
def test_git_inventory_excludes_local_credentials_live_artifacts_and_architecture_pngs() -> None:
    images = tuple(sorted(ARCHITECTURE_IMAGE_ROOT.rglob("*.png")))
    image_paths = frozenset(path.relative_to(PROJECT_ROOT).as_posix() for path in images)
    tracked = frozenset(_git_lines("ls-files", "--cached"))
    staged = frozenset(
        _git_lines(
            "diff",
            "--cached",
            "--name-only",
            "--diff-filter=ACMR",
        )
    )

    assert len(image_paths) == 26
    assert image_paths.isdisjoint(tracked)
    assert image_paths.isdisjoint(staged)
    assert not any(Path(path).name == ".env" for path in tracked | staged)

    forbidden_artifacts: list[str] = []
    for path in sorted(tracked | staged):
        lowered = path.lower()
        if not lowered.startswith("tests/m1c/"):
            continue
        parts = set(Path(lowered).parts)
        if (
            {"fixture", "fixtures", "cassette", "cassettes"} & parts
            or "live_response" in lowered
            or "live-response" in lowered
        ):
            forbidden_artifacts.append(path)

    assert forbidden_artifacts == []
