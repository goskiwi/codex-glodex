"""M2c-only BGE retrieval and cross-encoder reranking over isolated aliases."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, cast

from glodex.adapters.agent_indexes import AgentIndexes
from glodex.adapters.agent_item_search import DemoItemSource
from glodex.adapters.dashscope_rerank import RerankDocument, RerankRequest
from glodex.adapters.m2a_opensearch import M2aOpenSearch, M2aOpenSearchError
from glodex.adapters.m2c_indexes import CARD_ALIAS, M2C_PIPELINE_ID, PRODUCT_ALIAS
from glodex.adapters.m2c_model_service import M2cModelServiceError, M2cReranker
from glodex.application.agent.catalog import ManifestRecord
from glodex.application.agent.contracts import (
    CategoryInsightInput,
    CategoryInsightOutput,
    ItemSearchInput,
    ItemSearchRuntimeResult,
    ToolFailureCode,
)
from glodex.application.agent.ports import ToolPortError
from glodex.application.m2c_profile import M2cProfileEntry, profile_conflicts_with_current_query

_QUERY_TOP_K: Final = 30
_USER_TOP_K: Final = 10
_PRODUCT_RERANK_LIMIT: Final = 40
_FINAL_TOP_K: Final = 15


@dataclass(frozen=True, slots=True)
class M2cRetrievalTrace:
    query_candidate_count: int
    user_candidate_count: int
    selected_ids: tuple[str, ...]
    safe_codes: tuple[ToolFailureCode, ...] = ()

    def __post_init__(self) -> None:
        if (
            self.query_candidate_count < 0
            or self.user_candidate_count < 0
            or len(self.selected_ids) != len(set(self.selected_ids))
            or len(self.safe_codes) != len(set(self.safe_codes))
        ):
            raise ValueError("M2c retrieval trace is invalid")


def product_hybrid_body(
    *, query: str, query_vector: tuple[float, ...], platform: str
) -> dict[str, object]:
    filters = [{"term": {"platform": platform}}, {"term": {"hard_eligible": True}}]
    return {
        "_source": ["record_key"],
        "search_pipeline": M2C_PIPELINE_ID,
        "size": _QUERY_TOP_K,
        "query": {
            "hybrid": {
                "queries": [
                    {"bool": {"filter": filters, "must": [{"match": {"search_text": query}}]}},
                    {
                        "knn": {
                            "item_vector": {
                                "filter": {"bool": {"filter": filters}},
                                "k": _QUERY_TOP_K,
                                "vector": list(query_vector),
                            }
                        }
                    },
                ]
            }
        },
    }


def product_user_ann_body(*, vector: tuple[float, ...], platform: str) -> dict[str, object]:
    return {
        "_source": ["record_key"],
        "size": _USER_TOP_K,
        "query": {
            "knn": {
                "item_vector": {
                    "filter": {
                        "bool": {
                            "filter": [
                                {"term": {"platform": platform}},
                                {"term": {"hard_eligible": True}},
                            ]
                        }
                    },
                    "k": _USER_TOP_K,
                    "vector": list(vector),
                }
            }
        },
    }


def card_hybrid_body(
    *, category: str, query: str, query_vector: tuple[float, ...]
) -> dict[str, object]:
    filters = [{"term": {"category": category}}]
    return {
        "_source": ["card_id"],
        "search_pipeline": M2C_PIPELINE_ID,
        "size": _QUERY_TOP_K,
        "query": {
            "hybrid": {
                "queries": [
                    {
                        "bool": {
                            "filter": filters,
                            "must": [{"match": {"search_text": f"{category} {query}"}}],
                        }
                    },
                    {
                        "knn": {
                            "card_vector": {
                                "filter": {"bool": {"filter": filters}},
                                "k": _QUERY_TOP_K,
                                "vector": list(query_vector),
                            }
                        }
                    },
                ]
            }
        },
    }


def parse_ranked_ids(response: object, *, identity_field: str, maximum: int) -> tuple[str, ...]:
    if type(response) is not dict or type(identity_field) is not str or maximum < 1:
        raise ValueError("M2c search response is invalid")
    hits_root = cast("dict[str, object]", response).get("hits")
    if type(hits_root) is not dict:
        raise ValueError("M2c search hits are invalid")
    raw_hits = cast("dict[str, object]", hits_root).get("hits")
    if type(raw_hits) is not list or len(raw_hits) > maximum:
        raise ValueError("M2c search hit count is invalid")
    ranked: list[tuple[float, str]] = []
    for raw in raw_hits:
        if type(raw) is not dict:
            raise ValueError("M2c search hit is invalid")
        hit = cast("dict[str, object]", raw)
        source = hit.get("_source")
        identity, score = hit.get("_id"), hit.get("_score")
        if (
            type(source) is not dict
            or type(identity) is not str
            or type(score) not in (int, float)
            or type(score) is bool
            or cast("dict[str, object]", source).get(identity_field) != identity
            or not identity
        ):
            raise ValueError("M2c search projection is invalid")
        ranked.append((float(cast("int | float", score)), identity))
    if len({identity for _score, identity in ranked}) != len(ranked):
        raise ValueError("M2c search identities are duplicated")
    return tuple(
        identity for _score, identity in sorted(ranked, key=lambda item: (-item[0], item[1]))
    )


class M2cOpenSearchItemSource:
    """Query-protected M2c Product retrieval with an optional BGE Profile supplement."""

    def __init__(
        self,
        *,
        client: M2aOpenSearch,
        indexes: AgentIndexes,
        profile_entries: tuple[M2cProfileEntry, ...],
        reranker: M2cReranker,
        initial_safe_codes: tuple[ToolFailureCode, ...] = (),
    ) -> None:
        if (
            type(client) is not M2aOpenSearch
            or type(indexes) is not AgentIndexes
            or type(profile_entries) is not tuple
            or any(type(entry) is not M2cProfileEntry for entry in profile_entries)
            or type(reranker) is not M2cReranker
            or type(initial_safe_codes) is not tuple
            or any(type(code) is not ToolFailureCode for code in initial_safe_codes)
        ):
            raise TypeError("M2c item source inputs are invalid")
        self._client, self._indexes, self._reranker = client, indexes, reranker
        self._demo_source = DemoItemSource(indexes)
        self._profile_entries, self._initial_safe_codes = profile_entries, initial_safe_codes
        self.last_trace = M2cRetrievalTrace(0, 0, ())
        self.trace_history: list[M2cRetrievalTrace] = []

    @property
    def manifest_records(self) -> tuple[ManifestRecord, ...]:
        return self._demo_source.manifest_records

    def manifest_records_for(self, result: ItemSearchRuntimeResult) -> tuple[ManifestRecord, ...]:
        return self._demo_source.manifest_records_for(result)

    async def search(
        self, request: ItemSearchInput, *, query_vector: tuple[float, ...] | None
    ) -> ItemSearchRuntimeResult:
        if type(request) is not ItemSearchInput or query_vector is None:
            raise ToolPortError(ToolFailureCode.M2C_QUERY_EMBEDDING_FAILED)
        try:
            query_ids = parse_ranked_ids(
                await self._client.search(
                    alias=PRODUCT_ALIAS,
                    body=product_hybrid_body(
                        query=request.query,
                        query_vector=query_vector,
                        platform=request.platform.value,
                    ),
                ),
                identity_field="record_key",
                maximum=_QUERY_TOP_K,
            )
            if not query_ids:
                raise ValueError("M2c Query Hybrid returned no candidates")
        except (M2aOpenSearchError, ValueError):
            raise ToolPortError(ToolFailureCode.M2C_MODEL_UNAVAILABLE) from None
        safe_codes = list(self._initial_safe_codes)
        user_ids: tuple[str, ...] = ()
        try:
            user_ids = await self._user_ids(request)
        except (M2aOpenSearchError, ValueError):
            safe_codes.append(ToolFailureCode.M2C_USER_EMBEDDING_DEGRADED)
        protected = tuple(
            dict.fromkeys((*query_ids, *(item for item in user_ids if item not in query_ids)))
        )[:_PRODUCT_RERANK_LIMIT]
        selected = query_ids
        try:
            reranked = await self._rerank_products(query=request.query, identities=protected)
            selected = reranked[:_FINAL_TOP_K]
        except (M2cModelServiceError, ValueError):
            safe_codes.append(ToolFailureCode.M2C_RERANK_DEGRADED)
        selected = selected[: request.top_k]
        if not selected:
            raise ToolPortError(ToolFailureCode.M2C_MODEL_UNAVAILABLE)
        self.last_trace = M2cRetrievalTrace(
            query_candidate_count=len(query_ids),
            user_candidate_count=len(user_ids),
            selected_ids=selected,
            safe_codes=tuple(dict.fromkeys(safe_codes)),
        )
        self.trace_history.append(self.last_trace)
        try:
            return self._demo_source.materialize_record_keys(request, record_keys=selected)
        except ToolPortError:
            raise ToolPortError(ToolFailureCode.M2C_MODEL_UNAVAILABLE) from None

    async def _user_ids(self, request: ItemSearchInput) -> tuple[str, ...]:
        identities: list[str] = []
        for entry in self._profile_entries:
            if profile_conflicts_with_current_query(entry=entry, query=request.query):
                continue
            response = await self._client.search(
                alias=PRODUCT_ALIAS,
                body=product_user_ann_body(
                    vector=entry.user_vector, platform=request.platform.value
                ),
            )
            identities.extend(
                parse_ranked_ids(response, identity_field="record_key", maximum=_USER_TOP_K)
            )
        return tuple(dict.fromkeys(identities))[:_USER_TOP_K]

    async def _rerank_products(self, *, query: str, identities: tuple[str, ...]) -> tuple[str, ...]:
        products = {product.product_id: product for product in self._indexes.batch.products}
        documents = tuple(
            RerankDocument(identity=identity, text=_product_text(products[identity]))
            for identity in identities
        )
        if len(documents) != len(identities):
            raise ValueError("rerank identity is not a trusted product")
        result = await self._reranker.rerank(RerankRequest(query=query[:512], documents=documents))
        if set(result.identities) != set(identities):
            raise ValueError("rerank changed Product identity membership")
        return result.identities


class M2cOpenSearchCategoryInsight:
    """M2c Card hybrid retrieval, BGE rerank, then the existing trusted reducer."""

    def __init__(
        self, *, client: M2aOpenSearch, indexes: AgentIndexes, reranker: M2cReranker
    ) -> None:
        if (
            type(client) is not M2aOpenSearch
            or type(indexes) is not AgentIndexes
            or type(reranker) is not M2cReranker
        ):
            raise TypeError("M2c Category adapter inputs are invalid")
        self._client, self._indexes, self._reranker = client, indexes, reranker
        self.last_trace = M2cRetrievalTrace(0, 0, ())
        self.trace_history: list[M2cRetrievalTrace] = []

    async def retrieve(self, request: CategoryInsightInput) -> CategoryInsightOutput:
        if (
            type(request) is not CategoryInsightInput
            or request.index_version != self._indexes.index_version
        ):
            raise ToolPortError(ToolFailureCode.M2C_MODEL_UNAVAILABLE)
        try:
            identities = parse_ranked_ids(
                await self._client.search(
                    alias=CARD_ALIAS,
                    body=card_hybrid_body(
                        category=request.category,
                        query=request.query,
                        query_vector=request.query_vector,
                    ),
                ),
                identity_field="card_id",
                maximum=_QUERY_TOP_K,
            )
            if not identities:
                raise ValueError("M2c Card Hybrid returned no candidates")
        except (M2aOpenSearchError, ValueError):
            raise ToolPortError(ToolFailureCode.M2C_MODEL_UNAVAILABLE) from None
        selected, safe_codes = identities, []
        try:
            cards = {card.card_id: card for card in self._indexes.cards}
            result = await self._reranker.rerank(
                RerankRequest(
                    query=request.query[:512],
                    documents=tuple(
                        RerankDocument(identity=value, text=cards[value].index_text)
                        for value in identities
                    ),
                )
            )
            if set(result.identities) != set(identities):
                raise ValueError("rerank changed Card identity membership")
            selected = result.identities[:_FINAL_TOP_K]
        except (KeyError, M2cModelServiceError, ValueError):
            safe_codes.append(ToolFailureCode.M2C_RERANK_DEGRADED)
        self.last_trace = M2cRetrievalTrace(len(identities), 0, selected, tuple(safe_codes))
        self.trace_history.append(self.last_trace)
        try:
            return self._indexes.reduce_card_ids(
                category=request.category, card_ids=selected, depth=request.depth
            )
        except ToolPortError:
            raise ToolPortError(ToolFailureCode.M2C_MODEL_UNAVAILABLE) from None


def _product_text(product: object) -> str:
    from glodex.domain.catalog import Product

    if type(product) is not Product:
        raise ValueError("product is not a trusted Product")
    return "\n".join(
        (
            product.title,
            product.category,
            product.provider_id.removesuffix("-demo"),
            *(f"{attribute.name}: {attribute.value}" for attribute in product.attributes),
        )
    )[:2_000]


__all__ = [
    "M2cOpenSearchCategoryInsight",
    "M2cOpenSearchItemSource",
    "M2cRetrievalTrace",
    "card_hybrid_body",
    "parse_ranked_ids",
    "product_hybrid_body",
    "product_user_ann_body",
]
