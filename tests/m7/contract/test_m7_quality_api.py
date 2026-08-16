"""M7 generation stays offline while the browser can read one safe summary."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from glodex.memory.identity import LocalSession, LocalUser, hash_password, session_token_hash
from glodex.quality.runtime import (
    M7JudgeStatus,
    M7OfflineSummary,
    M7P2Dimension,
    M7P2ReasonCode,
    M7P2Score,
)
from tests.m5.test_m5_identity import _app, _IdentityStore

pytestmark = [
    pytest.mark.contract,
    pytest.mark.spec(
        "GLO-M7-P0-001",
        "GLO-M7-P0-006",
        "GLO-M7-P0-007",
        "GLO-M7-NFR-002",
        "M7-AC-004",
    ),
]

_ROOT = Path(__file__).resolve().parents[3]


def test_m7_generation_is_offline_and_browser_reads_only_a_safe_summary() -> None:
    durable = (_ROOT / "src/glodex/api/durable.py").read_text(encoding="utf-8")
    console = (_ROOT / "src/glodex/api/console_app.py").read_text(encoding="utf-8")
    service = (_ROOT / "src/glodex/runtime/service.py").read_text(encoding="utf-8")
    frontend = (_ROOT / "frontend/src/service/agent.ts").read_text(encoding="utf-8")
    assert '@_ROUTER.get("/m7/runs/{run_id}/summary"' in durable
    assert '@_ROUTER.get("/runs/{run_id}/evaluation-summary")' in console
    assert "quality_writer" not in service
    assert "M7QualityPanel" not in frontend
    assert "needCoverage" in frontend
    assert "evaluation-summary" not in frontend
    assert "CATEGORY_CONSTRAINT" not in frontend
    assert "rubric_fingerprint" not in frontend


@pytest.mark.acceptance
def test_m7_summary_route_is_owner_scoped_and_omits_private_report_content() -> None:
    class _SummaryStore(_IdentityStore):
        async def load_m7_offline_summary(self, *, run_id: str) -> M7OfflineSummary | None:
            if run_id != "run-m5":
                return None
            return M7OfflineSummary(
                run_id=run_id,
                judge_status=M7JudgeStatus.SCORED,
                p0_passed=True,
                p0_failure_count=0,
                p1_failure_count=0,
                p2_scores=tuple(
                    M7P2Score(dimension, score, reason)
                    for dimension, score, reason in (
                        (M7P2Dimension.NEED_COVERAGE, 4, M7P2ReasonCode.STRONG),
                        (M7P2Dimension.SCENARIO_FIT, 3, M7P2ReasonCode.PARTIAL),
                        (M7P2Dimension.DECISION_VALUE, 3, M7P2ReasonCode.PARTIAL),
                    )
                ),
                reward=67,
                training_candidate=False,
                judge_model="test-model",
                generated_at=datetime(2026, 8, 2, tzinfo=UTC),
            )

    store = _SummaryStore()
    owner = LocalUser(user_id="user-" + "1" * 32, username="owner.m7")
    other = LocalUser(user_id="user-" + "2" * 32, username="other.m7")
    store.users[owner.user_id] = (owner, hash_password("owner-passphrase"))
    store.users[other.user_id] = (other, hash_password("other-passphrase"))
    store.sessions[session_token_hash("a" * 32)] = LocalSession(
        user=owner,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    store.sessions[session_token_hash("b" * 32)] = LocalSession(
        user=other,
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    store.owners["thread-m5-owned"] = owner.user_id

    async def scenario() -> None:
        transport = httpx.ASGITransport(app=_app(store), raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://durable.test") as client:
            assert (await client.get("/api/v1/m7/runs/run-m5/summary")).status_code == 401
            client.cookies.set("glodex_local_session", "b" * 32)
            assert (await client.get("/api/v1/m7/runs/run-m5/summary")).status_code == 403
            client.cookies.set("glodex_local_session", "a" * 32)
            response = await client.get("/api/v1/m7/runs/run-m5/summary")
            assert response.status_code == 200
            payload = response.json()
            assert payload["reward"] == 67
            assert [value["score"] for value in payload["p2Scores"]] == [4, 3, 3]
            for private_key in ("query", "answer", "trace", "rubric", "products"):
                assert private_key not in payload

    asyncio.run(scenario())
