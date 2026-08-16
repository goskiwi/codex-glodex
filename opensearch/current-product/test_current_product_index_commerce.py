from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from glodex.interview_catalog.category_classifier import CategoryClassifier
from glodex.interview_catalog.category_policy import load_category_cards

MODULE_PATH = Path(__file__).with_name("build_current_product_index.py")
SPEC = importlib.util.spec_from_file_location("build_current_product_index", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
builder = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = builder
SPEC.loader.exec_module(builder)

CARD_ARTIFACT = (
    Path(__file__).resolve().parents[2]
    / "data"
    / "current-product"
    / "category-cards-v2"
    / "category_cards.json"
)


def _binding() -> object:
    return builder.Binding(
        config_path=Path("binding.json"),
        current_catalog_path=Path("catalog.sqlite3"),
        expected_count=926710,
        source_binding_sha256="a" * 64,
        vector_manifest_path=Path("vectors.json"),
        vector_manifest_sha256="b" * 64,
        vector_manifest_schema_version="current_catalog_item_vectors_v1",
        endpoint="http://127.0.0.1:9200",
        alias="glodex-current-product-hybrid",
        physical_index="glodex-current-product-hybrid-commerce-v3",
        pipeline="glodex-current-product-hybrid-v1",
        dimension=1024,
        vector_field="item_vector",
        semantic_weight=0.7,
        lexical_weight=0.3,
    )


def test_v3_mapping_contains_filterable_commerce_fields() -> None:
    body = builder.index_body(_binding())
    mappings = body["mappings"]
    properties = mappings["properties"]

    assert mappings["_meta"]["schema_version"] == "glodex.current-product-hybrid-index.v6"
    assert properties["attributes"] == {"type": "object", "enabled": False}
    assert properties["category_card_id"] == {"type": "keyword"}
    assert properties["entity_kind"] == {"type": "keyword"}
    assert properties["item_price"]["type"] == "scaled_float"
    assert properties["shipping"]["type"] == "scaled_float"
    assert properties["stock_status"] == {"type": "keyword"}
    assert properties["delivery_days_min"] == {"type": "integer"}
    assert properties["delivery_days_max"] == {"type": "integer"}


def test_enrichment_retains_identity_and_vector() -> None:
    policies = load_category_cards(CARD_ARTIFACT)
    classifier = CategoryClassifier(policies)
    vector = [1.0] + [0.0] * 1023
    source = {
        "document_id": "doc-1",
        "title": "16GB SSD gaming laptop",
        "search_text": "16GB SSD gaming laptop computer",
        "category": None,
        "source": "amazon",
        "brand": "Example",
        "color": None,
        "attribute_names": ["brand"],
    }

    enriched = builder._enrich_source(
        source,
        item_vector=vector,
        classifier=classifier,
        policies=policies,
        seed="semantic-category-clean-v3",
        vector_match=("electronics.laptop", 0.91, 0.40),
    )

    assert enriched["document_id"] == "doc-1"
    assert enriched["item_vector"] is vector
    assert enriched["category_card_id"] == "electronics.laptop"
    assert enriched["attributes"] == {"brand": "Example", "storage": "16 GB"}
    assert enriched["item_price"] >= 1500
    assert enriched["stock_status"] in {"IN_STOCK", "OUT_OF_STOCK"}
    assert enriched["stock_status"] != "UNKNOWN"


def test_category_vectors_are_encoded_in_server_batches() -> None:
    batch_sizes: list[int] = []

    def encode(texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        batch_sizes.append(len(texts))
        return tuple((1.0, 0.0, 0.0) for _ in texts)

    policies, classifier = builder._build_category_classifier(
        cards_path=CARD_ARTIFACT,
        dimension=3,
        encode=encode,
    )

    assert len(policies.cards) == 128
    assert batch_sizes == [8] * 16
    result = classifier.classify(
        category=None,
        item_vector=(1.0, 0.0, 0.0),
    )
    assert result.method == "FALLBACK"
