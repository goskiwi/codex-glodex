"""Unit coverage for the static M1f showcase evidence assets."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from glodex.application.agent.contracts import BUSINESS_TOOL_SET, ToolName
from scripts.validate_m1f_showcase import (
    ASSETS_ROOT,
    REPLAY_FILENAME,
    SUMMARY_FILENAME,
    ShowcaseValidationError,
    validate_assets,
)

pytestmark = pytest.mark.unit


def _write_canonical(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def _copy_assets(tmp_path: Path) -> Path:
    assets_root = tmp_path / "assets"
    assets_root.mkdir()
    for name in (REPLAY_FILENAME, SUMMARY_FILENAME):
        (assets_root / name).write_bytes((ASSETS_ROOT / name).read_bytes())
    return assets_root


@pytest.mark.spec(
    "GLO-M1F-P0-002",
    "GLO-M1F-P0-003",
    "GLO-M1F-NFR-002",
    "GLO-M1F-NFR-004",
)
def test_committed_assets_are_canonical_safe_and_match_the_committed_benchmark() -> None:
    executed_tools = validate_assets()

    assert executed_tools == {
        ToolName.PLANNER.value,
        ToolName.DISPATCH_TOOL.value,
        ToolName.ITEM_SEARCH.value,
        ToolName.SHOPPING_SUMMARY.value,
    }
    assert tuple(tool.value for tool in BUSINESS_TOOL_SET) == (
        "planner",
        "chat_fallback",
        "web_search",
        "category_insight",
        "item_search",
        "item_picker",
        "price_compare",
        "shipping_calc",
        "shopping_summary",
    )


@pytest.mark.spec("GLO-M1F-P0-002", "M1F-AC-002", "GLO-M1F-NFR-002")
def test_replay_rejects_unknown_fields_and_orphaned_children(tmp_path: Path) -> None:
    assets_root = _copy_assets(tmp_path)
    replay_path = assets_root / REPLAY_FILENAME
    replay = json.loads(replay_path.read_text(encoding="utf-8"))
    replay["events"][0]["query"] = "must-not-be-disclosed"
    _write_canonical(replay_path, replay)

    with pytest.raises(ShowcaseValidationError, match="public-contract valid"):
        validate_assets(assets_root=assets_root)

    replay["events"][0].pop("query")
    replay["events"][9]["childId"] = "unknown-child"
    _write_canonical(replay_path, replay)
    with pytest.raises(ShowcaseValidationError, match="orphaned"):
        validate_assets(assets_root=assets_root)


@pytest.mark.spec("GLO-M1F-P0-003", "M1F-AC-003", "GLO-M1F-NFR-002")
def test_summary_rejects_raw_fields_and_metric_drift(tmp_path: Path) -> None:
    assets_root = _copy_assets(tmp_path)
    summary_path = assets_root / SUMMARY_FILENAME
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["query"] = "must-not-be-disclosed"
    _write_canonical(summary_path, summary)

    with pytest.raises(ShowcaseValidationError, match="fields are not approved"):
        validate_assets(assets_root=assets_root)

    summary.pop("query")
    summary["metrics"]["exact_at_10"]["value"] = 0.1
    _write_canonical(summary_path, summary)
    with pytest.raises(ShowcaseValidationError, match="does not match the artifact"):
        validate_assets(assets_root=assets_root)
