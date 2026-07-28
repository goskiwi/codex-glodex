from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from glodex.domain.catalog import (
    CanonicalAttribute,
    CanonicalProduct,
    EntityKind,
    ProductSource,
)
from glodex.domain.eligibility import (
    PRODUCT_GATE_ORDER,
    EligibilityContext,
    ProductGateId,
    ProductRejectionCode,
    diagnose_product_rejections,
    run_product_gates,
)
from glodex.domain.evidence import EvidenceEntityType, EvidenceRef, FieldEvidence
from glodex.domain.intent import (
    Exclusion,
    InterpretedRequest,
    PreferredCriterion,
    SourceSpan,
    TargetCategory,
)

pytestmark = [
    pytest.mark.unit,
    pytest.mark.spec("GLO-P0-006", "GLO-P0-008", "AC-006"),
]

_SNAPSHOT = "m0-v1"
_CAPTURED_AT = datetime(2026, 1, 1, tzinfo=UTC)


class _StringValue(str):
    pass


def _product(
    *,
    product_id: str = "product-1",
    title: str = "TravelBook 14",
    category: str = "laptop",
    entity_kind: EntityKind = EntityKind.PRIMARY_PRODUCT,
    attributes: tuple[CanonicalAttribute, ...] | None = None,
) -> CanonicalProduct:
    if attributes is None:
        attributes = (
            CanonicalAttribute(
                name="condition",
                value="new",
                evidence_ids=(f"ev-{product_id}-attribute-condition",),
            ),
        )
    return CanonicalProduct(
        snapshot_version=_SNAPSHOT,
        product_id=product_id,
        title=title,
        category=category,
        entity_kind=entity_kind,
        snapshot_ordinal=0,
        sources=(
            ProductSource(
                provider_id="provider-a",
                source_uri=f"fixture://provider-a/products/{product_id}",
                snapshot_ordinal=0,
            ),
        ),
        attributes=attributes,
        field_evidence=(
            FieldEvidence(
                field_path="product.title",
                evidence_id=f"ev-{product_id}-title",
            ),
            FieldEvidence(
                field_path="product.category",
                evidence_id=f"ev-{product_id}-category",
            ),
            FieldEvidence(
                field_path="product.entity_kind",
                evidence_id=f"ev-{product_id}-entity-kind",
            ),
        ),
    )


def _evidence(product: CanonicalProduct) -> tuple[EvidenceRef, ...]:
    source = product.sources[0]
    core = tuple(
        EvidenceRef(
            evidence_id=binding.evidence_id,
            snapshot_version=product.snapshot_version,
            entity_type=EvidenceEntityType.PRODUCT,
            product_id=product.product_id,
            offer_id=None,
            currency=None,
            field_path=binding.field_path,
            provider_id=source.provider_id,
            source_uri=source.source_uri,
            captured_at=_CAPTURED_AT,
        )
        for binding in product.field_evidence
    )
    attributes = tuple(
        EvidenceRef(
            evidence_id=evidence_id,
            snapshot_version=product.snapshot_version,
            entity_type=EvidenceEntityType.PRODUCT,
            product_id=product.product_id,
            offer_id=None,
            currency=None,
            field_path=f"product.attributes.{attribute.name}",
            provider_id=source.provider_id,
            source_uri=source.source_uri,
            captured_at=_CAPTURED_AT,
        )
        for attribute in product.attributes
        for evidence_id in attribute.evidence_ids
    )
    return (*core, *attributes)


def _context(
    *,
    target_category: str | None = "laptop",
    exclusions: tuple[str, ...] = (),
    preferred: str = "lightweight",
) -> EligibilityContext:
    required: list[TargetCategory | Exclusion] = []
    if target_category is not None:
        required.append(
            TargetCategory(
                category=target_category,
                source_span=SourceSpan(start=0, end=3, text="笔记本"),
            )
        )
    required.extend(
        Exclusion(
            value=value,
            source_span=SourceSpan(start=index, end=index + len(value), text=value),
        )
        for index, value in enumerate(exclusions, start=4)
    )
    return EligibilityContext.from_interpreted_request(
        InterpretedRequest(
            required=tuple(required),
            preferred=(
                PreferredCriterion(
                    value=preferred,
                    source_span=SourceSpan(start=20, end=22, text="轻薄"),
                ),
            ),
            parser_version="test-v1",
        )
    )


def _result_for_gate(output, gate: ProductGateId):
    return next(result for result in output.gate_results if result.gate is gate)


def test_valid_primary_product_passes_every_product_gate_in_fixed_order() -> None:
    product = _product()

    output = run_product_gates((product,), _context(), _evidence(product))

    assert output.candidates == (product,)
    assert tuple(result.gate for result in output.gate_results) == PRODUCT_GATE_ORDER
    assert all(result.before == (product,) for result in output.gate_results)
    assert all(result.rejected == () for result in output.gate_results)


def test_wrong_category_is_rejected_at_the_first_gate_and_never_reappears() -> None:
    product = _product(
        category="camera",
        title="The best lightweight laptop replacement",
    )

    output = run_product_gates((product,), _context(), _evidence(product))

    category = _result_for_gate(output, ProductGateId.CATEGORY)
    assert category.rejected[0].reasons == (ProductRejectionCode.CATEGORY_MISMATCH.value,)
    assert output.candidates == ()
    assert all(
        result.before == ()
        for result in output.gate_results
        if result.gate is not ProductGateId.CATEGORY
    )


@pytest.mark.parametrize(
    "entity_kind",
    [
        EntityKind.ACCESSORY,
        EntityKind.REPLACEMENT_PART,
        EntityKind.DECORATION,
        EntityKind.UNKNOWN,
    ],
)
def test_every_non_primary_entity_is_rejected_despite_a_highly_relevant_title(
    entity_kind: EntityKind,
) -> None:
    product = _product(
        entity_kind=entity_kind,
        title="Perfect laptop for travel with every requested feature",
    )

    output = run_product_gates((product,), _context(), _evidence(product))

    entity = _result_for_gate(output, ProductGateId.ENTITY_KIND)
    assert entity.rejected[0].reasons == (ProductRejectionCode.NOT_PRIMARY_PRODUCT.value,)
    assert output.candidates == ()


def test_title_is_never_an_exclusion_surface() -> None:
    product = _product(
        title="Refurbished accessory camera sticker stand",
    )
    context = _context(
        exclusions=("翻新", "配件", "相机", "贴纸", "支架"),
    )

    output = run_product_gates((product,), context, _evidence(product))

    assert output.candidates == (product,)
    assert _result_for_gate(output, ProductGateId.EXCLUSION).rejected == ()


def test_exclusion_matches_the_standardized_category_surface() -> None:
    product = _product(title="Camera wording cannot override the catalog category")

    output = run_product_gates(
        (product,),
        _context(target_category=None, exclusions=("ＬＡＰＴＯＰ",)),  # noqa: RUF001
        _evidence(product),
    )

    exclusion = _result_for_gate(output, ProductGateId.EXCLUSION)
    assert exclusion.rejected[0].reasons == (
        f"{ProductRejectionCode.EXPLICIT_EXCLUSION.value}:laptop",
    )


def test_category_exclusion_requires_closed_category_evidence() -> None:
    product = _product()
    evidence = tuple(item for item in _evidence(product) if item.field_path != "product.category")

    output = run_product_gates(
        (product,),
        _context(target_category=None, exclusions=("laptop",)),
        evidence,
    )

    assert _result_for_gate(output, ProductGateId.EXCLUSION).rejected == ()
    evidence_result = _result_for_gate(output, ProductGateId.REQUIRED_EVIDENCE)
    assert evidence_result.rejected[0].reasons == (
        ProductRejectionCode.REQUIRED_EVIDENCE_INVALID.value,
    )


def test_entity_kind_exclusion_requires_closed_entity_kind_evidence() -> None:
    product = _product(entity_kind=EntityKind.ACCESSORY)
    evidence = tuple(
        replace(item, field_path="product.title")
        if item.field_path == "product.entity_kind"
        else item
        for item in _evidence(product)
    )

    diagnostics = diagnose_product_rejections(
        (product,),
        _context(exclusions=("配件",)),
        evidence,
    )

    assert tuple(item.gate for item in diagnostics) == (
        ProductGateId.ENTITY_KIND,
        ProductGateId.REQUIRED_EVIDENCE,
    )


@pytest.mark.parametrize(
    "attribute",
    [
        CanonicalAttribute(
            name="refurbished",
            value="true",
            evidence_ids=("ev-product-1-attribute-refurbished",),
        ),
        CanonicalAttribute(
            name="condition",
            value="REFURBISHED",
            evidence_ids=("ev-product-1-attribute-condition",),
        ),
    ],
)
def test_exclusion_matches_evidenced_attribute_name_or_value_only(
    attribute: CanonicalAttribute,
) -> None:
    product = _product(attributes=(attribute,))

    output = run_product_gates(
        (product,),
        _context(exclusions=("翻新",)),
        _evidence(product),
    )

    exclusion = _result_for_gate(output, ProductGateId.EXCLUSION)
    assert exclusion.rejected[0].reasons == (
        f"{ProductRejectionCode.EXPLICIT_EXCLUSION.value}:refurbished",
    )


def test_false_boolean_attribute_name_does_not_create_an_exclusion_false_positive() -> None:
    product = _product(
        attributes=(
            CanonicalAttribute(
                name="refurbished",
                value="false",
                evidence_ids=("ev-product-1-attribute-refurbished",),
            ),
        )
    )

    output = run_product_gates(
        (product,),
        _context(exclusions=("翻新",)),
        _evidence(product),
    )

    assert output.candidates == (product,)
    assert _result_for_gate(output, ProductGateId.EXCLUSION).rejected == ()


def test_exclusion_normalization_is_nfkc_casefolded_exact_and_reports_each_match() -> None:
    product = _product(
        attributes=(
            CanonicalAttribute(
                name="condition",
                value="REFURBISHED",
                evidence_ids=("ev-product-1-attribute-condition",),
            ),
            CanonicalAttribute(
                name="form_factor",
                value="lightweight",
                evidence_ids=("ev-product-1-attribute-form-factor",),
            ),
        )
    )

    output = run_product_gates(
        (product,),
        _context(exclusions=(" 翻新 ", "ＬＩＧＨＴＷＥＩＧＨＴ")),  # noqa: RUF001
        _evidence(product),
    )

    exclusion = _result_for_gate(output, ProductGateId.EXCLUSION)
    assert exclusion.rejected[0].reasons == (
        f"{ProductRejectionCode.EXPLICIT_EXCLUSION.value}:lightweight",
        f"{ProductRejectionCode.EXPLICIT_EXCLUSION.value}:refurbished",
    )


def test_unclosed_attribute_evidence_cannot_drive_exclusion() -> None:
    product = _product(
        attributes=(
            CanonicalAttribute(
                name="condition",
                value="refurbished",
                evidence_ids=("ev-product-1-attribute-condition",),
            ),
        )
    )
    evidence = tuple(
        replace(
            item,
            field_path="product.attributes.other",
        )
        if item.evidence_id == "ev-product-1-attribute-condition"
        else item
        for item in _evidence(product)
    )

    output = run_product_gates(
        (product,),
        _context(exclusions=("翻新",)),
        evidence,
    )

    assert _result_for_gate(output, ProductGateId.EXCLUSION).rejected == ()
    evidence_result = _result_for_gate(output, ProductGateId.REQUIRED_EVIDENCE)
    assert evidence_result.rejected[0].reasons == (
        ProductRejectionCode.REQUIRED_EVIDENCE_INVALID.value,
    )


@pytest.mark.parametrize(
    "case",
    [
        "unknown_id",
        "cross_snapshot",
        "cross_product",
        "wrong_entity",
        "wrong_provider",
        "wrong_source",
        "wrong_field",
        "wrong_attribute_field",
    ],
)
def test_required_evidence_gate_rejects_every_broken_closure(case: str) -> None:
    product = _product()
    evidence = _evidence(product)
    title_id = f"ev-{product.product_id}-title"
    attribute_id = f"ev-{product.product_id}-attribute-condition"

    if case == "unknown_id":
        object.__setattr__(
            product,
            "field_evidence",
            (
                FieldEvidence(field_path="product.title", evidence_id="ev-unknown"),
                *product.field_evidence[1:],
            ),
        )
    elif case == "wrong_entity":
        evidence = tuple(
            EvidenceRef(
                evidence_id=item.evidence_id,
                snapshot_version=item.snapshot_version,
                entity_type=EvidenceEntityType.OFFER,
                product_id=item.product_id,
                offer_id="offer-1",
                currency=None,
                field_path="offer.title",
                provider_id=item.provider_id,
                source_uri=item.source_uri,
                captured_at=item.captured_at,
            )
            if item.evidence_id == title_id
            else item
            for item in evidence
        )
    else:
        changes: dict[str, object]
        target_id = attribute_id if case == "wrong_attribute_field" else title_id
        if case == "cross_snapshot":
            changes = {"snapshot_version": "m0-other"}
        elif case == "cross_product":
            changes = {"product_id": "other-product"}
        elif case == "wrong_provider":
            changes = {"provider_id": "provider-b"}
        elif case == "wrong_source":
            changes = {"source_uri": "fixture://provider-a/products/other"}
        elif case == "wrong_field":
            changes = {"field_path": "product.category"}
        else:
            changes = {"field_path": "product.attributes.other"}
        evidence = tuple(
            replace(item, **changes) if item.evidence_id == target_id else item for item in evidence
        )

    output = run_product_gates((product,), _context(), evidence)

    evidence_result = _result_for_gate(output, ProductGateId.REQUIRED_EVIDENCE)
    assert evidence_result.rejected[0].reasons == (
        ProductRejectionCode.REQUIRED_EVIDENCE_INVALID.value,
    )
    assert output.candidates == ()


def test_duplicate_or_mutable_evidence_input_fails_closed() -> None:
    product = _product()
    evidence = _evidence(product)

    with pytest.raises(ValueError, match="duplicate evidence ID"):
        run_product_gates(
            (product,),
            _context(),
            (*evidence, evidence[0]),
        )
    with pytest.raises(TypeError, match="tuple"):
        run_product_gates(
            (product,),
            _context(),
            list(evidence),  # type: ignore[arg-type]
        )


def test_mutated_evidence_graph_fails_closed_before_gate_evaluation() -> None:
    product = _product()
    evidence = _evidence(product)
    object.__setattr__(evidence[0], "provider_id", ["provider-a"])

    with pytest.raises(TypeError, match="deeply immutable EvidenceRef"):
        run_product_gates((product,), _context(), evidence)


def test_uninitialized_evidence_ref_fails_closed_without_entering_the_funnel() -> None:
    product = _product()
    malformed = object.__new__(EvidenceRef)
    object.__setattr__(malformed, "evidence_id", "ev-malformed")

    with pytest.raises(TypeError, match="evidence"):
        run_product_gates((product,), _context(), (*_evidence(product), malformed))


def test_full_diagnostics_reports_all_gates_without_restoring_the_candidate() -> None:
    product = _product(
        category="camera",
        entity_kind=EntityKind.ACCESSORY,
        attributes=(
            CanonicalAttribute(
                name="condition",
                value="refurbished",
                evidence_ids=("ev-product-1-attribute-condition",),
            ),
        ),
    )
    evidence = tuple(
        item for item in _evidence(product) if item.evidence_id != "ev-product-1-title"
    )
    context = _context(
        target_category="laptop",
        exclusions=("翻新", "配件"),
    )

    output = run_product_gates((product,), context, evidence)
    diagnostics = diagnose_product_rejections((product,), context, evidence)

    assert tuple(item.gate for item in diagnostics) == PRODUCT_GATE_ORDER
    assert diagnostics[0].reasons == (ProductRejectionCode.CATEGORY_MISMATCH.value,)
    assert diagnostics[1].reasons == (ProductRejectionCode.NOT_PRIMARY_PRODUCT.value,)
    assert diagnostics[2].reasons == (
        f"{ProductRejectionCode.EXPLICIT_EXCLUSION.value}:accessory",
        f"{ProductRejectionCode.EXPLICIT_EXCLUSION.value}:refurbished",
    )
    assert diagnostics[3].reasons == (ProductRejectionCode.REQUIRED_EVIDENCE_INVALID.value,)
    assert output.candidates == ()
    assert sum(len(result.rejected) for result in output.gate_results) == 1


def test_preferred_changes_do_not_change_public_product_gate_membership() -> None:
    product = _product()

    lightweight = run_product_gates(
        (product,),
        _context(preferred="lightweight"),
        _evidence(product),
    )
    portable = run_product_gates(
        (product,),
        _context(preferred="portable"),
        _evidence(product),
    )

    assert lightweight.candidates == portable.candidates == (product,)


@pytest.mark.parametrize(
    ("constraint_type", "field_name", "bad_value", "message"),
    [
        ("target", "category", "", "target category must be non-empty"),
        ("target", "category", _StringValue("laptop"), "exact string"),
        ("target", "kind", "exclusion", "target category discriminator"),
        ("exclusion", "value", "  ", "exclusion value must be non-empty"),
        ("exclusion", "value", _StringValue("refurbished"), "exact string"),
        ("exclusion", "kind", "target_category", "exclusion discriminator"),
    ],
)
@pytest.mark.parametrize("api_name", ["run", "diagnose"])
def test_public_product_apis_reject_tampered_product_constraints(
    constraint_type: str,
    field_name: str,
    bad_value: object,
    message: str,
    api_name: str,
) -> None:
    if constraint_type == "target":
        constraint: TargetCategory | Exclusion = TargetCategory(
            category="laptop",
            source_span=SourceSpan(start=0, end=3, text="笔记本"),
        )
    else:
        constraint = Exclusion(
            value="refurbished",
            source_span=SourceSpan(start=4, end=6, text="翻新"),
        )
    object.__setattr__(constraint, field_name, bad_value)
    context = EligibilityContext(required=(constraint,))
    product = _product()
    api = run_product_gates if api_name == "run" else diagnose_product_rejections

    with pytest.raises((TypeError, ValueError), match=message):
        api((product,), context, _evidence(product))


@pytest.mark.parametrize("api_name", ["run", "diagnose"])
@pytest.mark.parametrize("case", ["multiple_targets", "duplicate_exclusions"])
def test_public_product_apis_reject_ambiguous_or_duplicate_constraints(
    api_name: str,
    case: str,
) -> None:
    if case == "multiple_targets":
        required: tuple[TargetCategory | Exclusion, ...] = (
            TargetCategory(
                category="laptop",
                source_span=SourceSpan(start=0, end=3, text="笔记本"),
            ),
            TargetCategory(
                category="camera",
                source_span=SourceSpan(start=4, end=6, text="相机"),
            ),
        )
        message = "at most one target category"
    else:
        required = (
            Exclusion(
                value="翻新",
                source_span=SourceSpan(start=0, end=2, text="翻新"),
            ),
            Exclusion(
                value=" REFURBISHED ",
                source_span=SourceSpan(start=3, end=5, text="翻新"),
            ),
        )
        message = "duplicate exclusion"
    context = EligibilityContext(required=required)
    product = _product()
    api = run_product_gates if api_name == "run" else diagnose_product_rejections

    with pytest.raises(ValueError, match=message):
        api((product,), context, _evidence(product))


def test_public_product_runner_requires_exact_canonical_product_tuple() -> None:
    product = _product()

    with pytest.raises(TypeError, match="canonical products must be a tuple"):
        run_product_gates(  # type: ignore[arg-type]
            [product],
            _context(),
            _evidence(product),
        )
    with pytest.raises(TypeError, match="exact CanonicalProduct"):
        run_product_gates(
            (object(),),  # type: ignore[arg-type]
            _context(),
            (),
        )


def test_public_product_runner_rejects_duplicate_ids_and_mixed_snapshots() -> None:
    first = _product(product_id="product-1")
    second = _product(product_id="product-2")
    mixed_snapshot = replace(second, snapshot_version="m0-v2")

    with pytest.raises(ValueError, match="product IDs must be unique"):
        run_product_gates(
            (first, first),
            _context(),
            _evidence(first),
        )
    with pytest.raises(ValueError, match="one snapshot"):
        run_product_gates(
            (first, mixed_snapshot),
            _context(),
            (*_evidence(first), *_evidence(mixed_snapshot)),
        )


@pytest.mark.parametrize(
    ("nested_model", "field_name", "bad_value"),
    [
        ("product", "title", ""),
        ("source", "provider_id", ""),
        ("attribute", "evidence_ids", ()),
        ("binding", "evidence_id", ""),
    ],
)
def test_public_product_runner_revalidates_canonical_nested_invariants(
    nested_model: str,
    field_name: str,
    bad_value: object,
) -> None:
    product = _product()
    evidence = _evidence(product)
    target: object
    if nested_model == "product":
        target = product
    elif nested_model == "source":
        target = product.sources[0]
    elif nested_model == "attribute":
        target = product.attributes[0]
    else:
        target = product.field_evidence[0]
    object.__setattr__(target, field_name, bad_value)

    with pytest.raises(TypeError, match="canonical product invariants"):
        run_product_gates((product,), _context(), evidence)


def test_legal_multi_source_and_multi_binding_product_still_passes() -> None:
    product = _product(attributes=())
    second_source = ProductSource(
        provider_id="provider-b",
        source_uri="fixture://provider-b/products/product-1",
        snapshot_ordinal=1,
    )
    second_title_binding = FieldEvidence(
        field_path="product.title",
        evidence_id="ev-product-1-title-provider-b",
    )
    product = replace(
        product,
        sources=(*product.sources, second_source),
        field_evidence=(*product.field_evidence, second_title_binding),
    )
    evidence = tuple(
        replace(
            item,
            provider_id=second_source.provider_id,
            source_uri=second_source.source_uri,
        )
        if item.evidence_id == second_title_binding.evidence_id
        else item
        for item in _evidence(product)
    )

    output = run_product_gates((product,), _context(), evidence)

    assert output.candidates == (product,)
