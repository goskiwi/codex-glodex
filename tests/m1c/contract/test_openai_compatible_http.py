"""Contract tests for the configured OpenAI-compatible HTTP transport."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest

from glodex.domain.intent import IntentIssueCode
from glodex.llm import config as llm_config
from glodex.llm import openai_compatible_http
from glodex.llm.config import LlmConfiguration, LlmConfigurationError
from glodex.llm.contracts import JsonCompletionTransport, LlmTransportError
from glodex.llm.openai_compatible_http import (
    LLM_HTTP_DEADLINE_SECONDS,
    build_json_completion_transport,
)

pytestmark = pytest.mark.contract

_BASE_URL = "https://llm.example.test/v1"
_ENDPOINT = f"{_BASE_URL}/chat/completions"
_MODEL_NAME = "test-tool-model"
_SECRET = "test-llm-secret"
_MAX_BODY = 65_536


@dataclass
class _CountingStream(httpx.AsyncByteStream):
    chunks: tuple[bytes, ...]
    explode_after: int | None = None
    reads: int = 0
    closed: bool = False
    active_check: Callable[[], bool] | None = None

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            if self.explode_after is not None and self.reads >= self.explode_after:
                raise AssertionError("response body was read past the approved boundary")
            if self.active_check is not None:
                assert self.active_check()
            self.reads += 1
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


@dataclass
class _DeadlineSpy:
    fail_on_exit: bool = False
    calls: list[float] = field(default_factory=list)
    active: bool = False

    def __call__(self, delay: float) -> _DeadlineSpy:
        self.calls.append(delay)
        return self

    async def __aenter__(self) -> None:
        self.active = True

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object,
    ) -> bool:
        del exc_type, exc, traceback
        self.active = False
        if self.fail_on_exit:
            raise TimeoutError("sensitive timeout detail")
        return False


@pytest.fixture(autouse=True)
def _configured_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", _SECRET)


def _configuration(
    *,
    api_key: str = _SECRET,
    base_url: str = _BASE_URL,
    model_name: str = _MODEL_NAME,
) -> LlmConfiguration:
    return LlmConfiguration(api_key=api_key, base_url=base_url, model_name=model_name)


def _mock_transport(
    *,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
    chunks: tuple[bytes, ...] = (b"{}",),
    explode_after: int | None = None,
    active_check: Callable[[], bool] | None = None,
) -> tuple[httpx.MockTransport, _CountingStream, list[httpx.Request]]:
    stream = _CountingStream(
        chunks,
        explode_after=explode_after,
        active_check=active_check,
    )
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            status_code,
            headers=headers,
            stream=stream,
            request=request,
        )

    return httpx.MockTransport(handler), stream, requests


def _transport(
    *,
    http_transport: httpx.AsyncBaseTransport | None = None,
    configuration: LlmConfiguration | None = None,
) -> JsonCompletionTransport:
    return build_json_completion_transport(
        configuration=_configuration() if configuration is None else configuration,
        http_transport=http_transport,
    )


def _run_failure(
    transport: httpx.AsyncBaseTransport,
    *,
    payload: bytes = b'{"fixed":"payload"}',
) -> LlmTransportError:
    callable_transport = _transport(http_transport=transport)
    with pytest.raises(LlmTransportError) as raised:
        asyncio.run(callable_transport(payload))
    return raised.value


def _set_environment(
    monkeypatch: pytest.MonkeyPatch,
    *,
    api_key: str | None,
) -> None:
    if api_key is None:
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    else:
        monkeypatch.setenv("DEEPSEEK_API_KEY", api_key)


@pytest.mark.spec("GLO-M1C-P0-002")
@pytest.mark.parametrize(
    ("api_key", "expected_code"),
    [
        (None, "LLM_CREDENTIALS_MISSING"),
        ("", "LLM_CREDENTIALS_MISSING"),
        ("   ", "LLM_CREDENTIALS_MISSING"),
        ("secret\nsuffix", "LLM_CONFIG_INVALID"),
        ("secret\rsuffix", "LLM_CONFIG_INVALID"),
    ],
)
def test_preflight_rejects_missing_or_invalid_deepseek_credential(
    monkeypatch: pytest.MonkeyPatch,
    api_key: str | None,
    expected_code: str,
) -> None:
    _set_environment(monkeypatch, api_key=api_key)

    with pytest.raises(LlmConfigurationError) as raised:
        build_json_completion_transport()

    assert raised.value.code == expected_code
    assert "secret" not in str(raised.value)
    assert _ENDPOINT not in str(raised.value)


@pytest.mark.spec("GLO-M1C-P0-002", "GLO-M1C-NFR-002")
def test_preflight_does_not_load_dotenv_and_rejects_non_string_values(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    _set_environment(monkeypatch, api_key=None)
    (tmp_path / ".env").write_text(
        "DEEPSEEK_API_KEY=dotenv-secret\n",
        encoding="utf-8",
    )

    with pytest.raises(LlmConfigurationError) as missing:
        build_json_completion_transport()

    assert missing.value.code == "LLM_CREDENTIALS_MISSING"
    monkeypatch.setattr(llm_config.os, "getenv", lambda _name: object())

    with pytest.raises(LlmConfigurationError) as invalid:
        build_json_completion_transport()

    assert invalid.value.code == "LLM_CONFIG_INVALID"
    assert "dotenv-secret" not in str(missing.value)


@pytest.mark.spec("GLO-M1C-P0-002", "GLO-M1C-NFR-002", "GLO-M1C-NFR-004")
def test_explicit_configuration_owns_the_wire_target_and_ignores_hostile_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "changed-after-construction")
    monkeypatch.setenv("LLM_BASE_URL", "https://attacker.invalid")
    for name in (
        "ALL_PROXY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "all_proxy",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    ):
        monkeypatch.setenv(name, "https://proxy.invalid")
    monkeypatch.setenv("SSL_CERT_FILE", "/attacker/file-ca.pem")
    monkeypatch.setenv("SSL_CERT_DIR", "/attacker/directory-ca")
    real_client = httpx.AsyncClient
    transport_kwargs: list[dict[str, object]] = []
    client_kwargs: list[dict[str, object]] = []
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=b'{"ok":true}', request=request)

    def recording_http_transport(**kwargs: object) -> httpx.AsyncBaseTransport:
        transport_kwargs.append(kwargs)
        return httpx.MockTransport(handler)

    def recording_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        client_kwargs.append(kwargs)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(
        openai_compatible_http.httpx,
        "AsyncHTTPTransport",
        recording_http_transport,
    )
    monkeypatch.setattr(openai_compatible_http.httpx, "AsyncClient", recording_client)
    payload = b'{"fixed":"payload"}'
    transport = _transport()

    assert asyncio.run(transport(payload)) == b'{"ok":true}'
    assert asyncio.run(transport(payload)) == b'{"ok":true}'

    assert transport_kwargs == [
        {"retries": 0, "trust_env": False},
        {"retries": 0, "trust_env": False},
    ]
    assert len(client_kwargs) == 2
    for kwargs in client_kwargs:
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        timeout = kwargs["timeout"]
        assert isinstance(timeout, httpx.Timeout)
        assert {
            timeout.connect,
            timeout.read,
            timeout.write,
            timeout.pool,
        } == {LLM_HTTP_DEADLINE_SECONDS}
    assert len(requests) == 2
    for request in requests:
        assert request.method == "POST"
        assert str(request.url) == _ENDPOINT
        assert request.headers["Authorization"] == f"Bearer {_SECRET}"
        assert request.headers["Accept-Encoding"] == "identity"
        assert request.headers["Content-Type"] == "application/json"
        assert request.content == payload
        assert set(request.extensions["timeout"].values()) == {LLM_HTTP_DEADLINE_SECONDS}


@pytest.mark.spec("GLO-M1C-P0-002", "GLO-M1C-NFR-004")
def test_transport_uses_one_total_deadline_around_request_and_full_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deadline = _DeadlineSpy()
    mock, stream, requests = _mock_transport(
        chunks=(b"first", b"second"),
        active_check=lambda: deadline.active,
    )
    monkeypatch.setattr(openai_compatible_http.asyncio, "timeout", deadline)
    transport = _transport(http_transport=mock)

    result = asyncio.run(transport(b"payload"))

    assert result == b"firstsecond"
    assert deadline.calls == [LLM_HTTP_DEADLINE_SECONDS]
    assert len(requests) == 1
    assert stream.reads == 2
    assert stream.closed


@pytest.mark.spec("GLO-M1C-P0-002", "GLO-M1C-NFR-002")
@pytest.mark.parametrize("status_code", [301, 307, 400, 401, 429, 500])
def test_redirect_and_non_success_status_never_reads_provider_body(status_code: int) -> None:
    mock, stream, requests = _mock_transport(
        status_code=status_code,
        chunks=(b"sensitive-provider-body",),
        explode_after=0,
    )

    error = _run_failure(mock)

    assert error.code is IntentIssueCode.PROVIDER_UNAVAILABLE
    assert "sensitive-provider-body" not in str(error)
    assert _ENDPOINT not in str(error)
    assert len(requests) == 1
    assert stream.reads == 0
    assert stream.closed


@pytest.mark.spec("GLO-M1C-P0-002", "GLO-M1C-NFR-004")
@pytest.mark.parametrize("encoding", ["", "gzip", "identity, gzip"])
def test_unsupported_content_encoding_is_rejected_before_body_read(encoding: str) -> None:
    mock, stream, requests = _mock_transport(
        headers={"Content-Encoding": encoding},
        chunks=(b"sensitive-provider-body",),
        explode_after=0,
    )

    error = _run_failure(mock)

    assert error.code is IntentIssueCode.PROVIDER_RESPONSE_INVALID
    assert len(requests) == 1
    assert stream.reads == 0
    assert stream.closed


@pytest.mark.spec("GLO-M1C-P0-002", "GLO-M1C-NFR-004")
@pytest.mark.parametrize("content_length", ["65537", "-1", "+1", "1, 1", "invalid"])
def test_invalid_or_oversized_content_length_is_rejected_before_body_read(
    content_length: str,
) -> None:
    mock, stream, requests = _mock_transport(
        headers={"Content-Length": content_length},
        chunks=(b"sensitive-provider-body",),
        explode_after=0,
    )

    error = _run_failure(mock)

    assert error.code is IntentIssueCode.PROVIDER_RESPONSE_INVALID
    assert len(requests) == 1
    assert stream.reads == 0
    assert stream.closed


@pytest.mark.spec("GLO-M1C-P0-002", "GLO-M1C-NFR-004")
@pytest.mark.parametrize(
    ("content_length", "chunks", "expected_reads"),
    [
        ("3", (b"{}",), 1),
        ("1", (b"{}", b"must-not-read"), 1),
        ("9" * 5_000, (b"sensitive-provider-body",), 0),
    ],
)
def test_declared_content_length_must_match_the_complete_body(
    content_length: str,
    chunks: tuple[bytes, ...],
    expected_reads: int,
) -> None:
    mock, stream, requests = _mock_transport(
        headers={"Content-Length": content_length},
        chunks=chunks,
        explode_after=expected_reads,
    )

    error = _run_failure(mock)

    assert error.code is IntentIssueCode.PROVIDER_RESPONSE_INVALID
    assert len(requests) == 1
    assert stream.reads == expected_reads
    assert stream.closed


@pytest.mark.spec("GLO-M1C-NFR-004")
@pytest.mark.parametrize("encoding", [None, "identity", "Identity"])
def test_exact_response_body_limit_and_identity_encoding_are_accepted(
    encoding: str | None,
) -> None:
    headers = {"Content-Length": str(_MAX_BODY)}
    if encoding is not None:
        headers["Content-Encoding"] = encoding
    body = b"x" * _MAX_BODY
    mock, stream, _requests = _mock_transport(headers=headers, chunks=(body,))
    transport = _transport(http_transport=mock)

    assert asyncio.run(transport(b"payload")) == body
    assert stream.reads == 1
    assert stream.closed


@pytest.mark.spec("GLO-M1C-NFR-004")
def test_body_reader_stops_on_the_first_byte_over_the_limit() -> None:
    mock, stream, requests = _mock_transport(
        chunks=(b"x" * 65_535, b"y", b"z", b"must-not-read"),
        explode_after=3,
    )

    error = _run_failure(mock)

    assert error.code is IntentIssueCode.PROVIDER_RESPONSE_INVALID
    assert len(requests) == 1
    assert stream.reads == 3
    assert stream.closed


@pytest.mark.spec("GLO-M1C-P0-002", "GLO-M1C-NFR-004")
@pytest.mark.parametrize(
    "exception_type",
    [
        httpx.ConnectError,
        httpx.ReadTimeout,
        httpx.WriteTimeout,
        httpx.PoolTimeout,
    ],
)
def test_httpx_failures_are_safely_mapped_without_retry(
    exception_type: type[httpx.HTTPError],
) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise exception_type("sensitive HTTPX detail", request=request)

    error = _run_failure(httpx.MockTransport(handler))

    assert error.code is IntentIssueCode.PROVIDER_UNAVAILABLE
    assert "sensitive HTTPX detail" not in str(error)
    assert len(requests) == 1


@pytest.mark.spec("GLO-M1C-P0-002", "GLO-M1C-NFR-004")
def test_total_timeout_is_safely_mapped(monkeypatch: pytest.MonkeyPatch) -> None:
    deadline = _DeadlineSpy(fail_on_exit=True)
    mock, stream, _requests = _mock_transport()
    monkeypatch.setattr(openai_compatible_http.asyncio, "timeout", deadline)
    transport = _transport(http_transport=mock)

    with pytest.raises(LlmTransportError) as raised:
        asyncio.run(transport(b"payload"))

    assert raised.value.code is IntentIssueCode.PROVIDER_UNAVAILABLE
    assert "sensitive timeout detail" not in str(raised.value)
    assert deadline.calls == [LLM_HTTP_DEADLINE_SECONDS]
    assert stream.closed
