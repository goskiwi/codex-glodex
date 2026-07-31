"""Unit coverage for the deterministic M2d verification runner contract."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts import verify_m2d
from scripts.verify_m2d import _STEPS, offline_environment

pytestmark = [
    pytest.mark.unit,
    pytest.mark.spec("GLO-M2D-NFR-001", "GLO-M2D-NFR-004", "GLO-M2D-NFR-006"),
]


def test_verification_steps_keep_parent_gate_and_frontend_install_offline() -> None:
    commands = tuple(step.command for step in _STEPS)

    assert commands[0][-1] == "scripts/verify_m2c.py"
    assert ("npm", "--prefix", "frontend", "ci", "--offline", "--ignore-scripts") in commands
    assert all("m2d-serve" not in command for command in commands)
    assert all("18000" not in command for command in commands)


def test_offline_environment_removes_service_and_tunnel_inputs() -> None:
    environment = offline_environment(
        {
            "PATH": "/usr/bin",
            "GLODEX_CONFIG": "private.toml",
            "M2C_GPU": "enabled",
            "SSH_AUTH_SOCK": "/private/socket",
            "AUTOSSH_PORT": "20000",
        }
    )

    assert environment["PATH"] == "/usr/bin"
    assert "GLODEX_CONFIG" not in environment
    assert "M2C_GPU" not in environment
    assert "SSH_AUTH_SOCK" not in environment
    assert "AUTOSSH_PORT" not in environment


def test_hygiene_rejects_a_private_endpoint_in_packaged_asset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frontend = tmp_path / "frontend"
    (frontend / "src").mkdir(parents=True)
    (frontend / "dist" / "assets").mkdir(parents=True)
    (frontend / "src" / "main.ts").write_text("export {};", encoding="utf-8")
    (frontend / "package-lock.json").write_text("{}", encoding="utf-8")
    (frontend / "dist" / "index.html").write_text("<main />", encoding="utf-8")
    asset = frontend / "dist" / "assets" / "main.js"
    asset.write_text("const api = '/api/v1/m2d';", encoding="utf-8")
    monkeypatch.setattr(verify_m2d, "PROJECT_ROOT", tmp_path)

    assert verify_m2d._check_hygiene()

    asset.write_text("const api = 'http://127.0.0.1:8766';", encoding="utf-8")
    assert not verify_m2d._check_hygiene()
