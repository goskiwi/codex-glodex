from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).parents[3]
sys.path.insert(0, str(PROJECT_ROOT))

import scripts.verify_m0 as verify  # noqa: E402

pytestmark = [
    pytest.mark.unit,
    pytest.mark.spec("GLO-P0-012"),
]


def test_phase_a_profile_contains_every_approved_gate() -> None:
    commands = tuple(step.command for step in verify.steps_for_phase("A"))

    assert ("uv", "lock", "--check") in commands
    assert ("uv", "run", "--locked", "ruff", "format", "--check", ".") in commands
    assert ("uv", "run", "--locked", "ruff", "check", ".") in commands
    assert ("uv", "run", "--locked", "mypy") in commands
    assert (
        "uv",
        "run",
        "--locked",
        "pytest",
        "-q",
        "tests/architecture",
    ) in commands
    assert (
        "uv",
        "run",
        "--locked",
        "python",
        "scripts/check_traceability.py",
        "--profile",
        "m0",
        "--mode",
        "references",
    ) in commands
    assert (
        "uv",
        "run",
        "--locked",
        "pytest",
        "-q",
        "tests",
        "--ignore=tests/m1a",
        "--ignore=tests/m1b",
        "--ignore=tests/m1c",
        "--ignore=tests/m1d",
        "--ignore=tests/m1e",
    ) in commands
    assert all(isinstance(command, tuple) for command in commands)


def test_historical_profiles_exclude_later_milestones_from_whole_suite_collection() -> None:
    for phase in ("A", "B", "C", "D"):
        whole_suite_commands = tuple(
            step.command
            for step in verify.steps_for_phase(phase)
            if "pytest" in step.command and "tests" in step.command
        )

        assert whole_suite_commands
        assert all("--ignore=tests/m1a" in command for command in whole_suite_commands)
        assert all("--ignore=tests/m1b" in command for command in whole_suite_commands)
        assert all("--ignore=tests/m1c" in command for command in whole_suite_commands)
        assert all("--ignore=tests/m1d" in command for command in whole_suite_commands)
        assert all("--ignore=tests/m1e" in command for command in whole_suite_commands)


def test_phase_b_profile_includes_phase_a_and_every_approved_phase_b_gate() -> None:
    phase_a = verify.steps_for_phase("A")
    phase_b = verify.steps_for_phase("B")
    commands = tuple(step.command for step in phase_b)

    assert phase_b[: len(phase_a)] == phase_a
    assert (
        "uv",
        "run",
        "--locked",
        "pytest",
        "-q",
        "tests/unit/domain/test_intent.py",
        "tests/unit/adapters/test_rule_intent.py",
        "tests/contract/test_intent_interpreter_contract.py",
    ) in commands
    assert (
        "uv",
        "run",
        "--locked",
        "pytest",
        "-q",
        "tests/contract/test_local_snapshot_manifest.py",
        "tests/contract/test_local_snapshot_quarantine.py",
    ) in commands
    assert (
        "uv",
        "run",
        "--locked",
        "pytest",
        "-q",
        "tests/unit/domain/test_catalog_aggregation.py",
    ) in commands
    assert (
        "uv",
        "run",
        "--locked",
        "pytest",
        "-q",
        "tests/contract/test_m0_snapshot_fixture.py",
    ) in commands
    assert (
        "uv",
        "run",
        "--locked",
        "pytest",
        "-q",
        "tests/contract/test_search_service_phase_b.py",
    ) in commands
    assert (
        "uv",
        "run",
        "--locked",
        "pytest",
        "-q",
        "tests/acceptance/test_ac_004_009_010.py",
    ) in commands


def test_phase_c_profile_includes_phase_b_and_exact_phase_c_gates() -> None:
    phase_b = verify.steps_for_phase("B")
    phase_c = verify.steps_for_phase("C")

    assert phase_c[: len(phase_b)] == phase_b
    assert tuple(step.command for step in phase_c[len(phase_b) :]) == (
        (
            "uv",
            "run",
            "--locked",
            "pytest",
            "-q",
            "tests/unit/domain/test_money_types.py",
            "tests/unit/domain/test_pricing.py",
            "tests/unit/domain/test_eligibility_order.py",
            "tests/unit/domain/test_product_gates.py",
            "tests/unit/domain/test_offer_gates.py",
            "tests/unit/domain/test_eligibility_pipeline.py",
            "tests/unit/domain/test_filter_summary.py",
        ),
        (
            "uv",
            "run",
            "--locked",
            "pytest",
            "-q",
            "tests/acceptance/test_ac_002_003_005_006.py",
        ),
    )


def test_phase_d_profile_includes_phase_c_and_exact_phase_d_gates() -> None:
    phase_c = verify.steps_for_phase("C")
    phase_d = verify.steps_for_phase("D")

    assert phase_d[: len(phase_c)] == phase_c
    assert tuple(step.command for step in phase_d[len(phase_c) :]) == (
        (
            "uv",
            "run",
            "--locked",
            "pytest",
            "-q",
            "tests/unit/domain/test_ranking_lexical_v1.py",
            "tests/unit/domain/test_ranking_batch.py",
            "tests/contract/test_query_ranker_contract.py",
        ),
        (
            "uv",
            "run",
            "--locked",
            "pytest",
            "-q",
            "tests/unit/domain/test_evidence_models.py",
            "tests/unit/domain/test_verified_claims.py",
            "tests/unit/domain/test_reason_renderer.py",
        ),
        (
            "uv",
            "run",
            "--locked",
            "pytest",
            "-q",
            "tests/unit/domain/test_result_assembly.py",
            "tests/unit/domain/test_final_guard.py",
        ),
        (
            "uv",
            "run",
            "--locked",
            "pytest",
            "-q",
            "tests/contract/test_search_service_phase_d.py",
        ),
        (
            "uv",
            "run",
            "--locked",
            "pytest",
            "-q",
            "tests/acceptance/test_ac_001_007_008_011.py",
        ),
    )


def test_final_m0_profile_contains_every_approved_gate_in_exact_order() -> None:
    commands = tuple(step.command for step in verify.steps_for_m0())

    assert commands == (
        ("uv", "lock", "--check"),
        ("uv", "run", "--locked", "ruff", "format", "--check", "."),
        ("uv", "run", "--locked", "ruff", "check", "."),
        ("uv", "run", "--locked", "mypy"),
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
            "tests/nfr/test_offline_and_external_calls.py",
            "tests/nfr/test_security_boundaries.py",
            "tests/architecture",
        ),
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
            "tests/unit",
            "tests/contract",
            "tests/generated",
        ),
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
            "tests/acceptance",
            "-m",
            "acceptance",
        ),
        (
            "uv",
            "run",
            "--locked",
            "python",
            "scripts/update_goldens.py",
            "--check",
        ),
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
            "tests/nfr/test_determinism.py",
            "-m",
            "nfr",
        ),
        (
            "uv",
            "run",
            "--locked",
            "python",
            "scripts/check_traceability.py",
            "--profile",
            "m0",
            "--mode",
            "coverage",
        ),
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
            "tests/nfr/test_performance.py",
            "-m",
            "performance",
            "-s",
        ),
    )


def test_final_m0_profile_never_updates_goldens_or_selects_around_tests() -> None:
    commands = tuple(step.command for step in verify.steps_for_m0())
    arguments = {argument for command in commands for argument in command}

    assert "--check" in arguments
    assert "--write" not in arguments
    assert "--ignore" not in arguments
    assert "--deselect" not in arguments
    assert "-k" not in arguments
    assert not any("skip" in argument or "xfail" in argument for argument in arguments)


def test_profiles_reject_unimplemented_phase() -> None:
    with pytest.raises(ValueError, match="unsupported verification phase"):
        verify.steps_for_phase("E")


def test_sanitized_environment_removes_external_service_configuration() -> None:
    original = {
        "PATH": "/tools",
        "HOME": "/safe-home",
        "HTTP_PROXY": "http://proxy.invalid",
        "OPENAI_API_KEY": "secret",
        "AWS_REGION": "region",
        "DB_HOST": "db.invalid",
        "DATABASE_URL": "postgresql://invalid",
        "DEEPSEEK_API_KEY": "deepseek-secret",
        "EBAY_APP_ID": "app-secret",
        "EBAY_CERT_ID": "cert-secret",
        "GEMINI_API_KEY": "secret",
        "GLODEX_CONFIG": "/untrusted/config.toml",
        "GLODEX_REFERENCE_CI": "1",
        "MISTRAL_API_KEY": "secret",
        "MODEL_ENDPOINT": "https://model.invalid",
        "MONGO_URI": "mongodb://invalid",
        "OLLAMA_HOST": "http://ollama.invalid",
        "PYTEST_ADDOPTS": "--ignore=tests/nfr",
        "PYTEST_DEBUG": "1",
        "PYTEST_PLUGINS": "untrusted_plugin",
        "PYTHONHOME": "/untrusted/python",
        "PYTHONPATH": "/untrusted/modules",
        "SQLALCHEMY_DATABASE_URI": "sqlite:///invalid.db",
    }

    sanitized = verify.sanitized_environment(original)

    assert sanitized["PATH"] == "/tools"
    assert sanitized["HOME"] == "/safe-home"
    assert sanitized["PYTHONHASHSEED"] == "0"
    assert sanitized["NO_COLOR"] == "1"
    assert sanitized["GLODEX_REFERENCE_CI"] == "1"
    assert "HTTP_PROXY" not in sanitized
    assert "OPENAI_API_KEY" not in sanitized
    assert "AWS_REGION" not in sanitized
    assert "DB_HOST" not in sanitized
    assert "DATABASE_URL" not in sanitized
    assert "DEEPSEEK_API_KEY" not in sanitized
    assert "EBAY_APP_ID" not in sanitized
    assert "EBAY_CERT_ID" not in sanitized
    assert "GEMINI_API_KEY" not in sanitized
    assert "GLODEX_CONFIG" not in sanitized
    assert "MISTRAL_API_KEY" not in sanitized
    assert "MODEL_ENDPOINT" not in sanitized
    assert "MONGO_URI" not in sanitized
    assert "OLLAMA_HOST" not in sanitized
    assert not any(name.upper().startswith("PYTEST_") for name in sanitized)
    assert "PYTHONHOME" not in sanitized
    assert "PYTHONPATH" not in sanitized
    assert "SQLALCHEMY_DATABASE_URI" not in sanitized
    assert original["OPENAI_API_KEY"] == "secret"
    assert original["DEEPSEEK_API_KEY"] == "deepseek-secret"


@pytest.mark.parametrize(
    ("body", "summary"),
    [
        ('@pytest.mark.skip(reason="disabled")\ndef test_case():\n    pass\n', "1 skipped"),
        ('pytest.skip("disabled", allow_module_level=True)\n', "1 skipped"),
        (
            '@pytest.mark.xfail(reason="known")\ndef test_case():\n    assert False\n',
            "1 xfailed",
        ),
        (
            '@pytest.mark.xfail(reason="known")\ndef test_case():\n    pass\n',
            "1 xpassed",
        ),
    ],
)
def test_strict_pytest_plugin_rejects_non_executed_outcomes_in_real_subprocess(
    tmp_path: Path,
    body: str,
    summary: str,
) -> None:
    test_file = tmp_path / "test_strict_outcome.py"
    test_file.write_text(f"import pytest\n\n{body}", encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "scripts.verify_m0",
            "-c",
            str(PROJECT_ROOT / "pyproject.toml"),
            str(test_file),
        ],
        cwd=PROJECT_ROOT,
        env=verify.sanitized_environment(os.environ),
        check=False,
        capture_output=True,
        text=True,
    )

    output = completed.stdout + completed.stderr
    assert completed.returncode == int(pytest.ExitCode.TESTS_FAILED), output
    assert summary in output


def test_strict_pytest_plugin_allows_a_fully_executed_passing_session(
    tmp_path: Path,
) -> None:
    test_file = tmp_path / "test_strict_pass.py"
    test_file.write_text("def test_case():\n    pass\n", encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "scripts.verify_m0",
            "-c",
            str(PROJECT_ROOT / "pyproject.toml"),
            str(test_file),
        ],
        cwd=PROJECT_ROOT,
        env=verify.sanitized_environment(os.environ),
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == int(pytest.ExitCode.OK), completed.stdout + completed.stderr


def test_runner_uses_argument_arrays_fixed_root_and_no_shell(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_run(
        command: list[str],
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(verify.subprocess, "run", fake_run)
    steps = (
        verify.VerificationStep(name="first", command=("tool", "first")),
        verify.VerificationStep(name="second", command=("tool", "second")),
    )

    exit_code = verify.run_verification(
        steps,
        phase="D",
        project_root=tmp_path,
        environ={"PATH": "/tools", "OPENAI_API_KEY": "secret"},
    )

    assert exit_code == 0
    assert [command for command, _kwargs in calls] == [
        ["tool", "first"],
        ["tool", "second"],
    ]
    for _command, kwargs in calls:
        assert kwargs["cwd"] == tmp_path.resolve()
        assert kwargs["shell"] is False
        assert kwargs["check"] is False
        assert "OPENAI_API_KEY" not in kwargs["env"]
    output = capsys.readouterr().out
    assert (
        "Phase D verification passed "
        "(ranking, evidence, and final result assembly gate; full M0 is not complete)." in output
    )
    assert "M0 complete" not in output


def test_runner_fails_fast_and_preserves_nonzero_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    def fake_run(
        command: list[str],
        **_kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return_code = 7 if command[-1] == "fail" else 0
        return subprocess.CompletedProcess(command, return_code)

    monkeypatch.setattr(verify.subprocess, "run", fake_run)
    steps = (
        verify.VerificationStep(name="ok", command=("tool", "ok")),
        verify.VerificationStep(name="broken", command=("tool", "fail")),
        verify.VerificationStep(name="never", command=("tool", "never")),
    )

    exit_code = verify.run_verification(
        steps,
        phase="A",
        project_root=tmp_path,
        environ={"PATH": "/tools"},
    )

    assert exit_code == 7
    assert calls == [["tool", "ok"], ["tool", "fail"]]


def test_main_dispatches_the_phase_a_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_verification(
        steps: tuple[verify.VerificationStep, ...],
        *,
        phase: str,
        project_root: Path = verify.PROJECT_ROOT,
        environ: dict[str, str] | None = None,
    ) -> int:
        captured["steps"] = steps
        captured["phase"] = phase
        captured["project_root"] = project_root
        captured["environ"] = environ
        return 0

    monkeypatch.setattr(verify, "run_verification", fake_verification)

    assert verify.main(["--phase", "A"]) == 0
    assert captured["phase"] == "A"
    assert captured["steps"] == verify.steps_for_phase("A")


def test_main_dispatches_the_phase_b_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_verification(
        steps: tuple[verify.VerificationStep, ...],
        *,
        phase: str,
        project_root: Path = verify.PROJECT_ROOT,
        environ: dict[str, str] | None = None,
    ) -> int:
        captured["steps"] = steps
        captured["phase"] = phase
        captured["project_root"] = project_root
        captured["environ"] = environ
        return 0

    monkeypatch.setattr(verify, "run_verification", fake_verification)

    assert verify.main(["--phase", "B"]) == 0
    assert captured["phase"] == "B"
    assert captured["steps"] == verify.steps_for_phase("B")


def test_main_dispatches_the_phase_c_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_verification(
        steps: tuple[verify.VerificationStep, ...],
        *,
        phase: str,
        project_root: Path = verify.PROJECT_ROOT,
        environ: dict[str, str] | None = None,
    ) -> int:
        captured["steps"] = steps
        captured["phase"] = phase
        captured["project_root"] = project_root
        captured["environ"] = environ
        return 0

    monkeypatch.setattr(verify, "run_verification", fake_verification)

    assert verify.main(["--phase", "C"]) == 0
    assert captured["phase"] == "C"
    assert captured["steps"] == verify.steps_for_phase("C")


def test_main_dispatches_the_phase_d_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_verification(
        steps: tuple[verify.VerificationStep, ...],
        *,
        phase: str,
        project_root: Path = verify.PROJECT_ROOT,
        environ: dict[str, str] | None = None,
    ) -> int:
        captured["steps"] = steps
        captured["phase"] = phase
        captured["project_root"] = project_root
        captured["environ"] = environ
        return 0

    monkeypatch.setattr(verify, "run_verification", fake_verification)

    assert verify.main(["--phase", "D"]) == 0
    assert captured["phase"] == "D"
    assert captured["steps"] == verify.steps_for_phase("D")


def test_main_defaults_to_the_final_m0_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_verification(
        steps: tuple[verify.VerificationStep, ...],
        *,
        phase: str,
        project_root: Path = verify.PROJECT_ROOT,
        environ: dict[str, str] | None = None,
    ) -> int:
        captured["steps"] = steps
        captured["phase"] = phase
        captured["project_root"] = project_root
        captured["environ"] = environ
        return 0

    monkeypatch.setattr(verify, "run_verification", fake_verification)

    assert verify.main([]) == 0
    assert captured["phase"] == "M0"
    assert captured["steps"] == verify.steps_for_m0()
