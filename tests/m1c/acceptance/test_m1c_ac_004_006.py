"""Black-box M1c acceptance scenarios 004 through 006."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx
import pytest

from glodex import cli
from glodex.config import load_config
from tests.m1c.conftest import RecordingDeepSeekTransport

pytestmark = pytest.mark.acceptance


class _SingleRunId:
    def __init__(self, value: str) -> None:
        self.value = value
        self.calls = 0

    def next_run_id(self) -> str:
        self.calls += 1
        if self.calls > 1:
            raise AssertionError("API allocated more than one Run ID")
        return self.value


@dataclass
class _FailingTransport:
    calls: list[bytes] = field(default_factory=list)

    async def __call__(self, payload: bytes) -> bytes:
        self.calls.append(payload)
        raise RuntimeError("raw-transport-sentinel")


def _payload(query: str) -> dict[str, object]:
    return {
        "thread_id": "thread-m1c-failure",
        "request": {
            "query": query,
            "locale": "zh-CN",
            "display_currency": "USD",
            "top_k": 3,
            "snapshot_version": "m0-v1",
        },
    }


def _required(query: str) -> list[dict[str, object]]:
    def span(text: str) -> dict[str, object]:
        start = query.index(text)
        return {"start": start, "end": start + len(text), "text": text}

    return [
        {
            "kind": "budget_max",
            "amount": "800",
            "currency": "USD",
            **span("800 美元以内"),
        },
        {"kind": "stock_required", **span("有库存")},
        {
            "kind": "target_category",
            "category": "laptop",
            **span("轻薄本"),
        },
    ]


def _sse_payloads(body: str) -> list[dict[str, object]]:
    payloads: list[dict[str, object]] = []
    for block in body.replace("\r\n", "\n").split("\n\n"):
        data = "\n".join(
            line.removeprefix("data: ") for line in block.splitlines() if line.startswith("data: ")
        )
        if data:
            parsed = json.loads(data)
            assert isinstance(parsed, dict)
            payloads.append(parsed)
    return payloads


async def _terminal_run(
    app: object,
    query: str,
) -> tuple[dict[str, object], list[dict[str, object]], str]:
    async with app.router.lifespan_context(app):
        asgi_transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(
            transport=asgi_transport,
            base_url="http://testserver",
        ) as client:
            accepted = await client.post("/api/v1/runs", json=_payload(query))
            assert accepted.status_code == 202
            await app.state.coordinator.wait_idle()
            run_id = accepted.json()["run_id"]
            status = await client.get(f"/api/v1/runs/{run_id}")
            events = await client.get(
                f"/api/v1/runs/{run_id}/events",
                headers={"accept": "text/event-stream"},
            )
    status_body = status.json()
    assert isinstance(status_body, dict)
    return status_body, _sse_payloads(events.text), events.text


def _assert_business_failure(
    status: dict[str, object],
    events: list[dict[str, object]],
    *,
    issue_code: str,
) -> None:
    assert status["state"] == "FAILED"
    assert status["error"] is None
    response = status["response"]
    assert isinstance(response, dict)
    assert response["status"] == "FAILED"
    diagnostics = response["diagnostics"]
    assert isinstance(diagnostics, dict)
    issues = diagnostics["issues"]
    assert isinstance(issues, list)
    assert issues[0]["code"] == issue_code
    assert [event["type"] for event in events][-2:] == [
        "STATE_SNAPSHOT",
        "RUN_ERROR",
    ]
    assert all(event["type"] != "RUN_ABORTED" for event in events)


@pytest.mark.spec("M1C-AC-004")
@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        ("transport", "intent.provider-unavailable"),
        ("provider-response", "intent.provider-response-invalid"),
    ],
)
def test_m1c_ac_004_provider_failure_is_one_safe_business_failure(
    monkeypatch: pytest.MonkeyPatch,
    install_live_transport: Callable[[object], list[object]],
    failure: str,
    expected_code: str,
) -> None:
    from glodex.api import live_app

    monkeypatch.setenv("DEEPSEEK_API_KEY", "secret-acceptance-sentinel")
    transport: _FailingTransport | RecordingDeepSeekTransport
    if failure == "transport":
        transport = _FailingTransport()
    else:
        transport = RecordingDeepSeekTransport(b'{"raw_provider_body":"raw-provider-sentinel"}')
    install_live_transport(transport)
    app = live_app.create_live_app(
        config=load_config(environ={}),
        run_id_provider=_SingleRunId(f"run-ac004-{failure}"),
    )

    status, events, raw_events = asyncio.run(_terminal_run(app, cli.DEMO_QUERY))

    _assert_business_failure(status, events, issue_code=expected_code)
    assert len(transport.calls) == 1
    public_text = json.dumps(status, ensure_ascii=False) + raw_events
    for secret in (
        "secret-acceptance-sentinel",
        "raw-transport-sentinel",
        "raw-provider-sentinel",
        "https://api.deepseek.com",
        cli.DEMO_QUERY,
    ):
        assert secret not in public_text


@pytest.mark.spec("M1C-AC-005")
@pytest.mark.parametrize(
    ("failure", "expected_code", "expected_calls"),
    [
        ("unsafe-baseline", "intent.required-baseline-failed", 0),
        ("missing-required", "intent.required-incomplete", 1),
    ],
)
def test_m1c_ac_005_required_failures_never_continue_past_intent(
    monkeypatch: pytest.MonkeyPatch,
    install_live_transport: Callable[[object], list[object]],
    deepseek_response_bytes: Callable[[object], bytes],
    failure: str,
    expected_code: str,
    expected_calls: int,
) -> None:
    from glodex.api import live_app

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-live-secret")
    if failure == "unsafe-baseline":
        query = "推荐笔记本和相机"
        response = b"must-not-be-called"
    else:
        query = cli.DEMO_QUERY
        required = _required(query)
        response = deepseek_response_bytes(
            {
                "required": [required[0], required[2]],
                "preferred": [],
            }
        )
    transport = RecordingDeepSeekTransport(response)
    install_live_transport(transport)
    app = live_app.create_live_app(
        config=load_config(environ={}),
        run_id_provider=_SingleRunId(f"run-ac005-{failure}"),
    )

    status, events, _raw_events = asyncio.run(_terminal_run(app, query))

    _assert_business_failure(status, events, issue_code=expected_code)
    response_body = status["response"]
    assert isinstance(response_body, dict)
    diagnostics = response_body["diagnostics"]
    assert isinstance(diagnostics, dict)
    assert [stage["stage"] for stage in diagnostics["stages"]] == ["intent"]
    assert len(transport.calls) == expected_calls


@pytest.mark.spec("M1C-AC-006")
def test_m1c_ac_006_default_acceptance_uses_fake_transport_not_live_http(
    monkeypatch: pytest.MonkeyPatch,
    install_live_transport: Callable[[object], list[object]],
    deepseek_response_bytes: Callable[[object], bytes],
) -> None:
    from glodex.adapters import deepseek_http
    from glodex.api import live_app

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-live-secret")
    query = cli.DEMO_QUERY
    transport = RecordingDeepSeekTransport(
        deepseek_response_bytes(
            {
                "required": _required(query),
                "preferred": [],
            }
        )
    )
    install_live_transport(transport)
    real_asgi_client = httpx.AsyncClient
    live_http_calls = 0

    def forbidden_live_client(*_args: object, **_kwargs: object) -> object:
        nonlocal live_http_calls
        live_http_calls += 1
        raise AssertionError("offline acceptance created a live HTTP client")

    monkeypatch.setattr(deepseek_http.httpx, "AsyncClient", forbidden_live_client)
    app = live_app.create_live_app(
        config=load_config(environ={}),
        run_id_provider=_SingleRunId("run-ac006-fake"),
    )

    async def exercise() -> dict[str, object]:
        async with app.router.lifespan_context(app):
            asgi_transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with real_asgi_client(
                transport=asgi_transport,
                base_url="http://testserver",
            ) as client:
                accepted = await client.post("/api/v1/runs", json=_payload(query))
                assert accepted.status_code == 202
                await app.state.coordinator.wait_idle()
                terminal = await client.get(f"/api/v1/runs/{accepted.json()['run_id']}")
        body = terminal.json()
        assert isinstance(body, dict)
        return body

    status = asyncio.run(exercise())

    assert status["state"] == "COMPLETED"
    assert len(transport.calls) == 1
    assert live_http_calls == 0
