# ruff: noqa: RUF001
from __future__ import annotations

import importlib.util
import io
import json
import sys
import urllib.request
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).with_name("category_knowledge_gateway.py")
SPEC = importlib.util.spec_from_file_location("category_knowledge_gateway", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
gateway = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = gateway
SPEC.loader.exec_module(gateway)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CATALOG = PROJECT_ROOT / "data/digital-interview-v1/products.json"
TAXONOMY = PROJECT_ROOT / "data/digital-interview-v1/category-taxonomy.json"
BUILDER_PATH = PROJECT_ROOT / "scripts/build_category_knowledge.py"
BUILDER_SPEC = importlib.util.spec_from_file_location("category_card_builder", BUILDER_PATH)
assert BUILDER_SPEC is not None and BUILDER_SPEC.loader is not None
builder = importlib.util.module_from_spec(BUILDER_SPEC)
sys.modules[BUILDER_SPEC.name] = builder
BUILDER_SPEC.loader.exec_module(builder)


def _write_artifacts(tmp_path: Path) -> tuple[Path, Path]:
    cards, manifest = builder.build_artifacts(CATALOG, TAXONOMY)
    cards_path = tmp_path / "category_cards.jsonl"
    manifest_path = tmp_path / "category_cards.manifest.json"
    cards_path.write_text(
        "".join(json.dumps(card, ensure_ascii=False) + "\n" for card in cards),
        encoding="utf-8",
    )
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return cards_path, manifest_path


def _runtime() -> object:
    return gateway.Runtime(
        alias="category-cards",
        physical_index="category-cards-v1-test",
        pipeline="category-cards-hybrid-v1",
        index_version="b" * 64,
        document_count=6,
        opensearch="http://127.0.0.1:9200",
        retrieval_model="http://127.0.0.1:18000",
        model_manifest_digest="a" * 64,
        embedding_model="embedding-test",
        reranker_model="reranker-test",
        embedding_dimension=1024,
        query_template_version=gateway.QUERY_TEMPLATE_VERSION,
        vector_payload_sha256="c" * 64,
        supported_scope_terms=("笔记本", "轻薄本", "游戏本", "办公"),
        unsupported_terms=("键盘", "相机", "汽车保险"),
    )


def _metadata() -> str:
    return json.dumps(
        {
            "kind": "category_metadata",
            "schema": gateway.CARD_SCHEMA,
            "category_id": "electronics.laptop",
            "recall_terms": ["笔记本电脑", "轻薄本", "游戏本", "laptop"],
            "semantic_profile": "便携式个人电脑，运行桌面操作系统和开发工具",
            "source_count": 60,
            "data_mode": gateway.DATA_MODE,
            "generator_version": gateway.GENERATOR_VERSION,
            "evidence_refs": ["https://example.com/spec"],
        },
        ensure_ascii=False,
    )


def _row(card_type: str) -> dict[str, object]:
    payloads = {
        "bestseller": {
            "kind": "bestseller_payload",
            "components": ["轻薄本", "游戏本", "商务本"],
            "bestsellers": [
                {
                    "name": "合成轻薄本 A",
                    "typical_price_cny": "5500",
                    "why_popular": "合成演示榜：轻薄本代表；不代表真实销量",
                }
            ],
        },
        "attribute": {
            "kind": "attribute_payload",
            "attributes": [{"name": "ram", "distribution": {"16 GB": "0.7", "32 GB": "0.3"}}],
        },
        "price_range": {
            "kind": "price_range_payload",
            "price_tiers": [
                {"tier": "budget", "range_cny": ["1500", "5000"], "notes": "合成面试"},
                {"tier": "mid", "range_cny": ["5000", "10000"], "notes": "合成面试"},
                {"tier": "premium", "range_cny": ["10000", "35000"], "notes": "合成面试"},
            ],
        },
    }
    return {
        "card_id": "electronics.laptop:" + card_type,
        "category_key": "electronics.laptop",
        "category": "笔记本电脑",
        "semantic_profile": "便携式个人电脑，运行桌面操作系统和开发工具",
        "recall_terms": ["笔记本电脑", "轻薄本", "游戏本", "laptop"],
        "card_type": card_type,
        "summary": "笔记本电脑 轻薄本 游戏本 " + card_type,
        "raw_evidence": [_metadata(), json.dumps(payloads[card_type], ensure_ascii=False)],
        "confidence": "0.9",
    }


def _rows() -> list[dict[str, object]]:
    return [_row("bestseller"), _row("attribute"), _row("price_range")]


def test_builder_emits_only_strict_small_cards(tmp_path: Path) -> None:
    cards_path, manifest_path = _write_artifacts(tmp_path)
    documents = gateway.build_documents(cards_path)
    manifest = gateway._load_manifest(manifest_path)
    cards = [json.loads(line) for line in cards_path.read_text(encoding="utf-8").splitlines()]

    assert len(cards) == 201
    assert manifest["card_count"] == 201
    assert manifest["data_mode"] == "SYNTHETIC_INTERVIEW"
    assert len(documents) == len(cards)
    assert {card["card_type"] for card in cards} == {
        "bestseller",
        "attribute",
        "price_range",
    }
    expected_fields = {
        "card_id",
        "category",
        "card_type",
        "summary",
        "raw_evidence",
        "last_updated",
        "confidence",
    }
    assert all(set(card) == expected_fields for card in cards)
    assert len({card["card_id"] for card in cards}) == len(cards)
    laptop = next(card for card in cards if card["card_id"] == "electronics.laptop:bestseller")
    _metadata_value, payload = gateway._card_evidence(laptop)
    assert payload["components"] == ["轻薄本", "游戏本"]
    assert all("合成演示榜" in item["why_popular"] for item in payload["bestsellers"])
    assert all("不代表真实销量" in item["why_popular"] for item in payload["bestsellers"])
    assert not any(
        value.startswith(("storage", "ram", "processor")) for value in payload["components"]
    )


def test_old_aggregate_seed_is_rejected_without_compatibility(tmp_path: Path) -> None:
    old = tmp_path / "old.json"
    old.write_text(json.dumps({"schema_version": "glodex.category-knowledge-seed.v6"}))
    with pytest.raises(gateway.CategoryKnowledgeError, match="schema"):
        gateway.build_documents(old)


@pytest.mark.parametrize(("depth", "expected"), (("quick", 8), ("deep", 15)))
def test_search_uses_the_depth_specific_top_k(
    monkeypatch: pytest.MonkeyPatch, depth: str, expected: int
) -> None:
    runtime = _runtime()
    observed: list[dict[str, object]] = []
    monkeypatch.setattr(
        gateway,
        "_embed",
        lambda *_args, **_kwargs: [[0.0] * gateway.DIMENSION],
    )

    def request(_base: str, _method: str, _path: str, body: object) -> dict[str, object]:
        assert type(body) is dict
        observed.append(body)
        return {"hits": {"hits": []}}

    monkeypatch.setattr(gateway, "_json_request", request)
    assert runtime._search("轻薄本", depth) == []
    assert observed[0]["size"] == expected
    assert observed[0]["query"]["hybrid"]["queries"][0]["knn"]["content_vector"]["k"] == expected


@pytest.mark.parametrize(("depth", "attribute_count"), (("quick", 0), ("deep", 1)))
def test_runtime_groups_recalled_cards_and_applies_depth(
    monkeypatch: pytest.MonkeyPatch, depth: str, attribute_count: int
) -> None:
    runtime = _runtime()
    monkeypatch.setattr(gateway.Runtime, "_search", lambda _self, _query, _depth: _rows())

    insight = runtime.retrieve("轻薄本", depth)

    assert insight["status"] == "FOUND"
    assert insight["components"] == ["轻薄本", "游戏本", "商务本"]
    assert insight["bestsellers"]
    assert len(insight["price_tiers"]) == 3
    assert len(insight["attributes"]) == attribute_count
    assert "card_id" not in insight
    assert "raw_evidence" not in insight


def test_runtime_accepts_partial_recalled_card_types(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _runtime()
    monkeypatch.setattr(
        gateway.Runtime,
        "_search",
        lambda _self, _query, _depth: [_row("bestseller"), _row("price_range")],
    )
    insight = runtime.retrieve("轻薄本", "deep")
    assert insight["status"] == "FOUND"
    assert insight["attributes"] == []
    assert insight["bestsellers"]


def test_runtime_rejects_out_of_scope_without_reranking(monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _runtime()
    monkeypatch.setattr(gateway.Runtime, "_search", lambda _self, _query, _depth: _rows())
    monkeypatch.setattr(
        gateway.Runtime,
        "_rerank",
        lambda *_args: pytest.fail("out-of-scope query must not rerank"),
    )
    ranking = runtime.rank_categories("汽车保险", "quick")
    assert ranking.selected_card_id is None
    assert ranking.route == "OUT_OF_SCOPE"


def test_runtime_returns_no_insight_when_rerank_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    monkeypatch.setattr(gateway.Runtime, "_search", lambda _self, _query, _depth: _rows())

    def unavailable(*_args: object) -> object:
        raise gateway.CategoryKnowledgeError("reranker unavailable")

    monkeypatch.setattr(gateway.Runtime, "_rerank", unavailable)
    assert runtime.retrieve("便携办公设备", "quick")["status"] == "NO_INSIGHT"


def test_rerank_document_excludes_payload_noise() -> None:
    row = _row("attribute")
    document = gateway._rerank_document([row])
    assert "便携式个人电脑" in document
    assert "16 GB" not in document


def test_semantic_selection_requires_both_score_and_margin() -> None:
    confident = (
        gateway.RankedCategory("electronics.laptop", 0.02),
        gateway.RankedCategory("electronics.phone", 0.01),
    )
    ambiguous = (
        gateway.RankedCategory("electronics.laptop", 0.03),
        gateway.RankedCategory("electronics.phone", 0.029),
    )
    assert gateway._confident_rerank_card_id(confident) == "electronics.laptop"
    assert gateway._confident_rerank_card_id(ambiguous) is None


def test_only_a_complete_alias_is_an_exact_fast_path() -> None:
    rows = _rows()
    assert gateway._exact_alias_card_id("轻薄本", rows) == "electronics.laptop"
    assert gateway._exact_alias_card_id("想找轻薄本", rows) is None


def test_depth_contracts_are_explicit() -> None:
    assert gateway._recall_depth("quick") == 8
    assert gateway._recall_depth("deep") == 15
    assert gateway._component_limit("quick") == 3
    assert gateway._component_limit("deep") == 8


def test_alias_lookup_returns_only_exact_previous_indexes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[str] = []

    class Response:
        def __enter__(self) -> io.BytesIO:
            return io.BytesIO(b'{"category-old":{"aliases":{"category":{}}}}')

        def __exit__(self, *_args: object) -> None:
            return None

    def open_request(request: urllib.request.Request, *, timeout: float) -> Response:
        observed.append(request.full_url)
        assert timeout == 30.0
        return Response()

    monkeypatch.setattr(urllib.request, "urlopen", open_request)
    assert gateway._alias_indexes("http://127.0.0.1:9200", "category") == ("category-old",)
    assert observed == ["http://127.0.0.1:9200/_alias/category"]
