"""Deterministic, cwd-independent configuration loading for Glodex."""

from __future__ import annotations

import hashlib
import json
import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from glodex.contracts import ConfigFingerprint, CurrencyCode, SnapshotVersion

DEFAULT_DATA_DIR = "data/snapshots"
DEFAULT_SNAPSHOT = "m0-v1"
DEFAULT_LOCALE: Final[Literal["zh-CN"]] = "zh-CN"
DEFAULT_CURRENCY = "USD"
DEFAULT_TOP_K = 3


class ConfigurationError(ValueError):
    """A safe configuration failure with a stable machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class _FrozenConfigModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
    )


class ConfigOverrides(_FrozenConfigModel):
    """Optional command-line values applied after file and environment values."""

    data_dir: Path | None = None
    default_snapshot: SnapshotVersion | None = None
    default_locale: Literal["zh-CN"] | None = None
    default_currency: CurrencyCode | None = None
    default_top_k: Annotated[int, Field(ge=1, le=3)] | None = None


class GlodexConfig(_FrozenConfigModel):
    """Fully resolved configuration used by one application instance."""

    data_dir: Path
    default_snapshot: SnapshotVersion
    default_locale: Literal["zh-CN"]
    default_currency: CurrencyCode
    default_top_k: Annotated[int, Field(ge=1, le=3)]
    fingerprint: ConfigFingerprint


class _FileAppConfig(_FrozenConfigModel):
    data_dir: str = DEFAULT_DATA_DIR
    default_snapshot: SnapshotVersion = DEFAULT_SNAPSHOT


class _FileSearchConfig(_FrozenConfigModel):
    default_locale: Literal["zh-CN"] = DEFAULT_LOCALE
    default_currency: CurrencyCode = DEFAULT_CURRENCY
    default_top_k: Annotated[int, Field(ge=1, le=3)] = DEFAULT_TOP_K


class _FileConfig(_FrozenConfigModel):
    app: _FileAppConfig = _FileAppConfig()
    search: _FileSearchConfig = _FileSearchConfig()


def _project_root(package_file: Path | None) -> Path:
    source_file = Path(__file__) if package_file is None else package_file
    resolved = source_file.resolve()
    try:
        return resolved.parents[2]
    except IndexError as error:
        raise ConfigurationError(
            "CONFIG_ROOT_INVALID",
            "package path has no repository root",
        ) from error


def discover_config_path(
    explicit_path: Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    package_file: Path | None = None,
) -> Path:
    """Resolve ``--config`` > ``GLODEX_CONFIG`` > package-root ``glodex.toml``."""

    environment = os.environ if environ is None else environ
    if explicit_path is not None:
        return explicit_path.expanduser().resolve()

    configured_path = environment.get("GLODEX_CONFIG")
    if configured_path:
        return Path(configured_path).expanduser().resolve()

    return (_project_root(package_file) / "glodex.toml").resolve()


def _read_file_config(path: Path, *, required: bool) -> _FileConfig:
    if not path.is_file():
        if required:
            raise ConfigurationError("CONFIG_NOT_FOUND", "configuration file not found")
        return _FileConfig()

    try:
        with path.open("rb") as stream:
            payload = tomllib.load(stream)
        return _FileConfig.model_validate(payload)
    except (OSError, tomllib.TOMLDecodeError, ValidationError) as error:
        raise ConfigurationError(
            "CONFIG_INVALID",
            "configuration file is invalid",
        ) from error


def _environment_overrides(environment: Mapping[str, str]) -> ConfigOverrides:
    values: dict[str, object] = {}
    names = {
        "GLODEX_DATA_DIR": "data_dir",
        "GLODEX_DEFAULT_SNAPSHOT": "default_snapshot",
        "GLODEX_DEFAULT_LOCALE": "default_locale",
        "GLODEX_DEFAULT_CURRENCY": "default_currency",
        "GLODEX_DEFAULT_TOP_K": "default_top_k",
    }
    for environment_name, field_name in names.items():
        value = environment.get(environment_name)
        if value is None:
            continue
        if field_name == "data_dir":
            values[field_name] = Path(value)
        elif field_name == "default_top_k":
            try:
                values[field_name] = int(value)
            except ValueError as error:
                raise ConfigurationError(
                    "CONFIG_INVALID",
                    f"{environment_name} must be an integer",
                ) from error
        else:
            values[field_name] = value

    try:
        return ConfigOverrides.model_validate(values)
    except ValidationError as error:
        raise ConfigurationError(
            "CONFIG_INVALID",
            "environment configuration is invalid",
        ) from error


def _override_values(overrides: ConfigOverrides) -> dict[str, object]:
    return {name: value for name, value in overrides.model_dump().items() if value is not None}


def _fingerprint(values: Mapping[str, object]) -> str:
    serializable = {
        key: str(value) if isinstance(value, Path) else value for key, value in values.items()
    }
    canonical = json.dumps(
        serializable,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def load_config(
    explicit_path: Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    cli_overrides: ConfigOverrides | None = None,
    package_file: Path | None = None,
) -> GlodexConfig:
    """Load and merge defaults/file, environment, then CLI overrides."""

    environment = os.environ if environ is None else environ
    config_path = discover_config_path(
        explicit_path,
        environ=environment,
        package_file=package_file,
    )
    path_was_explicit = explicit_path is not None or bool(environment.get("GLODEX_CONFIG"))
    file_config = _read_file_config(config_path, required=path_was_explicit)

    data_dir = Path(file_config.app.data_dir).expanduser()
    if not data_dir.is_absolute():
        data_dir = config_path.parent / data_dir

    values: dict[str, object] = {
        "data_dir": data_dir.resolve(),
        "default_snapshot": file_config.app.default_snapshot,
        "default_locale": file_config.search.default_locale,
        "default_currency": file_config.search.default_currency,
        "default_top_k": file_config.search.default_top_k,
    }

    environment_values = _override_values(_environment_overrides(environment))
    environment_data_dir = environment_values.get("data_dir")
    if environment_data_dir is not None:
        assert isinstance(environment_data_dir, Path)
        environment_path = environment_data_dir.expanduser()
        if not environment_path.is_absolute():
            environment_path = config_path.parent / environment_path
        environment_values["data_dir"] = environment_path.resolve()
    values.update(environment_values)

    command_values = _override_values(cli_overrides or ConfigOverrides())
    command_data_dir = command_values.get("data_dir")
    if command_data_dir is not None:
        assert isinstance(command_data_dir, Path)
        command_path = command_data_dir.expanduser()
        if not command_path.is_absolute():
            command_path = config_path.parent / command_path
        command_values["data_dir"] = command_path.resolve()
    values.update(command_values)

    fingerprint = _fingerprint(values)
    try:
        return GlodexConfig.model_validate({**values, "fingerprint": fingerprint})
    except ValidationError as error:
        raise ConfigurationError(
            "CONFIG_INVALID",
            "effective configuration is invalid",
        ) from error


__all__ = [
    "ConfigOverrides",
    "ConfigurationError",
    "GlodexConfig",
    "discover_config_path",
    "load_config",
]
