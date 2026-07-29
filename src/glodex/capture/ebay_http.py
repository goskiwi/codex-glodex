"""Fixed, bounded HTTPS transport for the one approved eBay Capture operation."""

from __future__ import annotations

import base64
import http.client
import json
import time
from collections.abc import Callable
from contextlib import suppress
from typing import Protocol, cast
from urllib.parse import urlencode

from glodex.capture.config import EBAY_CAPTURE_PROFILE, EbayCredentials
from glodex.capture.contracts import CaptureIssueCode
from glodex.capture.ports import (
    EbayProviderFailure,
    EbayProviderResult,
    EbaySearchPage,
)


class _HttpResponse(Protocol):
    status: int

    def getheader(self, name: str, default: str | None = None) -> str | None: ...

    def read(self, amount: int = -1) -> bytes: ...

    def close(self) -> None: ...


class _HttpsConnection(Protocol):
    def request(
        self,
        method: str,
        url: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> None: ...

    def getresponse(self) -> _HttpResponse: ...

    def close(self) -> None: ...


class _HttpsConnectionFactory(Protocol):
    def __call__(
        self,
        host: str,
        port: int,
        timeout: float,
    ) -> _HttpsConnection: ...


class _ProviderError(Exception):
    def __init__(
        self,
        issue_code: CaptureIssueCode,
        *,
        received_record_count: int = 0,
    ) -> None:
        super().__init__(issue_code.value)
        self.issue_code = issue_code
        self.received_record_count = received_record_count


def _default_connection(
    host: str,
    port: int,
    timeout: float,
) -> _HttpsConnection:
    return cast(
        _HttpsConnection,
        http.client.HTTPSConnection(host, port, timeout=timeout),
    )


def _status_issue_code(status: int, *, authentication: bool) -> CaptureIssueCode:
    if 300 <= status < 400:
        return CaptureIssueCode.PROVIDER_TARGET_REJECTED
    if status == 429:
        return CaptureIssueCode.PROVIDER_RATE_LIMITED
    if 500 <= status < 600:
        return CaptureIssueCode.PROVIDER_UNAVAILABLE
    if authentication and status in {400, 401, 403}:
        return CaptureIssueCode.PROVIDER_AUTH_REJECTED
    return CaptureIssueCode.PROVIDER_RESPONSE_INVALID


def _parse_content_length(response: _HttpResponse) -> int | None:
    raw_length = response.getheader("Content-Length")
    if raw_length is None:
        return None
    try:
        content_length = int(raw_length, 10)
    except ValueError as error:
        raise _ProviderError(CaptureIssueCode.PROVIDER_RESPONSE_INVALID) from error
    if content_length < 0:
        raise _ProviderError(CaptureIssueCode.PROVIDER_RESPONSE_INVALID)
    return content_length


def _read_bounded(response: _HttpResponse, limit: int) -> bytes:
    content_length = _parse_content_length(response)
    if content_length is not None and content_length > limit:
        raise _ProviderError(CaptureIssueCode.PROVIDER_RESPONSE_LIMIT)

    try:
        body = response.read(limit + 1)
    except TimeoutError as error:
        raise _ProviderError(CaptureIssueCode.PROVIDER_TIMEOUT) from error
    except http.client.IncompleteRead as error:
        raise _ProviderError(CaptureIssueCode.PROVIDER_RESPONSE_INCOMPLETE) from error
    except (OSError, http.client.HTTPException) as error:
        raise _ProviderError(CaptureIssueCode.PROVIDER_RESPONSE_INCOMPLETE) from error

    if len(body) > limit:
        raise _ProviderError(CaptureIssueCode.PROVIDER_RESPONSE_LIMIT)
    if content_length is not None and len(body) != content_length:
        raise _ProviderError(CaptureIssueCode.PROVIDER_RESPONSE_INCOMPLETE)
    return body


def _decode_json(
    body: bytes,
    *,
    invalid_code: CaptureIssueCode,
) -> object:
    try:
        return cast(object, json.loads(body.decode("utf-8")))
    except (ValueError, RecursionError) as error:
        raise _ProviderError(invalid_code) from error


class EbayHttpClient:
    """Perform exactly one OAuth attempt followed by at most one Search attempt."""

    def __init__(
        self,
        *,
        connection_factory: _HttpsConnectionFactory | None = None,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        self._connection_factory: _HttpsConnectionFactory = (
            _default_connection if connection_factory is None else connection_factory
        )
        self._monotonic_ns = monotonic_ns

    def fetch_page(
        self,
        *,
        query: str,
        credentials: EbayCredentials,
        deadline_ns: int,
    ) -> EbayProviderResult:
        """Fetch one complete fixed-size eBay Browse Search page."""

        try:
            token = self._authenticate(credentials=credentials, deadline_ns=deadline_ns)
        except _ProviderError as error:
            return EbayProviderFailure(
                issue_code=error.issue_code,
                request_count=1,
                received_record_count=error.received_record_count,
            )
        except Exception:
            return EbayProviderFailure(
                issue_code=CaptureIssueCode.PROVIDER_UNAVAILABLE,
                request_count=1,
            )

        try:
            return self._search(
                query=query,
                token=token,
                deadline_ns=deadline_ns,
            )
        except _ProviderError as error:
            return EbayProviderFailure(
                issue_code=error.issue_code,
                request_count=2,
                received_record_count=error.received_record_count,
            )
        except Exception:
            return EbayProviderFailure(
                issue_code=CaptureIssueCode.PROVIDER_UNAVAILABLE,
                request_count=2,
            )

    def _authenticate(
        self,
        *,
        credentials: EbayCredentials,
        deadline_ns: int,
    ) -> str:
        try:
            basic_value = base64.b64encode(
                f"{credentials.app_id}:{credentials.cert_id}".encode()
            ).decode("ascii")
        except UnicodeError as error:
            raise _ProviderError(CaptureIssueCode.PROVIDER_AUTH_REJECTED) from error
        body = urlencode(
            (
                ("grant_type", "client_credentials"),
                ("scope", EBAY_CAPTURE_PROFILE.oauth_scope),
            )
        ).encode("ascii")
        response_body = self._exchange(
            method="POST",
            target=EBAY_CAPTURE_PROFILE.auth_path,
            body=body,
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "Authorization": f"Basic {basic_value}",
                "Connection": "close",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            body_limit=EBAY_CAPTURE_PROFILE.oauth_body_limit_bytes,
            operation_timeout=EBAY_CAPTURE_PROFILE.oauth_timeout_seconds,
            deadline_ns=deadline_ns,
            authentication=True,
        )
        decoded = _decode_json(
            response_body,
            invalid_code=CaptureIssueCode.PROVIDER_AUTH_REJECTED,
        )
        if type(decoded) is not dict:
            raise _ProviderError(CaptureIssueCode.PROVIDER_AUTH_REJECTED)
        root = cast(dict[str, object], decoded)
        token = root.get("access_token")
        if (
            type(token) is not str
            or not token
            or token != token.strip()
            or not token.isascii()
            or any(not 0x21 <= ord(character) <= 0x7E for character in token)
        ):
            raise _ProviderError(CaptureIssueCode.PROVIDER_AUTH_REJECTED)
        return token

    def _search(
        self,
        *,
        query: str,
        token: str,
        deadline_ns: int,
    ) -> EbaySearchPage:
        try:
            query_string = urlencode(
                (
                    ("q", query),
                    ("category_ids", EBAY_CAPTURE_PROFILE.category_id),
                    ("limit", str(EBAY_CAPTURE_PROFILE.limit)),
                    ("offset", str(EBAY_CAPTURE_PROFILE.offset)),
                    ("filter", EBAY_CAPTURE_PROFILE.buying_option_filter),
                )
            )
        except UnicodeError as error:
            raise _ProviderError(CaptureIssueCode.PROVIDER_RESPONSE_INVALID) from error
        response_body = self._exchange(
            method="GET",
            target=f"{EBAY_CAPTURE_PROFILE.search_path}?{query_string}",
            body=None,
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "Authorization": f"Bearer {token}",
                "Connection": "close",
                "X-EBAY-C-MARKETPLACE-ID": EBAY_CAPTURE_PROFILE.marketplace,
            },
            body_limit=EBAY_CAPTURE_PROFILE.search_body_limit_bytes,
            operation_timeout=EBAY_CAPTURE_PROFILE.search_timeout_seconds,
            deadline_ns=deadline_ns,
            authentication=False,
        )
        decoded = _decode_json(
            response_body,
            invalid_code=CaptureIssueCode.PROVIDER_RESPONSE_INVALID,
        )
        if type(decoded) is not dict:
            raise _ProviderError(CaptureIssueCode.PROVIDER_RESPONSE_INVALID)
        root = cast(dict[str, object], decoded)
        item_summaries = root.get("itemSummaries")
        if type(item_summaries) is not list:
            raise _ProviderError(CaptureIssueCode.PROVIDER_RESPONSE_INVALID)
        items = cast(list[object], item_summaries)
        if len(items) > EBAY_CAPTURE_PROFILE.limit:
            raise _ProviderError(
                CaptureIssueCode.PROVIDER_RESPONSE_LIMIT,
                received_record_count=len(items),
            )
        return EbaySearchPage(item_summaries=tuple(items))

    def _exchange(
        self,
        *,
        method: str,
        target: str,
        body: bytes | None,
        headers: dict[str, str],
        body_limit: int,
        operation_timeout: int,
        deadline_ns: int,
        authentication: bool,
    ) -> bytes:
        timeout = self._bounded_timeout(
            operation_timeout=operation_timeout,
            deadline_ns=deadline_ns,
        )
        connection: _HttpsConnection | None = None
        response: _HttpResponse | None = None
        try:
            connection = self._connection_factory(
                EBAY_CAPTURE_PROFILE.https_host,
                EBAY_CAPTURE_PROFILE.https_port,
                timeout,
            )
            connection.request(method, target, body=body, headers=headers)
            response = connection.getresponse()
            if type(response.status) is not int or not 100 <= response.status < 600:
                raise _ProviderError(CaptureIssueCode.PROVIDER_RESPONSE_INVALID)
            if not 200 <= response.status < 300:
                raise _ProviderError(
                    _status_issue_code(
                        response.status,
                        authentication=authentication,
                    )
                )
            return _read_bounded(response, body_limit)
        except _ProviderError:
            raise
        except TimeoutError as error:
            raise _ProviderError(CaptureIssueCode.PROVIDER_TIMEOUT) from error
        except http.client.IncompleteRead as error:
            raise _ProviderError(CaptureIssueCode.PROVIDER_RESPONSE_INCOMPLETE) from error
        except http.client.RemoteDisconnected as error:
            raise _ProviderError(CaptureIssueCode.PROVIDER_RESPONSE_INCOMPLETE) from error
        except (
            http.client.BadStatusLine,
            http.client.LineTooLong,
            http.client.UnknownProtocol,
            http.client.UnknownTransferEncoding,
        ) as error:
            raise _ProviderError(CaptureIssueCode.PROVIDER_RESPONSE_INVALID) from error
        except ValueError as error:
            issue_code = (
                CaptureIssueCode.PROVIDER_AUTH_REJECTED
                if authentication
                else CaptureIssueCode.PROVIDER_RESPONSE_INVALID
            )
            raise _ProviderError(issue_code) from error
        except (OSError, http.client.HTTPException) as error:
            raise _ProviderError(CaptureIssueCode.PROVIDER_UNAVAILABLE) from error
        finally:
            if response is not None:
                self._close_response(response)
            if connection is not None:
                self._close_connection(connection)

    def _bounded_timeout(
        self,
        *,
        operation_timeout: int,
        deadline_ns: int,
    ) -> float:
        remaining_ns = deadline_ns - self._monotonic_ns()
        if remaining_ns <= 0:
            raise _ProviderError(CaptureIssueCode.PROVIDER_TIMEOUT)
        return min(float(operation_timeout), remaining_ns / 1_000_000_000)

    @staticmethod
    def _close_response(response: _HttpResponse) -> None:
        with suppress(OSError, http.client.HTTPException):
            response.close()

    @staticmethod
    def _close_connection(connection: _HttpsConnection) -> None:
        with suppress(OSError, http.client.HTTPException):
            connection.close()


__all__ = ["EbayHttpClient"]
