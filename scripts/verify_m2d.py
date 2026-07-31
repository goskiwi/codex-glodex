"""Run the offline M2d gate; the real M2b browser smoke remains explicit."""

from __future__ import annotations

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

_OFFLINE_ENV_PREFIXES = ("AUTOSSH_", "M2C_", "SSH_", "GLODEX_")


@dataclass(frozen=True, slots=True)
class VerificationStep:
    """One reproducible subprocess action in the M2d default gate."""

    name: str
    command: tuple[str, ...]


_STEPS = (
    VerificationStep(
        "complete M2c gate",
        ("uv", "run", "--locked", "python", "scripts/verify_m2c.py"),
    ),
    VerificationStep(
        "M2d socket-blocked Python suite",
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
            "tests/m2d",
        ),
    ),
    VerificationStep(
        "M2d format and lint",
        (
            "uv",
            "run",
            "--locked",
            "ruff",
            "format",
            "--check",
            "src",
            "tests/m2d",
            "scripts",
        ),
    ),
    VerificationStep(
        "M2d type check",
        ("uv", "run", "--locked", "mypy", "src/glodex", "scripts"),
    ),
    VerificationStep(
        "M2d traceability coverage",
        (
            "uv",
            "run",
            "--locked",
            "python",
            "scripts/check_traceability.py",
            "--profile",
            "m2d",
            "--mode",
            "coverage",
        ),
    ),
    VerificationStep(
        "locked frontend install without network",
        ("npm", "--prefix", "frontend", "ci", "--offline", "--ignore-scripts"),
    ),
    VerificationStep(
        "M2d React tests",
        ("npm", "--prefix", "frontend", "test"),
    ),
    VerificationStep(
        "M2d React production build",
        ("npm", "--prefix", "frontend", "run", "build"),
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
    source_root = frontend_root / "src"
    source = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(source_root.glob("*.ts*"))
    )
    source_forbidden = (
        "localStorage",
        "sessionStorage",
        "console.",
        "http://",
        "https://",
        "127.0.0.1:8766",
        "127.0.0.1:18000",
    )
    if any(token in source for token in source_forbidden):
        print("M2d browser-source hygiene check failed.", file=sys.stderr)
        return False
    if not (frontend_root / "package-lock.json").is_file():
        print("M2d frontend lockfile is missing.", file=sys.stderr)
        return False
    build_root = frontend_root / "dist"
    if not (build_root / "index.html").is_file() or not (build_root / "assets").is_dir():
        print("M2d frontend production build is missing.", file=sys.stderr)
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
        print("M2d packaged-asset hygiene check failed.", file=sys.stderr)
        return False
    return True


def run_verification(*, environ: Mapping[str, str] | None = None) -> int:
    """Run deterministic offline evidence without starting a server or M2b composition."""

    environment = offline_environment(os.environ if environ is None else environ)
    for position, step in enumerate(_STEPS, start=1):
        print(f"[M2d {position}/{len(_STEPS)}] {step.name}", flush=True)
        completed = subprocess.run(
            step.command,
            cwd=PROJECT_ROOT,
            env=environment,
            check=False,
            shell=False,
        )
        if completed.returncode != 0:
            print(f"M2d verification failed at: {step.name}", file=sys.stderr)
            return completed.returncode if completed.returncode > 0 else 1
    if not _check_hygiene():
        return 1
    print("M2d offline verification passed. Run the explicit local M2b browser smoke separately.")
    return 0


def main() -> int:
    return run_verification()


if __name__ == "__main__":
    raise SystemExit(main())
