"""Unit coverage for the deterministic WebConsole verification runner contract."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import verify_web_console
from scripts.verify_web_console import _STEPS, offline_environment

pytestmark = [
    pytest.mark.unit,
    pytest.mark.spec(
        "GLO-WEB_CONSOLE-NFR-001", "GLO-WEB_CONSOLE-NFR-004", "GLO-WEB_CONSOLE-NFR-006"
    ),
]


def test_verification_steps_are_web_console_scoped_and_keep_pnpm_offline() -> None:
    commands = tuple(step.command for step in _STEPS)

    assert commands[0][-1] == "tests/web_console"
    assert (
        "pnpm",
        "--dir",
        "frontend",
        "install",
        "--frozen-lockfile",
        "--offline",
        "--ignore-scripts",
    ) in commands
    assert ("pnpm", "--dir", "frontend", "run", "typecheck") in commands
    assert ("pnpm", "--dir", "frontend", "run", "build") in commands
    assert all(command[0] != "npm" for command in commands)
    assert all("verify_retrieval_model.py" not in command for command in commands)
    assert all("web_console-serve" not in command for command in commands)
    assert all("18000" not in command for command in commands)


def test_offline_environment_removes_service_and_tunnel_inputs() -> None:
    environment = offline_environment(
        {
            "PATH": "/usr/bin",
            "GLODEX_CONFIG": "private.toml",
            "RETRIEVAL_MODEL_GPU": "enabled",
            "SSH_AUTH_SOCK": "/private/socket",
            "AUTOSSH_PORT": "20000",
        }
    )

    assert environment["PATH"] == "/usr/bin"
    assert "GLODEX_CONFIG" not in environment
    assert "RETRIEVAL_MODEL_GPU" not in environment
    assert "SSH_AUTH_SOCK" not in environment
    assert "AUTOSSH_PORT" not in environment


def test_hygiene_rejects_a_private_endpoint_in_packaged_asset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frontend = tmp_path / "frontend"
    (frontend / "src").mkdir(parents=True)
    (frontend / "src" / "service").mkdir()
    (frontend / "src" / "views" / "chat" / "modules").mkdir(parents=True)
    (frontend / "src" / "store" / "modules" / "research-history").mkdir(parents=True)
    (frontend / "dist" / "assets").mkdir(parents=True)
    (frontend / "src" / "service" / "agent.ts").write_text("export {};", encoding="utf-8")
    (frontend / "src" / "service" / "local-auth.ts").write_text("export {};", encoding="utf-8")
    (frontend / "src" / "views" / "chat" / "index.vue").write_text("<template />", encoding="utf-8")
    (frontend / "src" / "views" / "chat" / "modules" / "research-workspace.vue").write_text(
        "<template />", encoding="utf-8"
    )
    (frontend / "src" / "store" / "modules" / "research-history" / "index.ts").write_text(
        "export {};", encoding="utf-8"
    )
    (frontend / "package.json").write_text('{"packageManager":"pnpm@10.32.1"}', encoding="utf-8")
    (frontend / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")
    (frontend / "dist" / "index.html").write_text("<main />", encoding="utf-8")
    asset = frontend / "dist" / "assets" / "main.js"
    asset.write_text("const api = '/api/v1/web-console';", encoding="utf-8")
    monkeypatch.setattr(verify_web_console, "PROJECT_ROOT", tmp_path)

    assert verify_web_console._check_hygiene()

    asset.write_text("const api = 'http://127.0.0.1:8766';", encoding="utf-8")
    assert not verify_web_console._check_hygiene()
