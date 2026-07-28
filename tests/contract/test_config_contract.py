from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from glodex.config import (
    ConfigOverrides,
    ConfigurationError,
    discover_config_path,
    load_config,
)

pytestmark = [
    pytest.mark.contract,
    pytest.mark.spec(
        "GLO-P0-001",
        "GLO-P0-011",
        "GLO-NFR-008",
        "GLO-NFR-011",
    ),
]


def write_config(
    path: Path,
    *,
    data_dir: str = "data/snapshots",
    snapshot: str = "file-v1",
    locale: str = "zh-CN",
    currency: str = "USD",
    top_k: int = 2,
) -> None:
    path.write_text(
        (
            "[app]\n"
            f'data_dir = "{data_dir}"\n'
            f'default_snapshot = "{snapshot}"\n'
            "\n"
            "[search]\n"
            f'default_locale = "{locale}"\n'
            f'default_currency = "{currency}"\n'
            f"default_top_k = {top_k}\n"
        ),
        encoding="utf-8",
    )


def package_file_in(project_root: Path) -> Path:
    package_file = project_root / "src" / "glodex" / "config.py"
    package_file.parent.mkdir(parents=True)
    package_file.touch()
    return package_file


def test_configuration_precedence_is_cli_then_environment_then_file(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    package_file = package_file_in(project_root)
    config_path = project_root / "glodex.toml"
    write_config(config_path)

    config = load_config(
        environ={
            "GLODEX_DEFAULT_SNAPSHOT": "env-v1",
            "GLODEX_DEFAULT_CURRENCY": "EUR",
            "GLODEX_DEFAULT_TOP_K": "1",
        },
        cli_overrides=ConfigOverrides(
            default_snapshot="cli-v1",
            default_top_k=3,
        ),
        package_file=package_file,
    )

    assert config.default_snapshot == "cli-v1"
    assert config.default_locale == "zh-CN"
    assert config.default_currency == "EUR"
    assert config.default_top_k == 3
    assert config.data_dir == (project_root / "data" / "snapshots").resolve()


def test_explicit_path_wins_over_environment_config_path(tmp_path: Path) -> None:
    explicit = tmp_path / "explicit.toml"
    from_environment = tmp_path / "environment.toml"
    write_config(explicit, snapshot="explicit-v1")
    write_config(from_environment, snapshot="environment-v1")

    config = load_config(
        explicit_path=explicit,
        environ={"GLODEX_CONFIG": str(from_environment)},
        package_file=package_file_in(tmp_path / "project"),
    )

    assert config.default_snapshot == "explicit-v1"


def test_default_discovery_uses_package_root_not_current_working_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "project"
    package_file = package_file_in(project_root)
    write_config(project_root / "glodex.toml", snapshot="package-v1")

    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    write_config(unrelated / "glodex.toml", snapshot="cwd-v1")
    monkeypatch.chdir(unrelated)

    discovered = discover_config_path(environ={}, package_file=package_file)
    config = load_config(environ={}, package_file=package_file)

    assert discovered == (project_root / "glodex.toml").resolve()
    assert config.default_snapshot == "package-v1"
    assert config.data_dir == (project_root / "data" / "snapshots").resolve()


def test_explicit_or_environment_config_missing_fails_closed(tmp_path: Path) -> None:
    missing = tmp_path / "missing.toml"

    with pytest.raises(ConfigurationError, match="CONFIG_NOT_FOUND"):
        load_config(explicit_path=missing, environ={})
    with pytest.raises(ConfigurationError, match="CONFIG_NOT_FOUND"):
        load_config(environ={"GLODEX_CONFIG": str(missing)})


def test_missing_default_file_uses_cwd_independent_code_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "project"
    package_file = package_file_in(project_root)
    first_cwd = tmp_path / "first"
    second_cwd = tmp_path / "second"
    first_cwd.mkdir()
    second_cwd.mkdir()

    monkeypatch.chdir(first_cwd)
    first = load_config(environ={}, package_file=package_file)
    monkeypatch.chdir(second_cwd)
    second = load_config(environ={}, package_file=package_file)

    assert first == second
    assert first.data_dir == (project_root / "data" / "snapshots").resolve()
    assert first.default_snapshot == "m0-v1"
    assert first.default_locale == "zh-CN"
    assert first.default_currency == "USD"
    assert first.default_top_k == 3


@pytest.mark.parametrize(
    "toml",
    [
        "[app]\ndata_dir='data'\ndefault_snapshot='m0-v1'\nunknown=true\n",
        "[other]\nvalue=true\n",
        (
            "[app]\ndata_dir='data'\ndefault_snapshot='m0-v1'\n"
            "[search]\ndefault_locale='zh-CN'\ndefault_currency='usd'\ndefault_top_k=3\n"
        ),
        (
            "[app]\ndata_dir='data'\ndefault_snapshot='m0-v1'\n"
            "[search]\ndefault_locale='zh-CN'\ndefault_currency='USD'\ndefault_top_k=4\n"
        ),
    ],
)
def test_unknown_or_invalid_file_configuration_fails_closed(
    tmp_path: Path,
    toml: str,
) -> None:
    config_path = tmp_path / "glodex.toml"
    config_path.write_text(toml, encoding="utf-8")

    with pytest.raises(ConfigurationError, match="CONFIG_INVALID"):
        load_config(explicit_path=config_path, environ={})


def test_relative_data_dir_is_anchored_to_config_file(tmp_path: Path) -> None:
    config_dir = tmp_path / "configuration"
    config_dir.mkdir()
    config_path = config_dir / "glodex.toml"
    write_config(config_path, data_dir="../fixtures")

    config = load_config(explicit_path=config_path, environ={})

    assert config.data_dir == (tmp_path / "fixtures").resolve()


def test_effective_config_has_a_stable_sensitive_fingerprint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "project"
    package_file = package_file_in(project_root)
    write_config(project_root / "glodex.toml")

    first = load_config(environ={}, package_file=package_file)
    monkeypatch.chdir(tmp_path)
    second = load_config(environ={}, package_file=package_file)
    changed = load_config(
        environ={"GLODEX_DEFAULT_TOP_K": "3"},
        package_file=package_file,
    )

    assert first.fingerprint == second.fingerprint
    assert re.fullmatch(r"[0-9a-f]{64}", first.fingerprint)
    assert changed.fingerprint != first.fingerprint


def test_config_does_not_validate_snapshot_existence_or_currency_support(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "glodex.toml"
    write_config(
        config_path,
        data_dir="does-not-exist",
        snapshot="also-does-not-exist",
        currency="ZZZ",
    )

    config = load_config(explicit_path=config_path, environ={})

    assert config.default_snapshot == "also-does-not-exist"
    assert config.default_currency == "ZZZ"
    assert not config.data_dir.exists()


def test_config_and_overrides_are_frozen_and_reject_extra_fields(tmp_path: Path) -> None:
    config = load_config(
        environ={},
        package_file=package_file_in(tmp_path / "project"),
    )

    with pytest.raises(ValidationError):
        config.default_top_k = 1  # type: ignore[misc]
    with pytest.raises(ValidationError):
        ConfigOverrides.model_validate({"default_top_k": 1, "unknown": True})
