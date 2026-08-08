# Current Product Commerce Enrichment Design

## Goal

Rebuild the 926,710-document current-product OpenSearch index with coherent price, inventory, shipping, tax, duty, delivery, platform, and evidence fields, then expose those documents through the existing shopping Agent contracts without a runtime side database.

## Decisions

- Keep the existing canonical `document_id` and existing 1,024-dimensional Item vectors. Do not retrain or re-encode the catalog.
- Replace the current derived Category Card v1 artifact with a typed Category Card v2 policy artifact. Card v2 is an input to index construction, not a summary generated after arbitrary prices already exist.
- Generate all commerce fields once during index construction. Runtime search and adapters only read and validate them.
- Rebuild a new physical OpenSearch index and switch the alias only after exact mapping, document-count, Card-manifest, vector-manifest, distribution, and hybrid-query checks pass.
- Do not add a runtime SQLite sidecar and do not generate commerce values inside `CurrentProductItemSource`.

## Category Card V2

One Card represents one price- and fulfillment-coherent leaf category. Cards form a parent/child taxonomy so classification and policy can fall back to a parent without using one global price range.

```json
{
  "schema_version": "glodex.category-card.v2",
  "card_id": "electronics.laptop",
  "parent_id": "electronics.computer",
  "display_name": "Laptop computer",
  "aliases": ["laptop", "notebook", "笔记本", "轻薄本", "游戏本"],
  "classification": {
    "prototype_text": "laptop notebook portable computer",
    "positive_keywords": ["laptop", "notebook", "macbook", "ssd", "ram"],
    "negative_keywords": ["case", "sticker", "replacement battery", "charger"],
    "entity_kind": "PRIMARY_PRODUCT",
    "minimum_score": "0.42",
    "minimum_margin": "0.03"
  },
  "attributes": {
    "important": ["brand", "processor", "memory", "storage", "screen_size"],
    "premium_signals": ["pro", "ultra", "gaming", "workstation"],
    "budget_signals": ["entry", "basic", "student"],
    "bundle_signals": ["bundle", "kit", "set"]
  },
  "pricing": {
    "currency": "CNY",
    "hard_min": "1500.00",
    "p10": "2800.00",
    "p50": "6500.00",
    "p90": "15000.00",
    "hard_max": "35000.00",
    "distribution": "LOG_NORMAL",
    "brand_multipliers": {"apple": "1.45", "generic": "0.72"},
    "premium_signal_multiplier": "1.25",
    "budget_signal_multiplier": "0.78",
    "bundle_multiplier_max": "1.30"
  },
  "inventory": {
    "in_stock_probability": "0.86",
    "out_of_stock_probability": "0.10",
    "unknown_probability": "0.04"
  },
  "fulfillment": {
    "size_class": "MEDIUM",
    "weight_class": "MEDIUM",
    "shipping_min": "0.00",
    "shipping_p50": "35.00",
    "shipping_max": "120.00",
    "free_shipping_probability": "0.45",
    "free_shipping_threshold": "5000.00",
    "eta_days_min": 2,
    "eta_days_max": 10
  },
  "platforms": {
    "amazon": {"price_multiplier": "1.05", "stock_multiplier": "1.05"},
    "ebay": {"price_multiplier": "0.88", "stock_multiplier": "0.92"}
  },
  "validation": {
    "maximum_shipping_price_ratio": "0.50"
  }
}
```

The first committed taxonomy contains exactly 128 leaf Cards under a bounded parent taxonomy. It includes distinct primary-product, accessory, replacement-part, bundle, and decorative categories where their price distributions differ materially. Every parent declares one leaf `fallback_leaf_id`; indexed documents always receive a leaf Card ID. The taxonomy manifest records the exact Card count and logical SHA-256.

## Classification

Classification runs once during index construction:

1. Normalize an existing source category and match exact aliases.
2. Score positive and negative title/search-text/attribute terms.
3. For unresolved documents, compare the existing Item vector against precomputed Card prototype vectors from the same embedding model.
4. Accept a leaf Card only when its minimum score and winning margin pass. Otherwise use the best accepted parent's declared `fallback_leaf_id`, recording `PARENT_FALLBACK` as the assignment method.
5. Reject the build if any document has no accepted Card or if primary/accessory/part exclusion fixtures regress.

The classification artifact is bound to the Card manifest and Item-vector manifest. Re-running it with the same inputs must produce identical Card IDs and report hashes.

## Commerce Generation

For each classified document, the builder deterministically samples within its Card distributions using a versioned build seed. The digest selects a quantile; the Card decides the monetary range and modifiers. The digest never directly defines a global amount.

Price is sampled around `p50`, adjusted by bounded brand/title/platform multipliers, then clamped to `hard_min` and `hard_max`. Inventory probabilities come from the Card and platform modifier. Shipping is sampled from the Card fulfillment profile, respects free-shipping probability/threshold, and must not exceed either `shipping_max` or the Card's maximum shipping/price ratio. Tax and duty use one versioned ruleset. Delivery bounds come from the Card and platform profile.

Each indexed document contains stable IDs and all fields needed to construct one `Product`, one `Offer`, its `EvidenceRef` objects, and a CNY identity exchange rate without another storage read.

## OpenSearch Document Contract

The v3 mapping adds exact fields for:

- `category_card_id`, normalized category path, and `entity_kind`;
- `platform`, `provider_id`, `market`, `offer_id`, and `source_uri`;
- scaled-decimal `item_price`, `shipping`, `tax`, and `duty`, plus `currency`;
- `stock_status`, `delivery_days_min`, and `delivery_days_max`;
- stable evidence IDs for product core fields, inventory, currency, and every cost component;
- `commerce_ruleset_version`, `category_card_manifest_sha256`, and fixed `captured_at`.

The vector and `search_text` remain excluded from returned `_source`; commerce fields remain in `_source`. The gateway accepts only `query`, `top_k`, one enum `platform`, and one validated leaf `category_card_id`, applies fixed term filters, and validates every returned commerce field.

## Agent Adapter

`CurrentProductItemSource` implements `ItemSourcePort`. It calls the loopback current-product gateway, validates its lineage, converts each hit to `Candidate`, `Product`, `Offer`, `EvidenceRef`, and `CatalogBatch`, and returns `ItemSearchRuntimeResult`. A manifest factory derives `ManifestRecord` values from those exact results.

The adapter does not classify products, generate values, repair responses, or query another store. Invalid or incomplete commerce documents fail with `ITEM_SOURCE_INVALID`.

The shipping tool consumes exact offer cost and delivery facts. Existing platform shipping rules remain only for records whose offer cost is explicitly unknown; the rebuilt current-product documents use their own Card-derived exact fields.

## Composition

The Agent API composition validates PostgreSQL, Redis, model-service identity, current-product gateway identity, Card manifest, Item-vector manifest, and OpenSearch alias before binding `127.0.0.1:8766`. It then constructs `ReActAgentService`, `DurableAgentCoordinator`, and the public durable app. No removed Demo Snapshot adapter is selected as a fallback.

## Verification

- Unit tests cover Card schema, inheritance, classification exclusions, price bands, multiplier clamps, inventory probabilities, shipping caps, and deterministic generation.
- Builder tests cover exact v3 mapping/source shape and manifest binding.
- Gateway tests cover platform filtering and malformed commerce-hit rejection.
- Adapter tests prove exact `Candidate`/`CatalogBatch` evidence closure and manifest construction.
- Composition tests prove `agent-api serve` constructs the ReAct executor only after dependency validation.
- Server acceptance rebuilds a new physical index, verifies 926,710 documents, audits per-Card price and shipping quantiles, runs laptop/cup/accessory canaries, switches the alias, and completes one Agent search.
