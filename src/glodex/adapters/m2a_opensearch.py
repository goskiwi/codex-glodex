"""Bounded, loopback-only Async OpenSearch adapter for the opt-in M2a path."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, cast
from urllib.parse import SplitResult, urlsplit

_DEFAULT_ENDPOINT: Final = "http://127.0.0.1:9200"
_MAX_BODY_BYTES: Final = 1_048_576
_REQUEST_TIMEOUT_SECONDS: Final = 5.0
_LOOPBACK_HOSTS: Final = frozenset({"127.0.0.1", "::1"})


class M2aOpenSearchError(RuntimeError):
    """A stable OpenSearch failure that contains no server response body."""


def validate_loopback_endpoint(value: object) -> str:
    """Return one canonical local endpoint or reject every non-loopback shape."""

    if type(value) is not str or not value or len(value) > 128:
        raise ValueError("M2a OpenSearch endpoint is invalid")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in _LOOPBACK_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or parsed.port != 9200
    ):
        raise ValueError("M2a OpenSearch endpoint must be the fixed loopback service")
    return f"http://{_render_host(parsed)}:9200"


def _render_host(parsed: SplitResult) -> str:
    host = parsed.hostname
    if host == "::1":
        return "[::1]"
    if host == "127.0.0.1":
        return host
    raise ValueError("M2a OpenSearch endpoint host is invalid")


def _json_bytes(value: object) -> bytes:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(payload) > _MAX_BODY_BYTES:
        raise M2aOpenSearchError("M2A_RETRIEVAL_FAILED")
    return payload


def _object(value: object) -> dict[str, object]:
    if type(value) is not dict:
        raise M2aOpenSearchError("M2A_RETRIEVAL_FAILED")
    return cast("dict[str, object]", value)


@dataclass(slots=True)
class M2aOpenSearch:
    """One owned async client with a deliberately tiny M2a command surface."""

    endpoint: str = _DEFAULT_ENDPOINT
    _client: Any | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.endpoint = validate_loopback_endpoint(self.endpoint)

    async def __aenter__(self) -> M2aOpenSearch:
        await self.open()
        return self

    async def __aexit__(self, *_arguments: object) -> None:
        await self.close()

    async def open(self) -> None:
        if self._client is not None:
            return
        try:
            from opensearchpy._async.client import AsyncOpenSearch

            parsed = urlsplit(self.endpoint)
            self._client = AsyncOpenSearch(
                hosts=[{"host": parsed.hostname, "port": 9200, "scheme": "http"}],
                timeout=_REQUEST_TIMEOUT_SECONDS,
                max_retries=0,
                retry_on_timeout=False,
                use_ssl=False,
                verify_certs=False,
                ssl_assert_hostname=False,
            )
        except Exception as error:
            raise M2aOpenSearchError("M2A_RETRIEVAL_FAILED") from error

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is None:
            return
        try:
            await client.close()
        except Exception:
            # Closing a failed local socket must not obscure the primary safe code.
            return

    async def health(self) -> dict[str, object]:
        client = await self._require_client()
        try:
            # ``opensearch-py`` 2.8 forwards the API-level ``timeout`` keyword
            # to aiohttp as the transport timeout.  Supplying the OpenSearch
            # duration string (``"5s"``) therefore fails before the request is
            # sent.  The client itself already owns the fixed five-second
            # transport bound configured in ``open``.
            return _object(await client.cluster.health(wait_for_status="yellow"))
        except Exception as error:
            raise M2aOpenSearchError("M2A_RETRIEVAL_FAILED") from error

    async def create_index(self, *, name: str, body: Mapping[str, object]) -> None:
        client = await self._require_client()
        _json_bytes(body)
        try:
            exists = await client.indices.exists(index=name)
            if not exists:
                await client.indices.create(index=name, body=dict(body))
        except Exception as error:
            raise M2aOpenSearchError("M2A_RETRIEVAL_FAILED") from error

    async def bulk_index(
        self,
        *,
        index: str,
        documents: Sequence[tuple[str, Mapping[str, object]]],
    ) -> None:
        if not documents or len(documents) > 128:
            raise M2aOpenSearchError("M2A_RETRIEVAL_FAILED")
        payload = bytearray()
        for identity, document in documents:
            if type(identity) is not str or not identity:
                raise M2aOpenSearchError("M2A_RETRIEVAL_FAILED")
            payload.extend(_json_bytes({"index": {"_id": identity}}))
            payload.extend(b"\n")
            payload.extend(_json_bytes(dict(document)))
            payload.extend(b"\n")
        if len(payload) > _MAX_BODY_BYTES:
            raise M2aOpenSearchError("M2A_RETRIEVAL_FAILED")
        client = await self._require_client()
        try:
            response = _object(
                await client.bulk(index=index, body=bytes(payload), refresh="wait_for")
            )
            if response.get("errors") is not False:
                raise ValueError("bulk indexing failed")
        except M2aOpenSearchError:
            raise
        except Exception as error:
            raise M2aOpenSearchError("M2A_RETRIEVAL_FAILED") from error

    async def put_search_pipeline(self, *, pipeline_id: str, body: Mapping[str, object]) -> None:
        client = await self._require_client()
        _json_bytes(body)
        try:
            await client.transport.perform_request(
                "PUT",
                f"/_search/pipeline/{pipeline_id}",
                body=_json_bytes(dict(body)),
                headers={"content-type": "application/json"},
            )
        except Exception as error:
            raise M2aOpenSearchError("M2A_RETRIEVAL_FAILED") from error

    async def publish_alias(self, *, alias: str, index: str) -> None:
        client = await self._require_client()
        try:
            try:
                aliases = await client.indices.get_alias(name=alias)
            except Exception as error:
                # The async 2.8 client returns a 404 response payload when
                # ``ignore=[404]`` is supplied.  That payload contains an
                # ``error`` key and was previously treated as an index name
                # during the first publish.  Only an explicit missing-alias
                # response is the normal empty state.
                if getattr(error, "status_code", None) != 404:
                    raise
                aliases = {}
            remove = [
                {"remove": {"index": current, "alias": alias}}
                for current in _object(aliases)
                if current != index
            ]
            actions = [*remove, {"add": {"index": index, "alias": alias}}]
            await client.indices.update_aliases(body={"actions": actions})
        except Exception as error:
            raise M2aOpenSearchError("M2A_RETRIEVAL_FAILED") from error

    async def mapping(self, *, alias: str) -> dict[str, object]:
        client = await self._require_client()
        try:
            return _object(await client.indices.get_mapping(index=alias))
        except Exception as error:
            raise M2aOpenSearchError("M2A_RETRIEVAL_FAILED") from error

    async def alias_target(self, *, alias: str) -> str:
        """Resolve exactly one published alias without accepting wildcard index names."""

        client = await self._require_client()
        try:
            response = _object(await client.indices.get_alias(name=alias))
            names = tuple(name for name in response if type(name) is str)
            if len(names) != 1:
                raise ValueError("alias target is ambiguous")
            aliases = response[names[0]]
            if type(aliases) is not dict:
                raise ValueError("alias target is invalid")
            alias_values = cast("dict[str, object]", aliases).get("aliases")
            if type(alias_values) is not dict or alias not in alias_values:
                raise ValueError("alias target is invalid")
            return names[0]
        except Exception as error:
            raise M2aOpenSearchError("M2A_RETRIEVAL_FAILED") from error

    async def count(self, *, alias: str) -> int:
        client = await self._require_client()
        try:
            response = _object(await client.count(index=alias))
            value = response.get("count")
            if type(value) is not int or isinstance(value, bool) or value < 0:
                raise ValueError("invalid count")
            return value
        except Exception as error:
            raise M2aOpenSearchError("M2A_RETRIEVAL_FAILED") from error

    async def search(self, *, alias: str, body: Mapping[str, object]) -> dict[str, object]:
        client = await self._require_client()
        _json_bytes(body)
        try:
            request_body = dict(body)
            pipeline = request_body.pop("search_pipeline", None)
            if pipeline is not None and (
                type(pipeline) is not str
                or not pipeline
                or len(pipeline) > 128
                or any(
                    character not in "abcdefghijklmnopqrstuvwxyz0123456789-_"
                    for character in pipeline
                )
            ):
                raise ValueError("search pipeline is invalid")
            # In OpenSearch 2.17 the named search pipeline belongs to the
            # Search API parameters, not to the JSON query body.  Keeping the
            # declaration in M2a's query descriptor lets the adapter make that
            # version-specific translation at its sole transport boundary.
            params = {"search_pipeline": pipeline} if pipeline is not None else None
            return _object(await client.search(index=alias, body=request_body, params=params))
        except Exception as error:
            raise M2aOpenSearchError("M2A_RETRIEVAL_FAILED") from error

    async def index_document(
        self,
        *,
        alias: str,
        identity: str,
        body: Mapping[str, object],
    ) -> None:
        client = await self._require_client()
        _json_bytes(body)
        try:
            await client.index(index=alias, id=identity, body=dict(body), refresh="wait_for")
        except Exception as error:
            raise M2aOpenSearchError("M2A_RETRIEVAL_FAILED") from error

    async def delete_document(self, *, alias: str, identity: str) -> bool:
        client = await self._require_client()
        try:
            response = await client.delete(
                index=alias,
                id=identity,
                refresh="wait_for",
                ignore=[404],
            )
            return _object(response).get("result") == "deleted"
        except Exception as error:
            raise M2aOpenSearchError("M2A_RETRIEVAL_FAILED") from error

    async def _require_client(self) -> Any:
        await self.open()
        if self._client is None:
            raise M2aOpenSearchError("M2A_RETRIEVAL_FAILED")
        return self._client


__all__ = ["M2aOpenSearch", "M2aOpenSearchError", "validate_loopback_endpoint"]
