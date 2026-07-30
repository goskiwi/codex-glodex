"""Acceptance coverage for the explicit operator-only Agent CLI."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from glodex import cli
from glodex.application.agent.contracts import (
    AgentAnswer,
    AgentAnswerKind,
    AgentDemoResponse,
    AgentExecution,
    AgentRunRecord,
)
from glodex.config import GlodexConfig
from glodex.contracts import RunStatus, SearchRequest

pytestmark = [
    pytest.mark.acceptance,
    pytest.mark.spec(
        "GLO-M1D-P0-001",
        "GLO-M1D-P0-006",
        "GLO-M1D-NFR-001",
        "GLO-M1D-NFR-005",
    ),
]

_PROJECT_ROOT = Path(__file__).parents[3]


def _execution(status: RunStatus) -> AgentExecution:
    run_id = "agent-run-cli-1"
    if status is RunStatus.FAILED:
        response = AgentDemoResponse(run_id=run_id, status=status)
        terminal_code = "MODEL_INVALID"
    else:
        assert status is RunStatus.COMPLETED
        response = AgentDemoResponse(
            run_id=run_id,
            status=status,
            answer=AgentAnswer(
                kind=AgentAnswerKind.CHAT_FALLBACK,
                text="这是服务器生成的安全回复。",
            ),
        )
        terminal_code = None
    return AgentExecution(
        response=response,
        record=AgentRunRecord(
            run_id=run_id,
            status=status,
            model_calls=0,
            tool_calls=0,
            child_runs=0,
            terminal_code=terminal_code,
        ),
    )


@dataclass
class _FakeService:
    execution: AgentExecution
    requests: list[SearchRequest] = field(default_factory=list)

    async def execute(self, request: SearchRequest) -> AgentExecution:
        self.requests.append(request)
        return self.execution


@dataclass
class _FakeFactory:
    execution: AgentExecution
    calls: list[dict[str, object]] = field(default_factory=list)
    service: _FakeService | None = None

    async def __call__(
        self,
        config: GlodexConfig,
        *,
        live_data: bool,
        output_root: Path | None,
        preflight_query: str,
    ) -> _FakeService:
        self.calls.append(
            {
                "config": config,
                "live_data": live_data,
                "output_root": output_root,
                "preflight_query": preflight_query,
            }
        )
        self.service = _FakeService(self.execution)
        return self.service


def _single_payload(
    capsys: pytest.CaptureFixture[str],
) -> tuple[dict[str, object], str]:
    captured = capsys.readouterr()
    lines = captured.out.splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert isinstance(payload, dict)
    return payload, captured.err


def test_demo_mode_is_explicit_fixed_and_emits_one_agent_result_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    factory = _FakeFactory(_execution(RunStatus.COMPLETED))

    exit_code = cli.main(
        (
            "agent-demo",
            "--live",
            "--query",
            "  推荐手机  ",
            "--currency",
            "CNY",
            "--top-k",
            "2",
        ),
        agent_service_factory=factory,
    )
    payload, stderr = _single_payload(capsys)

    assert exit_code == 0
    assert stderr == ""
    assert AgentDemoResponse.model_validate_json(json.dumps(payload)).status is RunStatus.COMPLETED
    assert len(factory.calls) == 1
    assert factory.calls[0]["live_data"] is False
    assert factory.calls[0]["output_root"] is None
    assert factory.calls[0]["preflight_query"] == "推荐手机"
    assert factory.service is not None
    assert factory.service.requests == [
        SearchRequest(
            query="推荐手机",
            locale="zh-CN",
            display_currency="CNY",
            top_k=2,
            snapshot_version="m1d-demo-v1",
        )
    ]


def test_live_data_mode_is_snapshot_exclusive_and_passes_the_external_root(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output_root = tmp_path / "agent-output"
    output_root.mkdir()
    output_root.chmod(0o700)
    factory = _FakeFactory(_execution(RunStatus.COMPLETED))

    exit_code = cli.main(
        (
            "agent-demo",
            "--live",
            "--live-data",
            "--query",
            "在 eBay 找手机并参考近期评测",
            "--output-root",
            str(output_root),
        ),
        agent_service_factory=factory,
    )
    payload, stderr = _single_payload(capsys)

    assert exit_code == 0
    assert stderr == ""
    assert payload["status"] == "COMPLETED"
    assert factory.calls[0]["live_data"] is True
    assert factory.calls[0]["output_root"] == output_root
    assert factory.service is not None
    assert factory.service.requests[0].snapshot_version is None
    assert factory.service.requests[0].display_currency == "CNY"


def test_agent_failed_maps_to_exit_one_without_exposing_the_record(
    capsys: pytest.CaptureFixture[str],
) -> None:
    factory = _FakeFactory(_execution(RunStatus.FAILED))

    exit_code = cli.main(
        ("agent-demo", "--live", "--query", "推荐手机"),
        agent_service_factory=factory,
    )
    payload, stderr = _single_payload(capsys)

    assert exit_code == 1
    assert stderr == ""
    assert payload["status"] == "FAILED"
    assert "record" not in payload
    assert "terminal_code" not in payload


@pytest.mark.parametrize(
    "arguments",
    [
        ("agent-demo", "--query", "推荐手机"),
        (
            "agent-demo",
            "--live",
            "--query",
            "推荐手机",
            "--snapshot",
            "m0-v1",
        ),
        (
            "agent-demo",
            "--live",
            "--live-data",
            "--query",
            "推荐手机",
            "--snapshot",
            "m1d-demo-v1",
            "--output-root",
            "/tmp/agent-output",
        ),
        ("agent-demo", "--live", "--live-data", "--query", "推荐手机"),
        (
            "agent-demo",
            "--live",
            "--query",
            "推荐手机",
            "--output-root",
            "/tmp/agent-output",
        ),
        ("agent-demo", "--live", "--query", "推荐手机", "--top-k", "4"),
        ("agent-demo", "--live", "--query", "", "--top-k", "3"),
        (
            "agent-demo",
            "--live",
            "--query",
            "推荐手机",
            "--provider",
            "another-provider",
        ),
        (
            "agent-demo",
            "--live",
            "--query",
            "推荐手机",
            "--model",
            "another-model",
        ),
    ],
)
def test_invalid_mode_request_or_provider_switch_is_pre_run_rejected(
    arguments: tuple[str, ...],
    capsys: pytest.CaptureFixture[str],
) -> None:
    factory = _FakeFactory(_execution(RunStatus.COMPLETED))

    exit_code = cli.main(arguments, agent_service_factory=factory)
    payload, stderr = _single_payload(capsys)

    assert exit_code == 2
    assert payload["type"] == "request_rejected"
    assert "run_id" not in payload
    assert payload["errors"]
    assert factory.calls == []
    assert "Traceback" not in stderr


def test_composition_preflight_failure_is_safe_and_has_no_run_id(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from glodex.agent_bootstrap import AgentPreflightError

    async def rejected_factory(
        config: GlodexConfig,
        *,
        live_data: bool,
        output_root: Path | None,
        preflight_query: str,
    ) -> object:
        del config, live_data, output_root, preflight_query
        raise AgentPreflightError("AGENT_INDEX_INVALID")

    exit_code = cli.main(
        ("agent-demo", "--live", "--query", "推荐手机"),
        agent_service_factory=rejected_factory,
    )
    payload, stderr = _single_payload(capsys)

    assert exit_code == 2
    assert payload == {
        "type": "request_rejected",
        "errors": [
            {
                "field": "agent",
                "code": "AGENT_INDEX_INVALID",
                "message": "Agent demo indexes are invalid.",
            }
        ],
    }
    assert "run_id" not in payload
    assert "Traceback" not in stderr


@pytest.mark.spec("M1D-AC-001")
def test_old_command_ignores_agent_credentials_and_composition_factory(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for name in (
        "DEEPSEEK_API_KEY",
        "DASHSCOPE_API_KEY",
        "TAVILY_API_KEY",
        "EBAY_APP_ID",
        "EBAY_CERT_ID",
    ):
        monkeypatch.setenv(name, "must-be-ignored")
    factory_calls = 0

    async def forbidden_factory(*args: object, **kwargs: object) -> object:
        nonlocal factory_calls
        del args, kwargs
        factory_calls += 1
        raise AssertionError("default CLI activated Agent composition")

    exit_code = cli.main(("demo",), agent_service_factory=forbidden_factory)
    payload, stderr = _single_payload(capsys)

    assert exit_code == 0
    assert stderr == ""
    assert payload["status"] == "COMPLETED"
    assert payload["algorithm_version"] == "phase-d-v1"
    assert factory_calls == 0


def test_live_composition_creates_a_fresh_capture_source_for_each_root_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from glodex.adapters.agent_item_search import LiveEbayItemSource
    from glodex.agent_bootstrap import PerRunAgentExecutor, build_agent_service
    from glodex.application.agent.runtime import AgentService
    from glodex.application.agent.tools import ToolDependencies

    for name in ("DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY", "TAVILY_API_KEY"):
        monkeypatch.setenv(name, "test-credential")
    output_root = tmp_path / "agent-live-output"
    output_root.mkdir()
    output_root.chmod(0o700)
    config = GlodexConfig(
        data_dir=_PROJECT_ROOT / "data" / "snapshots",
        default_snapshot="m0-v1",
        default_locale="zh-CN",
        default_currency="USD",
        default_top_k=3,
        fingerprint="0" * 64,
    )
    request = SearchRequest(
        query="在 eBay 找手机",
        locale="zh-CN",
        display_currency="CNY",
        top_k=3,
        snapshot_version=None,
    )

    executor = asyncio.run(
        build_agent_service(
            config,
            live_data=True,
            output_root=output_root,
            preflight_query=request.query,
            project_root=_PROJECT_ROOT,
            ebay_environ={
                "EBAY_APP_ID": "test-app",
                "EBAY_CERT_ID": "test-cert",
            },
        )
    )
    assert type(executor) is PerRunAgentExecutor
    factory = executor._service_factory
    first = factory(request)
    second = factory(request)

    assert type(first) is AgentService
    assert type(second) is AgentService
    assert first is not second
    first_dependencies = first._tool_dependencies
    second_dependencies = second._tool_dependencies
    assert type(first_dependencies) is ToolDependencies
    assert type(second_dependencies) is ToolDependencies
    assert type(first_dependencies.item_source) is LiveEbayItemSource
    assert type(second_dependencies.item_source) is LiveEbayItemSource
    assert first_dependencies.item_source is not second_dependencies.item_source


def test_default_cli_import_does_not_load_agent_or_live_modules() -> None:
    environment = os.environ.copy()
    environment.update(
        {
            "DEEPSEEK_API_KEY": "ignored",
            "DASHSCOPE_API_KEY": "ignored",
            "TAVILY_API_KEY": "ignored",
            "EBAY_APP_ID": "ignored",
            "EBAY_CERT_ID": "ignored",
        }
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json,sys;"
                "import glodex.cli;"
                "names=['glodex.agent_bootstrap',"
                "'glodex.adapters.deepseek_agent',"
                "'glodex.adapters.agent_live_http',"
                "'glodex.application.agent.runtime'];"
                "print(json.dumps({name:name in sys.modules for name in names}))"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.returncode == 0
    assert json.loads(completed.stdout) == {
        "glodex.agent_bootstrap": False,
        "glodex.adapters.deepseek_agent": False,
        "glodex.adapters.agent_live_http": False,
        "glodex.application.agent.runtime": False,
    }
    assert completed.stderr == ""
