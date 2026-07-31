"""GPU-free M2c model-index and profile identity closure evidence."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from glodex.adapters.agent_indexes import load_agent_indexes
from glodex.adapters.m2a_indexes import (
    CARD_ALIAS as M2A_CARD_ALIAS,
)
from glodex.adapters.m2a_indexes import (
    PRODUCT_ALIAS as M2A_PRODUCT_ALIAS,
)
from glodex.adapters.m2a_indexes import (
    PROFILE_ALIAS as M2A_PROFILE_ALIAS,
)
from glodex.adapters.m2c_indexes import (
    CARD_ALIAS,
    PRODUCT_ALIAS,
    PROFILE_ALIAS,
    card_mapping,
    manifest_for,
    product_mapping,
)
from glodex.adapters.m2c_profile_store import _parse_entries
from glodex.application.m2c_profile import M2cProfileError
from glodex.m2c_contract import M2C_EMBEDDING_MODEL, M2C_RERANKER_MODEL, M2cModelIdentity

pytestmark = [
    pytest.mark.unit,
    pytest.mark.spec(
        "GLO-M2C-P0-003",
        "GLO-M2C-P0-004",
        "GLO-M2C-NFR-001",
        "GLO-M2C-NFR-003",
        "GLO-M2C-NFR-004",
    ),
]

_ROOT = Path(__file__).parents[3]


def _identity() -> M2cModelIdentity:
    return M2cModelIdentity(
        manifest_digest="a" * 64,
        embedding_model=M2C_EMBEDDING_MODEL,
        reranker_model=M2C_RERANKER_MODEL,
        dimension=1024,
        max_embedding_texts=8,
        max_text_characters=2000,
        max_rerank_documents=40,
        max_query_characters=512,
        device_class="cuda",
        gpu_model_class="a100",
    )


def _vector() -> list[float]:
    return [1.0, *([0.0] * 1023)]


def test_m2c_mapping_binds_validated_assets_to_bge_and_never_reuses_m2a_aliases() -> None:
    indexes = asyncio.run(
        load_agent_indexes(
            snapshot_root=_ROOT / "data" / "snapshots",
            agent_root=_ROOT / "data" / "agent",
        )
    )
    manifest = manifest_for(indexes=indexes, identity=_identity())
    product = product_mapping(manifest)
    card = card_mapping(manifest)

    assert len(manifest.product_ids) == 8
    assert len(manifest.card_ids) == 8
    assert product["mappings"]["_meta"]["embedding_model"] == M2C_EMBEDDING_MODEL
    assert product["mappings"]["_meta"]["model_manifest_digest"] == "a" * 64
    assert card["mappings"]["properties"]["card_vector"]["dimension"] == 1024
    assert {PRODUCT_ALIAS, CARD_ALIAS, PROFILE_ALIAS}.isdisjoint(
        {M2A_PRODUCT_ALIAS, M2A_CARD_ALIAS, M2A_PROFILE_ALIAS}
    )


def test_profile_reader_rejects_a_cross_model_vector_instead_of_converting_it() -> None:
    response = {
        "hits": {
            "hits": [
                {
                    "_source": {
                        "entry_id": "pref-01",
                        "kind": "preference",
                        "model_manifest_digest": "b" * 64,
                        "profile_id": "m2c-demo",
                        "schema_version": "glodex.m2c-profile.v1",
                        "scope": "soft",
                        "user_vector": _vector(),
                        "value": "lightweight",
                    }
                }
            ]
        }
    }

    with pytest.raises(M2cProfileError, match="M2C_PROFILE_MODEL_MISMATCH"):
        _parse_entries(response, profile_id="m2c-demo", identity=_identity())
