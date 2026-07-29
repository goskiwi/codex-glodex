from __future__ import annotations

import base64
import http.client
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

import pytest

from glodex.capture.config import EBAY_CAPTURE_PROFILE, EbayCredentials
from glodex.capture.contracts import CaptureIssueCode
from glodex.capture.ebay_http import EbayHttpClient
from glodex.capture.ports import EbayProviderFailure, EbaySearchPage

pytestmark = [
    pytest.mark.contract,
    pytest.mark.spec(
        "GLO-M1B-P0-001",
        "GLO-M1B-P0-002",
        "GLO-M1B-P0-005",
        "M1B-AC-002",
        "M1B-AC-004",
        "M1B-AC-005",
        "GLO-M1B-NFR-004",
        "GLO-M1B-NFR-005",
    ),
]

_APP_ID = "synthetic-app"
_CERT_ID = "synthetic-cert"
_TOKEN = "synthetic-token"
_DEADLINE_NS = 30_000_000_000


@dataclass
class FakeResponse:
    status: int
    body: bytes = b""
    content_length: str | None = None
    read_error: BaseException | None = None
    closed: bool = False

    def getheader(self, name: str, default: str | None = None) -> str | None:
        if name.lower() == "content-length":
            return self.content_length if self.content_length is not None else default
        return default

    def read(self, amount: int = -1) -> bytes:
        if self.read_error is not None:
            raise self.read_error
        return self.body if amount < 0 else self.body[:amount]

    def close(self) -> None:
        self.closed = True


@dataclass
class FakeConnection:
    response: FakeResponse
    request_error: BaseException | None = None
    response_error: BaseException | None = None
    requests: list[tuple[str, str, bytes | None, dict[str, str]]] = field(default_factory=list)
    closed: bool = False

    def request(
        self,
        method: str,
        url: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        if self.request_error is not None:
            raise self.request_error
        self.requests.append((method, url, body, dict(headers or {})))

    def getresponse(self) -> FakeResponse:
        if self.response_error is not None:
            raise self.response_error
        return self.response

    def close(self) -> None:
        self.closed = True


@dataclass
class FakeFactory:
    connections: list[FakeConnection]
    failure: BaseException | None = None
    calls: list[tuple[str, int, float]] = field(default_factory=list)

    def __call__(self, host: str, port: int, timeout: float) -> FakeConnection:
        self.calls.append((host, port, timeout))
        if self.failure is not None:
            raise self.failure
        return self.connections.pop(0)


def _json_response(payload: object, *, status: int = 200) -> FakeResponse:
    body = json.dumps(payload, separators=(",", ":")).encode()
    return FakeResponse(status=status, body=body, content_length=str(len(body)))


def _client(
    *responses: FakeResponse,
    monotonic_ns: Callable[[], int] = lambda: 0,
) -> tuple[EbayHttpClient, FakeFactory, tuple[FakeConnection, ...]]:
    connections = tuple(FakeConnection(response=response) for response in responses)
    factory = FakeFactory(list(connections))
    return (
        EbayHttpClient(connection_factory=factory, monotonic_ns=monotonic_ns),
        factory,
        connections,
    )


def _fetch(client: EbayHttpClient, query: str = "smartphone") -> object:
    return client.fetch_page(
        query=query,
        credentials=EbayCredentials(app_id=_APP_ID, cert_id=_CERT_ID),
        deadline_ns=_DEADLINE_NS,
    )


def test_happy_path_has_exact_oauth_and_search_wire_shape() -> None:
    client, factory, connections = _client(
        _json_response({"access_token": _TOKEN}),
        _json_response({"itemSummaries": [{"itemId": "synthetic|item|1"}]}),
    )

    result = _fetch(client, query="smart phone & travel")

    assert isinstance(result, EbaySearchPage)
    assert result.received_record_count == 1
    assert result.request_count == 2
    assert factory.calls == [
        ("api.ebay.com", 443, 10.0),
        ("api.ebay.com", 443, 15.0),
    ]

    auth_request = connections[0].requests
    assert len(auth_request) == 1
    auth_method, auth_target, auth_body, auth_headers = auth_request[0]
    assert auth_method == "POST"
    assert auth_target == "/identity/v1/oauth2/token"
    assert auth_body is not None
    assert parse_qs(auth_body.decode(), strict_parsing=True) == {
        "grant_type": ["client_credentials"],
        "scope": ["https://api.ebay.com/oauth/api_scope"],
    }
    expected_basic = base64.b64encode(f"{_APP_ID}:{_CERT_ID}".encode()).decode()
    assert auth_headers == {
        "Accept": "application/json",
        "Accept-Encoding": "identity",
        "Authorization": f"Basic {expected_basic}",
        "Connection": "close",
        "Content-Type": "application/x-www-form-urlencoded",
    }

    search_request = connections[1].requests
    assert len(search_request) == 1
    search_method, search_target, search_body, search_headers = search_request[0]
    assert search_method == "GET"
    assert search_body is None
    split_target = urlsplit(search_target)
    assert split_target.path == "/buy/browse/v1/item_summary/search"
    assert parse_qs(split_target.query, strict_parsing=True) == {
        "q": ["smart phone & travel"],
        "category_ids": ["9355"],
        "limit": ["10"],
        "offset": ["0"],
        "filter": ["buyingOptions:{FIXED_PRICE}"],
    }
    assert search_headers == {
        "Accept": "application/json",
        "Accept-Encoding": "identity",
        "Authorization": f"Bearer {_TOKEN}",
        "Connection": "close",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
    }
    assert all(connection.closed and connection.response.closed for connection in connections)


def test_empty_page_and_next_link_do_not_trigger_another_request() -> None:
    client, factory, connections = _client(
        _json_response({"access_token": _TOKEN}),
        _json_response(
            {
                "itemSummaries": [],
                "next": "https://example.invalid/must-not-follow",
            }
        ),
    )

    result = _fetch(client)

    assert isinstance(result, EbaySearchPage)
    assert result.item_summaries == ()
    assert len(factory.calls) == 2
    assert sum(len(connection.requests) for connection in connections) == 2


@pytest.mark.parametrize(
    ("auth_status", "expected"),
    [
        (302, CaptureIssueCode.PROVIDER_TARGET_REJECTED),
        (401, CaptureIssueCode.PROVIDER_AUTH_REJECTED),
        (500, CaptureIssueCode.PROVIDER_UNAVAILABLE),
    ],
)
def test_auth_statuses_map_to_safe_one_attempt_failures(
    auth_status: int,
    expected: CaptureIssueCode,
) -> None:
    client, _factory, connections = _client(FakeResponse(status=auth_status))

    result = _fetch(client)

    assert result == EbayProviderFailure(issue_code=expected, request_count=1)
    assert connections[0].closed and connections[0].response.closed


@pytest.mark.parametrize(
    "payload",
    [b"{", b"[]", b'{"wrong":"value"}', b'{"access_token":"\\ud800"}'],
)
def test_invalid_auth_payload_maps_to_auth_rejected(payload: bytes) -> None:
    client, _factory, _connections = _client(
        FakeResponse(status=200, body=payload, content_length=str(len(payload)))
    )

    result = _fetch(client)

    assert result == EbayProviderFailure(
        issue_code=CaptureIssueCode.PROVIDER_AUTH_REJECTED,
        request_count=1,
    )


@pytest.mark.parametrize(
    ("search_status", "expected"),
    [
        (302, CaptureIssueCode.PROVIDER_TARGET_REJECTED),
        (429, CaptureIssueCode.PROVIDER_RATE_LIMITED),
        (503, CaptureIssueCode.PROVIDER_UNAVAILABLE),
        (418, CaptureIssueCode.PROVIDER_RESPONSE_INVALID),
    ],
)
def test_search_statuses_map_to_safe_two_attempt_failures(
    search_status: int,
    expected: CaptureIssueCode,
) -> None:
    client, _factory, _connections = _client(
        _json_response({"access_token": _TOKEN}),
        FakeResponse(status=search_status),
    )

    result = _fetch(client)

    assert result == EbayProviderFailure(issue_code=expected, request_count=2)


@pytest.mark.parametrize(
    ("payload", "expected", "received_count"),
    [
        (b"{", CaptureIssueCode.PROVIDER_RESPONSE_INVALID, 0),
        (b"[]", CaptureIssueCode.PROVIDER_RESPONSE_INVALID, 0),
        (b'{"wrong":[]}', CaptureIssueCode.PROVIDER_RESPONSE_INVALID, 0),
        (
            json.dumps({"itemSummaries": [{}] * 11}).encode(),
            CaptureIssueCode.PROVIDER_RESPONSE_LIMIT,
            11,
        ),
    ],
)
def test_invalid_search_payloads_fail_without_raw_body(
    payload: bytes,
    expected: CaptureIssueCode,
    received_count: int,
) -> None:
    client, _factory, _connections = _client(
        _json_response({"access_token": _TOKEN}),
        FakeResponse(status=200, body=payload, content_length=str(len(payload))),
    )

    result = _fetch(client)

    assert result == EbayProviderFailure(
        issue_code=expected,
        request_count=2,
        received_record_count=received_count,
    )
    assert payload.decode(errors="ignore") not in repr(result)


def test_json_integer_limit_maps_to_stable_failures() -> None:
    long_integer = b"1" * 5_000
    auth_payload = b'{"access_token":"token","extra":' + long_integer + b"}"
    search_payload = b'{"itemSummaries":[],"extra":' + long_integer + b"}"
    auth_client, _factory, _connections = _client(
        FakeResponse(
            status=200,
            body=auth_payload,
            content_length=str(len(auth_payload)),
        )
    )
    search_client, _factory, _connections = _client(
        _json_response({"access_token": _TOKEN}),
        FakeResponse(
            status=200,
            body=search_payload,
            content_length=str(len(search_payload)),
        ),
    )

    assert _fetch(auth_client) == EbayProviderFailure(
        issue_code=CaptureIssueCode.PROVIDER_AUTH_REJECTED,
        request_count=1,
    )
    assert _fetch(search_client) == EbayProviderFailure(
        issue_code=CaptureIssueCode.PROVIDER_RESPONSE_INVALID,
        request_count=2,
    )


def test_body_limit_accepts_exact_boundary_and_rejects_one_more_byte() -> None:
    token_prefix = b'{"access_token":"synthetic-token","padding":"'
    token_suffix = b'"}'
    exact_token_body = (
        token_prefix
        + b"x" * (EBAY_CAPTURE_PROFILE.oauth_body_limit_bytes - len(token_prefix) - 2)
        + token_suffix
    )
    exact_search_prefix = b'{"itemSummaries":[]}'
    exact_search_body = exact_search_prefix + b" " * (
        EBAY_CAPTURE_PROFILE.search_body_limit_bytes - len(exact_search_prefix)
    )
    exact_client, _factory, _connections = _client(
        FakeResponse(
            status=200,
            body=exact_token_body,
            content_length=str(len(exact_token_body)),
        ),
        FakeResponse(
            status=200,
            body=exact_search_body,
            content_length=str(len(exact_search_body)),
        ),
    )
    oversized_oauth = b"x" * (EBAY_CAPTURE_PROFILE.oauth_body_limit_bytes + 1)
    oversized_oauth_client, _factory, _connections = _client(
        FakeResponse(
            status=200,
            body=oversized_oauth,
            content_length=str(len(oversized_oauth)),
        )
    )
    oversized_search = b"x" * (EBAY_CAPTURE_PROFILE.search_body_limit_bytes + 1)
    oversized_client, _factory, _connections = _client(
        _json_response({"access_token": _TOKEN}),
        FakeResponse(
            status=200,
            body=oversized_search,
            content_length=str(len(oversized_search)),
        ),
    )

    assert isinstance(_fetch(exact_client), EbaySearchPage)
    assert _fetch(oversized_oauth_client) == EbayProviderFailure(
        issue_code=CaptureIssueCode.PROVIDER_RESPONSE_LIMIT,
        request_count=1,
    )
    assert _fetch(oversized_client) == EbayProviderFailure(
        issue_code=CaptureIssueCode.PROVIDER_RESPONSE_LIMIT,
        request_count=2,
    )


def test_incomplete_body_and_timeout_are_safe_and_connections_close() -> None:
    incomplete_client, _factory, incomplete_connections = _client(
        _json_response({"access_token": _TOKEN}),
        FakeResponse(
            status=200,
            body=b'{"itemSummaries":[]}',
            content_length="999",
        ),
    )
    timeout_client, timeout_factory, _connections = _client()
    timeout_factory.failure = TimeoutError("secret-timeout-detail")

    incomplete = _fetch(incomplete_client)
    timeout = _fetch(timeout_client)

    assert incomplete == EbayProviderFailure(
        issue_code=CaptureIssueCode.PROVIDER_RESPONSE_INCOMPLETE,
        request_count=2,
    )
    assert timeout == EbayProviderFailure(
        issue_code=CaptureIssueCode.PROVIDER_TIMEOUT,
        request_count=1,
    )
    assert all(
        connection.closed and connection.response.closed for connection in incomplete_connections
    )
    assert "secret-timeout-detail" not in repr(timeout)


def test_total_deadline_caps_operation_timeout() -> None:
    clock_values = iter((0, 25_000_000_000))
    client, factory, _connections = _client(
        _json_response({"access_token": _TOKEN}),
        _json_response({"itemSummaries": []}),
        monotonic_ns=lambda: next(clock_values),
    )

    result = _fetch(client)

    assert isinstance(result, EbaySearchPage)
    assert factory.calls == [
        ("api.ebay.com", 443, 10.0),
        ("api.ebay.com", 443, 5.0),
    ]


def test_transport_protocol_failures_are_stable_and_close_connections() -> None:
    bad_status_client, _factory, bad_status_connections = _client(_json_response({"unused": True}))
    bad_status_connections[0].response_error = http.client.BadStatusLine("sentinel-bad-status")
    request_client, _factory, request_connections = _client(_json_response({"unused": True}))
    request_connections[0].request_error = http.client.CannotSendRequest("sentinel-request-error")

    bad_status = _fetch(bad_status_client)
    request_failure = _fetch(request_client)

    assert bad_status == EbayProviderFailure(
        issue_code=CaptureIssueCode.PROVIDER_RESPONSE_INVALID,
        request_count=1,
    )
    assert request_failure == EbayProviderFailure(
        issue_code=CaptureIssueCode.PROVIDER_UNAVAILABLE,
        request_count=1,
    )
    assert bad_status_connections[0].closed
    assert request_connections[0].closed
    assert "sentinel-bad-status" not in repr(bad_status)
    assert "sentinel-request-error" not in repr(request_failure)


def test_query_cannot_replace_fixed_target_or_filters() -> None:
    client, factory, connections = _client(
        _json_response({"access_token": _TOKEN}),
        _json_response({"itemSummaries": []}),
    )

    result = _fetch(client, query="phone&limit=999&redirect=https://evil.invalid")

    assert isinstance(result, EbaySearchPage)
    assert all(host == "api.ebay.com" for host, _port, _timeout in factory.calls)
    search_target = connections[1].requests[0][1]
    query = parse_qs(urlsplit(search_target).query, strict_parsing=True)
    assert query["q"] == ["phone&limit=999&redirect=https://evil.invalid"]
    assert query["limit"] == ["10"]
    assert set(query) == {"q", "category_ids", "limit", "offset", "filter"}


def test_invalid_unicode_query_is_a_stable_failure_without_an_outbound_search() -> None:
    query = "sentinel-query-\ud800"
    client, factory, _connections = _client(_json_response({"access_token": _TOKEN}))

    result = _fetch(client, query=query)

    assert result == EbayProviderFailure(
        issue_code=CaptureIssueCode.PROVIDER_RESPONSE_INVALID,
        request_count=2,
    )
    assert len(factory.calls) == 1
    assert query not in repr(result)


def test_client_retains_no_credentials_or_token_after_failure() -> None:
    secret = "sentinel-secret-must-not-escape"
    client, factory, _connections = _client()
    factory.failure = OSError(secret)

    result = client.fetch_page(
        query="smartphone",
        credentials=EbayCredentials(app_id=secret, cert_id=secret),
        deadline_ns=_DEADLINE_NS,
    )

    assert isinstance(result, EbayProviderFailure)
    assert secret not in repr(client)
    assert secret not in repr(result)
    assert not any(secret in repr(value) for value in vars(client).values() if not callable(value))


def test_unexpected_transport_exceptions_preserve_exact_attempt_stage() -> None:
    secret = "sentinel-unexpected-transport-detail"
    auth_client, auth_factory, _auth_connections = _client()
    auth_factory.failure = RuntimeError(secret)

    auth_result = _fetch(auth_client)

    assert auth_result == EbayProviderFailure(
        issue_code=CaptureIssueCode.PROVIDER_UNAVAILABLE,
        request_count=1,
    )
    assert secret not in repr(auth_result)

    search_client, _search_factory, search_connections = _client(
        _json_response({"access_token": _TOKEN}),
        _json_response({"itemSummaries": []}),
    )
    search_connections[1].request_error = RuntimeError(secret)

    search_result = _fetch(search_client)

    assert search_result == EbayProviderFailure(
        issue_code=CaptureIssueCode.PROVIDER_UNAVAILABLE,
        request_count=2,
    )
    assert secret not in repr(search_result)
    assert all(connection.closed for connection in search_connections)


def test_failure_output_contains_no_credentials_token_query_or_provider_body(
    capsys: pytest.CaptureFixture[str],
) -> None:
    credentials = "sentinel-credential"
    token = "sentinel-token"
    query = "sentinel-query"
    provider_body = b"sentinel-provider-body"
    client, _factory, _connections = _client(
        _json_response({"access_token": token}),
        FakeResponse(
            status=503,
            body=provider_body,
            content_length=str(len(provider_body)),
        ),
    )

    result = client.fetch_page(
        query=query,
        credentials=EbayCredentials(app_id=credentials, cert_id=credentials),
        deadline_ns=_DEADLINE_NS,
    )
    captured = capsys.readouterr()

    public_text = repr(result)
    for secret in (credentials, token, query, provider_body.decode()):
        assert secret not in public_text
        assert secret not in captured.out
        assert secret not in captured.err
