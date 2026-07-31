"""Source-level architecture boundaries for the intentionally narrow M2d seam."""

from __future__ import annotations

from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
M2D_SOURCE = PROJECT_ROOT / "src" / "glodex" / "api"

pytestmark = [
    pytest.mark.architecture,
    pytest.mark.spec("GLO-M2D-P0-006", "GLO-M2D-NFR-006", "M2D-AC-006"),
]


def test_m2d_never_imports_storage_gpu_provider_or_retrieval_adapters() -> None:
    forbidden = (
        "m2b_postgres",
        "m2b_redis",
        "m2b_m2a_executor",
        "opensearch",
        "deepseek",
        "dashscope",
        "m2c_",
        "gpu_service",
    )
    source = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(M2D_SOURCE.glob("m2d_*.py"))
    ).lower()

    assert all(name not in source for name in forbidden)


def test_m2d_has_one_fixed_public_upstream_and_no_gpu_or_browser_private_route() -> None:
    client_source = (M2D_SOURCE / "m2d_durable_client.py").read_text(encoding="utf-8")
    app_source = (M2D_SOURCE / "m2d_app.py").read_text(encoding="utf-8")
    frontend_source = (PROJECT_ROOT / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")

    assert "http://127.0.0.1:8766" in client_source
    assert "18000" not in client_source
    assert "18000" not in app_source
    assert "8766" not in frontend_source
    assert "18000" not in frontend_source
