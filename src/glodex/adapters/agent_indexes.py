"""Hash-closed M1d demo indexes with deterministic BM25/cosine/RRF retrieval."""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Final, Never, cast
from urllib.parse import urlsplit

from glodex.adapters.local_snapshot import LocalSnapshotCatalog
from glodex.application.agent.contracts import (
    PLATFORM_SET,
    CategoryInsightInput,
    CategoryInsightOutput,
    EmbeddingResult,
    InsightDepth,
    InsightStatus,
    Platform,
    ToolFailureCode,
)
from glodex.application.agent.ports import ToolPortError
from glodex.domain.catalog import CatalogBatch, Product

AGENT_ASSET_SCHEMA: Final = "glodex.agent-assets-manifest.v1"
CATEGORY_CARD_SCHEMA: Final = "glodex.category-card.v1"
CATEGORY_EMBEDDING_SCHEMA: Final = "glodex.category-embedding.v1"
ITEM_EMBEDDING_SCHEMA: Final = "glodex.item-embedding.v1"
SHIPPING_RULES_SCHEMA: Final = "glodex.shipping-rules.v1"
M1D_DEMO_VERSION: Final = "m1d-demo-v1"
M1D_INDEX_VERSION: Final = "m1d-demo-index-v1"
M1D_RULESET_VERSION: Final = "m1d-cn-shipping-v1"
EMBEDDING_MODEL: Final = "text-embedding-v4"
EMBEDDING_DIMENSIONS: Final = 1_024

_MAX_MANIFEST_BYTES: Final = 1 * 1024 * 1024
_MAX_ASSET_BYTES: Final = 8 * 1024 * 1024
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._:-]{0,127})\Z")
_ASCII_WORD = re.compile(r"[a-z0-9]+")
_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
_DECIMAL = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?\Z")
_SNAPSHOT_FILES: Final = {
    "manifest": "manifest.json",
    "products": "products.jsonl",
    "offers": "offers.jsonl",
    "evidence": "evidence.jsonl",
    "exchange_rates": "exchange_rates.json",
}
_AGENT_FILES: Final = {
    "category_cards": "category_cards.jsonl",
    "category_embeddings": "category_embeddings.jsonl",
    "item_embeddings": "item_embeddings.jsonl",
    "shipping_rules": "shipping_rules.json",
}
_CATEGORY_COVERAGE: Final = frozenset(
    {
        "laptop",
        "tablet",
        "phone",
        "laptop-accessory",
        "laptop-part",
        "laptop-decoration",
    }
)
_MANIFEST_KEYS: Final = frozenset(
    {
        "agent_files",
        "card_sources",
        "category_coverage",
        "created_at",
        "data_mode",
        "embedding",
        "generator",
        "index_version",
        "non_live_statement",
        "platforms",
        "reducer",
        "ruleset_version",
        "schema_version",
        "snapshot_files",
        "snapshot_version",
    }
)
_FILE_KEYS: Final = frozenset({"path", "record_count", "sha256"})
_EMBEDDING_KEYS: Final = frozenset({"dimensions", "model", "provenance"})
_GENERATOR_KEYS: Final = frozenset({"name", "version"})
_REDUCER_KEYS: Final = frozenset(
    {
        "bm25_b",
        "bm25_k1",
        "lexical_top_k",
        "normalization",
        "rrf_k",
        "vector_top_k",
    }
)
_PLATFORM_KEYS: Final = frozenset({"provider_ids", "record_keys"})
_CARD_SOURCE_KEYS: Final = frozenset(
    {
        "captured_at",
        "card_id",
        "source_confidence",
        "source_domain",
        "source_url",
    }
)
_CARD_KEYS: Final = frozenset(
    {
        "attributes",
        "bestsellers",
        "captured_at",
        "card_id",
        "category",
        "components",
        "price_tiers",
        "schema_version",
        "source_confidence",
        "source_domain",
        "source_url",
        "summary",
    }
)
_CATEGORY_VECTOR_KEYS: Final = frozenset({"card_id", "checksum", "schema_version", "vector"})
_ITEM_VECTOR_KEYS: Final = frozenset({"checksum", "record_key", "schema_version", "vector"})
_SHIPPING_KEYS: Final = frozenset(
    {
        "currency",
        "destination_country",
        "effective_date",
        "rules",
        "ruleset_version",
        "schema_version",
    }
)
_SHIPPING_RULE_KEYS: Final = frozenset(
    {
        "base_shipping",
        "duty_rate",
        "duty_threshold",
        "eta_max_days",
        "eta_min_days",
        "platform",
    }
)


@dataclass(frozen=True, slots=True)
class CategoryCard:
    """One validated, source-bound local category card."""

    card_id: str
    category: str
    summary: str
    components: tuple[str, ...]
    bestsellers: tuple[str, ...]
    attributes: tuple[str, ...]
    price_tiers: tuple[str, ...]
    source_url: str
    source_domain: str
    captured_at: datetime
    source_confidence: Decimal

    @property
    def index_text(self) -> str:
        return "\n".join(
            (
                self.category,
                self.summary,
                self.source_domain,
                *self.components,
                *self.bestsellers,
                *self.attributes,
                *self.price_tiers,
            )
        )


@dataclass(frozen=True, slots=True)
class PlatformInventory:
    platform: Platform
    provider_ids: tuple[str, ...]
    record_keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ItemIndexHit:
    """One stable exact-cosine result; full facts remain in ``CatalogBatch``."""

    record_key: str
    score: float


@dataclass(frozen=True, slots=True)
class ShippingRule:
    platform: Platform
    base_shipping: Decimal
    duty_rate: Decimal
    duty_threshold: Decimal
    effective_date: date
    eta_min_days: int
    eta_max_days: int


@dataclass(frozen=True, slots=True)
class _CardVector:
    card_id: str
    vector: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class _ItemVector:
    record_key: str
    vector: tuple[float, ...]


@dataclass(frozen=True, slots=True)
class _RankedCard:
    card: CategoryCard
    contribution: Decimal


@dataclass(frozen=True, slots=True)
class AgentIndexes:
    """Validated immutable M1d assets and their bounded retrieval operations."""

    snapshot_version: str
    index_version: str
    ruleset_version: str
    embedding_model: str
    embedding_provenance: str
    batch: CatalogBatch
    cards: tuple[CategoryCard, ...]
    platform_inventories: tuple[PlatformInventory, ...]
    shipping_rules: tuple[ShippingRule, ...]
    _card_vectors: tuple[_CardVector, ...]
    _item_vectors: tuple[_ItemVector, ...]

    async def retrieve(self, request: CategoryInsightInput) -> CategoryInsightOutput:
        """Run fixed lexical/vector fusion and reduce only retrieved Card facts."""

        if type(request) is not CategoryInsightInput or request.index_version != self.index_version:
            raise ToolPortError(ToolFailureCode.INDEX_INVALID)
        try:
            ranked = self._rank_cards(
                category=request.category,
                query=f"{request.category} {request.query}",
                query_vector=request.query_vector,
                depth=request.depth,
            )
            if not ranked:
                return CategoryInsightOutput(status=InsightStatus.NO_INSIGHT)
            limits = (3, 3, 5, 3) if request.depth is InsightDepth.QUICK else (8, 5, 12, 5)
            confidence_weight = sum(
                (item.contribution for item in ranked),
                start=Decimal(0),
            )
            confidence = (
                sum(
                    (item.card.source_confidence * item.contribution for item in ranked),
                    start=Decimal(0),
                )
                / confidence_weight
            ).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
            return CategoryInsightOutput(
                status=InsightStatus.FOUND,
                components=_reduce_facts(ranked, "components", limits[0]),
                bestsellers=_reduce_facts(ranked, "bestsellers", limits[1]),
                attributes=_reduce_facts(ranked, "attributes", limits[2]),
                price_tiers=_reduce_facts(ranked, "price_tiers", limits[3]),
                confidence=confidence,
                card_ids=tuple(item.card.card_id for item in ranked),
            )
        except ToolPortError:
            raise
        except Exception:
            raise ToolPortError(ToolFailureCode.INDEX_INVALID) from None

    def _rank_cards(
        self,
        *,
        category: str,
        query: str,
        query_vector: tuple[float, ...],
        depth: InsightDepth,
    ) -> tuple[_RankedCard, ...]:
        EmbeddingResult(vectors=(query_vector,))
        eligible_cards = tuple(card for card in self.cards if card.category == category)
        documents = tuple(tokenize_index_text(card.index_text) for card in eligible_cards)
        query_tokens = tokenize_index_text(query)
        lexical = _bm25_rank(eligible_cards, documents, query_tokens)
        vectors = {item.card_id: item.vector for item in self._card_vectors}
        semantic = tuple(
            sorted(
                (
                    (card.card_id, _dot(query_vector, vectors[card.card_id]))
                    for card in eligible_cards
                    if _dot(query_vector, vectors[card.card_id]) > 0
                ),
                key=lambda item: (-item[1], item[0]),
            )[:30]
        )
        contributions: dict[str, Decimal] = {}
        for ranking in (lexical, semantic):
            for rank, (card_id, _score) in enumerate(ranking, start=1):
                contributions[card_id] = contributions.get(card_id, Decimal(0)) + (
                    Decimal(1) / Decimal(60 + rank)
                )
        by_id = {card.card_id: card for card in eligible_cards}
        ordered = sorted(
            contributions.items(),
            key=lambda item: (-item[1], item[0]),
        )
        limit = 8 if depth is InsightDepth.QUICK else 15
        return tuple(
            _RankedCard(card=by_id[card_id], contribution=contribution)
            for card_id, contribution in ordered[:limit]
        )

    def search_items(
        self,
        *,
        platform: Platform,
        query_vector: tuple[float, ...],
        top_k: int,
    ) -> tuple[ItemIndexHit, ...]:
        """Filter by trusted platform ownership, then rank by exact cosine."""

        if type(platform) is not Platform:
            raise ToolPortError(ToolFailureCode.ITEM_SOURCE_INVALID)
        if type(top_k) is not int or isinstance(top_k, bool) or not 1 <= top_k <= 50:
            raise ToolPortError(ToolFailureCode.ITEM_SOURCE_INVALID)
        try:
            EmbeddingResult(vectors=(query_vector,))
            inventory = next(
                item for item in self.platform_inventories if item.platform is platform
            )
            allowed = frozenset(inventory.record_keys)
            ranked = sorted(
                (
                    ItemIndexHit(
                        record_key=item.record_key,
                        score=_dot(query_vector, item.vector),
                    )
                    for item in self._item_vectors
                    if item.record_key in allowed
                ),
                key=lambda item: (-item.score, item.record_key),
            )
            return tuple(ranked[:top_k])
        except ToolPortError:
            raise
        except Exception:
            raise ToolPortError(ToolFailureCode.INDEX_INVALID) from None

    def platform_inventory(self, platform: Platform) -> PlatformInventory:
        try:
            return next(item for item in self.platform_inventories if item.platform is platform)
        except StopIteration:
            raise ToolPortError(ToolFailureCode.INDEX_INVALID) from None

    def item_vector(self, record_key: str) -> tuple[float, ...]:
        """Expose one already-validated Item projection for an opt-in index builder."""

        if type(record_key) is not str:
            raise ToolPortError(ToolFailureCode.INDEX_INVALID)
        try:
            return next(item.vector for item in self._item_vectors if item.record_key == record_key)
        except StopIteration:
            raise ToolPortError(ToolFailureCode.INDEX_INVALID) from None

    def card_vector(self, card_id: str) -> tuple[float, ...]:
        """Expose one already-validated Card projection for an opt-in index builder."""

        if type(card_id) is not str:
            raise ToolPortError(ToolFailureCode.INDEX_INVALID)
        try:
            return next(item.vector for item in self._card_vectors if item.card_id == card_id)
        except StopIteration:
            raise ToolPortError(ToolFailureCode.INDEX_INVALID) from None

    def reduce_card_ids(
        self,
        *,
        category: str,
        card_ids: tuple[str, ...],
        depth: InsightDepth,
    ) -> CategoryInsightOutput:
        """Reduce only validated, category-scoped Card IDs from an external ranker."""

        if (
            type(category) is not str
            or type(card_ids) is not tuple
            or any(type(card_id) is not str for card_id in card_ids)
            or len(card_ids) != len(set(card_ids))
            or type(depth) is not InsightDepth
        ):
            raise ToolPortError(ToolFailureCode.INDEX_INVALID)
        try:
            by_id = {card.card_id: card for card in self.cards if card.category == category}
            if not card_ids:
                return CategoryInsightOutput(status=InsightStatus.NO_INSIGHT)
            if not set(card_ids).issubset(by_id):
                raise ValueError("external Card IDs are not trusted for this category")
            ranked = tuple(
                _RankedCard(
                    card=by_id[card_id],
                    contribution=Decimal(1) / Decimal(60 + rank),
                )
                for rank, card_id in enumerate(card_ids, start=1)
            )
            limits = (3, 3, 5, 3) if depth is InsightDepth.QUICK else (8, 5, 12, 5)
            confidence_weight = sum((item.contribution for item in ranked), start=Decimal(0))
            confidence = (
                sum(
                    (item.card.source_confidence * item.contribution for item in ranked),
                    start=Decimal(0),
                )
                / confidence_weight
            ).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
            return CategoryInsightOutput(
                status=InsightStatus.FOUND,
                components=_reduce_facts(ranked, "components", limits[0]),
                bestsellers=_reduce_facts(ranked, "bestsellers", limits[1]),
                attributes=_reduce_facts(ranked, "attributes", limits[2]),
                price_tiers=_reduce_facts(ranked, "price_tiers", limits[3]),
                confidence=confidence,
                card_ids=card_ids,
            )
        except ToolPortError:
            raise
        except Exception:
            raise ToolPortError(ToolFailureCode.INDEX_INVALID) from None


async def load_agent_indexes(
    *,
    snapshot_root: Path | str,
    agent_root: Path | str,
    version: str = M1D_DEMO_VERSION,
) -> AgentIndexes:
    """Load one exact hash-closed M1d asset version or fail with a safe code."""

    try:
        if version != M1D_DEMO_VERSION:
            raise ValueError("unsupported M1d asset version")
        snapshot_directory = _version_directory(snapshot_root, version)
        agent_directory = _version_directory(agent_root, version)
        manifest = _object(
            _read_json(agent_directory / "manifest.json", _MAX_MANIFEST_BYTES),
            _MANIFEST_KEYS,
        )
        _validate_manifest_header(manifest)
        _validate_file_group(
            manifest["snapshot_files"],
            directory=snapshot_directory,
            expected=_SNAPSHOT_FILES,
        )
        _validate_file_group(
            manifest["agent_files"],
            directory=agent_directory,
            expected=_AGENT_FILES,
        )

        batch = await LocalSnapshotCatalog(Path(snapshot_root)).load(
            version,
            display_currency="CNY",
            budget_currency="CNY",
        )
        if batch.fatal_issues or batch.quarantine_issues or not batch.products or not batch.offers:
            raise ValueError("snapshot is not a complete validated demo batch")

        cards = _load_cards(agent_directory / _AGENT_FILES["category_cards"])
        platform_inventories = _load_platform_inventories(manifest["platforms"])
        _validate_platform_ownership(batch, platform_inventories)
        card_vectors = _load_card_vectors(
            agent_directory / _AGENT_FILES["category_embeddings"],
            cards,
        )
        item_vectors = _load_item_vectors(
            agent_directory / _AGENT_FILES["item_embeddings"],
            batch,
            platform_inventories,
        )
        shipping_rules = _load_shipping_rules(agent_directory / _AGENT_FILES["shipping_rules"])
        _validate_card_manifest(manifest, cards)
        return AgentIndexes(
            snapshot_version=version,
            index_version=M1D_INDEX_VERSION,
            ruleset_version=M1D_RULESET_VERSION,
            embedding_model=EMBEDDING_MODEL,
            embedding_provenance=_embedding_provenance(manifest),
            batch=batch,
            cards=cards,
            platform_inventories=platform_inventories,
            shipping_rules=shipping_rules,
            _card_vectors=card_vectors,
            _item_vectors=item_vectors,
        )
    except ToolPortError:
        raise
    except Exception:
        raise ToolPortError(ToolFailureCode.INDEX_INVALID) from None


def tokenize_index_text(text: str) -> tuple[str, ...]:
    """Apply NFKC, ASCII lowercase, ASCII word and CJK bigram tokenization."""

    if type(text) is not str:
        raise TypeError("index text must be a string")
    normalized = unicodedata.normalize("NFKC", text)
    lowered = "".join(
        character.lower() if character.isascii() else character for character in normalized
    )
    tokens: list[tuple[int, str]] = []
    for match in _ASCII_WORD.finditer(lowered):
        tokens.append((match.start(), match.group()))
    for match in _CJK_RUN.finditer(lowered):
        run = match.group()
        if len(run) == 1:
            tokens.append((match.start(), run))
        else:
            tokens.extend(
                (match.start() + offset, run[offset : offset + 2]) for offset in range(len(run) - 1)
            )
    return tuple(token for _offset, token in sorted(tokens, key=lambda item: item[0]))


def _bm25_rank(
    cards: tuple[CategoryCard, ...],
    documents: tuple[tuple[str, ...], ...],
    query_tokens: tuple[str, ...],
) -> tuple[tuple[str, float], ...]:
    if not cards or not query_tokens:
        return ()
    document_count = len(documents)
    average_length = sum(len(document) for document in documents) / document_count
    document_frequencies = Counter(token for document in documents for token in frozenset(document))
    query_frequency = Counter(query_tokens)
    scored: list[tuple[str, float]] = []
    for card, document in zip(cards, documents, strict=True):
        term_frequency = Counter(document)
        score = 0.0
        length_ratio = len(document) / average_length if average_length else 0.0
        for token, query_count in query_frequency.items():
            frequency = term_frequency[token]
            if frequency == 0:
                continue
            document_frequency = document_frequencies[token]
            inverse_document_frequency = math.log(
                1 + (document_count - document_frequency + 0.5) / (document_frequency + 0.5)
            )
            denominator = frequency + 1.2 * (1 - 0.75 + 0.75 * length_ratio)
            score += (
                inverse_document_frequency * (frequency * (1.2 + 1) / denominator) * query_count
            )
        if score > 0:
            scored.append((card.card_id, score))
    return tuple(sorted(scored, key=lambda item: (-item[1], item[0]))[:30])


def _reduce_facts(
    ranked: tuple[_RankedCard, ...],
    attribute: str,
    limit: int,
) -> tuple[str, ...]:
    contributions: dict[str, tuple[Decimal, str]] = {}
    for item in ranked:
        values = cast("tuple[str, ...]", getattr(item.card, attribute))
        for value in values:
            previous = contributions.get(value)
            if previous is None:
                contributions[value] = (item.contribution, item.card.card_id)
            else:
                contributions[value] = (
                    previous[0] + item.contribution,
                    min(previous[1], item.card.card_id),
                )
    return tuple(
        value
        for value, _ranking in sorted(
            contributions.items(),
            key=lambda item: (-item[1][0], item[1][1], item[0]),
        )[:limit]
    )


def _version_directory(root: Path | str, version: str) -> Path:
    if not isinstance(root, (Path, str)):
        raise TypeError("asset root must be a path")
    resolved_root = Path(root).expanduser().resolve()
    directory = (resolved_root / version).resolve()
    if resolved_root not in directory.parents or not directory.is_dir():
        raise ValueError("asset version directory is unavailable")
    return directory


def _validate_manifest_header(manifest: dict[str, object]) -> None:
    if (
        manifest["schema_version"] != AGENT_ASSET_SCHEMA
        or manifest["snapshot_version"] != M1D_DEMO_VERSION
        or manifest["index_version"] != M1D_INDEX_VERSION
        or manifest["ruleset_version"] != M1D_RULESET_VERSION
        or manifest["data_mode"] != "DEMO_SNAPSHOT"
    ):
        raise ValueError("unsupported M1d asset manifest")
    _utc(manifest["created_at"])
    statement = _text(manifest["non_live_statement"], maximum=512)
    if "not a real-time marketplace API" not in statement:
        raise ValueError("demo manifest must disclose non-live data")
    generator = _object(manifest["generator"], _GENERATOR_KEYS)
    _text(generator["name"], maximum=128)
    _text(generator["version"], maximum=128)
    embedding = _object(manifest["embedding"], _EMBEDDING_KEYS)
    if (
        embedding["model"] != EMBEDDING_MODEL
        or embedding["dimensions"] != EMBEDDING_DIMENSIONS
        or embedding["provenance"] not in {"DETERMINISTIC_DEMO_FIXTURE", "DASHSCOPE_OPERATOR_BUILD"}
    ):
        raise ValueError("unsupported embedding manifest")
    reducer = _object(manifest["reducer"], _REDUCER_KEYS)
    if reducer != {
        "bm25_b": "0.75",
        "bm25_k1": "1.2",
        "lexical_top_k": 30,
        "normalization": "NFKC_ASCII_LOWER_CJK_BIGRAM",
        "rrf_k": 60,
        "vector_top_k": 30,
    }:
        raise ValueError("unsupported reducer manifest")
    coverage = frozenset(_string_list(manifest["category_coverage"], maximum=128))
    if not _CATEGORY_COVERAGE.issubset(coverage):
        raise ValueError("category card coverage is incomplete")


def _embedding_provenance(manifest: dict[str, object]) -> str:
    embedding = _object(manifest["embedding"], _EMBEDDING_KEYS)
    return _text(embedding["provenance"], maximum=128)


def _validate_file_group(
    value: object,
    *,
    directory: Path,
    expected: dict[str, str],
) -> None:
    root = _object(value, frozenset(expected))
    for role, expected_name in expected.items():
        spec = _object(root[role], _FILE_KEYS)
        path_text = _text(spec["path"], maximum=128)
        if path_text != expected_name or PurePosixPath(path_text).name != path_text:
            raise ValueError("asset file path is not fixed")
        digest = _text(spec["sha256"], maximum=64)
        if _SHA256.fullmatch(digest) is None:
            raise ValueError("asset hash is invalid")
        count = spec["record_count"]
        if type(count) is not int or isinstance(count, bool) or count < 0:
            raise ValueError("asset record count is invalid")
        path = (directory / path_text).resolve()
        if directory not in path.parents or not path.is_file():
            raise ValueError("asset file is unavailable")
        payload = path.read_bytes()
        if len(payload) > _MAX_ASSET_BYTES or hashlib.sha256(payload).hexdigest() != digest:
            raise ValueError("asset hash or size is invalid")
        if role in {"manifest", "shipping_rules"}:
            if role == "manifest":
                actual_count = 1
            else:
                rules = _object(_parse_json(payload), _SHIPPING_KEYS)["rules"]
                if type(rules) is not list:
                    raise ValueError("shipping rules must be a list")
                actual_count = len(rules)
        elif role == "exchange_rates":
            decoded = _parse_json(payload)
            if type(decoded) is not dict:
                raise ValueError("exchange-rate asset must be an object")
            rates = cast("dict[str, object]", decoded)["rates"]
            if type(rates) is not list:
                raise ValueError("exchange rates must be a list")
            actual_count = len(rates)
        else:
            actual_count = len(payload.splitlines())
        if actual_count != count:
            raise ValueError("asset record count is invalid")


def _load_cards(path: Path) -> tuple[CategoryCard, ...]:
    cards: list[CategoryCard] = []
    for value in _read_jsonl(path):
        item = _object(value, _CARD_KEYS)
        if item["schema_version"] != CATEGORY_CARD_SCHEMA:
            raise ValueError("unsupported category card schema")
        card_id = _identifier(item["card_id"])
        source_url = _https_url(item["source_url"])
        source_domain = _text(item["source_domain"], maximum=128)
        parsed = urlsplit(source_url)
        if parsed.hostname != source_domain:
            raise ValueError("category card source domain mismatch")
        confidence = _decimal(item["source_confidence"], maximum=Decimal(1))
        cards.append(
            CategoryCard(
                card_id=card_id,
                category=_text(item["category"], maximum=128),
                summary=_text(item["summary"], maximum=2_000),
                components=_unique_strings(item["components"], maximum=128),
                bestsellers=_unique_strings(item["bestsellers"], maximum=128),
                attributes=_unique_strings(item["attributes"], maximum=128),
                price_tiers=_unique_strings(item["price_tiers"], maximum=128),
                source_url=source_url,
                source_domain=source_domain,
                captured_at=_utc(item["captured_at"]),
                source_confidence=confidence,
            )
        )
    ids = tuple(card.card_id for card in cards)
    if not cards or len(ids) != len(set(ids)):
        raise ValueError("category card IDs must be non-empty and unique")
    return tuple(cards)


def _load_platform_inventories(value: object) -> tuple[PlatformInventory, ...]:
    root = _object(value, frozenset(platform.value for platform in PLATFORM_SET))
    inventories: list[PlatformInventory] = []
    all_providers: set[str] = set()
    all_records: set[str] = set()
    for platform in PLATFORM_SET:
        item = _object(root[platform.value], _PLATFORM_KEYS)
        providers = _unique_strings(item["provider_ids"], maximum=128)
        records = _unique_strings(item["record_keys"], maximum=128)
        if not providers or not records:
            raise ValueError("each demo platform must be non-empty")
        if all_providers.intersection(providers) or all_records.intersection(records):
            raise ValueError("platform ownership must be unique")
        all_providers.update(providers)
        all_records.update(records)
        inventories.append(
            PlatformInventory(
                platform=platform,
                provider_ids=providers,
                record_keys=records,
            )
        )
    return tuple(inventories)


def _validate_platform_ownership(
    batch: CatalogBatch,
    inventories: tuple[PlatformInventory, ...],
) -> None:
    by_record = {
        record_key: inventory for inventory in inventories for record_key in inventory.record_keys
    }
    by_product = {product.product_id: product for product in batch.products}
    if set(by_record) != set(by_product):
        raise ValueError("platform record ownership must cover every product exactly once")
    for record_key, inventory in by_record.items():
        product = by_product[record_key]
        if product.provider_id not in inventory.provider_ids:
            raise ValueError("product provider does not match platform manifest")
    offer_product_ids = {offer.product_id for offer in batch.offers}
    if offer_product_ids != set(by_product):
        raise ValueError("every demo product must have an offer")
    for offer in batch.offers:
        product = by_product[offer.product_id]
        if offer.provider_id != product.provider_id:
            raise ValueError("offer provider does not match product")


def _load_card_vectors(
    path: Path,
    cards: tuple[CategoryCard, ...],
) -> tuple[_CardVector, ...]:
    by_id = {card.card_id: card for card in cards}
    vectors: list[_CardVector] = []
    for value in _read_jsonl(path):
        item = _object(value, _CATEGORY_VECTOR_KEYS)
        if item["schema_version"] != CATEGORY_EMBEDDING_SCHEMA:
            raise ValueError("unsupported category embedding schema")
        card_id = _identifier(item["card_id"])
        card = by_id.get(card_id)
        if card is None or item["checksum"] != _checksum(card.index_text):
            raise ValueError("category embedding checksum is invalid")
        vectors.append(_CardVector(card_id=card_id, vector=_vector(item["vector"])))
    ids = tuple(item.card_id for item in vectors)
    if len(ids) != len(set(ids)) or set(ids) != set(by_id):
        raise ValueError("category embeddings must cover cards exactly once")
    return tuple(vectors)


def _load_item_vectors(
    path: Path,
    batch: CatalogBatch,
    inventories: tuple[PlatformInventory, ...],
) -> tuple[_ItemVector, ...]:
    products = {product.product_id: product for product in batch.products}
    platforms = {
        record_key: inventory.platform
        for inventory in inventories
        for record_key in inventory.record_keys
    }
    vectors: list[_ItemVector] = []
    for value in _read_jsonl(path):
        item = _object(value, _ITEM_VECTOR_KEYS)
        if item["schema_version"] != ITEM_EMBEDDING_SCHEMA:
            raise ValueError("unsupported item embedding schema")
        record_key = _identifier(item["record_key"])
        product = products.get(record_key)
        platform = platforms.get(record_key)
        if (
            product is None
            or platform is None
            or item["checksum"] != _checksum(_item_text(product, platform))
        ):
            raise ValueError("item embedding checksum is invalid")
        vectors.append(_ItemVector(record_key=record_key, vector=_vector(item["vector"])))
    keys = tuple(item.record_key for item in vectors)
    if len(keys) != len(set(keys)) or set(keys) != set(products):
        raise ValueError("item embeddings must cover products exactly once")
    return tuple(vectors)


def _item_text(product: Product, platform: Platform) -> str:
    attributes = {attribute.name: attribute.value for attribute in product.attributes}
    return "\n".join(
        (
            product.title,
            product.category,
            platform.value,
            attributes["verified_signal"],
            attributes["pack_note"],
        )
    )


def _load_shipping_rules(path: Path) -> tuple[ShippingRule, ...]:
    root = _object(_read_json(path, _MAX_ASSET_BYTES), _SHIPPING_KEYS)
    if (
        root["schema_version"] != SHIPPING_RULES_SCHEMA
        or root["ruleset_version"] != M1D_RULESET_VERSION
        or root["destination_country"] != "CN"
        or root["currency"] != "CNY"
    ):
        raise ValueError("unsupported shipping rules")
    effective_date = _date(root["effective_date"])
    raw_rules = root["rules"]
    if type(raw_rules) is not list:
        raise ValueError("shipping rules must be a list")
    rules: list[ShippingRule] = []
    for raw_rule in raw_rules:
        item = _object(raw_rule, _SHIPPING_RULE_KEYS)
        eta_min = _integer(item["eta_min_days"], minimum=0)
        eta_max = _integer(item["eta_max_days"], minimum=eta_min)
        rules.append(
            ShippingRule(
                platform=Platform(_text(item["platform"], maximum=32)),
                base_shipping=_decimal(item["base_shipping"]),
                duty_rate=_decimal(item["duty_rate"], maximum=Decimal(1)),
                duty_threshold=_decimal(item["duty_threshold"]),
                effective_date=effective_date,
                eta_min_days=eta_min,
                eta_max_days=eta_max,
            )
        )
    if tuple(rule.platform for rule in rules) != PLATFORM_SET:
        raise ValueError("shipping rules must cover platforms in fixed order")
    return tuple(rules)


def _validate_card_manifest(
    manifest: dict[str, object],
    cards: tuple[CategoryCard, ...],
) -> None:
    raw_sources = manifest["card_sources"]
    if type(raw_sources) is not list:
        raise ValueError("card_sources must be a list")
    expected = tuple(
        (
            card.card_id,
            card.source_url,
            card.source_domain,
            card.captured_at,
            card.source_confidence,
        )
        for card in cards
    )
    actual = tuple(
        (
            _identifier(source["card_id"]),
            _https_url(source["source_url"]),
            _text(source["source_domain"], maximum=128),
            _utc(source["captured_at"]),
            _decimal(source["source_confidence"], maximum=Decimal(1)),
        )
        for raw_source in raw_sources
        for source in (_object(raw_source, _CARD_SOURCE_KEYS),)
    )
    if actual != expected:
        raise ValueError("card source manifest does not match category cards")


def _read_json(path: Path, maximum: int) -> object:
    payload = path.read_bytes()
    if len(payload) > maximum:
        raise ValueError("JSON asset is too large")
    return _parse_json(payload)


def _read_jsonl(path: Path) -> tuple[object, ...]:
    payload = path.read_bytes()
    if len(payload) > _MAX_ASSET_BYTES:
        raise ValueError("JSONL asset is too large")
    if not payload or not payload.endswith(b"\n"):
        raise ValueError("JSONL asset must be non-empty canonical lines")
    return tuple(_parse_json(line) for line in payload.splitlines())


def _parse_json(payload: bytes) -> object:
    return json.loads(
        payload.decode("utf-8"),
        object_pairs_hook=_unique_object,
        parse_constant=_reject_constant,
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_value: str) -> Never:
    raise ValueError("non-standard JSON number")


def _object(value: object, keys: frozenset[str]) -> dict[str, object]:
    if type(value) is not dict:
        raise ValueError("JSON value must be an object")
    result = cast("dict[str, object]", value)
    if frozenset(result) != keys:
        raise ValueError("JSON object keys do not match the schema")
    return result


def _text(value: object, *, maximum: int) -> str:
    if (
        type(value) is not str
        or not value.strip()
        or value != value.strip()
        or len(value) > maximum
        or any(character in value for character in ("\0", "\r"))
        or any(0xD800 <= ord(character) <= 0xDFFF for character in value)
    ):
        raise ValueError("invalid text")
    return value


def _identifier(value: object) -> str:
    result = _text(value, maximum=128)
    if _IDENTIFIER.fullmatch(result) is None:
        raise ValueError("invalid identifier")
    return result


def _string_list(value: object, *, maximum: int) -> tuple[str, ...]:
    if type(value) is not list:
        raise ValueError("value must be a list")
    return tuple(_text(item, maximum=maximum) for item in value)


def _unique_strings(value: object, *, maximum: int) -> tuple[str, ...]:
    result = _string_list(value, maximum=maximum)
    if len(result) != len(set(result)):
        raise ValueError("string collection must be unique")
    return result


def _vector(value: object) -> tuple[float, ...]:
    if type(value) is not list:
        raise ValueError("embedding vector must be a list")
    result = tuple(value)
    EmbeddingResult(vectors=(result,))
    return cast("tuple[float, ...]", result)


def _dot(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    return math.fsum(a * b for a, b in zip(left, right, strict=True))


def _checksum(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _decimal(value: object, *, maximum: Decimal | None = None) -> Decimal:
    if type(value) is not str or _DECIMAL.fullmatch(value) is None:
        raise ValueError("invalid canonical decimal")
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise ValueError("invalid decimal") from None
    if not result.is_finite() or result < 0 or (maximum is not None and result > maximum):
        raise ValueError("decimal is outside its allowed range")
    return result


def _integer(value: object, *, minimum: int) -> int:
    if type(value) is not int or isinstance(value, bool) or value < minimum:
        raise ValueError("invalid integer")
    return value


def _utc(value: object) -> datetime:
    text = _text(value, maximum=64)
    if not text.endswith("Z"):
        raise ValueError("timestamp must be UTC")
    parsed = datetime.fromisoformat(f"{text[:-1]}+00:00")
    offset = parsed.utcoffset()
    if offset is None or offset.total_seconds() != 0:
        raise ValueError("timestamp must be UTC")
    return parsed


def _date(value: object) -> date:
    text = _text(value, maximum=10)
    parsed = datetime.strptime(text, "%Y-%m-%d").date()
    if parsed.isoformat() != text:
        raise ValueError("invalid date")
    return parsed


def _https_url(value: object) -> str:
    result = _text(value, maximum=2_048)
    parsed = urlsplit(result)
    if (
        parsed.scheme != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("source URL must be safe HTTPS without query or fragment")
    return result


__all__ = [
    "AGENT_ASSET_SCHEMA",
    "EMBEDDING_DIMENSIONS",
    "EMBEDDING_MODEL",
    "M1D_DEMO_VERSION",
    "M1D_INDEX_VERSION",
    "M1D_RULESET_VERSION",
    "AgentIndexes",
    "CategoryCard",
    "ItemIndexHit",
    "PlatformInventory",
    "ShippingRule",
    "load_agent_indexes",
    "tokenize_index_text",
]
