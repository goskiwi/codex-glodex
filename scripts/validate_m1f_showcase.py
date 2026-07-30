"""Validate the versioned, offline M1f showcase evidence assets."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from pydantic import TypeAdapter, ValidationError

from glodex.api.agent_events import AgentPublicEvent
from glodex.esci_benchmark import benchmark_summary

PROJECT_ROOT: Final = Path(__file__).resolve().parents[1]
ASSETS_ROOT: Final = PROJECT_ROOT / "showcase" / "assets"
DEFAULT_ARTIFACT_ROOT: Final = PROJECT_ROOT / "data" / "benchmarks" / "esci-small-us-v1"
REPLAY_FILENAME: Final = "m1d-replay.v1.json"
SUMMARY_FILENAME: Final = "m1e-summary.v1.json"
REPLAY_SCHEMA_VERSION: Final = "glodex.showcase.m1d-replay.v1"
PUBLIC_EVENT_SCHEMA_VERSION: Final = "glodex.agent.event.v1"
MAX_ASSET_BYTES: Final = 256 * 1024

_EVENT_ADAPTER: Final[TypeAdapter[AgentPublicEvent]] = TypeAdapter(AgentPublicEvent)
_REPLAY_KEYS: Final = frozenset(
    {"schemaVersion", "eventSchemaVersion", "threadId", "runId", "events"}
)
_SUMMARY_KEYS: Final = frozenset(
    {
        "status",
        "benchmark_id",
        "scorer_version",
        "artifact_manifest_sha256",
        "source",
        "counts",
        "label_distribution",
        "metrics",
    }
)
_SUMMARY_SOURCE_KEYS: Final = frozenset({"repository", "revision"})
_SUMMARY_COUNT_KEYS: Final = frozenset({"queries", "products", "judgements"})
_SUMMARY_LABEL_KEYS: Final = frozenset({"Exact", "Substitute", "Complement", "Irrelevant"})
_SUMMARY_METRIC_KEYS: Final = frozenset({"exact_at_10", "mrr_at_10", "ndcg_at_10"})
_SUMMARY_METRIC_VALUE_KEYS: Final = frozenset({"value", "denominator", "excluded"})


class ShowcaseValidationError(ValueError):
    """Raised when a showcase asset is not safe, deterministic, and contract-valid."""


def _canonical_json_bytes(payload: object) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def _read_canonical_json(path: Path) -> object:
    if not path.is_file() or path.is_symlink():
        raise ShowcaseValidationError("showcase asset is missing or unsafe")
    try:
        raw = path.read_bytes()
        decoded = raw.decode("utf-8")
        payload = json.loads(decoded)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ShowcaseValidationError("showcase asset is not valid UTF-8 JSON") from error
    if raw != _canonical_json_bytes(payload):
        raise ShowcaseValidationError("showcase asset is not canonical JSON")
    return payload


def _require_exact_keys(payload: Mapping[str, object], expected: frozenset[str]) -> None:
    if frozenset(payload) != expected:
        raise ShowcaseValidationError("showcase asset fields are not approved")


def _require_mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ShowcaseValidationError("showcase object must be a JSON object")
    return value


def _validate_replay(payload: object) -> frozenset[str]:
    replay = _require_mapping(payload)
    _require_exact_keys(replay, _REPLAY_KEYS)
    if replay["schemaVersion"] != REPLAY_SCHEMA_VERSION:
        raise ShowcaseValidationError("showcase replay schema version is invalid")
    if replay["eventSchemaVersion"] != PUBLIC_EVENT_SCHEMA_VERSION:
        raise ShowcaseValidationError("showcase replay event schema version is invalid")
    thread_id = replay["threadId"]
    run_id = replay["runId"]
    events = replay["events"]
    if not isinstance(thread_id, str) or not isinstance(run_id, str):
        raise ShowcaseValidationError("showcase replay identifiers are invalid")
    if not isinstance(events, list) or not events:
        raise ShowcaseValidationError("showcase replay must contain events")

    active_children: set[str] = set()
    executed_tools: set[str] = set()
    terminal_seen = False
    for expected_sequence, value in enumerate(events, start=1):
        event = _require_mapping(value)
        try:
            _EVENT_ADAPTER.validate_json(_canonical_json_bytes(event))
        except ValidationError as error:
            raise ShowcaseValidationError(
                "showcase replay event is not public-contract valid"
            ) from error
        if event.get("sequence") != expected_sequence:
            raise ShowcaseValidationError("showcase replay sequence is not contiguous")
        if event.get("threadId") != thread_id or event.get("runId") != run_id:
            raise ShowcaseValidationError("showcase replay identity changed mid-run")
        event_type = event.get("type")
        if terminal_seen:
            raise ShowcaseValidationError("showcase replay emits an event after terminal")
        if expected_sequence == 1 and event_type != "AGENT_STARTED":
            raise ShowcaseValidationError("showcase replay must start with AGENT_STARTED")
        if event_type in {"AGENT_RESULT", "AGENT_ERROR"}:
            terminal_seen = True

        child_id = event.get("childId")
        if event_type == "FORK_STARTED":
            if not isinstance(child_id, str) or child_id in active_children:
                raise ShowcaseValidationError("showcase replay fork is invalid")
            active_children.add(child_id)
        elif event_type == "FORK_FINISHED":
            if not isinstance(child_id, str) or child_id not in active_children:
                raise ShowcaseValidationError("showcase replay fork finish is invalid")
            active_children.remove(child_id)
        elif event.get("scope") == "child":
            if not isinstance(child_id, str) or child_id not in active_children:
                raise ShowcaseValidationError("showcase replay child event is orphaned")

        tool_name = event.get("toolName")
        if isinstance(tool_name, str):
            executed_tools.add(tool_name)

    if not terminal_seen or events[-1].get("type") not in {"AGENT_RESULT", "AGENT_ERROR"}:
        raise ShowcaseValidationError("showcase replay must end at a terminal event")
    if active_children:
        raise ShowcaseValidationError("showcase replay has unfinished children")
    return frozenset(executed_tools)


def _validate_summary_shape(payload: object) -> Mapping[str, object]:
    summary = _require_mapping(payload)
    _require_exact_keys(summary, _SUMMARY_KEYS)
    for key, expected in (
        ("source", _SUMMARY_SOURCE_KEYS),
        ("counts", _SUMMARY_COUNT_KEYS),
        ("label_distribution", _SUMMARY_LABEL_KEYS),
        ("metrics", _SUMMARY_METRIC_KEYS),
    ):
        _require_exact_keys(_require_mapping(summary[key]), expected)
    metrics = _require_mapping(summary["metrics"])
    for metric in metrics.values():
        _require_exact_keys(_require_mapping(metric), _SUMMARY_METRIC_VALUE_KEYS)
    return summary


def _summary_projection(summary: object) -> Mapping[str, object]:
    return _validate_summary_shape(summary)


def validate_assets(
    *,
    assets_root: Path = ASSETS_ROOT,
    artifact_root: Path = DEFAULT_ARTIFACT_ROOT,
) -> frozenset[str]:
    """Validate committed replay and summary assets against their safe sources."""

    if not assets_root.is_dir() or assets_root.is_symlink():
        raise ShowcaseValidationError("showcase assets directory is invalid")
    expected_paths = {REPLAY_FILENAME, SUMMARY_FILENAME}
    actual_paths = {path.name for path in assets_root.iterdir()}
    if actual_paths != expected_paths:
        raise ShowcaseValidationError("showcase asset inventory is invalid")
    total_bytes = sum((assets_root / name).stat().st_size for name in expected_paths)
    if total_bytes > MAX_ASSET_BYTES:
        raise ShowcaseValidationError("showcase assets exceed the approved size limit")

    replay = _read_canonical_json(assets_root / REPLAY_FILENAME)
    summary = _read_canonical_json(assets_root / SUMMARY_FILENAME)
    executed_tools = _validate_replay(replay)
    if not {"planner", "dispatch_tool", "item_search", "shopping_summary"} <= executed_tools:
        raise ShowcaseValidationError("showcase replay does not evidence its declared tool flow")
    expected_summary = _summary_projection(benchmark_summary(artifact_root))
    if _summary_projection(summary) != expected_summary:
        raise ShowcaseValidationError("showcase benchmark summary does not match the artifact")
    return executed_tools


def main() -> int:
    """Run the public, non-diagnostic M1f static-asset validation command."""

    try:
        executed_tools = validate_assets()
    except Exception:
        print('{"code":"M1F_SHOWCASE_INVALID","status":"FAILED"}')
        return 1
    print(
        json.dumps(
            {"executed_tool_count": len(executed_tools), "status": "COMPLETED"},
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
