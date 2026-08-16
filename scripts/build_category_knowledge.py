#!/usr/bin/env python3
# ruff: noqa: RUF001
"""Build strict CategoryInsight JSONL cards for the interview knowledge RAG."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Final, cast

CARD_SCHEMA: Final = "glodex.category-card.v1"
MANIFEST_SCHEMA: Final = "glodex.category-card-manifest.v1"
SOURCE_SCHEMA: Final = "glodex.digital-product-evidence.v4"
TAXONOMY_SCHEMA: Final = "glodex.digital-category-taxonomy.v3"
DATA_MODE: Final = "SYNTHETIC_INTERVIEW"
GENERATOR_VERSION: Final = "category-card-electronics-v1"
CARD_TYPES: Final = ("bestseller", "attribute", "price_range")

_EXCLUDED_ATTRIBUTES: Final = frozenset(
    {
        "brand",
        "model",
        "release_date",
        "configuration",
        "configuration_detail",
        "product_family",
        "market_segment",
    }
)
_REJECTED_VALUE_MARKERS: Final = (
    "未公布",
    "未单列",
    "未完整枚举",
    "未在当前",
    "未给出",
    "未知",
    "不详",
    "暂无",
    "以官网",
    "视配置",
    "因配置",
    "以具体型号",
    "以该型号",
    "以厂商规格",
    "为准",
    "仅标注",
    "随配置而异",
    "其他接口",
)
_ADMITTED_PRICE_SOURCES: Final = frozenset(
    {
        "MANUFACTURER_LIST_PRICE",
        "PUBLIC_RETAIL_PRICE_REPORT",
        "PUBLIC_RETAILER_LIST_PRICE",
        "CATALOG_REFERENCE_PRICE",
    }
)
_ADMITTED_SPECIFICATION_SOURCES: Final = frozenset(
    {"MANUFACTURER_SPECIFICATION", "PUBLIC_PRODUCT_SPECIFICATION"}
)
_YEAR = re.compile(r"(?:19|20)\d{2}")


class CategoryKnowledgeBuildError(ValueError):
    """Reject an invalid or unsupported Category Card build."""


def build_artifacts(
    catalog_path: Path,
    taxonomy_path: Path,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    catalog_bytes = catalog_path.read_bytes()
    taxonomy_bytes = taxonomy_path.read_bytes()
    catalog = _load_object(catalog_bytes, "digital catalog")
    taxonomy = _load_object(taxonomy_bytes, "digital taxonomy")
    catalog_categories_value = catalog.get("categories")
    baselines_value = catalog.get("evidence_baseline")
    products_value = catalog.get("products")
    last_updated = catalog.get("catalog_as_of")
    snapshot_version = catalog.get("snapshot_version")
    if (
        catalog.get("schema_version") != SOURCE_SCHEMA
        or type(catalog_categories_value) is not dict
        or type(baselines_value) is not dict
        or type(products_value) is not list
        or type(snapshot_version) is not str
        or type(last_updated) is not str
    ):
        raise CategoryKnowledgeBuildError("digital catalog contract is invalid")
    if taxonomy.get("schema_version") != TAXONOMY_SCHEMA:
        raise CategoryKnowledgeBuildError("digital taxonomy contract is invalid")

    taxonomy_categories = _taxonomy_categories(taxonomy)
    catalog_categories = cast(dict[str, object], catalog_categories_value)
    baselines = cast(dict[str, object], baselines_value)
    source_products = cast(list[object], products_value)
    if set(catalog_categories) - set(taxonomy_categories):
        raise CategoryKnowledgeBuildError("catalog contains categories absent from taxonomy")

    grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
    for raw_product in source_products:
        if type(raw_product) is not dict:
            raise CategoryKnowledgeBuildError("digital catalog product is invalid")
        category = raw_product.get("category")
        if type(category) is not str or category not in catalog_categories:
            raise CategoryKnowledgeBuildError("digital catalog category is invalid")
        if _is_admitted_product(raw_product):
            grouped[category].append(raw_product)

    cards: list[dict[str, object]] = []
    supported: set[str] = set()
    for category_id in sorted(grouped):
        metadata = taxonomy_categories[category_id]
        semantic_profile = metadata["semantic_profile"]
        baseline = baselines.get(category_id)
        if semantic_profile is None:
            continue
        if type(baseline) is not list or any(type(value) is not str for value in baseline):
            raise CategoryKnowledgeBuildError("electronics category metadata is invalid")
        cards.extend(
            _category_cards(
                category_id=category_id,
                label=cast(str, metadata["label"]),
                aliases=cast(list[str], metadata["aliases"]),
                semantic_profile=cast(str, semantic_profile),
                baseline=cast(list[str], baseline),
                products=grouped[category_id],
                last_updated=last_updated,
            )
        )
        supported.add(category_id)
    if not cards:
        raise CategoryKnowledgeBuildError("catalog has no admitted Category Cards")
    if len(cards) != len(supported) * len(CARD_TYPES):
        raise CategoryKnowledgeBuildError("every supported category must produce three cards")

    manifest = {
        "schema_version": MANIFEST_SCHEMA,
        "card_schema": CARD_SCHEMA,
        "data_mode": DATA_MODE,
        "generator_version": GENERATOR_VERSION,
        "source_artifacts": [
            {
                "name": catalog_path.name,
                "schema_version": SOURCE_SCHEMA,
                "sha256": hashlib.sha256(catalog_bytes).hexdigest(),
            },
            {
                "name": taxonomy_path.name,
                "schema_version": TAXONOMY_SCHEMA,
                "sha256": hashlib.sha256(taxonomy_bytes).hexdigest(),
            },
        ],
        "snapshot_version": snapshot_version,
        "last_updated": last_updated,
        "card_count": len(cards),
        "supported_scope_terms": _supported_scope_terms(taxonomy_categories, supported),
        "unsupported_terms": _unsupported_terms(taxonomy_categories, supported),
    }
    return cards, manifest


def build_cards(catalog_path: Path, taxonomy_path: Path) -> list[dict[str, object]]:
    cards, _manifest = build_artifacts(catalog_path, taxonomy_path)
    return cards


def _load_object(raw: bytes, name: str) -> dict[str, object]:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CategoryKnowledgeBuildError(name + " is invalid") from error
    if type(value) is not dict:
        raise CategoryKnowledgeBuildError(name + " must be an object")
    return value


def _taxonomy_categories(taxonomy: dict[str, object]) -> dict[str, dict[str, object]]:
    groups = taxonomy.get("groups")
    if type(groups) is not list:
        raise CategoryKnowledgeBuildError("digital taxonomy groups are invalid")
    result: dict[str, dict[str, object]] = {}
    for group in groups:
        if type(group) is not dict or type(group.get("categories")) is not list:
            raise CategoryKnowledgeBuildError("digital taxonomy group is invalid")
        for category in group["categories"]:
            if type(category) is not dict:
                raise CategoryKnowledgeBuildError("digital taxonomy category is invalid")
            category_id = category.get("id")
            label = category.get("label")
            aliases = category.get("aliases")
            semantic_profile = category.get("semantic_profile")
            scope_terms = category.get("scope_terms")
            if (
                type(category_id) is not str
                or not category_id
                or category_id in result
                or type(label) is not str
                or not label
                or type(aliases) is not list
                or any(type(alias) is not str or not alias for alias in aliases)
                or (
                    semantic_profile is not None
                    and (type(semantic_profile) is not str or not semantic_profile.strip())
                )
                or (
                    scope_terms is not None
                    and (
                        type(scope_terms) is not list
                        or not scope_terms
                        or len(scope_terms) != len(set(scope_terms))
                        or any(
                            type(term) is not str or len(term.strip()) < 2 for term in scope_terms
                        )
                    )
                )
            ):
                raise CategoryKnowledgeBuildError("digital taxonomy category is invalid")
            result[category_id] = {
                "label": label,
                "aliases": aliases,
                "semantic_profile": semantic_profile,
                "scope_terms": scope_terms,
            }
    if not result:
        raise CategoryKnowledgeBuildError("digital taxonomy is empty")
    return result


def _is_admitted_product(product: dict[str, object]) -> bool:
    price_source = product.get("price_source")
    specification_source = product.get("specification_source")
    attributes = product.get("attributes")
    if (
        type(price_source) is not dict
        or price_source.get("source_type") not in _ADMITTED_PRICE_SOURCES
        or type(price_source.get("url")) is not str
        or type(specification_source) is not dict
        or specification_source.get("source_type") not in _ADMITTED_SPECIFICATION_SOURCES
        or type(specification_source.get("url")) is not str
        or type(attributes) is not dict
    ):
        return False
    _positive_decimal(product.get("reference_price_cny"), "reference price")
    return True


def _category_cards(
    *,
    category_id: str,
    label: str,
    aliases: list[str],
    semantic_profile: str,
    baseline: list[str],
    products: list[dict[str, object]],
    last_updated: str,
) -> list[dict[str, object]]:
    recall_terms = list(dict.fromkeys([label, *aliases, category_id]))
    components = _category_components(label, aliases)
    attribute_values: dict[str, Counter[str]] = defaultdict(Counter)
    priced_products: list[tuple[Decimal, dict[str, object]]] = []
    evidence_refs: set[str] = set()
    valid_baseline_values = 0
    relevant_baseline = [name for name in baseline if name not in _EXCLUDED_ATTRIBUTES]
    for product in products:
        price = _positive_decimal(product.get("reference_price_cny"), "reference price")
        priced_products.append((price, product))
        attributes = product["attributes"]
        if type(attributes) is not dict:
            raise CategoryKnowledgeBuildError("product attributes are invalid")
        for name in relevant_baseline:
            value = attributes.get(name)
            if _is_valid_attribute_value(value):
                attribute_values[name][" ".join(str(value).split())] += 1
                valid_baseline_values += 1
        for source_name in ("specification_source", "price_source"):
            source = product[source_name]
            if type(source) is not dict or type(source.get("url")) is not str:
                raise CategoryKnowledgeBuildError("product source is invalid")
            evidence_refs.add(source["url"])

    attributes = _attribute_distributions(attribute_values, len(products))
    tiers = _price_tiers([price for price, _product in priced_products], len(products))
    bestsellers = _synthetic_bestsellers(priced_products, last_updated)
    if not attributes:
        raise CategoryKnowledgeBuildError("category has no sufficiently covered attributes")
    total_baseline_values = len(products) * len(relevant_baseline)
    coverage = Decimal(valid_baseline_values) / Decimal(total_baseline_values)
    sample_factor = min(Decimal(1), Decimal(len(products)) / Decimal(50))
    confidence = min(
        Decimal("0.95"),
        Decimal("0.45") + Decimal("0.25") * sample_factor + Decimal("0.30") * coverage,
    ).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)
    metadata = {
        "kind": "category_metadata",
        "schema": CARD_SCHEMA,
        "category_id": "electronics." + category_id,
        "recall_terms": recall_terms,
        "semantic_profile": " ".join(semantic_profile.split()),
        "source_count": len(products),
        "data_mode": DATA_MODE,
        "generator_version": GENERATOR_VERSION,
        "evidence_refs": sorted(evidence_refs)[:16],
    }
    prefix = [label, *aliases, semantic_profile]
    return [
        _validated_card(
            category_id,
            label,
            "bestseller",
            _bounded_text(
                [
                    *prefix,
                    "类目组成 " + "、".join(components),
                    "合成演示榜 " + "、".join(str(item["name"]) for item in bestsellers),
                    "不代表真实销量",
                ],
                200,
            ),
            metadata,
            {"kind": "bestseller_payload", "components": components, "bestsellers": bestsellers},
            last_updated,
            confidence,
        ),
        _validated_card(
            category_id,
            label,
            "attribute",
            _bounded_text(
                [
                    *prefix,
                    "属性分布 " + "、".join(str(item["name"]) for item in attributes),
                    "样本 " + str(len(products)),
                ],
                200,
            ),
            metadata,
            {"kind": "attribute_payload", "attributes": attributes},
            last_updated,
            confidence,
        ),
        _validated_card(
            category_id,
            label,
            "price_range",
            _bounded_text(
                [
                    *prefix,
                    "价格带 "
                    + "、".join(
                        str(item["tier"])
                        + " "
                        + "-".join(str(value) for value in cast(list[object], item["range_cny"]))
                        for item in tiers
                    ),
                    "样本 " + str(len(products)),
                ],
                200,
            ),
            metadata,
            {"kind": "price_range_payload", "price_tiers": tiers},
            last_updated,
            confidence,
        ),
    ]


def _validated_card(
    category_id: str,
    category: str,
    card_type: str,
    summary: str,
    metadata: dict[str, object],
    payload: dict[str, object],
    last_updated: str,
    confidence: Decimal,
) -> dict[str, object]:
    card: dict[str, object] = {
        "card_id": "electronics." + category_id + ":" + card_type,
        "category": category,
        "card_type": card_type,
        "summary": summary,
        "raw_evidence": [
            json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        ],
        "last_updated": last_updated,
        "confidence": str(confidence),
    }
    _validate_card(card)
    return card


def _category_components(label: str, aliases: list[str]) -> list[str]:
    values: list[str] = []
    for value in aliases:
        compact = " ".join(value.split())
        if compact == label or not any("\u4e00" <= character <= "\u9fff" for character in compact):
            continue
        if compact not in values:
            values.append(compact)
        if len(values) == 3:
            break
    return values or [label]


def _synthetic_bestsellers(
    priced_products: list[tuple[Decimal, dict[str, object]]], last_updated: str
) -> list[dict[str, object]]:
    ordered_prices = sorted(price for price, _product in priced_products)
    low, high = ordered_prices[0], ordered_prices[-1]
    median = ordered_prices[len(ordered_prices) // 2]
    max_attributes = max(
        len(cast(dict[str, object], product["attributes"])) for _, product in priced_products
    )
    current_year_match = _YEAR.search(last_updated)
    current_year = int(current_year_match.group()) if current_year_match else 2026
    ranked: list[tuple[Decimal, str, str, dict[str, object]]] = []
    for price, product in priced_products:
        title = product.get("title")
        attributes = product.get("attributes")
        if type(title) is not str or not title.strip() or type(attributes) is not dict:
            raise CategoryKnowledgeBuildError("synthetic bestseller input is invalid")
        price_score = Decimal(1) if high == low else Decimal(1) - abs(price - median) / (high - low)
        specification_score = Decimal(len(attributes)) / Decimal(max_attributes)
        year_match = _YEAR.search(str(attributes.get("release_date", "")) + " " + title)
        year = int(year_match.group()) if year_match else current_year - 5
        recency_score = max(
            Decimal(0), Decimal(1) - Decimal(max(0, current_year - year)) / Decimal(5)
        )
        score = (
            price_score * Decimal("0.40")
            + specification_score * Decimal("0.35")
            + recency_score * Decimal("0.25")
        )
        ranked.append((-score, title, _product_segment(title), product))
    ranked.sort(key=lambda item: (item[0], item[1]))

    selected: list[dict[str, object]] = []
    seen_models: set[str] = set()
    seen_brands: set[str] = set()
    seen_segments: set[str] = set()
    for require_diversity in (True, False):
        for _negative_score, title, segment, product in ranked:
            attributes = cast(dict[str, object], product["attributes"])
            model = str(attributes.get("model", title)).casefold()
            brand = str(attributes.get("brand", "")).casefold()
            if model in seen_models:
                continue
            if require_diversity and ((brand and brand in seen_brands) or segment in seen_segments):
                continue
            price = _positive_decimal(product.get("reference_price_cny"), "reference price")
            selected.append(
                {
                    "name": title[:200],
                    "typical_price_cny": str(price),
                    "why_popular": (
                        "合成演示榜："
                        + segment
                        + "代表，规格覆盖 "
                        + str(len(attributes))
                        + " 项，价格位于类目主流区间；不代表真实销量"
                    )[:200],
                }
            )
            seen_models.add(model)
            if brand:
                seen_brands.add(brand)
            seen_segments.add(segment)
            if len(selected) == 3:
                return selected
    if not selected:
        raise CategoryKnowledgeBuildError("synthetic bestsellers are empty")
    return selected


def _product_segment(title: str) -> str:
    for marker in (
        "轻薄游戏本",
        "游戏本",
        "轻薄本",
        "商务本",
        "创作本",
        "全能本",
        "工作站",
        "旗舰",
        "专业",
    ):
        if marker in title:
            return marker
    return "主流型号"


def _is_valid_attribute_value(value: object) -> bool:
    if type(value) is not str:
        return False
    normalized = " ".join(value.split())
    return (
        bool(normalized)
        and len(normalized) <= 256
        and not any(marker in normalized for marker in _REJECTED_VALUE_MARKERS)
    )


def _attribute_distributions(
    values_by_name: dict[str, Counter[str]], product_count: int
) -> list[dict[str, object]]:
    ranked: list[tuple[float, float, str, Counter[str]]] = []
    minimum_observed = max(3, (product_count + 1) // 2)
    for name, counts in values_by_name.items():
        observed = sum(counts.values())
        if observed < minimum_observed or len(counts) < 2:
            continue
        ranked.append((observed / product_count, max(counts.values()) / observed, name, counts))
    ranked.sort(key=lambda item: (-item[0], -item[1], item[2]))
    return [
        {"name": name, "distribution": _normalized_distribution(counts)}
        for _coverage, _concentration, name, counts in ranked[:8]
    ]


def _normalized_distribution(counts: Counter[str]) -> dict[str, str]:
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    kept = ordered[:7]
    omitted = sum(count for _value, count in ordered[7:])
    if omitted:
        kept.append(("其他", omitted))
    total = sum(count for _value, count in kept)
    result: dict[str, str] = {}
    running = Decimal(0)
    for index, (value, count) in enumerate(kept):
        probability = (
            Decimal(1) - running
            if index == len(kept) - 1
            else (Decimal(count) / Decimal(total)).quantize(
                Decimal("0.000001"), rounding=ROUND_HALF_UP
            )
        )
        result[value] = str(probability)
        running += probability
    return result


def _price_tiers(prices: list[Decimal], source_count: int) -> list[dict[str, object]]:
    unique = sorted(set(prices))
    if len(unique) < 4:
        raise CategoryKnowledgeBuildError("category has insufficient observed price diversity")
    low = unique[0]
    first = unique[max(1, (len(unique) - 1) // 3)]
    second = unique[max(2, ((len(unique) - 1) * 2) // 3)]
    high = unique[-1]
    if not low < first < second < high:
        raise CategoryKnowledgeBuildError("category observed price quantiles are not increasing")
    note = f"合成面试类目价格区间，样本 {source_count}"
    return [
        {"tier": "budget", "range_cny": [str(low), str(first)], "notes": note},
        {"tier": "mid", "range_cny": [str(first), str(second)], "notes": note},
        {"tier": "premium", "range_cny": [str(second), str(high)], "notes": note},
    ]


def _validate_card(card: dict[str, object]) -> None:
    required = {
        "card_id",
        "category",
        "card_type",
        "summary",
        "raw_evidence",
        "last_updated",
        "confidence",
    }
    if set(card) != required or card.get("card_type") not in CARD_TYPES:
        raise CategoryKnowledgeBuildError("Category Card schema is invalid")
    for field in ("card_id", "category", "summary", "last_updated"):
        if (
            type(card.get(field)) is not str
            or not str(card[field]).strip()
            or "\0" in str(card[field])
        ):
            raise CategoryKnowledgeBuildError("Category Card text is invalid")
    if len(str(card["summary"])) > 200:
        raise CategoryKnowledgeBuildError("Category Card summary is too long")
    evidence = card.get("raw_evidence")
    if (
        type(evidence) is not list
        or len(evidence) != 2
        or any(type(item) is not str for item in evidence)
    ):
        raise CategoryKnowledgeBuildError("Category Card evidence is invalid")
    confidence = Decimal(str(card.get("confidence")))
    if not confidence.is_finite() or not Decimal("0.5") <= confidence <= Decimal(1):
        raise CategoryKnowledgeBuildError("Category Card confidence is invalid")


def _unsupported_terms(
    taxonomy_categories: dict[str, dict[str, object]], supported: set[str]
) -> list[str]:
    terms: list[str] = []
    for category_id in sorted(set(taxonomy_categories) - supported):
        category = taxonomy_categories[category_id]
        terms.extend(
            [cast(str, category["label"]), category_id, *cast(list[str], category["aliases"])]
        )
    return list(dict.fromkeys(term for term in terms if len(term.strip()) >= 2))


def _supported_scope_terms(
    taxonomy_categories: dict[str, dict[str, object]], supported: set[str]
) -> list[str]:
    terms: list[str] = []
    for category_id in sorted(supported):
        category = taxonomy_categories[category_id]
        scope_terms = category["scope_terms"]
        if type(scope_terms) is not list or not scope_terms:
            raise CategoryKnowledgeBuildError("supported category scope terms are missing")
        terms.extend(
            [
                cast(str, category["label"]),
                *cast(list[str], category["aliases"]),
                *cast(list[str], scope_terms),
            ]
        )
    return list(dict.fromkeys(terms))


def _positive_decimal(value: object, name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as error:
        raise CategoryKnowledgeBuildError(name + " is invalid") from error
    if not result.is_finite() or result <= 0:
        raise CategoryKnowledgeBuildError(name + " must be positive")
    return result


def _bounded_text(parts: list[str], limit: int) -> str:
    selected: list[str] = []
    used = 0
    for raw in parts:
        part = " ".join(raw.split())
        if not part or part in selected:
            continue
        required = len(part) + int(bool(selected))
        if used + required <= limit:
            selected.append(part)
            used += required
        elif not selected:
            return part[:limit]
    return " ".join(selected)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--taxonomy", type=Path, required=True)
    parser.add_argument("--cards", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    cards, manifest = build_artifacts(args.catalog.resolve(), args.taxonomy.resolve())
    args.cards.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.cards.write_text(
        "".join(
            json.dumps(card, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            for card in cards
        ),
        encoding="utf-8",
    )
    args.manifest.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"CATEGORY_CARDS_BUILT cards={len(cards)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
