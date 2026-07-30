"""Isolated M2c BGE OpenSearch indexes, rebuilt only from validated M1d assets."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, cast

from glodex.adapters.agent_indexes import EMBEDDING_DIMENSIONS, AgentIndexes
from glodex.adapters.m2a_opensearch import M2aOpenSearch, M2aOpenSearchError
from glodex.adapters.m2c_model_service import M2cModelServiceClient, M2cModelServiceError
from glodex.domain.catalog import EntityKind, StockStatus
from glodex.m2c_contract import M2C_EMBEDDING_MODEL, M2cModelIdentity

M2C_INDEX_SCHEMA: Final = "glodex.m2c-index-manifest.v1"
M2C_PROFILE_SCHEMA: Final = "glodex.m2c-profile.v1"
M2C_PIPELINE_ID: Final = "glodex-m2c-hybrid-v1"
PRODUCT_ALIAS: Final = "glodex-m2c-product"
CARD_ALIAS: Final = "glodex-m2c-card"
PROFILE_ALIAS: Final = "glodex-m2c-profile-v1"

_INDEX_PREFIX: Final = "glodex-m2c"
_MAX_DOCUMENTS: Final = 32


class M2cIndexError(RuntimeError):
    """A safe M2c index build/verification failure."""


@dataclass(frozen=True, slots=True)
class M2cIndexManifest:
    """Immutable binding between validated M1d records and one BGE model manifest."""

    snapshot_version: str
    index_version: str
    product_ids: tuple[str, ...]
    card_ids: tuple[str, ...]
    model_manifest_digest: str
    embedding_model: str = M2C_EMBEDDING_MODEL
    embedding_dimension: int = EMBEDDING_DIMENSIONS
    pipeline_id: str = M2C_PIPELINE_ID
    schema_version: str = M2C_INDEX_SCHEMA

    def __post_init__(self) -> None:
        if (
            not all(
                type(value) is str and value
                for value in (
                    self.snapshot_version,
                    self.index_version,
                    self.model_manifest_digest,
                )
            )
            or self.embedding_model != M2C_EMBEDDING_MODEL
            or self.embedding_dimension != EMBEDDING_DIMENSIONS
            or self.pipeline_id != M2C_PIPELINE_ID
            or self.schema_version != M2C_INDEX_SCHEMA
            or len(self.model_manifest_digest) != 64
            or any(character not in "0123456789abcdef" for character in self.model_manifest_digest)
        ):
            raise ValueError("M2c index manifest is invalid")
        for identities in (self.product_ids, self.card_ids):
            if (
                not identities
                or len(identities) != len(set(identities))
                or any(not item for item in identities)
            ):
                raise ValueError("M2c index identities are invalid")

    @property
    def canonical_payload(self) -> dict[str, object]:
        return {
            "card_ids": list(self.card_ids),
            "embedding_dimension": self.embedding_dimension,
            "embedding_model": self.embedding_model,
            "index_version": self.index_version,
            "model_manifest_digest": self.model_manifest_digest,
            "pipeline_id": self.pipeline_id,
            "product_ids": list(self.product_ids),
            "schema_version": self.schema_version,
            "snapshot_version": self.snapshot_version,
        }

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(
            json.dumps(
                self.canonical_payload,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    def mapping_meta(self, *, kind: str) -> dict[str, object]:
        if kind not in {"product", "card"}:
            raise ValueError("M2c index kind is invalid")
        return {
            "asset_fingerprint": self.fingerprint,
            "builder_version": "m2c-v1",
            "embedding_dimension": self.embedding_dimension,
            "embedding_model": self.embedding_model,
            "kind": kind,
            "manifest": self.canonical_payload,
            "model_manifest_digest": self.model_manifest_digest,
            "pipeline_id": self.pipeline_id,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True, slots=True)
class M2cIndexSet:
    product_index: str
    card_index: str
    profile_index: str
    manifest_fingerprint: str
    product_count: int
    card_count: int
    profile_count: int


def manifest_for(*, indexes: AgentIndexes, identity: M2cModelIdentity) -> M2cIndexManifest:
    if type(indexes) is not AgentIndexes or type(identity) is not M2cModelIdentity:
        raise TypeError("M2c manifest requires exact validated assets and model identity")
    return M2cIndexManifest(
        snapshot_version=indexes.snapshot_version,
        index_version=indexes.index_version,
        product_ids=tuple(product.product_id for product in indexes.batch.products),
        card_ids=tuple(card.card_id for card in indexes.cards),
        model_manifest_digest=identity.manifest_digest,
    )


async def build_indexes(
    *,
    client: M2aOpenSearch,
    indexes: AgentIndexes,
    gpu: M2cModelServiceClient,
) -> M2cIndexSet:
    """Rebuild BGE vectors before atomically publishing only M2c aliases."""

    if type(client) is not M2aOpenSearch or type(indexes) is not AgentIndexes:
        raise TypeError("M2c index build requires exact OpenSearch and asset inputs")
    try:
        identity = await gpu.health()
        manifest = manifest_for(indexes=indexes, identity=identity)
        product_sources = _product_sources(indexes=indexes, manifest=manifest)
        card_sources = _card_sources(indexes=indexes, manifest=manifest)
        products = await _with_bge_vectors(
            sources=product_sources,
            vector_field="item_vector",
            gpu=gpu,
            identity=identity,
        )
        cards = await _with_bge_vectors(
            sources=card_sources,
            vector_field="card_vector",
            gpu=gpu,
            identity=identity,
        )
        product_index = _physical_index("product", manifest.fingerprint)
        card_index = _physical_index("card", manifest.fingerprint)
        profile_index = _physical_index("profile", identity.manifest_digest)
        await client.health()
        await client.put_search_pipeline(pipeline_id=M2C_PIPELINE_ID, body=hybrid_pipeline_body())
        await client.create_index(name=product_index, body=product_mapping(manifest))
        await client.create_index(name=card_index, body=card_mapping(manifest))
        await client.create_index(name=profile_index, body=profile_mapping(identity=identity))
        await client.bulk_index(index=product_index, documents=products)
        await client.bulk_index(index=card_index, documents=cards)
        if await client.count(alias=product_index) != len(products):
            raise M2cIndexError("M2C_INDEX_FAILED")
        if await client.count(alias=card_index) != len(cards):
            raise M2cIndexError("M2C_INDEX_FAILED")
        await client.publish_alias(alias=PRODUCT_ALIAS, index=product_index)
        await client.publish_alias(alias=CARD_ALIAS, index=card_index)
        await client.publish_alias(alias=PROFILE_ALIAS, index=profile_index)
        return await verify_indexes(client=client, indexes=indexes, identity=identity)
    except (M2aOpenSearchError, M2cModelServiceError, M2cIndexError):
        raise M2cIndexError("M2C_INDEX_FAILED") from None
    except Exception:
        raise M2cIndexError("M2C_INDEX_FAILED") from None


async def verify_indexes(
    *,
    client: M2aOpenSearch,
    indexes: AgentIndexes,
    identity: M2cModelIdentity,
) -> M2cIndexSet:
    if (
        type(client) is not M2aOpenSearch
        or type(indexes) is not AgentIndexes
        or type(identity) is not M2cModelIdentity
    ):
        raise TypeError("M2c index verification requires exact validated inputs")
    try:
        manifest = manifest_for(indexes=indexes, identity=identity)
        product_index = await client.alias_target(alias=PRODUCT_ALIAS)
        card_index = await client.alias_target(alias=CARD_ALIAS)
        profile_index = await client.alias_target(alias=PROFILE_ALIAS)
        _verify_mapping_meta(
            await client.mapping(alias=PRODUCT_ALIAS),
            physical_index=product_index,
            expected=manifest.mapping_meta(kind="product"),
        )
        _verify_mapping_meta(
            await client.mapping(alias=CARD_ALIAS),
            physical_index=card_index,
            expected=manifest.mapping_meta(kind="card"),
        )
        expected_profile = {
            "embedding_dimension": EMBEDDING_DIMENSIONS,
            "embedding_model": M2C_EMBEDDING_MODEL,
            "model_manifest_digest": identity.manifest_digest,
            "schema_version": M2C_PROFILE_SCHEMA,
        }
        if (
            _mapping_meta(await client.mapping(alias=PROFILE_ALIAS), profile_index)
            != expected_profile
        ):
            raise ValueError("M2c profile mapping differs from model identity")
        product_count = await client.count(alias=PRODUCT_ALIAS)
        card_count = await client.count(alias=CARD_ALIAS)
        profile_count = await client.count(alias=PROFILE_ALIAS)
        if product_count != len(manifest.product_ids) or card_count != len(manifest.card_ids):
            raise ValueError("M2c document count differs from validated assets")
        return M2cIndexSet(
            product_index=product_index,
            card_index=card_index,
            profile_index=profile_index,
            manifest_fingerprint=manifest.fingerprint,
            product_count=product_count,
            card_count=card_count,
            profile_count=profile_count,
        )
    except (M2aOpenSearchError, M2cIndexError):
        raise M2cIndexError("M2C_INDEX_FAILED") from None
    except Exception:
        raise M2cIndexError("M2C_INDEX_FAILED") from None


def hybrid_pipeline_body() -> dict[str, object]:
    return {
        "description": "Glodex M2c fixed BGE hybrid normalization",
        "phase_results_processors": [
            {
                "normalization-processor": {
                    "normalization": {"technique": "min_max"},
                    "combination": {
                        "technique": "arithmetic_mean",
                        "parameters": {"weights": [0.7, 0.3]},
                    },
                }
            }
        ],
    }


def product_mapping(manifest: M2cIndexManifest) -> dict[str, object]:
    return {
        "settings": _index_settings(),
        "mappings": {
            "_meta": manifest.mapping_meta(kind="product"),
            "properties": {
                "asset_fingerprint": {"type": "keyword"},
                "hard_eligible": {"type": "boolean"},
                "item_vector": _vector_mapping(),
                "platform": {"type": "keyword"},
                "record_key": {"type": "keyword"},
                "search_text": {"type": "text"},
            },
        },
    }


def card_mapping(manifest: M2cIndexManifest) -> dict[str, object]:
    return {
        "settings": _index_settings(),
        "mappings": {
            "_meta": manifest.mapping_meta(kind="card"),
            "properties": {
                "asset_fingerprint": {"type": "keyword"},
                "card_id": {"type": "keyword"},
                "card_vector": _vector_mapping(),
                "category": {"type": "keyword"},
                "search_text": {"type": "text"},
            },
        },
    }


def profile_mapping(*, identity: M2cModelIdentity) -> dict[str, object]:
    return {
        "settings": _index_settings(),
        "mappings": {
            "_meta": {
                "embedding_dimension": EMBEDDING_DIMENSIONS,
                "embedding_model": M2C_EMBEDDING_MODEL,
                "model_manifest_digest": identity.manifest_digest,
                "schema_version": M2C_PROFILE_SCHEMA,
            },
            "properties": {
                "entry_id": {"type": "keyword"},
                "kind": {"type": "keyword"},
                "model_manifest_digest": {"type": "keyword"},
                "profile_id": {"type": "keyword"},
                "scope": {"type": "keyword"},
                "schema_version": {"type": "keyword"},
                "user_vector": _vector_mapping(),
                "value": {"type": "text"},
            },
        },
    }


def _index_settings() -> dict[str, object]:
    return {"index": {"knn": True, "number_of_replicas": 0, "number_of_shards": 1}}


def _vector_mapping() -> dict[str, object]:
    return {
        "type": "knn_vector",
        "dimension": EMBEDDING_DIMENSIONS,
        "method": {
            "engine": "lucene",
            "name": "hnsw",
            "space_type": "cosinesimil",
            "parameters": {"ef_construction": 128, "m": 16},
        },
    }


def _product_sources(
    *, indexes: AgentIndexes, manifest: M2cIndexManifest
) -> tuple[tuple[str, Mapping[str, object]], ...]:
    platforms = {
        record_key: inventory.platform
        for inventory in indexes.platform_inventories
        for record_key in inventory.record_keys
    }
    offers = {offer.product_id: offer for offer in indexes.batch.offers}
    documents: list[tuple[str, Mapping[str, object]]] = []
    for product in indexes.batch.products:
        platform = platforms.get(product.product_id)
        offer = offers.get(product.product_id)
        if platform is None or offer is None:
            raise M2cIndexError("M2C_INDEX_FAILED")
        documents.append(
            (
                product.product_id,
                {
                    "asset_fingerprint": manifest.fingerprint,
                    "hard_eligible": (
                        product.entity_kind is EntityKind.PRIMARY_PRODUCT
                        and offer.stock_status is StockStatus.IN_STOCK
                    ),
                    "platform": platform.value,
                    "record_key": product.product_id,
                    "search_text": "\n".join(
                        (
                            product.title,
                            product.category,
                            product.provider_id.removesuffix("-demo"),
                            *(
                                f"{attribute.name}: {attribute.value}"
                                for attribute in product.attributes
                            ),
                        )
                    ),
                },
            )
        )
    return _bounded_sources(documents)


def _card_sources(
    *, indexes: AgentIndexes, manifest: M2cIndexManifest
) -> tuple[tuple[str, Mapping[str, object]], ...]:
    return _bounded_sources(
        [
            (
                card.card_id,
                {
                    "asset_fingerprint": manifest.fingerprint,
                    "card_id": card.card_id,
                    "category": card.category,
                    "search_text": card.index_text,
                },
            )
            for card in indexes.cards
        ]
    )


def _bounded_sources(
    sources: Sequence[tuple[str, Mapping[str, object]]],
) -> tuple[tuple[str, Mapping[str, object]], ...]:
    if not sources or len(sources) > _MAX_DOCUMENTS:
        raise M2cIndexError("M2C_INDEX_FAILED")
    if len({identity for identity, _source in sources}) != len(sources):
        raise M2cIndexError("M2C_INDEX_FAILED")
    return tuple(sources)


async def _with_bge_vectors(
    *,
    sources: tuple[tuple[str, Mapping[str, object]], ...],
    vector_field: str,
    gpu: M2cModelServiceClient,
    identity: M2cModelIdentity,
) -> tuple[tuple[str, Mapping[str, object]], ...]:
    texts = tuple(_text(source.get("search_text")) for _identifier, source in sources)
    vectors: list[tuple[float, ...]] = []
    for start in range(0, len(texts), identity.max_embedding_texts):
        vectors.extend(
            await gpu.embed_texts(
                texts=texts[start : start + identity.max_embedding_texts], identity=identity
            )
        )
    if len(vectors) != len(sources):
        raise M2cIndexError("M2C_INDEX_FAILED")
    return tuple(
        (
            source_id,
            {**dict(source), vector_field: list(vector)},
        )
        for (source_id, source), vector in zip(sources, vectors, strict=True)
    )


def _physical_index(kind: str, fingerprint: str) -> str:
    if kind not in {"product", "card", "profile"} or len(fingerprint) < 16:
        raise ValueError("M2c physical index identity is invalid")
    return f"{_INDEX_PREFIX}-{kind}-{fingerprint[:24]}"


def _mapping_meta(mapping: Mapping[str, object], physical_index: str) -> dict[str, object]:
    raw_index = mapping.get(physical_index)
    if type(raw_index) is not dict:
        raise ValueError("mapping has no physical index entry")
    mappings = cast("dict[str, object]", raw_index).get("mappings")
    if type(mappings) is not dict:
        raise ValueError("mapping has no mappings entry")
    meta = cast("dict[str, object]", mappings).get("_meta")
    if type(meta) is not dict:
        raise ValueError("mapping has no metadata")
    return cast("dict[str, object]", meta)


def _verify_mapping_meta(
    mapping: Mapping[str, object],
    *,
    physical_index: str,
    expected: Mapping[str, object],
) -> None:
    if _mapping_meta(mapping, physical_index) != dict(expected):
        raise ValueError("M2c mapping differs from its validated manifest")


def _text(value: object) -> str:
    if type(value) is not str or not value:
        raise M2cIndexError("M2C_INDEX_FAILED")
    return value


__all__ = [
    "CARD_ALIAS",
    "M2C_INDEX_SCHEMA",
    "M2C_PIPELINE_ID",
    "M2C_PROFILE_SCHEMA",
    "PRODUCT_ALIAS",
    "PROFILE_ALIAS",
    "M2cIndexError",
    "M2cIndexManifest",
    "M2cIndexSet",
    "build_indexes",
    "card_mapping",
    "hybrid_pipeline_body",
    "manifest_for",
    "product_mapping",
    "profile_mapping",
    "verify_indexes",
]
