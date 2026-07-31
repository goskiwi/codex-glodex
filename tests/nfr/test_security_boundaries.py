"""Security and privacy proofs at the M0 application boundary."""

from __future__ import annotations

import ast
import asyncio
import json
import re
from pathlib import Path

import pytest

from glodex.adapters.local_snapshot import (
    MAX_FILE_BYTES,
    MAX_JSONL_RECORDS,
    LocalSnapshotCatalog,
)
from glodex.bootstrap import build_service
from glodex.cli import DEMO_QUERY, main
from glodex.config import load_config
from glodex.contracts import RequestRejected, RunStatus, SearchRequest, validate_search_request
from glodex.domain.catalog import CatalogBatch
from glodex.domain.issues import IssueCode
from tests.builders import build_product

pytestmark = [
    pytest.mark.nfr,
    pytest.mark.spec("GLO-P0-012", "AC-012", "GLO-NFR-009", "GLO-NFR-010"),
]

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = PROJECT_ROOT / "src" / "glodex"
SNAPSHOT_ROOT = PROJECT_ROOT / "data" / "snapshots"

_SECRET_PATTERNS = {
    "aws-access-key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "bearer-token": re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/-]{16,}=*"),
    "generic-secret-assignment": re.compile(
        r"""(?ix)
        \b(?:api[_-]?key|access[_-]?token|client[_-]?secret|password|passwd)
        \s*[:=]\s*["']?[a-z0-9_./+=-]{12,}
        """
    ),
    "github-token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    "openai-style-key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "private-key": re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----"),
    "url-credential": re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@"),
}
_PRIVACY_IDENTIFIER_FRAGMENTS = (
    "conversationhistory",
    "longtermmemory",
    "persistrequest",
    "profilegateway",
    "profilestore",
    "requestrepository",
    "requeststore",
    "savequery",
    "saverequest",
    "storequery",
    "userprofile",
)
_PRIVATE_DATA_IDENTIFIERS = frozenset({"profile", "query", "request"})
_PERSISTENCE_IMPORTS = ("dbm", "pickle", "shelve", "sqlite3")
_OPT_IN_PROFILE_IDENTIFIERS = frozenset(
    {
        "M2aProfileStore",
        "m2a_profile_store",
        "M2cProfileStore",
        "m2c_profile_store",
        "profile_store",
    }
)
_OPT_IN_PROFILE_OWNER_PATHS = frozenset(
    {
        Path("adapters/m2a_profile_store.py"),
        Path("adapters/m2b_profile_projection.py"),
        Path("adapters/m2c_profile_store.py"),
        Path("agent_bootstrap.py"),
        Path("cli.py"),
    }
)


def _secret_violations(surfaces: dict[str, str]) -> list[str]:
    violations: list[str] = []
    for surface_name, text in sorted(surfaces.items()):
        for pattern_name, pattern in sorted(_SECRET_PATTERNS.items()):
            if pattern.search(text):
                violations.append(f"{surface_name}: {pattern_name}")
    return violations


def _normalized_identifier(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _identifier_tokens(value: str) -> frozenset[str]:
    words = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    return frozenset(part.casefold() for part in re.split(r"[^A-Za-z0-9]+", words) if part)


def _mentions_private_data(node: ast.AST) -> bool:
    identifiers = (
        child.id if isinstance(child, ast.Name) else child.attr
        for child in ast.walk(node)
        if isinstance(child, (ast.Attribute, ast.Name))
    )
    return any(
        _identifier_tokens(identifier) & _PRIVATE_DATA_IDENTIFIERS for identifier in identifiers
    )


def _write_mode(open_call: ast.Call) -> bool:
    function = open_call.func
    if isinstance(function, ast.Name):
        if function.id != "open":
            return False
        mode_position = 1
    elif isinstance(function, ast.Attribute) and function.attr == "open":
        mode_position = 0
    else:
        return False

    mode: object = None
    if len(open_call.args) > mode_position:
        candidate = open_call.args[mode_position]
        if isinstance(candidate, ast.Constant):
            mode = candidate.value
    for keyword in open_call.keywords:
        if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant):
            mode = keyword.value.value
    return isinstance(mode, str) and bool(set(mode) & {"a", "w", "x", "+"})


def _write_handle_names(tree: ast.AST) -> frozenset[str]:
    handles: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if isinstance(value, ast.Call) and _write_mode(value):
                handles.update(target.id for target in targets if isinstance(target, ast.Name))
        elif isinstance(node, ast.withitem):
            context = node.context_expr
            target = node.optional_vars
            if (
                isinstance(context, ast.Call)
                and _write_mode(context)
                and isinstance(target, ast.Name)
            ):
                handles.add(target.id)
    return frozenset(handles)


def _is_file_write_target(
    node: ast.AST,
    write_handles: frozenset[str],
) -> bool:
    return (isinstance(node, ast.Name) and node.id in write_handles) or (
        isinstance(node, ast.Call) and _write_mode(node)
    )


def _writes_private_data(call: ast.Call, write_handles: frozenset[str]) -> bool:
    function = call.func
    if not isinstance(function, ast.Attribute):
        return False

    values: list[ast.AST]
    if function.attr in {"write_bytes", "write_text"} or (
        function.attr == "write" and _is_file_write_target(function.value, write_handles)
    ):
        values = [*call.args, *(keyword.value for keyword in call.keywords)]
    elif (
        function.attr == "dump"
        and isinstance(function.value, ast.Name)
        and function.value.id == "json"
        and len(call.args) >= 2
        and isinstance(call.args[1], ast.Name)
        and call.args[1].id in write_handles
    ):
        values = [call.args[0]]
    else:
        return False
    return any(_mentions_private_data(value) for value in values)


def _privacy_violations(package_root: Path) -> list[str]:
    violations: set[str] = set()
    for path in sorted(package_root.rglob("*.py")):
        relative = path.relative_to(package_root)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        write_handles = _write_handle_names(tree)
        names = {path.stem}
        for node in ast.walk(tree):
            if isinstance(node, (ast.AsyncFunctionDef, ast.ClassDef, ast.FunctionDef)):
                names.add(node.name)
            elif isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, ast.arg):
                names.add(node.arg)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                module = node.module if isinstance(node, ast.ImportFrom) else None
                imported = [alias.name for alias in node.names]
                names.update(alias.asname or alias.name for alias in node.names)
                modules = imported if module is None else [module]
                if any(
                    item == prefix or item.startswith(f"{prefix}.")
                    for item in modules
                    for prefix in _PERSISTENCE_IMPORTS
                ):
                    violations.add(f"{relative}: persistence import")
            if isinstance(node, ast.Call) and _writes_private_data(node, write_handles):
                violations.add(f"{relative}: private request data written to persistent file")

        for name in names:
            normalized = _normalized_identifier(name)
            if relative in _OPT_IN_PROFILE_OWNER_PATHS and name in _OPT_IN_PROFILE_IDENTIFIERS:
                continue
            if any(fragment in normalized for fragment in _PRIVACY_IDENTIFIER_FRAGMENTS):
                violations.add(f"{relative}: privacy adapter identifier ({name})")
    return sorted(violations)


def test_secret_scanner_detects_representative_credentials() -> None:
    violations = _secret_violations(
        {
            "synthetic-config": 'api_key = "sk-abcdefghijklmnopqrstuvwxyz123456"',
            "synthetic-github-classic": "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
            "synthetic-github-fine": (
                "github_pat_11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyz123456"
            ),
            "synthetic-log": "Authorization: Bearer abcdefghijklmnopqrstuvwxyz",
        }
    )

    assert violations == [
        "synthetic-config: generic-secret-assignment",
        "synthetic-config: openai-style-key",
        "synthetic-github-classic: github-token",
        "synthetic-github-fine: github-token",
        "synthetic-log: bearer-token",
    ]


def test_config_fixture_journal_and_output_are_secret_free(
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = load_config(environ={})
    execution = asyncio.run(
        build_service(config).execute(
            SearchRequest(
                query=DEMO_QUERY,
                locale="zh-CN",
                display_currency="USD",
                top_k=3,
                snapshot_version="m0-v1",
            )
        )
    )
    assert execution.response.status is RunStatus.COMPLETED

    assert main(["demo"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""

    fixture_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(SNAPSHOT_ROOT.rglob("*"))
        if path.is_file()
    )
    repository_logs = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in sorted(PROJECT_ROOT.rglob("*.log"))
        if ".git" not in path.parts and ".venv" not in path.parts
    )
    surfaces = {
        "config": (PROJECT_ROOT / "glodex.toml").read_text(encoding="utf-8"),
        "fixture": fixture_text,
        "journal": repr(execution.journal),
        "log-files": repository_logs,
        "output": captured.out,
    }

    assert _secret_violations(surfaces) == []


def test_snapshot_and_public_field_limits_remain_fail_closed(tmp_path: Path) -> None:
    assert MAX_FILE_BYTES == 128 * 1024 * 1024
    assert MAX_JSONL_RECORDS == 500_000

    escaped = asyncio.run(
        LocalSnapshotCatalog(tmp_path).load(
            "../outside",
            display_currency="USD",
        )
    )
    assert escaped.products == ()
    assert escaped.offers == ()
    assert len(escaped.fatal_issues) == 1
    assert escaped.fatal_issues[0].code is IssueCode.PATH_INVALID

    with pytest.raises(ValueError, match="product ID"):
        build_product(product_id="x" * 129)
    with pytest.raises(ValueError, match="source URI"):
        build_product(source_uri="x" * 4_097)
    with pytest.raises(ValueError, match="product title"):
        build_product(title="x" * 16_385)

    rejected = validate_search_request({"query": "x" * 2_001})
    assert isinstance(rejected, RequestRejected)
    assert rejected.errors[0].field == "query"


def test_runtime_package_has_no_profile_or_request_persistence_adapter(
    tmp_path: Path,
) -> None:
    assert _privacy_violations(PACKAGE_ROOT) == []

    synthetic = tmp_path / "glodex"
    synthetic.mkdir()
    (synthetic / "profile.py").write_text(
        "import sqlite3\n\nclass UserProfileStore:\n    pass\n",
        encoding="utf-8",
    )
    (synthetic / "request_sink.py").write_text(
        "from pathlib import Path\n\n"
        "def persist(request):\n"
        '    Path("requests.json").write_text(request.query)\n',
        encoding="utf-8",
    )
    (synthetic / "profile_stream_sink.py").write_text(
        "def persist(profile):\n"
        '    with open("profiles.json", "w", encoding="utf-8") as stream:\n'
        "        stream.write(profile)\n",
        encoding="utf-8",
    )
    (synthetic / "query_json_sink.py").write_text(
        "import json\n\n"
        "def persist(query):\n"
        '    with open("queries.json", mode="w", encoding="utf-8") as stream:\n'
        "        json.dump(query, stream)\n",
        encoding="utf-8",
    )
    (synthetic / "snapshot_reader.py").write_text(
        "from pathlib import Path\n\n"
        "def load(request):\n"
        '    with Path(request.snapshot_path).open("rb") as stream:\n'
        "        return stream.read()\n",
        encoding="utf-8",
    )
    assert _privacy_violations(synthetic) == [
        "profile.py: persistence import",
        "profile.py: privacy adapter identifier (UserProfileStore)",
        "profile_stream_sink.py: private request data written to persistent file",
        "query_json_sink.py: private request data written to persistent file",
        "request_sink.py: private request data written to persistent file",
    ]


def test_cli_maps_internal_path_and_traceback_to_safe_public_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    private_path = str((tmp_path / "private" / "snapshot.jsonl").resolve())

    async def fail_with_private_details(
        _catalog: LocalSnapshotCatalog,
        _snapshot_version: str,
        *,
        display_currency: str | None = None,
        budget_currency: str | None = None,
    ) -> CatalogBatch:
        del display_currency, budget_currency
        raise RuntimeError(f"{private_path}\nTraceback (most recent call last)")

    monkeypatch.setattr(LocalSnapshotCatalog, "load", fail_with_private_details)

    assert main(["demo"]) == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    rendered = captured.out + captured.err

    assert payload["status"] == "FAILED"
    assert payload["diagnostics"]["issues"] == [
        {
            "code": "catalog.load-failed",
            "stage": "snapshot",
            "message": "Catalog snapshot loading failed.",
            "severity": "ERROR",
            "entity_ref": None,
            "details": [],
        }
    ]
    assert private_path not in rendered
    assert str(tmp_path.resolve()) not in rendered
    assert "Traceback" not in rendered
