"""Run the explicit real local OpenSearch M2a smoke after Docker is already started."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

_COMMANDS = (
    (
        "uv",
        "run",
        "--locked",
        "glodex",
        "m2a-index",
        "--action",
        "build",
        "--snapshot",
        "m1d-demo-v1",
    ),
    (
        "uv",
        "run",
        "--locked",
        "glodex",
        "m2a-index",
        "--action",
        "verify",
        "--snapshot",
        "m1d-demo-v1",
    ),
)


def run_smoke(*, environ: Mapping[str, str] | None = None) -> int:
    """Build then verify only the fixed loopback indexes; never starts Docker itself."""

    environment = dict(os.environ if environ is None else environ)
    for command in _COMMANDS:
        completed = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            env=environment,
            check=False,
            shell=False,
        )
        if completed.returncode != 0:
            return completed.returncode if completed.returncode > 0 else 1
    return 0


def main() -> int:
    return run_smoke()


if __name__ == "__main__":
    raise SystemExit(main())
