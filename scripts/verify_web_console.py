"""Run the offline WebConsole gate; the real Durable browser smoke remains explicit."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if not __package__:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.verify_m0 import sanitized_environment  # noqa: E402

_OFFLINE_ENV_PREFIXES = ("AUTOSSH_", "RETRIEVAL_MODEL_", "SSH_", "GLODEX_")


@dataclass(frozen=True, slots=True)
class VerificationStep:
    """One reproducible subprocess action in the WebConsole default gate."""

    name: str
    command: tuple[str, ...]


_STEPS = (
    VerificationStep(
        "WebConsole socket-blocked Python suite",
        (
            "uv",
            "run",
            "--locked",
            "python",
            "-m",
            "pytest",
            "-q",
            "-p",
            "scripts.verify_m0",
            "tests/web_console",
        ),
    ),
    VerificationStep(
        "WebConsole format",
        (
            "uv",
            "run",
            "--locked",
            "ruff",
            "format",
            "--check",
            "src/glodex/api/agui.py",
            "src/glodex/api/console_app.py",
            "src/glodex/api/console_contracts.py",
            "src/glodex/api/durable_client.py",
            "tests/web_console",
            "scripts/verify_web_console.py",
        ),
    ),
    VerificationStep(
        "WebConsole lint",
        (
            "uv",
            "run",
            "--locked",
            "ruff",
            "check",
            "src/glodex/api/agui.py",
            "src/glodex/api/console_app.py",
            "src/glodex/api/console_contracts.py",
            "src/glodex/api/durable_client.py",
            "tests/web_console",
            "scripts/verify_web_console.py",
        ),
    ),
    VerificationStep(
        "WebConsole type check",
        (
            "uv",
            "run",
            "--locked",
            "mypy",
            "src/glodex/api/agui.py",
            "src/glodex/api/console_app.py",
            "src/glodex/api/console_contracts.py",
            "src/glodex/api/durable_client.py",
            "scripts/verify_web_console.py",
        ),
    ),
    VerificationStep(
        "WebConsole traceability coverage",
        (
            "uv",
            "run",
            "--locked",
            "python",
            "scripts/check_traceability.py",
            "--profile",
            "web_console",
            "--mode",
            "coverage",
        ),
    ),
    VerificationStep(
        "locked frontend install without network",
        (
            "pnpm",
            "--dir",
            "frontend",
            "install",
            "--frozen-lockfile",
            "--offline",
            "--ignore-scripts",
        ),
    ),
    VerificationStep(
        "WebConsole Vue type check",
        ("pnpm", "--dir", "frontend", "run", "typecheck"),
    ),
    VerificationStep(
        "WebConsole Vue production build",
        ("pnpm", "--dir", "frontend", "run", "build"),
    ),
)


def offline_environment(environ: Mapping[str, str]) -> dict[str, str]:
    """Remove provider, proxy, GPU, tunnel, and service settings from the default gate."""

    sanitized = sanitized_environment(environ)
    return {
        name: value
        for name, value in sanitized.items()
        if not name.upper().startswith(_OFFLINE_ENV_PREFIXES)
    }


def _check_hygiene() -> bool:
    """Keep browser source and its packaged asset free of private client paths."""

    frontend_root = PROJECT_ROOT / "frontend"
    source_paths = (
        frontend_root / "src" / "service" / "agent.ts",
        frontend_root / "src" / "service" / "local-auth.ts",
        frontend_root / "src" / "views" / "chat" / "index.vue",
        frontend_root / "src" / "views" / "chat" / "modules" / "research-workspace.vue",
        frontend_root / "src" / "store" / "modules" / "research-history" / "index.ts",
    )
    if any(not path.is_file() for path in source_paths):
        print("WebConsole browser-source entrypoints are missing.", file=sys.stderr)
        return False
    source = "\n".join(path.read_text(encoding="utf-8") for path in source_paths)
    source_forbidden = (
        "fetch('http://",
        'fetch("http://',
        "fetch('https://",
        'fetch("https://',
        "new WebSocket('ws://",
        'new WebSocket("ws://',
        "new WebSocket('wss://",
        'new WebSocket("wss://',
        "127.0.0.1:8766",
        "127.0.0.1:18000",
        "Authorization: Bearer",
        "raw-fixture",
        "".join(("DEEP", "SEEK_API_KEY")),
        "".join(("DASH", "SCOPE_API_KEY")),
    )
    if any(token in source for token in source_forbidden):
        print("WebConsole browser-source hygiene check failed.", file=sys.stderr)
        return False
    if (
        "localStg.set('glodexResearchHistoryItems'" in source
        or "localStg.get('glodexResearchHistoryItems'" in source
    ):
        print(
            "WebConsole must not persist raw research history in browser storage.", file=sys.stderr
        )
        return False
    lockfile = frontend_root / "pnpm-lock.yaml"
    package_manifest = frontend_root / "package.json"
    if not lockfile.is_file():
        print("WebConsole pnpm lockfile is missing.", file=sys.stderr)
        return False
    if (frontend_root / "package-lock.json").exists():
        print("WebConsole must not carry an npm package-lock.json.", file=sys.stderr)
        return False
    try:
        package_manager = json.loads(package_manifest.read_text(encoding="utf-8"))["packageManager"]
    except (OSError, ValueError, KeyError, TypeError):
        print("WebConsole package manager declaration is invalid.", file=sys.stderr)
        return False
    if type(package_manager) is not str or not package_manager.startswith("pnpm@"):
        print("WebConsole must declare pnpm as its package manager.", file=sys.stderr)
        return False
    build_root = frontend_root / "dist"
    if not (build_root / "index.html").is_file() or not (build_root / "assets").is_dir():
        print("WebConsole frontend production build is missing.", file=sys.stderr)
        return False
    packaged_asset = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(build_root.rglob("*"))
        if path.is_file() and path.suffix in {".css", ".html", ".js"}
    )
    provider_key_markers = (
        "".join(("DEEP", "SEEK_API_KEY")),
        "".join(("DASH", "SCOPE_API_KEY")),
    )
    packaged_forbidden = (
        "127.0.0.1:8766",
        "127.0.0.1:18000",
        "Authorization: Bearer",
        "raw-fixture",
        *provider_key_markers,
    )
    if any(token in packaged_asset for token in packaged_forbidden):
        print("WebConsole packaged-asset hygiene check failed.", file=sys.stderr)
        return False
    return True


def run_verification(*, environ: Mapping[str, str] | None = None) -> int:
    """Run deterministic offline evidence without starting a server or Durable composition."""

    environment = offline_environment(os.environ if environ is None else environ)
    for position, step in enumerate(_STEPS, start=1):
        print(f"[WebConsole {position}/{len(_STEPS)}] {step.name}", flush=True)
        completed = subprocess.run(
            step.command,
            cwd=PROJECT_ROOT,
            env=environment,
            check=False,
            shell=False,
        )
        if completed.returncode != 0:
            print(f"WebConsole verification failed at: {step.name}", file=sys.stderr)
            return completed.returncode if completed.returncode > 0 else 1
    if not _check_hygiene():
        return 1
    print(
        "Web console verification passed. Run the explicit local durable browser smoke separately."
    )
    return 0


def main() -> int:
    return run_verification()


if __name__ == "__main__":
    raise SystemExit(main())
