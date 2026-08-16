# CategoryInsight Category Card service

This service follows the interview architecture's card-first RAG design. The
offline producer writes one strict JSON object per line. Every supported
category has three independent card types: `bestseller`, `attribute`, and
`price_range`.

The seven-field card contract is:

```text
card_id, category, card_type, summary, raw_evidence, last_updated, confidence
```

The dataset is explicitly `SYNTHETIC_INTERVIEW`. Bestseller cards are
deterministic synthetic exemplars built from admitted specification and price
samples. Every `why_popular` value says `合成演示榜` and `不代表真实销量`.
They must never be presented as real sales rankings or used as an ItemSearch
allowlist.

Build the cards and their separate manifest:

```bash
uv run python scripts/build_category_knowledge.py \
  --catalog data/digital-interview-v1/products.json \
  --taxonomy data/digital-interview-v1/category-taxonomy.json \
  --cards data/digital-interview-v1/category-cards/category_cards.jsonl \
  --manifest data/digital-interview-v1/category-cards/category_cards.manifest.json
```

Build and serve the independent OpenSearch index:

```bash
uv run python opensearch/category-knowledge/category_knowledge_gateway.py build \
  --cards data/digital-interview-v1/category-cards/category_cards.jsonl \
  --manifest data/digital-interview-v1/category-cards/category_cards.manifest.json \
  --binding opensearch/category-knowledge/category_knowledge_binding.json

uv run python opensearch/category-knowledge/category_knowledge_gateway.py serve \
  --binding opensearch/category-knowledge/category_knowledge_binding.json
```

Runtime behavior:

- `quick`: Hybrid Top-K 8, components/bestsellers/price tiers, no attributes.
- `deep`: Hybrid Top-K 15, plus attribute distributions.
- Hybrid results are grouped by category and then by `card_type`; there is no
  sibling-card second query and no legacy aggregate-seed reader.
- Missing card types produce a partial `FOUND`; no accepted cards produce
  `NO_INSIGHT`.
- The product index remains independent and continues to contain all concrete
  product models.

Run tests and the fixed recall gate after every retrieval change:

```bash
uv run pytest -q opensearch/category-knowledge/test_category_knowledge_gateway.py
uv run python scripts/evaluate_category_insight_recall.py
```
