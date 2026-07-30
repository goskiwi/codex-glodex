"""Versioned M2a OpenSearch index manifests built only from validated M1d assets."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, cast

from glodex.adapters.agent_indexes import EMBEDDING_DIMENSIONS, AgentIndexes
from glodex.adapters.m2a_opensearch import M2aOpenSearch, M2aOpenSearchError
from glodex.application.agent.ports import ToolPortError
from glodex.domain.catalog import EntityKind, StockStatus

M2A_INDEX_SCHEMA: Final = "glodex.m2a-index-manifest.v1"
M2A_PROFILE_SCHEMA: Final = "glodex.m2a-profile.v1"
M2A_PIPELINE_ID: Final = "glodex-m2a-hybrid-v1"
PRODUCT_ALIAS: Final = "glodex-m2a-product"
CARD_ALIAS: Final = "glodex-m2a-card"
PROFILE_ALIAS: Final = "glodex-m2a-profile-v1"

_INDEX_PREFIX: Final = "glodex-m2a"
_MAX_DOCUMENTS: Final = 32


class M2aIndexError(RuntimeError):
    """A stable local index build/verification failure."""


@dataclass(frozen=True, slots=True)
class M2aIndexManifest:
    """The complete identity of one immutable M2a Product/Card index build."""

    snapshot_version: str
    index_version: str
    embedding_model: str
    embedding_dimension: int
    product_ids: tuple[str, ...]
    card_ids: tuple[str, ...]
    pipeline_id: str = M2A_PIPELINE_ID
    schema_version: str = M2A_INDEX_SCHEMA

    def __post_init__(self) -> None:
        if (
            type(self.snapshot_version) is not str
            or type(self.index_version) is not str
            or type(self.embedding_model) is not str
            or not self.snapshot_version
            or not self.index_version
            or not self.embedding_model
            or self.embedding_dimension != EMBEDDING_DIMENSIONS
            or self.pipeline_id != M2A_PIPELINE_ID
            or self.schema_version != M2A_INDEX_SCHEMA
        ):
            raise ValueError("M2a index manifest is invalid")
        for values in (self.product_ids, self.card_ids):
            if not values or len(values) != len(set(values)) or any(not item for item in values):
                raise ValueError("M2a index manifest identities are invalid")

    @property
    def canonical_payload(self) -> dict[str, object]:
        return {
            "card_ids": list(self.card_ids),
            "embedding_dimension": self.embedding_dimension,
            "embedding_model": self.embedding_model,
            "index_version": self.index_version,
            "pipeline_id": self.pipeline_id,
            "product_ids": list(self.product_ids),
            "schema_version": self.schema_version,
            "snapshot_version": self.snapshot_version,
        }

    @property
    def fingerprint(self) -> str:
        encoded = json.dumps(
            self.canonical_payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def mapping_meta(self, *, kind: str) -> dict[str, object]:
        if kind not in {"product", "card"}:
            raise ValueError("M2a index kind is invalid")
        return {
            "asset_fingerprint": self.fingerprint,
            "builder_version": "m2a-v1",
            "embedding_dimension": self.embedding_dimension,
            "embedding_model": self.embedding_model,
            "kind": kind,
            "manifest": self.canonical_payload,
            "pipeline_id": self.pipeline_id,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True, slots=True)
class M2aIndexSet:
    product_index: str
    card_index: str
    profile_index: str
    manifest_fingerprint: str
    product_count: int
    card_count: int
    profile_count: int


def manifest_for(indexes: AgentIndexes) -> M2aIndexManifest:
    """Derive a deterministic manifest without reopening or trusting raw assets."""

    if type(indexes) is not AgentIndexes:
        raise TypeError("M2a manifest requires exact AgentIndexes")
    return M2aIndexManifest(
        snapshot_version=indexes.snapshot_version,
        index_version=indexes.index_version,
        embedding_model=indexes.embedding_model,
        embedding_dimension=EMBEDDING_DIMENSIONS,
        product_ids=tuple(product.product_id for product in indexes.batch.products),
        card_ids=tuple(card.card_id for card in indexes.cards),
    )


def hybrid_pipeline_body() -> dict[str, object]:
    """Return the one named native-hybrid normalization pipeline body."""

    return {
        "description": "Glodex M2a fixed vector-first hybrid normalization",
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


def product_mapping(manifest: M2aIndexManifest) -> dict[str, object]:
    return {
        "settings": {
            "index": {
                "knn": True,
                "number_of_replicas": 0,
                "number_of_shards": 1,
            }
        },
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


def card_mapping(manifest: M2aIndexManifest) -> dict[str, object]:
    return {
        "settings": {
            "index": {
                "knn": True,
                "number_of_replicas": 0,
                "number_of_shards": 1,
            }
        },
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


def profile_mapping() -> dict[str, object]:
    return {
        "settings": {
            "index": {
                "knn": True,
                "number_of_replicas": 0,
                "number_of_shards": 1,
            }
        },
        "mappings": {
            "_meta": {"schema_version": M2A_PROFILE_SCHEMA},
            "properties": {
                "entry_id": {"type": "keyword"},
                "kind": {"type": "keyword"},
                "profile_id": {"type": "keyword"},
                "scope": {"type": "keyword"},
                "schema_version": {"type": "keyword"},
                "user_vector": _vector_mapping(),
                "value": {"type": "text"},
            },
        },
    }


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


async def build_indexes(*, client: M2aOpenSearch, indexes: AgentIndexes) -> M2aIndexSet:
    """Build/reuse fixed indexes, then publish aliases only after a complete bulk write."""

    if type(client) is not M2aOpenSearch or type(indexes) is not AgentIndexes:
        raise TypeError("M2a index build requires exact client and AgentIndexes")
    try:
        manifest = manifest_for(indexes)
        product_index = _physical_index("product", manifest.fingerprint)
        card_index = _physical_index("card", manifest.fingerprint)
        profile_index = _physical_index("profile", M2A_PROFILE_SCHEMA)
        products = _product_documents(indexes, manifest)
        cards = _card_documents(indexes, manifest)
        await client.health()
        await client.put_search_pipeline(pipeline_id=M2A_PIPELINE_ID, body=hybrid_pipeline_body())
        await client.create_index(name=product_index, body=product_mapping(manifest))
        await client.create_index(name=card_index, body=card_mapping(manifest))
        await client.create_index(name=profile_index, body=profile_mapping())
        await client.bulk_index(index=product_index, documents=products)
        await client.bulk_index(index=card_index, documents=cards)
        if await client.count(alias=product_index) != len(products):
            raise M2aIndexError("M2A_RETRIEVAL_FAILED")
        if await client.count(alias=card_index) != len(cards):
            raise M2aIndexError("M2A_RETRIEVAL_FAILED")
        await client.publish_alias(alias=PRODUCT_ALIAS, index=product_index)
        await client.publish_alias(alias=CARD_ALIAS, index=card_index)
        await client.publish_alias(alias=PROFILE_ALIAS, index=profile_index)
        return await verify_indexes(client=client, indexes=indexes)
    except (M2aOpenSearchError, ToolPortError, M2aIndexError):
        raise M2aIndexError("M2A_RETRIEVAL_FAILED") from None
    except Exception:
        raise M2aIndexError("M2A_RETRIEVAL_FAILED") from None


async def verify_indexes(*, client: M2aOpenSearch, indexes: AgentIndexes) -> M2aIndexSet:
    """Verify every published alias, mapping identity and count before M2a use."""

    if type(client) is not M2aOpenSearch or type(indexes) is not AgentIndexes:
        raise TypeError("M2a index verification requires exact client and AgentIndexes")
    try:
        manifest = manifest_for(indexes)
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
        profile_meta = _mapping_meta(await client.mapping(alias=PROFILE_ALIAS), profile_index)
        if profile_meta != {"schema_version": M2A_PROFILE_SCHEMA}:
            raise ValueError("profile schema does not match")
        product_count = await client.count(alias=PRODUCT_ALIAS)
        card_count = await client.count(alias=CARD_ALIAS)
        profile_count = await client.count(alias=PROFILE_ALIAS)
        if product_count != len(indexes.batch.products) or card_count != len(indexes.cards):
            raise ValueError("published M2a index count does not match assets")
        return M2aIndexSet(
            product_index=product_index,
            card_index=card_index,
            profile_index=profile_index,
            manifest_fingerprint=manifest.fingerprint,
            product_count=product_count,
            card_count=card_count,
            profile_count=profile_count,
        )
    except (M2aOpenSearchError, M2aIndexError):
        raise M2aIndexError("M2A_RETRIEVAL_FAILED") from None
    except Exception:
        raise M2aIndexError("M2A_RETRIEVAL_FAILED") from None


def _product_documents(
    indexes: AgentIndexes,
    manifest: M2aIndexManifest,
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
            raise M2aIndexError("M2A_RETRIEVAL_FAILED")
        search_text = "\n".join(
            (
                product.title,
                product.category,
                product.provider_id.removesuffix("-demo"),
                *(f"{attribute.name}: {attribute.value}" for attribute in product.attributes),
            )
        )
        hard_eligible = (
            product.entity_kind is EntityKind.PRIMARY_PRODUCT
            and offer.stock_status is StockStatus.IN_STOCK
        )
        documents.append(
            (
                product.product_id,
                {
                    "asset_fingerprint": manifest.fingerprint,
                    "hard_eligible": hard_eligible,
                    "item_vector": list(indexes.item_vector(product.product_id)),
                    "platform": platform.value,
                    "record_key": product.product_id,
                    "search_text": search_text,
                },
            )
        )
    if not documents or len(documents) > _MAX_DOCUMENTS:
        raise M2aIndexError("M2A_RETRIEVAL_FAILED")
    return tuple(documents)


def _card_documents(
    indexes: AgentIndexes,
    manifest: M2aIndexManifest,
) -> tuple[tuple[str, Mapping[str, object]], ...]:
    documents = tuple(
        (
            card.card_id,
            {
                "asset_fingerprint": manifest.fingerprint,
                "card_id": card.card_id,
                "card_vector": list(indexes.card_vector(card.card_id)),
                "category": card.category,
                "search_text": card.index_text,
            },
        )
        for card in indexes.cards
    )
    if not documents or len(documents) > _MAX_DOCUMENTS:
        raise M2aIndexError("M2A_RETRIEVAL_FAILED")
    return documents


def _physical_index(kind: str, fingerprint: str) -> str:
    if kind not in {"product", "card", "profile"} or not fingerprint:
        raise ValueError("M2a physical index identity is invalid")
    safe = "".join(character for character in fingerprint.lower() if character.isalnum())
    if len(safe) < 16:
        raise ValueError("M2a physical index fingerprint is invalid")
    return f"{_INDEX_PREFIX}-{kind}-{safe[:24]}"


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
        raise ValueError("published mapping manifest differs from validated assets")


__all__ = [
    "CARD_ALIAS",
    "M2A_INDEX_SCHEMA",
    "M2A_PIPELINE_ID",
    "M2A_PROFILE_SCHEMA",
    "PRODUCT_ALIAS",
    "PROFILE_ALIAS",
    "M2aIndexError",
    "M2aIndexManifest",
    "M2aIndexSet",
    "build_indexes",
    "card_mapping",
    "hybrid_pipeline_body",
    "manifest_for",
    "product_mapping",
    "profile_mapping",
    "verify_indexes",
]
