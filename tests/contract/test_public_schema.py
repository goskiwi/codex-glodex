from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from glodex.contracts import (
    Diagnostics,
    FilterSummary,
    InterpretedCriterionSummary,
    InterpretedRequestSummary,
    Issue,
    MoneySummary,
    OfferSummary,
    RequestRejected,
    RunStatus,
    SearchRequest,
    SearchResponse,
    SearchResult,
    SemanticQueryFilters,
    SourceSpanSummary,
    validate_search_request,
)

pytestmark = [
    pytest.mark.contract,
    pytest.mark.spec(
        "GLO-P0-001",
        "GLO-P0-009",
        "GLO-P0-011",
        "GLO-NFR-007",
        "GLO-NFR-008",
        "GLO-NFR-011",
    ),
]


def valid_request_data(**updates: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "query": "  推荐轻薄本  ",
        "locale": "zh-CN",
        "display_currency": "USD",
        "top_k": 3,
        "snapshot_version": "m0-v1",
    }
    data.update(updates)
    return data


def test_valid_search_request_is_trimmed_and_strictly_typed() -> None:
    request = SearchRequest.model_validate(valid_request_data())

    assert request.query == "推荐轻薄本"
    assert request.locale == "zh-CN"
    assert request.display_currency == "USD"
    assert request.top_k == 3
    assert request.snapshot_version == "m0-v1"

    with pytest.raises(ValidationError):
        SearchRequest.model_validate(valid_request_data(top_k="3"))


def test_semantic_query_filters_are_explicit_wire_values() -> None:
    query_only = SearchRequest(query="イヤホン")
    assert query_only.semantic_filters == SemanticQueryFilters()
    assert query_only.semantic_filters.is_empty

    filtered = SearchRequest.model_validate_json(
        '{"query":"イヤホン","semantic_filters":{"item_languages":["ja"],'
        '"canonical_category_ids":["electronics.headphones"],'
        '"require_carried_source_category":true,'
        '"attribute_projection_provenance":"SOURCE_ONLY"}}'
    )
    assert filtered.semantic_filters == SemanticQueryFilters(
        item_languages=("ja",),
        canonical_category_ids=("electronics.headphones",),
        require_carried_source_category=True,
        attribute_projection_provenance="SOURCE_ONLY",
    )
    assert filtered.model_dump(mode="json")["semantic_filters"] == {
        "item_languages": ["ja"],
        "canonical_category_ids": ["electronics.headphones"],
        "require_carried_source_category": True,
        "attribute_projection_provenance": "SOURCE_ONLY",
    }

    with pytest.raises(ValidationError, match="semantic item languages must be unique"):
        SearchRequest.model_validate_json(
            '{"query":"イヤホン","semantic_filters":{"item_languages":["ja","ja"]}}'
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("query", ""),
        ("query", " \t\n"),
        ("query", "好" * 2001),
        ("top_k", 0),
        ("top_k", 4),
        ("locale", "en-US"),
        ("display_currency", "US"),
        ("display_currency", "usd"),
        ("display_currency", "US1"),
        ("snapshot_version", "../m0-v1"),
    ],
)
def test_pre_run_rejects_only_malformed_request_fields(field: str, value: object) -> None:
    outcome = validate_search_request(valid_request_data(**{field: value}))

    assert isinstance(outcome, RequestRejected)
    assert outcome.type == "request_rejected"
    assert outcome.errors
    assert "run_id" not in outcome.model_dump()
    assert "run_id" not in RequestRejected.model_json_schema()["properties"]


def test_pre_run_rejects_extra_fields() -> None:
    outcome = validate_search_request(valid_request_data(unsupported=True))

    assert isinstance(outcome, RequestRejected)
    assert outcome.errors[0].code == "extra_forbidden"
    assert outcome.errors[0].field == "unsupported"


def test_snapshot_and_currency_compatibility_are_deferred_to_the_run() -> None:
    outcome = validate_search_request(
        valid_request_data(
            display_currency="ZZZ",
            snapshot_version="not-present-but-well-formed",
        )
    )

    assert isinstance(outcome, SearchRequest)
    assert outcome.display_currency == "ZZZ"
    assert outcome.snapshot_version == "not-present-but-well-formed"


def test_request_rejection_errors_are_stable_and_frozen() -> None:
    outcome = validate_search_request(valid_request_data(query=" ", top_k=0))
    assert isinstance(outcome, RequestRejected)

    assert tuple((error.field, error.code) for error in outcome.errors) == (
        ("query", "string_too_short"),
        ("top_k", "greater_than_equal"),
    )
    with pytest.raises(ValidationError):
        outcome.errors[0].message = "changed"  # type: ignore[misc]


def test_search_response_has_a_run_id_and_the_minimum_terminal_contract() -> None:
    response = SearchResponse(
        run_id="run-0001",
        status=RunStatus.NO_MATCH,
        snapshot_version="m0-v1",
        config_fingerprint="a" * 64,
        algorithm_version="walking-skeleton-v1",
    )

    assert response.model_dump(mode="json") == {
        "run_id": "run-0001",
        "status": "NO_MATCH",
        "snapshot_version": "m0-v1",
        "config_fingerprint": "a" * 64,
        "algorithm_version": "walking-skeleton-v1",
        "interpreted_request": {
            "required": [],
            "preferred": [],
            "parser_version": "unavailable",
        },
        "results": [],
        "filter_summary": {"stages": [], "reason_counts": []},
        "warnings": [],
        "diagnostics": {
            "ranker_degraded": False,
            "stages": [],
            "issues": [],
        },
    }
    assert "run_id" in SearchResponse.model_json_schema()["required"]


def test_public_models_are_deeply_immutable_by_construction() -> None:
    warning = Issue(code="EMPTY_POOL", stage="filter", message="No candidates")
    required = InterpretedCriterionSummary(
        kind="budget_max",
        value="800 USD",
        source_span=SourceSpanSummary(start=0, end=2, text="预算"),
    )
    preferred = InterpretedCriterionSummary(
        kind="lightweight",
        value="轻薄",
        source_span=SourceSpanSummary(start=2, end=4, text="轻薄"),
    )
    response = SearchResponse(
        run_id="run-0001",
        status=RunStatus.NO_MATCH,
        snapshot_version="m0-v1",
        config_fingerprint="a" * 64,
        algorithm_version="walking-skeleton-v1",
        interpreted_request=InterpretedRequestSummary(
            required=(required,),
            preferred=(preferred,),
            parser_version="rules-v1",
        ),
        filter_summary=FilterSummary(),
        warnings=(warning,),
        diagnostics=Diagnostics(issues=(warning,)),
    )

    assert isinstance(response.results, tuple)
    assert isinstance(response.warnings, tuple)
    assert isinstance(response.interpreted_request.required, tuple)
    assert isinstance(response.diagnostics.issues, tuple)

    with pytest.raises(ValidationError):
        response.status = RunStatus.FAILED  # type: ignore[misc]
    with pytest.raises(TypeError):
        response.warnings[0] = warning  # type: ignore[index]
    with pytest.raises(ValidationError):
        response.warnings[0].message = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("exact", "display"),
    [
        ("1e3", "1000.00"),
        ("-1", "-1.00"),
        ("01", "1.00"),
        ("1.2300", "1.23"),
        ("not-money", "1.00"),
        ("1", "1e0"),
        ("1", "01.00"),
    ],
)
def test_money_summary_rejects_noncanonical_decimal_strings(
    exact: str,
    display: str,
) -> None:
    with pytest.raises(ValidationError):
        MoneySummary(currency="USD", exact=exact, display=display)


def _offer(
    *,
    offer_id: str = "offer-1",
    market: str = "US",
    exact: str = "799.99",
) -> OfferSummary:
    cost = MoneySummary(currency="USD", exact=exact, display=exact)
    return OfferSummary(
        offer_id=offer_id,
        provider_id="provider-1",
        market=market,
        landed_cost=cost,
    )


def _result(
    *,
    selected_offer: OfferSummary,
    eligible_offers: tuple[OfferSummary, ...],
) -> SearchResult:
    return SearchResult(
        product_id="product-1",
        title="Laptop",
        category="laptop",
        selected_offer=selected_offer,
        eligible_offers=eligible_offers,
        landed_cost=selected_offer.landed_cost,
        reason="Verified fixture",
        evidence=({"evidence_id": "evidence-1"},),
    )


def test_selected_offer_must_be_the_complete_eligible_offer() -> None:
    selected = _offer(market="US")
    same_identity_but_different_offer = _offer(market="CN")

    with pytest.raises(ValidationError, match="selected_offer must be present"):
        _result(
            selected_offer=selected,
            eligible_offers=(same_identity_but_different_offer,),
        )


def test_eligible_offer_identities_must_be_unique() -> None:
    selected = _offer(market="US")
    duplicate_identity = _offer(market="CN")

    with pytest.raises(ValidationError, match="duplicate offer identities"):
        _result(
            selected_offer=selected,
            eligible_offers=(selected, duplicate_identity),
        )


def test_response_rejects_extra_fields_and_invalid_terminal_shape() -> None:
    base = {
        "run_id": "run-0001",
        "status": "FAILED",
        "snapshot_version": "m0-v1",
        "config_fingerprint": "a" * 64,
        "algorithm_version": "walking-skeleton-v1",
    }

    with pytest.raises(ValidationError):
        SearchResponse.model_validate({**base, "secret": "not allowed"})
    with pytest.raises(ValidationError, match="COMPLETED responses must contain results"):
        SearchResponse.model_validate({**base, "status": RunStatus.COMPLETED})
    with pytest.raises(ValidationError):
        SearchResponse.model_validate(
            {
                **base,
                "results": [{"product_id": "must-not-leak"}],
            }
        )
