from __future__ import annotations

import asyncio
import hashlib
import json
import math
import shutil
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

from glodex.adapters.agent_indexes import (
    EMBEDDING_DIMENSIONS,
    M1D_DEMO_VERSION,
    M1D_INDEX_VERSION,
    load_agent_indexes,
    tokenize_index_text,
)
from glodex.application.agent.contracts import (
    CategoryInsightInput,
    EmbeddingBatch,
    EmbeddingResult,
    InsightDepth,
    InsightStatus,
    Platform,
    ToolFailureCode,
)
from glodex.application.agent.ports import ToolPortError
from scripts import generate_m1d_demo_assets as asset_generator

pytestmark = [
    pytest.mark.contract,
    pytest.mark.spec(
        "GLO-M1D-P0-004",
        "GLO-M1D-NFR-003",
        "GLO-M1D-NFR-006",
    ),
]

_ROOT = Path(__file__).parents[3]
_SNAPSHOT_ROOT = _ROOT / "data" / "snapshots"
_AGENT_ROOT = _ROOT / "data" / "agent"


def _load(
    snapshot_root: Path = _SNAPSHOT_ROOT,
    agent_root: Path = _AGENT_ROOT,
) -> object:
    return asyncio.run(
        load_agent_indexes(
            snapshot_root=snapshot_root,
            agent_root=agent_root,
        )
    )


def _vector(path: Path, identity_key: str, identity: str) -> tuple[float, ...]:
    for line in path.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if record[identity_key] == identity:
            return tuple(record["vector"])
    raise AssertionError(f"missing vector fixture: {identity}")


def _basis(index: int) -> tuple[float, ...]:
    values = [0.0] * EMBEDDING_DIMENSIONS
    values[index] = 1.0
    return tuple(values)


class _RecordingEmbeddingPort:
    def __init__(self, *, fail_on_call: int | None = None) -> None:
        self.batches: list[tuple[str, ...]] = []
        self.vector_count = 0
        self.fail_on_call = fail_on_call

    async def embed(self, request: EmbeddingBatch) -> EmbeddingResult:
        self.batches.append(request.texts)
        if len(self.batches) == self.fail_on_call:
            raise ToolPortError(ToolFailureCode.PROVIDER_UNAVAILABLE)
        vectors = tuple(_basis(self.vector_count + offset) for offset in range(len(request.texts)))
        self.vector_count += len(vectors)
        return EmbeddingResult(vectors=vectors)


def _copy_assets(tmp_path: Path) -> tuple[Path, Path]:
    snapshot_root = tmp_path / "snapshots"
    agent_root = tmp_path / "agent"
    shutil.copytree(
        _SNAPSHOT_ROOT / M1D_DEMO_VERSION,
        snapshot_root / M1D_DEMO_VERSION,
    )
    shutil.copytree(
        _AGENT_ROOT / M1D_DEMO_VERSION,
        agent_root / M1D_DEMO_VERSION,
    )
    return snapshot_root, agent_root


def _directory_bytes(directory: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()}


def _expected_embedding_texts(snapshot_dir: Path, agent_dir: Path) -> tuple[str, ...]:
    cards = [
        json.loads(line)
        for line in (agent_dir / "category_cards.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    card_texts = tuple(
        "\n".join(
            (
                card["category"],
                card["summary"],
                card["source_domain"],
                *card["components"],
                *card["bestsellers"],
                *card["attributes"],
                *card["price_tiers"],
            )
        )
        for card in cards
    )
    products = [
        json.loads(line)
        for line in (snapshot_dir / "products.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    item_texts: list[str] = []
    for product in products:
        attributes = {attribute["name"]: attribute["value"] for attribute in product["attributes"]}
        item_texts.append(
            "\n".join(
                (
                    product["title"],
                    product["category"],
                    product["provider_id"].removesuffix("-demo"),
                    attributes["verified_signal"],
                    attributes["pack_note"],
                )
            )
        )
    return (*card_texts, *item_texts)


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()


def _rewrite_agent_file(
    agent_root: Path,
    *,
    role: str,
    records: list[dict[str, object]],
) -> None:
    directory = agent_root / M1D_DEMO_VERSION
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    filename = cast("dict[str, object]", manifest["agent_files"])[role]["path"]
    path = directory / str(filename)
    payload = b"".join(_canonical_json(record) for record in records)
    path.write_bytes(payload)
    file_spec = cast("dict[str, dict[str, object]]", manifest["agent_files"])[role]
    file_spec["record_count"] = len(records)
    file_spec["sha256"] = hashlib.sha256(payload).hexdigest()
    manifest_path.write_bytes(_canonical_json(manifest))


def _rewrite_shipping_rules(
    agent_root: Path,
    document: dict[str, object],
) -> None:
    directory = agent_root / M1D_DEMO_VERSION
    path = directory / "shipping_rules.json"
    payload = _canonical_json(document)
    path.write_bytes(payload)
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    file_spec = cast(
        "dict[str, dict[str, object]]",
        manifest["agent_files"],
    )["shipping_rules"]
    rules = document["rules"]
    if not isinstance(rules, list):
        raise TypeError("shipping rules fixture must contain a list")
    file_spec["record_count"] = len(rules)
    file_spec["sha256"] = hashlib.sha256(payload).hexdigest()
    manifest_path.write_bytes(_canonical_json(manifest))


def test_committed_assets_are_reproducible_hash_closed_and_loadable() -> None:
    asset_generator.verify_assets(
        _SNAPSHOT_ROOT / M1D_DEMO_VERSION,
        _AGENT_ROOT / M1D_DEMO_VERSION,
    )

    indexes = _load()

    assert indexes.snapshot_version == M1D_DEMO_VERSION
    assert indexes.index_version == M1D_INDEX_VERSION
    assert indexes.embedding_model == "text-embedding-v4"
    assert indexes.embedding_provenance in {
        "DETERMINISTIC_DEMO_FIXTURE",
        "DASHSCOPE_OPERATOR_BUILD",
    }
    assert len(indexes.batch.products) == 8
    assert len(indexes.batch.offers) == 8
    assert indexes.batch.fatal_issues == ()
    assert indexes.batch.quarantine_issues == ()
    assert all(
        rule.effective_date == date(2026, 7, 1)
        and rule.duty_threshold == 5_000
        and Decimal("0") <= rule.duty_rate <= Decimal("1")
        for rule in indexes.shipping_rules
    )
    assert tuple(
        (inventory.platform, len(inventory.record_keys))
        for inventory in indexes.platform_inventories
    ) == (
        (Platform.AMAZON, 2),
        (Platform.SHOPEE, 2),
        (Platform.ALIEXPRESS, 2),
        (Platform.EBAY, 2),
    )

    for filename in ("category_embeddings.jsonl", "item_embeddings.jsonl"):
        for line in (
            (_AGENT_ROOT / M1D_DEMO_VERSION / filename).read_text(encoding="utf-8").splitlines()
        ):
            vector = json.loads(line)["vector"]
            assert len(vector) == 1_024
            assert all(type(value) is float and math.isfinite(value) for value in vector)
            assert math.isclose(
                math.sqrt(sum(value * value for value in vector)),
                1.0,
                rel_tol=0.0,
                abs_tol=1e-6,
            )


def test_shipping_rule_rate_above_one_is_rejected_after_hash_update(
    tmp_path: Path,
) -> None:
    snapshot_root, agent_root = _copy_assets(tmp_path)
    path = agent_root / M1D_DEMO_VERSION / "shipping_rules.json"
    document = json.loads(path.read_bytes())
    document["rules"][0]["duty_rate"] = "1.01"
    _rewrite_shipping_rules(agent_root, document)

    with pytest.raises(ToolPortError) as raised:
        _load(snapshot_root, agent_root)

    assert raised.value.code is ToolFailureCode.INDEX_INVALID


def test_default_generate_and_check_are_offline_and_deterministic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot_dir = tmp_path / "snapshots" / M1D_DEMO_VERSION
    agent_dir = tmp_path / "agent" / M1D_DEMO_VERSION

    def unexpected_live_builder() -> object:
        pytest.fail("offline mode must not construct a live embedding port")

    monkeypatch.setattr(
        asset_generator,
        "build_dashscope_embedding",
        unexpected_live_builder,
        raising=False,
    )
    common = [
        "--snapshot-output",
        str(snapshot_dir),
        "--agent-output",
        str(agent_dir),
    ]

    assert asset_generator.main(common) == 0
    first_snapshot = _directory_bytes(snapshot_dir)
    first_agent = _directory_bytes(agent_dir)
    assert asset_generator.main([*common, "--check"]) == 0
    assert asset_generator.main(common) == 0
    assert _directory_bytes(snapshot_dir) == first_snapshot
    assert _directory_bytes(agent_dir) == first_agent


def test_operator_dashscope_build_is_batched_ordered_hash_closed_and_validated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    snapshot_dir = tmp_path / "snapshots" / M1D_DEMO_VERSION
    agent_dir = tmp_path / "agent" / M1D_DEMO_VERSION
    asset_generator.generate_assets(snapshot_dir, agent_dir)
    expected_texts = _expected_embedding_texts(snapshot_dir, agent_dir)
    snapshot_before = _directory_bytes(snapshot_dir)
    fake = _RecordingEmbeddingPort()
    builder_calls = 0
    loader_calls = 0
    real_loader = load_agent_indexes

    def fake_builder() -> _RecordingEmbeddingPort:
        nonlocal builder_calls
        builder_calls += 1
        return fake

    async def validating_loader(**kwargs: object) -> object:
        nonlocal loader_calls
        loader_calls += 1
        return await real_loader(**kwargs)

    monkeypatch.setenv("DASHSCOPE_API_KEY", "must-not-be-written")
    monkeypatch.setattr(
        asset_generator,
        "build_dashscope_embedding",
        fake_builder,
        raising=False,
    )
    monkeypatch.setattr(
        asset_generator,
        "load_agent_indexes",
        validating_loader,
        raising=False,
    )

    result = asset_generator.main(
        [
            "--build-dashscope-embeddings",
            "--snapshot-output",
            str(snapshot_dir),
            "--agent-output",
            str(agent_dir),
        ]
    )

    assert result == 0
    assert builder_calls == 1
    assert loader_calls == 1
    assert all(1 <= len(batch) <= 2 for batch in fake.batches)
    assert tuple(text for batch in fake.batches for text in batch) == expected_texts
    assert fake.vector_count == len(expected_texts) == 16
    assert capsys.readouterr() == ("", "")

    manifest = json.loads((agent_dir / "manifest.json").read_bytes())
    assert manifest["embedding"] == {
        "dimensions": 1_024,
        "model": "text-embedding-v4",
        "provenance": "DASHSCOPE_OPERATOR_BUILD",
    }
    for role, spec in manifest["agent_files"].items():
        payload = (agent_dir / spec["path"]).read_bytes()
        assert hashlib.sha256(payload).hexdigest() == spec["sha256"]
        expected_count = (
            len(json.loads(payload)["rules"])
            if role == "shipping_rules"
            else len(payload.splitlines())
        )
        assert spec["record_count"] == expected_count
        assert b"must-not-be-written" not in payload
    assert b"must-not-be-written" not in (agent_dir / "manifest.json").read_bytes()

    category_vectors = [
        json.loads(line)["vector"]
        for line in (agent_dir / "category_embeddings.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    item_vectors = [
        json.loads(line)["vector"]
        for line in (agent_dir / "item_embeddings.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    for index, vector in enumerate((*category_vectors, *item_vectors)):
        assert len(vector) == 1_024
        assert vector[index] == 1.0

    indexes = _load(snapshot_dir.parent, agent_dir.parent)
    assert indexes.embedding_provenance == "DASHSCOPE_OPERATOR_BUILD"
    assert (
        asset_generator.main(
            [
                "--check",
                "--snapshot-output",
                str(snapshot_dir),
                "--agent-output",
                str(agent_dir),
            ]
        )
        == 0
    )
    assert builder_calls == 1
    assert loader_calls == 2
    assert _directory_bytes(snapshot_dir) == snapshot_before
    assert capsys.readouterr() == ("", "")


def test_operator_dashscope_failure_preserves_existing_agent_assets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot_dir = tmp_path / "snapshots" / M1D_DEMO_VERSION
    agent_dir = tmp_path / "agent" / M1D_DEMO_VERSION
    asset_generator.generate_assets(snapshot_dir, agent_dir)
    before = _directory_bytes(agent_dir)
    fake = _RecordingEmbeddingPort(fail_on_call=3)
    monkeypatch.setattr(
        asset_generator,
        "build_dashscope_embedding",
        lambda: fake,
        raising=False,
    )

    with pytest.raises(ToolPortError) as raised:
        asset_generator.main(
            [
                "--build-dashscope-embeddings",
                "--snapshot-output",
                str(snapshot_dir),
                "--agent-output",
                str(agent_dir),
            ]
        )

    assert raised.value.code is ToolFailureCode.PROVIDER_UNAVAILABLE
    assert len(fake.batches) == 3
    assert _directory_bytes(agent_dir) == before


def test_operator_validation_failure_preserves_existing_agent_assets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot_dir = tmp_path / "snapshots" / M1D_DEMO_VERSION
    agent_dir = tmp_path / "agent" / M1D_DEMO_VERSION
    asset_generator.generate_assets(snapshot_dir, agent_dir)
    before = _directory_bytes(agent_dir)
    fake = _RecordingEmbeddingPort()

    async def reject_staged_assets(**_kwargs: object) -> object:
        raise ToolPortError(ToolFailureCode.INDEX_INVALID)

    monkeypatch.setattr(
        asset_generator,
        "build_dashscope_embedding",
        lambda: fake,
        raising=False,
    )
    monkeypatch.setattr(
        asset_generator,
        "load_agent_indexes",
        reject_staged_assets,
        raising=False,
    )

    with pytest.raises(ToolPortError) as raised:
        asset_generator.main(
            [
                "--build-dashscope-embeddings",
                "--snapshot-output",
                str(snapshot_dir),
                "--agent-output",
                str(agent_dir),
            ]
        )

    assert raised.value.code is ToolFailureCode.INDEX_INVALID
    assert fake.vector_count == 16
    assert _directory_bytes(agent_dir) == before


def test_operator_build_and_check_flags_are_mutually_exclusive(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        asset_generator.main(
            [
                "--check",
                "--build-dashscope-embeddings",
                "--snapshot-output",
                str(tmp_path / "snapshot"),
                "--agent-output",
                str(tmp_path / "agent"),
            ]
        )

    assert raised.value.code == 2
    stderr = capsys.readouterr().err
    assert "--check" in stderr
    assert "--build-dashscope-embeddings" in stderr
    assert "not allowed with argument" in stderr


def test_hash_mismatch_fails_closed_without_partial_indexes(tmp_path: Path) -> None:
    snapshot_root, agent_root = _copy_assets(tmp_path)
    cards = agent_root / M1D_DEMO_VERSION / "category_cards.jsonl"
    cards.write_bytes(cards.read_bytes() + b" ")

    with pytest.raises(ToolPortError) as raised:
        _load(snapshot_root, agent_root)

    assert raised.value.code is ToolFailureCode.INDEX_INVALID
    assert str(raised.value) == ToolFailureCode.INDEX_INVALID.value


def test_vector_dimension_is_revalidated_after_matching_hash_update(
    tmp_path: Path,
) -> None:
    snapshot_root, agent_root = _copy_assets(tmp_path)
    path = agent_root / M1D_DEMO_VERSION / "category_embeddings.jsonl"
    records = [
        cast("dict[str, object]", json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    cast("list[float]", records[0]["vector"]).pop()
    _rewrite_agent_file(
        agent_root,
        role="category_embeddings",
        records=records,
    )

    with pytest.raises(ToolPortError) as raised:
        _load(snapshot_root, agent_root)

    assert raised.value.code is ToolFailureCode.INDEX_INVALID


def test_nfkc_ascii_lower_and_cjk_bigram_tokenization_is_fixed() -> None:
    assert tokenize_index_text("\uff30\uff28\uff2f\uff2e\uff25 手机轻薄") == (
        "phone",
        "手机",
        "机轻",
        "轻薄",
    )


def test_hybrid_rrf_is_stable_and_quick_deep_reducers_are_bounded() -> None:
    indexes = _load()
    core_vector = _vector(
        _AGENT_ROOT / M1D_DEMO_VERSION / "category_embeddings.jsonl",
        "card_id",
        "card-phone-core",
    )

    quick = asyncio.run(
        indexes.retrieve(
            CategoryInsightInput(
                category="phone",
                depth=InsightDepth.QUICK,
                query="旅行 手机 快充",
                query_vector=core_vector,
                index_version=M1D_INDEX_VERSION,
            )
        )
    )
    deep = asyncio.run(
        indexes.retrieve(
            CategoryInsightInput(
                category="laptop",
                depth=InsightDepth.DEEP,
                query="轻薄本 出差 续航 接口",
                query_vector=_basis(1),
                index_version=M1D_INDEX_VERSION,
            )
        )
    )

    assert quick.status is InsightStatus.FOUND
    assert quick.card_ids == ("card-phone-core", "card-phone-travel")
    assert len(quick.components) <= 3
    assert len(quick.bestsellers) <= 3
    assert len(quick.attributes) <= 5
    assert len(quick.price_tiers) <= 3
    assert quick.confidence is not None
    assert deep.status is InsightStatus.FOUND
    assert deep.card_ids == ("card-laptop-core", "card-laptop-travel")
    assert len(deep.components) > len(quick.components)
    assert len(deep.components) <= 8
    assert len(deep.attributes) <= 12


def test_empty_lexical_and_vector_recall_returns_typed_no_insight() -> None:
    indexes = _load()

    result = asyncio.run(
        indexes.retrieve(
            CategoryInsightInput(
                category="camera",
                depth=InsightDepth.QUICK,
                query="不存在的品类",
                query_vector=_basis(999),
                index_version=M1D_INDEX_VERSION,
            )
        )
    )

    assert result.status is InsightStatus.NO_INSIGHT
    assert result.card_ids == ()
    assert result.confidence is None


def test_item_exact_cosine_filters_platform_and_is_stably_sorted() -> None:
    indexes = _load()
    amazon_platform_vector = _basis(100)

    first = indexes.search_items(
        platform=Platform.AMAZON,
        query_vector=amazon_platform_vector,
        top_k=1,
    )
    full = indexes.search_items(
        platform=Platform.AMAZON,
        query_vector=amazon_platform_vector,
        top_k=2,
    )

    assert first == full[:1]
    assert {hit.record_key for hit in full} == {
        "amazon-laptop-travel",
        "amazon-phone-air",
    }
    assert full == tuple(sorted(full, key=lambda hit: (-hit.score, hit.record_key)))
    if indexes.embedding_provenance == "DETERMINISTIC_DEMO_FIXTURE":
        assert tuple(hit.record_key for hit in full) == (
            "amazon-laptop-travel",
            "amazon-phone-air",
        )
        assert full[0].score == pytest.approx(full[1].score)
    assert set(hit.record_key for hit in full).issubset(
        set(indexes.platform_inventory(Platform.AMAZON).record_keys)
    )
