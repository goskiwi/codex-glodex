# Current Product Commerce Enrichment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild the 926,710-document current-product OpenSearch index with Category Card-driven price, inventory, shipping, and delivery facts, then make the durable ReAct Agent consume those facts through its existing typed contracts.

**Architecture:** Category Card v2 is a committed, hash-verified build policy with exactly 128 leaf Cards. The current OpenSearch builder reuses the existing Item-vector matrix, classifies every product against those Cards, generates commerce fields once, validates their distributions, and atomically publishes a new physical index. The loopback gateway returns complete documents; package adapters convert them into `Candidate`, `CatalogBatch`, and `CandidateManifest`, and a focused composition root starts the durable Agent API.

**Tech Stack:** Python 3.12, Pydantic v2, NumPy, SQLite read-only catalog access, OpenSearch 2.17 hybrid search, FastAPI/httpx, LangGraph, PostgreSQL, Redis, pytest.

---

## File Map

- Create `src/glodex/interview_catalog/category_policy.py`: strict Card v2 models, inheritance, manifest verification, and lookup.
- Create `src/glodex/interview_catalog/category_classifier.py`: existing-category, lexical, and vector classification.
- Create `src/glodex/interview_catalog/commerce_generator.py`: Card-driven product-level commerce generation and audit statistics.
- Create `data/current-product/category-cards-v2/cards.json`: exactly 128 leaf Cards plus parent policies.
- Create `data/current-product/category-cards-v2/manifest.json`: hashes, Card count, schema and ruleset versions.
- Create `scripts/build_category_card_vectors.py`: encode Card prototypes with the bound retrieval model and write a verified server artifact.
- Modify `src/glodex/interview_catalog/cards.py`: remove the v1 post-price aggregation role; expose v2 verification/build reporting only.
- Modify `src/glodex/interview_catalog/generator.py`: replace global/hash price ranges with Card classification and Card-driven generation.
- Create `tests/interview_catalog/test_category_policy.py`.
- Create `tests/interview_catalog/test_category_classifier.py`.
- Create `tests/interview_catalog/test_commerce_generator.py`.
- Modify `opensearch/current-product/current_product_binding.json`: bind Card manifest, Card vectors, commerce ruleset, and v3 physical index.
- Modify `opensearch/current-product/build_current_product_index.py`: enrich batches, create v3 mapping, audit distributions, and verify canaries.
- Modify `opensearch/current-product/current_product_hybrid_gateway.py`: accept category/platform filters and return validated commerce fields.
- Modify `opensearch/current-product/test_current_product_hybrid_gateway.py`.
- Create `src/glodex/retrieval/current_product.py`: bounded gateway client and `ItemSourcePort` adapter.
- Create `src/glodex/interview_catalog/runtime.py`: query-to-Card intent/category filters and `CategoryInsightPort`.
- Create `tests/retrieval/test_current_product_item_source.py`.
- Create `tests/interview_catalog/test_runtime.py`.
- Modify `src/glodex/agent/contracts.py`: allow the 128-Card capability set.
- Modify `src/glodex/domain/catalog.py`: retain offer delivery bounds.
- Modify `src/glodex/tools/engine.py`: use offer delivery bounds before platform fallback rules.
- Modify `tests/m1d/unit/test_agent_tools.py` and relevant domain tests.
- Create `src/glodex/composition/agent_api.py`: validate dependencies and build the ReAct durable app.
- Modify `src/glodex/cli.py`: replace the current fail-closed stub with composition startup.
- Create `tests/composition/test_agent_api.py`.
- Modify `README.md` and `opensearch/current-product/README.md`.

### Task 1: Card V2 Contract and Artifact Verification

**Files:**
- Create: `src/glodex/interview_catalog/category_policy.py`
- Create: `tests/interview_catalog/test_category_policy.py`
- Modify: `src/glodex/interview_catalog/__init__.py`

- [ ] **Step 1: Write failing model and manifest tests**

```python
def test_laptop_card_preserves_numeric_policy() -> None:
    policy = CategoryPolicy.model_validate(LAPTOP_CARD)
    assert policy.card_id == "electronics.laptop"
    assert policy.pricing.hard_min == Decimal("1500.00")
    assert policy.pricing.p10 < policy.pricing.p50 < policy.pricing.p90
    assert policy.pricing.p90 <= policy.pricing.hard_max


def test_manifest_requires_exactly_128_leaf_cards(tmp_path: Path) -> None:
    root = write_policy_artifact(tmp_path, cards=(LAPTOP_CARD,))
    with pytest.raises(CategoryPolicyError, match="128 leaf Cards"):
        load_category_policies(root)
```

- [ ] **Step 2: Run the tests and confirm RED**

Run: `UV_CACHE_DIR=/tmp/glodex-uv-cache uv run pytest tests/interview_catalog/test_category_policy.py -q`

Expected: collection fails because `category_policy` does not exist.

- [ ] **Step 3: Implement strict immutable Card types**

Implement exact Pydantic models for `ClassificationPolicy`, `AttributePolicy`, `PricingPolicy`, `InventoryPolicy`, `FulfillmentPolicy`, `PlatformPolicy`, `ValidationPolicy`, and `CategoryPolicy`. Enforce:

```python
hard_min <= p10 < p50 < p90 <= hard_max
in_stock_probability + out_of_stock_probability + unknown_probability == Decimal("1")
shipping_min <= shipping_p50 <= shipping_max
0 <= maximum_shipping_price_ratio <= 1
eta_days_min <= eta_days_max
```

Implement `load_category_policies(root: Path) -> CategoryPolicySet` that verifies both file SHA-256 and canonical logical SHA-256, rejects duplicate IDs/aliases, resolves parent inheritance, detects cycles, requires exactly 128 leaves, and requires every parent `fallback_leaf_id` to name one of its descendant leaves.

- [ ] **Step 4: Run tests and confirm GREEN**

Run: `UV_CACHE_DIR=/tmp/glodex-uv-cache uv run pytest tests/interview_catalog/test_category_policy.py -q`

Expected: all Task 1 tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/glodex/interview_catalog/category_policy.py src/glodex/interview_catalog/__init__.py tests/interview_catalog/test_category_policy.py
git commit -m "feat: define category card v2 policy"
```

### Task 2: Commit the 128-Leaf Commerce Taxonomy

**Files:**
- Create: `data/current-product/category-cards-v2/cards.json`
- Create: `data/current-product/category-cards-v2/manifest.json`
- Test: `tests/interview_catalog/test_category_policy.py`

- [ ] **Step 1: Add failing taxonomy canaries**

```python
def test_committed_taxonomy_has_required_price_distinctions() -> None:
    policies = load_category_policies(CARD_ROOT)
    assert len(policies.leaves) == 128
    assert policies.require("electronics.laptop").pricing.hard_min >= Decimal("1500")
    assert policies.require("home.drinkware.cup").pricing.hard_max <= Decimal("800")
    assert policies.require("electronics.laptop-accessory").classification.entity_kind == "ACCESSORY"
    assert policies.require("electronics.laptop-part").classification.entity_kind == "REPLACEMENT_PART"
```

- [ ] **Step 2: Run the canary and confirm RED**

Run: `UV_CACHE_DIR=/tmp/glodex-uv-cache uv run pytest tests/interview_catalog/test_category_policy.py::test_committed_taxonomy_has_required_price_distinctions -q`

Expected: failure because the committed artifact is absent.

- [ ] **Step 3: Author the taxonomy artifact**

Create parent policies for electronics, appliances, home, furniture, kitchen, clothing, shoes, beauty, sports, outdoor, automotive, tools, office, books/media, toys, baby, pets, grocery, health, jewelry, luggage, musical instruments, industrial, and general merchandise. Create exactly 128 leaf Cards under those parents. Every leaf must define or inherit all classification, pricing, inventory, fulfillment, platform, and validation fields. Include explicit laptop, laptop-accessory, laptop-part, phone, tablet, monitor, headphone, cup/drinkware, furniture, book, clothing, grocery, and appliance Cards.

Generate `manifest.json` through the Task 1 artifact writer, then assert its concrete values rather than hand-entering hashes:

```python
manifest = write_category_policy_artifact(cards=cards, output_root=CARD_ROOT)
assert manifest.schema_version == "glodex.category-card-manifest.v2"
assert manifest.ruleset_version == "current-commerce-v1"
assert manifest.leaf_card_count == 128
assert manifest.files["cards"].sha256 == sha256_file(CARD_ROOT / "cards.json")
assert manifest.files["cards"].logical_sha256 == canonical_json_sha256(cards)
```

The exact leaf-ID set is fixed in **Appendix A**. Changing an ID, count, or group is a design change, not an implementation detail.

- [ ] **Step 4: Run policy and canary tests**

Run: `UV_CACHE_DIR=/tmp/glodex-uv-cache uv run pytest tests/interview_catalog/test_category_policy.py -q`

Expected: all tests pass and the loader reports 128 leaves.

- [ ] **Step 5: Commit**

```bash
git add data/current-product/category-cards-v2 tests/interview_catalog/test_category_policy.py
git commit -m "feat: add current product category policies"
```

### Task 3: Category Classification

**Files:**
- Create: `src/glodex/interview_catalog/category_classifier.py`
- Create: `tests/interview_catalog/test_category_classifier.py`
- Create: `scripts/build_category_card_vectors.py`

- [ ] **Step 1: Write failing classification tests**

```python
def test_classifier_separates_laptop_from_accessory() -> None:
    assert classifier.classify(title="16GB SSD gaming laptop", category=None).card_id == "electronics.laptop"
    assert classifier.classify(title="protective laptop sleeve case", category=None).card_id == "electronics.laptop-accessory"


def test_existing_category_then_vector_fallback() -> None:
    direct = classifier.classify(title="X", category="Kitchen / Drinkware / Cups")
    fallback = classifier.classify(title="ceramic vessel", category=None, item_vector=CUP_VECTOR)
    assert direct.card_id == "home.drinkware.cup"
    assert fallback.card_id == "home.drinkware.cup"
```

- [ ] **Step 2: Run and confirm RED**

Run: `UV_CACHE_DIR=/tmp/glodex-uv-cache uv run pytest tests/interview_catalog/test_category_classifier.py -q`

Expected: import failure for `category_classifier`.

- [ ] **Step 3: Implement the classifier**

Implement `CategoryClassifier.classify(...) -> CategoryAssignment` with this exact precedence: exact normalized category alias; lexical positive-minus-negative score; cosine prototype score. Require each Card's score and margin. When no leaf passes but a parent does, return that parent's declared leaf `fallback_leaf_id` with method `PARENT_FALLBACK`. Return an error rather than a global random Card if no parent is accepted.

Add a batch vector path using matrix multiplication over existing FP16 Item vectors and normalized Card prototype vectors. The vector script must encode all 128 Card prototype texts with the same model identity and dimension as the Item vectors, write `vectors.npy`, ordered `card_ids.json`, and `manifest.json`, then reload and hash-verify them. Persist `card_id`, score, method, Card-manifest hash, Card-vector-manifest hash, and Item-vector-manifest hash in the build audit, but only the leaf `card_id` is indexed as a filter.

- [ ] **Step 4: Run classifier tests**

Run: `UV_CACHE_DIR=/tmp/glodex-uv-cache uv run pytest tests/interview_catalog/test_category_classifier.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/glodex/interview_catalog/category_classifier.py scripts/build_category_card_vectors.py tests/interview_catalog/test_category_classifier.py
git commit -m "feat: classify current products with category cards"
```

### Task 4: Card-Driven Commerce Generator

**Files:**
- Create: `src/glodex/interview_catalog/commerce_generator.py`
- Create: `tests/interview_catalog/test_commerce_generator.py`
- Modify: `src/glodex/interview_catalog/generator.py`

- [ ] **Step 1: Write failing commerce invariants**

```python
def test_laptop_and_cup_prices_respect_category_bounds() -> None:
    laptop = generate_commerce(LAPTOP_DOCUMENT, laptop_card, context=CONTEXT)
    cup = generate_commerce(CUP_DOCUMENT, cup_card, context=CONTEXT)
    assert Decimal("1500") <= laptop.item_price <= Decimal("35000")
    assert Decimal("5") <= cup.item_price <= Decimal("800")
    assert laptop.shipping <= min(laptop_card.fulfillment.shipping_max, laptop.item_price / 2)


def test_generation_is_repeatable() -> None:
    assert generate_commerce(DOCUMENT, CARD, context=CONTEXT) == generate_commerce(DOCUMENT, CARD, context=CONTEXT)
```

- [ ] **Step 2: Run and confirm RED**

Run: `UV_CACHE_DIR=/tmp/glodex-uv-cache uv run pytest tests/interview_catalog/test_commerce_generator.py -q`

Expected: import failure for `commerce_generator`.

- [ ] **Step 3: Implement generation**

Implement `CommerceGenerationContext(seed, ruleset_version, captured_at)` and immutable `GeneratedCommerce`. Convert a stable digest into a quantile, sample a bounded log-normal price around Card `p50`, apply at most one brand, one premium/budget, one bundle, and one platform multiplier, then clamp. Generate inventory from Card probabilities. Generate shipping from the Card distribution/free-shipping policy and cap it by both `shipping_max` and price ratio. Generate tax/duty using fixed `current-commerce-v1` rates and preserve two-decimal `Decimal` values.

Replace `_PRIMARY_PRICE_BANDS_USD` and `_synthetic_item_price()` in `generator.py`; `_record_for()` must require a `CategoryAssignment` and `CategoryPolicy`.

- [ ] **Step 4: Run generator and existing interview-catalog tests**

Run: `UV_CACHE_DIR=/tmp/glodex-uv-cache uv run pytest tests/interview_catalog tests/facts -q`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/glodex/interview_catalog/commerce_generator.py src/glodex/interview_catalog/generator.py tests/interview_catalog/test_commerce_generator.py
git commit -m "feat: generate commerce facts from category policies"
```

### Task 5: OpenSearch V3 Enrichment and Mapping

**Files:**
- Modify: `opensearch/current-product/current_product_binding.json`
- Modify: `opensearch/current-product/build_current_product_index.py`
- Modify: `opensearch/current-product/build.sh`
- Modify: `opensearch/current-product/test_current_product_hybrid_gateway.py`
- Modify: `src/glodex/cli.py`
- Modify: `tests/m2b/contract/test_m2b_contracts.py`

- [ ] **Step 1: Add failing builder mapping/source tests**

Assert `index_body()` contains keyword/scaled-float/integer/date mappings for every field in the design, `_meta.schema_version == "glodex.current-product-hybrid.v3"`, and `_meta` binds Card/vector manifests and `current-commerce-v1`. Assert one `_iter_sources()` fixture contains complete commerce fields while retaining its exact `document_id` and existing vector. Assert the shell wrapper and semantic CLI expose `validate-assets`, `build-candidate`, `publish`, and `verify` with those exact names.

- [ ] **Step 2: Run and confirm RED**

Run: `UV_CACHE_DIR=/tmp/glodex-uv-cache uv run pytest opensearch/current-product/test_current_product_hybrid_gateway.py -q`

Expected: v3 mapping assertions fail against v2.

- [ ] **Step 3: Implement build-time enrichment**

Load Card policies and Card prototype vectors before creating the physical index. During each existing 192-row `_iter_sources()` batch, classify documents with the same Item-vector batch, generate commerce facts, and add the complete fields. Keep the Item vector unchanged. Change the physical index to `glodex-current-product-hybrid-commerce-v3-20260809`; retain alias `glodex-current-product-hybrid`.

Split the builder into explicit actions: `validate-assets`, `build-candidate`, `publish`, and `verify`. `build-candidate` creates and audits the physical index without touching the alias. `publish` accepts an exact candidate index name, reruns count/mapping/manifest/canary checks, and performs one atomic alias update. No build action deletes an index.

Add build audit counters and per-Card `min/p10/p50/p90/max` for price and shipping, stock counts, classification methods, rejected records, and canary assertions. Fail before alias publication unless count is exactly 926,710, every record has a leaf Card and offer, and laptop/cup/accessory invariants pass.

- [ ] **Step 4: Run builder tests**

Run: `UV_CACHE_DIR=/tmp/glodex-uv-cache uv run pytest opensearch/current-product/test_current_product_hybrid_gateway.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add opensearch/current-product/current_product_binding.json opensearch/current-product/build_current_product_index.py opensearch/current-product/build.sh opensearch/current-product/test_current_product_hybrid_gateway.py src/glodex/cli.py tests/m2b/contract/test_m2b_contracts.py
git commit -m "feat: enrich current product search index"
```

### Task 6: Gateway Commerce Contract

**Files:**
- Modify: `opensearch/current-product/current_product_hybrid_gateway.py`
- Modify: `opensearch/current-product/test_current_product_hybrid_gateway.py`

- [ ] **Step 1: Add failing request/response tests**

Test `POST /v1/hybrid-search` with `{"query":"travel laptop","top_k":10,"platform":"amazon","category_card_id":"electronics.laptop"}`. Assert the OpenSearch request contains fixed term filters and the response includes all commerce fields. Add negative tests for arbitrary fields, invalid platform/Card ID, missing evidence IDs, negative prices, reversed ETA, and mapping-manifest mismatch.

- [ ] **Step 2: Run and confirm RED**

Run: `UV_CACHE_DIR=/tmp/glodex-uv-cache uv run pytest opensearch/current-product/test_current_product_hybrid_gateway.py -q`

Expected: request shape or response fields fail.

- [ ] **Step 3: Implement strict gateway validation**

Extend the accepted request keys to exactly `query`, `top_k`, `platform`, and `category_card_id`. Add filters without exposing arbitrary DSL. Extend `_source` and `_format_hit()` with the v3 fields and enforce bounded decimals, enum values, exact evidence IDs, and ETA ordering. Return `total_recall` and `truncated` consistently with `ItemSearchRuntimeResult`.

- [ ] **Step 4: Run gateway tests**

Run: `UV_CACHE_DIR=/tmp/glodex-uv-cache uv run pytest opensearch/current-product/test_current_product_hybrid_gateway.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add opensearch/current-product/current_product_hybrid_gateway.py opensearch/current-product/test_current_product_hybrid_gateway.py
git commit -m "feat: expose current product commerce search"
```

### Task 7: Agent Category Capacity and Runtime Category Port

**Files:**
- Modify: `src/glodex/agent/contracts.py`
- Create: `src/glodex/interview_catalog/runtime.py`
- Create: `tests/interview_catalog/test_runtime.py`

- [ ] **Step 1: Add failing 128-Card and query tests**

```python
def test_capabilities_accept_all_leaf_cards(policy_set: CategoryPolicySet) -> None:
    capabilities = AgentCapabilities(
        data_mode=DataMode.SYNTHETIC_INTERVIEW_CATALOG,
        available_platforms=(Platform.AMAZON,),
        supported_categories=policy_set.leaf_ids,
    )
    assert len(capabilities.supported_categories) == 128


def test_runtime_maps_query_to_card_and_filters() -> None:
    interpreted = interpreter.interpret(SearchRequest(query="推荐出差用轻薄本", top_k=3))
    assert target_category(interpreted) == "electronics.laptop"
```

- [ ] **Step 2: Run and confirm RED**

Run: `UV_CACHE_DIR=/tmp/glodex-uv-cache uv run pytest tests/interview_catalog/test_runtime.py -q`

Expected: the 32-category bound or missing runtime module fails.

- [ ] **Step 3: Implement runtime category services**

Raise the `supported_categories` maximum to 128. Implement a Card-aware `IntentInterpreter` that reuses validated budget/preference parsing and resolves the target category from Card aliases/classification. Implement `parse_catalog_specification_filters()` required by `ShoppingToolSession`. Implement `CategoryInsightPort.retrieve()` by reducing the matched Card's components, important attributes, numeric price tiers, confidence, and Card ID.

- [ ] **Step 4: Run Agent contract and runtime tests**

Run: `UV_CACHE_DIR=/tmp/glodex-uv-cache uv run pytest tests/interview_catalog/test_runtime.py tests/m1d/unit -q`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/glodex/agent/contracts.py src/glodex/interview_catalog/runtime.py tests/interview_catalog/test_runtime.py
git commit -m "feat: expose category policies to the agent"
```

### Task 8: Current Product ItemSource Adapter and Manifest

**Files:**
- Create: `src/glodex/retrieval/current_product.py`
- Create: `tests/retrieval/test_current_product_item_source.py`

- [ ] **Step 1: Write failing adapter tests**

Use `httpx.MockTransport` to return one v3 laptop hit. Assert `CurrentProductItemSource.search()` returns one exact `Candidate`, a `CatalogBatch` with one `Product`, one `Offer`, complete evidence, CNY identity FX, correct delivery bounds, and `total_recall/truncated`. Assert `build_current_product_manifest()` binds the exact record/offer/provider IDs. Add malformed-field and lineage-mismatch rejection tests.

- [ ] **Step 2: Run and confirm RED**

Run: `UV_CACHE_DIR=/tmp/glodex-uv-cache uv run pytest tests/retrieval/test_current_product_item_source.py -q`

Expected: import failure for `current_product`.

- [ ] **Step 3: Implement client, adapter, and manifest factory**

Implement a fixed-loopback async client with byte limits, deadlines, no redirects, no proxy, and exact health identity. Map `ItemSearchInput.platform/category/top_k` to the gateway. Convert every hit without deriving or repairing values. Build stable `Product`, `Offer`, `CostComponents`, `FieldEvidence`, `EvidenceRef`, and `Candidate` values. Return `ToolPortError(ITEM_SOURCE_INVALID)` on any invalid response.

- [ ] **Step 4: Run adapter and catalog tests**

Run: `UV_CACHE_DIR=/tmp/glodex-uv-cache uv run pytest tests/retrieval/test_current_product_item_source.py tests/m1d/unit/test_agent_catalog.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/glodex/retrieval/current_product.py tests/retrieval/test_current_product_item_source.py
git commit -m "feat: adapt current products to agent candidates"
```

### Task 9: Preserve Offer Delivery Facts Through ShippingCalc

**Files:**
- Modify: `src/glodex/domain/catalog.py`
- Modify: `src/glodex/tools/engine.py`
- Modify: `tests/m1d/unit/test_agent_tools.py`
- Modify: `tests/unit/domain/test_catalog_models.py`

- [ ] **Step 1: Add failing exact-delivery test**

Construct an offer with exact shipping and `delivery_days_min=2`, `delivery_days_max=6`; pass a conflicting platform fallback rule with 9–18 days. Assert `run_shipping_calc()` returns 2–6 days from the offer.

- [ ] **Step 2: Run and confirm RED**

Run: `UV_CACHE_DIR=/tmp/glodex-uv-cache uv run pytest tests/m1d/unit/test_agent_tools.py -q`

Expected: returned ETA comes from the platform rule.

- [ ] **Step 3: Implement delivery fields**

Add optional paired `delivery_days_min/max` to `Offer`, validate non-negative ordered pairs, and require matching evidence when known. In `_build_shipping_advisory()`, prefer offer delivery bounds; use the active platform rule only when the offer has none.

- [ ] **Step 4: Run domain, pricing, and Agent tool tests**

Run: `UV_CACHE_DIR=/tmp/glodex-uv-cache uv run pytest tests/unit/domain tests/m1d/unit/test_agent_tools.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/glodex/domain/catalog.py src/glodex/tools/engine.py tests/m1d/unit/test_agent_tools.py tests/unit/domain/test_catalog_models.py
git commit -m "feat: retain offer delivery facts"
```

### Task 10: Durable ReAct Agent API Composition

**Files:**
- Create: `src/glodex/composition/__init__.py`
- Create: `src/glodex/composition/agent_api.py`
- Create: `tests/composition/test_agent_api.py`
- Modify: `src/glodex/cli.py`

- [ ] **Step 1: Add failing composition tests**

Test that `build_agent_api()` validates Card, current-product, model-service, PostgreSQL and Redis identities before creating an app; constructs `ReActAgentService` with `M2cEmbedding`, Card runtime, `CurrentProductItemSource`, manifest factory and search-service factory; passes it to `create_durable_agent_app`; and closes resources on shutdown. Assert any identity mismatch prevents `uvicorn.run()`.

- [ ] **Step 2: Run and confirm RED**

Run: `UV_CACHE_DIR=/tmp/glodex-uv-cache uv run pytest tests/composition/test_agent_api.py -q`

Expected: composition module is missing and CLI still emits `AGENT_API_COMPOSITION_UNAVAILABLE`.

- [ ] **Step 3: Implement the composition root**

Create immutable startup settings, preflight all dependencies, derive `asset_version` and `config_fingerprint` from exact manifest hashes, create `AgentRuntimeConfig` with all 128 Card IDs and four platforms, construct `ReActAgentService`, stores/projector/cache, and call `create_durable_agent_app()`. Replace `_run_agent_api()` with this builder and bind uvicorn only to `127.0.0.1:8766`.

- [ ] **Step 4: Run composition, CLI, durable, and architecture tests**

Run: `UV_CACHE_DIR=/tmp/glodex-uv-cache uv run pytest tests/composition tests/m2b tests/m2c tests/architecture -q`

Expected: all tests pass; the old fail-closed test is replaced by successful fake composition and failed-preflight tests.

- [ ] **Step 5: Commit**

```bash
git add src/glodex/composition src/glodex/cli.py tests/composition tests/m2b/contract/test_m2b_contracts.py
git commit -m "feat: compose the durable shopping agent api"
```

### Task 11: Documentation and Offline Verification

**Files:**
- Modify: `README.md`
- Modify: `opensearch/current-product/README.md`
- Create: `scripts/verify_current_product_commerce.py`
- Test: `tests/architecture/test_current_product_commerce.py`

- [ ] **Step 1: Add failing documentation/verification contract**

Assert the README contains the exact Card artifact, v3 physical-index rebuild command, gateway validation, `agent-api serve`, and rollback command; assert it no longer states that composition is unavailable.

- [ ] **Step 2: Run and confirm RED**

Run: `UV_CACHE_DIR=/tmp/glodex-uv-cache uv run pytest tests/architecture/test_current_product_commerce.py -q`

Expected: missing runbook assertions fail.

- [ ] **Step 3: Add verifier and runbook**

The verifier must run Card verification, tiny classification/generation fixtures, mapping contract tests, gateway tests, adapter evidence-closure tests, composition tests, and existing architecture tests without network. Document exact server preflight/build/verify/alias/rollback steps and state that existing Item vectors are reused.

- [ ] **Step 4: Run the offline gate and full relevant suite**

Run: `UV_CACHE_DIR=/tmp/glodex-uv-cache uv run python scripts/verify_current_product_commerce.py`

Run: `UV_CACHE_DIR=/tmp/glodex-uv-cache uv run pytest tests/interview_catalog tests/retrieval tests/composition tests/m1d/unit tests/m2b tests/m2c tests/architecture -q`

Expected: both commands exit 0.

- [ ] **Step 5: Commit**

```bash
git add README.md opensearch/current-product/README.md scripts/verify_current_product_commerce.py tests/architecture/test_current_product_commerce.py
git commit -m "docs: add current product commerce runbook"
```

### Task 12: Server Rebuild, Audit, Alias Switch, and Live Acceptance

**Files on server:**
- `/data3/sybai/glodex/opensearch/current-product/`
- `/data3/sybai/glodex/product-search/current-product-catalog.sqlite3`
- `/data3/sybai/glodex/current-model/item-vectors/`
- `/data3/sybai/glodex/current-model/category-card-vectors/`
- `/data3/sybai/glodex/product-search/current-commerce-audit.json`

- [ ] **Step 1: Run read-only preflight**

```bash
ssh glodex-a100 'cd /data3/sybai/glodex && /home/sybai/miniforge3/envs/glodex/bin/python -m pytest opensearch/current-product/test_current_product_hybrid_gateway.py -q'
ssh glodex-a100 'cd /data3/sybai/glodex/opensearch/current-product && ./build.sh validate-assets'
```

Expected: tests pass; preflight reports 926,710 catalog rows and a `(926710, 1024)` FP16 vector matrix.

- [ ] **Step 2: Encode and bind the 128 Card prototype vectors**

Run `scripts/build_category_card_vectors.py` with the same model artifact used by the existing Item vectors. Require `(128, 1024)` normalized vectors, exact ordered Card IDs, model-identity equality, Card-manifest binding, and a reload/hash verification. Update the server binding with the resulting manifest path and SHA-256, then rerun `validate-assets`.

- [ ] **Step 3: Build the new physical index without moving the alias**

Run the v3 builder's explicit `build-candidate` action and write `/data3/sybai/glodex/product-search/current-commerce-audit.json`. The action must refuse an existing physical index and must not update the alias.

Expected: 926,710 indexed documents, zero unclassified documents, exact Card/vector manifest hashes, and passing price/shipping canaries.

- [ ] **Step 4: Inspect the generated audit**

Verify laptop min is at least CNY 1,500, cup max is at most CNY 800, shipping ratios pass, all 128 Cards have counts or are explicitly listed as zero-coverage failures, and stock counts sum to 926,710. A zero-coverage leaf fails this first release because the 128-Card set must describe the actual corpus.

- [ ] **Step 5: Verify gateway against the candidate physical index**

Start the gateway on a temporary loopback port targeting the candidate physical index. Run laptop, cup, phone, furniture, accessory, out-of-stock, four-platform, and malformed-response probes. Stop only the temporary process.

- [ ] **Step 6: Atomically publish and verify**

Run the builder's `publish` action, which rechecks the candidate index then atomically moves `glodex-current-product-hybrid` from the old physical index to `glodex-current-product-hybrid-commerce-v3-20260809`. Run `product-index verify --live` and confirm the alias has one target.

- [ ] **Step 7: Run one durable Agent acceptance**

Start the verified model service/current-product gateway, PostgreSQL, Redis, `agent-api serve --live`, and `web-console serve --live`. Submit one laptop request with a budget and one cup request. Require a trusted terminal result, exact price/shipping facts, inventory filtering, evidence closure, and durable event replay.

- [ ] **Step 8: Record rollback instructions**

Rollback moves only the alias back to the recorded old physical index after verifying its mapping and count. It does not delete either index, catalog, Item vectors, model files, PostgreSQL data, or Redis data. Delete the old physical index only in a separately approved cleanup after acceptance evidence is retained.

---

## Appendix A: Fixed 128 Leaf Card IDs

The first taxonomy release uses this exact set; the parenthesized counts sum to 128.

- **Electronics (18):** `electronics.laptop`, `electronics.desktop`, `electronics.tablet`, `electronics.phone`, `electronics.monitor`, `electronics.television`, `electronics.camera`, `electronics.headphone`, `electronics.speaker`, `electronics.keyboard`, `electronics.mouse`, `electronics.printer`, `electronics.storage`, `electronics.networking`, `electronics.wearable`, `electronics.gaming-console`, `electronics.laptop-accessory`, `electronics.laptop-part`.
- **Home, kitchen, appliances (18):** `home.drinkware.cup`, `home.drinkware.bottle`, `kitchen.cookware`, `kitchen.bakeware`, `kitchen.tableware`, `kitchen.utensil`, `kitchen.food-storage`, `kitchen.small-appliance`, `appliances.major-appliance`, `home.bedding`, `home.bath`, `home.decor`, `home.lighting`, `home.cleaning`, `home.storage`, `home.rug`, `home.window-treatment`, `home.accessory`.
- **Furniture (10):** `furniture.chair`, `furniture.sofa`, `furniture.bed`, `furniture.table`, `furniture.desk`, `furniture.cabinet`, `furniture.shelf`, `furniture.outdoor`, `furniture.mattress`, `furniture.accessory`.
- **Clothing (12):** `clothing.mens-top`, `clothing.mens-bottom`, `clothing.womens-top`, `clothing.womens-bottom`, `clothing.dress`, `clothing.outerwear`, `clothing.underwear`, `clothing.sleepwear`, `clothing.activewear`, `clothing.swimwear`, `clothing.uniform`, `clothing.accessory`.
- **Shoes (6):** `shoes.athletic`, `shoes.casual`, `shoes.formal`, `shoes.boot`, `shoes.sandal`, `shoes.accessory`.
- **Beauty and health (10):** `beauty.skincare`, `beauty.makeup`, `beauty.haircare`, `beauty.fragrance`, `beauty.personal-care`, `health.vitamin`, `health.medical-supply`, `health.wellness-device`, `health.oral-care`, `health.vision-care`.
- **Sports and outdoor (10):** `sports.fitness`, `sports.team-sport`, `sports.cycling`, `sports.racket-sport`, `sports.water-sport`, `outdoor.camping`, `outdoor.hiking`, `outdoor.fishing`, `outdoor.hunting`, `outdoor.accessory`.
- **Automotive, tools, industrial (10):** `automotive.part`, `automotive.accessory`, `automotive.tire`, `automotive.fluid`, `tools.hand-tool`, `tools.power-tool`, `tools.accessory`, `industrial.safety-supply`, `industrial.material-handling`, `industrial.component`.
- **Books, media, office (8):** `books.book`, `media.music-recording`, `media.movie-video`, `media.video-game`, `office.stationery`, `office.electronics`, `office.art-supply`, `office.school-supply`.
- **Toys, baby, pets (10):** `toys.figure-doll`, `toys.building-set`, `toys.game-puzzle`, `toys.outdoor-toy`, `baby.diaper`, `baby.feeding`, `baby.gear`, `pets.food`, `pets.litter-supply`, `pets.accessory`.
- **Grocery (6):** `grocery.snack`, `grocery.beverage`, `grocery.pantry`, `grocery.fresh`, `grocery.frozen`, `grocery.household-consumable`.
- **Jewelry, luggage, instruments, general (10):** `jewelry.fine`, `jewelry.fashion`, `jewelry.watch`, `luggage.suitcase`, `luggage.bag`, `luggage.travel-accessory`, `musical-instruments.string`, `musical-instruments.keyboard`, `musical-instruments.percussion`, `general.general-merchandise`.

Every parent policy must name one of its listed descendants as `fallback_leaf_id`. Tests compare the loaded leaf-ID set to this exact list, not only to the number 128.

---

## Completion Criteria

- The committed Card artifact has exactly 128 valid leaf Cards and no inheritance cycles or alias collisions.
- Every one of 926,710 indexed documents has a Card, complete commerce fields, stable evidence IDs, and the unchanged canonical `document_id`/Item vector.
- Per-Card audit bounds prevent category-incoherent prices and shipping.
- The current-product gateway supports bounded category/platform retrieval and rejects incomplete hits.
- `CurrentProductItemSource` produces evidence-closed Agent domain values without another data read or value generation.
- `agent-api serve --live` constructs and serves the native durable ReAct path.
- Offline suites pass, the server alias has exactly one verified v3 target, and live laptop/cup runs reach trusted terminal states.
