from __future__ import annotations

import asyncio

import pytest

from glodex.adapters.rule_intent import RuleIntentInterpreter
from glodex.application.ports import IntentInterpreter
from glodex.contracts import SearchRequest
from glodex.domain.intent import InterpretedRequest, validate_interpreted_request

pytestmark = [
    pytest.mark.contract,
    pytest.mark.spec("GLO-P0-002", "AC-009", "GLO-NFR-011"),
]


def test_rule_interpreter_satisfies_the_async_port_and_independent_validator() -> None:
    interpreter: IntentInterpreter = RuleIntentInterpreter()
    request = SearchRequest(query="推荐 800 美元以内、有库存、适合出差的轻薄本")

    interpreted = asyncio.run(interpreter.interpret(request))

    assert isinstance(interpreted, InterpretedRequest)
    validation = validate_interpreted_request(request.query, interpreted)
    assert validation.is_valid
    assert validation.interpreted_request is interpreted


def test_interpreter_does_not_invent_required_constraints_for_unknown_text() -> None:
    interpreter = RuleIntentInterpreter()
    request = SearchRequest(query="随便看看")

    interpreted = asyncio.run(interpreter.interpret(request))

    assert interpreted.required == ()
    assert interpreted.preferred == ()
    assert validate_interpreted_request(request.query, interpreted).is_valid
