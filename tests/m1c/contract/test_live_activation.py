"""Explicit live composition and CLI activation contracts."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from glodex import bootstrap, cli
from glodex.adapters.deepseek_http import DeepSeekPreflightError
from glodex.adapters.deepseek_intent import DeepSeekIntentInterpreter
from glodex.adapters.rule_intent import RuleIntentInterpreter
from glodex.config import GlodexConfig, load_config
from glodex.contracts import RequestRejected
from tests.m1c.conftest import RecordingDeepSeekTransport

pytestmark = pytest.mark.contract


def _install_fake_http_transport(
    monkeypatch: pytest.MonkeyPatch,
    transport: RecordingDeepSeekTransport,
) -> None:
    from glodex.adapters import deepseek_http

    real_builder = deepseek_http.build_deepseek_transport

    def build() -> RecordingDeepSeekTransport:
        real_builder()
        return transport

    monkeypatch.setattr(deepseek_http, "build_deepseek_transport", build)


def _captured_rejection(
    capsys: pytest.CaptureFixture[str],
) -> tuple[dict[str, object], str]:
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert isinstance(payload, dict)
    return payload, captured.err


def _install_fake_live_builder(
    monkeypatch: pytest.MonkeyPatch,
    transport: RecordingDeepSeekTransport,
) -> list[GlodexConfig]:
    _install_fake_http_transport(monkeypatch, transport)
    real_builder = bootstrap.build_live_intent_service
    configs: list[GlodexConfig] = []

    def build(config: GlodexConfig) -> object:
        configs.append(config)
        return real_builder(config)

    monkeypatch.setattr(bootstrap, "build_live_intent_service", build)
    return configs


@pytest.mark.spec("GLO-M1C-NFR-001", "GLO-M1C-NFR-006")
def test_default_imports_do_not_load_live_or_http_modules() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json, sys;"
                "import glodex.bootstrap, glodex.cli, glodex.api, glodex.api.app;"
                "print(json.dumps({name: name in sys.modules for name in "
                "['httpx','glodex.api.live_app','glodex.adapters.deepseek_http',"
                "'glodex.adapters.deepseek_intent']}))"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    assert json.loads(completed.stdout) == {
        "httpx": False,
        "glodex.api.live_app": False,
        "glodex.adapters.deepseek_http": False,
        "glodex.adapters.deepseek_intent": False,
    }
    assert completed.stderr == ""


@pytest.mark.spec("GLO-M1C-NFR-006")
def test_shared_live_builder_preflights_then_composes_fixed_interpreters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-live-secret")
    transport = RecordingDeepSeekTransport(b"unused")
    clock = object()
    run_id_provider = object()
    _install_fake_http_transport(monkeypatch, transport)

    service = bootstrap.build_live_intent_service(
        load_config(environ={}),
        clock=clock,
        run_id_provider=run_id_provider,
    )

    assert type(service.intent_interpreter) is DeepSeekIntentInterpreter
    assert type(service.required_baseline_interpreter) is RuleIntentInterpreter
    assert service.clock is clock
    assert service.run_id_provider is run_id_provider
    assert transport.calls == []

    monkeypatch.delenv("DEEPSEEK_API_KEY")
    with pytest.raises(DeepSeekPreflightError) as raised:
        bootstrap.build_live_intent_service(
            load_config(environ={}),
        )
    assert raised.value.code == "INTENT_LIVE_CREDENTIALS_MISSING"


@pytest.mark.spec("GLO-M1C-P0-001")
def test_live_flag_belongs_only_to_search(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = cli.main(("demo", "--live-intent"))
    payload, stderr = _captured_rejection(capsys)

    assert exit_code == 2
    assert payload["type"] == "request_rejected"
    assert payload["errors"][0]["field"] == "cli"
    assert payload["errors"][0]["code"] == "CLI_USAGE"
    assert "Traceback" not in stderr


@pytest.mark.spec("GLO-M1C-NFR-002")
@pytest.mark.parametrize(
    ("credential", "expected_code"),
    [
        (None, "INTENT_LIVE_CREDENTIALS_MISSING"),
        ("invalid\ncredential", "INTENT_LIVE_CONFIG_INVALID"),
    ],
)
def test_cli_live_preflight_is_safe_and_precedes_request_validation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    credential: str | None,
    expected_code: str,
) -> None:
    if credential is None:
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    else:
        monkeypatch.setenv("DEEPSEEK_API_KEY", credential)

    def forbidden_submit(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("preflight failure reached submit_search")

    monkeypatch.setattr(cli, "submit_search", forbidden_submit)
    exit_code = cli.main(("search", "--live-intent", "--query", ""))
    payload, stderr = _captured_rejection(capsys)

    assert exit_code == 2
    assert payload["type"] == "request_rejected"
    assert payload["errors"] == [
        {
            "field": "intent",
            "code": expected_code,
            "message": (
                "DeepSeek live credentials are missing."
                if credential is None
                else "DeepSeek live configuration is invalid."
            ),
        }
    ]
    assert "credential" not in stderr
    assert "Traceback" not in stderr


@pytest.mark.spec("GLO-M1C-NFR-001")
def test_valid_live_preflight_still_uses_existing_pre_run_rejection(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-live-secret")
    transport = RecordingDeepSeekTransport(b"must-not-be-called")
    configs = _install_fake_live_builder(monkeypatch, transport)

    exit_code = cli.main(("search", "--live-intent", "--query", ""))
    payload, _stderr = _captured_rejection(capsys)

    assert exit_code == 2
    assert len(configs) == 1
    assert payload["type"] == "request_rejected"
    assert payload["errors"][0]["field"] == "query"
    assert transport.calls == []
    assert RequestRejected.model_validate_json(json.dumps(payload))


@pytest.mark.spec("GLO-M1C-NFR-001")
def test_live_help_discloses_explicit_query_transfer(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(("search", "--help"))

    captured = capsys.readouterr()
    assert raised.value.code == 0
    assert "--live-intent" in captured.out
    assert "complete query" in captured.out
    assert "DeepSeek" in captured.out
    assert captured.err == ""


@pytest.mark.spec("GLO-M1C-NFR-004")
@pytest.mark.parametrize("run_timeout_seconds", [1, 15])
def test_live_api_rejects_a_timeout_that_cannot_outlive_the_model_deadline(
    monkeypatch: pytest.MonkeyPatch,
    run_timeout_seconds: int,
) -> None:
    from glodex.api import live_app
    from glodex.api.settings import ApiSettings

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-live-secret")
    builder_calls = 0

    def forbidden_builder(*_args: object, **_kwargs: object) -> object:
        nonlocal builder_calls
        builder_calls += 1
        raise AssertionError("invalid API timeout reached live composition")

    monkeypatch.setattr(live_app, "build_live_intent_service", forbidden_builder)

    with pytest.raises(DeepSeekPreflightError) as raised:
        live_app.create_live_app(
            settings=ApiSettings(run_timeout_seconds=run_timeout_seconds),
            config=load_config(environ={}),
        )

    assert raised.value.code == "INTENT_LIVE_CONFIG_INVALID"
    assert builder_calls == 0


@pytest.mark.spec("GLO-M1C-NFR-001", "GLO-M1C-NFR-004")
def test_live_api_preflights_credentials_before_creating_an_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from glodex.api.live_app import create_live_app

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    with pytest.raises(DeepSeekPreflightError) as raised:
        create_live_app(config=load_config(environ={}))

    assert raised.value.code == "INTENT_LIVE_CREDENTIALS_MISSING"


@pytest.mark.spec("GLO-M1C-NFR-006")
def test_live_api_reuses_the_shared_builder_and_clock_run_id_instances(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from glodex.api import live_app
    from glodex.api.settings import ApiSettings

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-live-secret")
    transport = RecordingDeepSeekTransport(b"unused")
    _install_fake_http_transport(monkeypatch, transport)
    real_builder = bootstrap.build_live_intent_service
    clock = object()
    run_id_provider = object()
    builder_arguments: list[tuple[object, object]] = []

    def build(
        config: GlodexConfig,
        *,
        clock: object,
        run_id_provider: object,
    ) -> object:
        builder_arguments.append((clock, run_id_provider))
        return real_builder(
            config,
            clock=clock,
            run_id_provider=run_id_provider,
        )

    monkeypatch.setattr(live_app, "build_live_intent_service", build)
    app = live_app.create_live_app(
        settings=ApiSettings(run_timeout_seconds=16),
        config=load_config(environ={}),
        clock=clock,
        run_id_provider=run_id_provider,
    )

    assert builder_arguments == [(clock, run_id_provider)]
    assert app.state.registry._clock is clock
    assert app.state.coordinator._run_id_provider is run_id_provider
    assert app.state.coordinator._service.clock is clock
    assert app.state.coordinator._service.run_id_provider is run_id_provider
    assert transport.calls == []


@pytest.mark.spec("GLO-M1C-NFR-001")
def test_live_factory_preserves_the_existing_openapi_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from glodex.api import live_app
    from glodex.api.app import create_app

    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-live-secret")
    transport = RecordingDeepSeekTransport(b"unused")
    _install_fake_http_transport(monkeypatch, transport)
    real_builder = bootstrap.build_live_intent_service

    def build(config: GlodexConfig, **kwargs: object) -> object:
        return real_builder(config, **kwargs)

    monkeypatch.setattr(live_app, "build_live_intent_service", build)
    config = load_config(environ={})

    default_schema = create_app(config=config).openapi()
    live_schema = live_app.create_live_app(config=config).openapi()

    assert live_schema == default_schema
