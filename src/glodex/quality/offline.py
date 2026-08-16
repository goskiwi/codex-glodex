"""Build a complete offline M7 evaluation input from durable run truth."""

from __future__ import annotations

import json
from dataclasses import replace

from glodex.agent.contracts import (
    AgentAnswerKind,
    AgentDemoResponse,
    SemanticAssertionCandidate,
    SemanticAssertionInput,
)
from glodex.agent.ports import SemanticAssertionPort
from glodex.observability.runtime import M6RunTrace
from glodex.quality.runtime import (
    M7EvaluationFacts,
    M7NeedFact,
    M7NeedImportance,
    M7NeedStatus,
    M7ProductFact,
    M7TraceFact,
)
from glodex.runtime.contracts import DurableRun, DurableRunState
from glodex.runtime.request_payload import search_request_from_durable_payload


def facts_from_durable_run(*, run: DurableRun, trace: M6RunTrace | None) -> M7EvaluationFacts:
    """Rehydrate canonical JSON and bind every evaluated claim to a product/evidence record."""

    if run.state is not DurableRunState.COMPLETED or run.terminal_response is None:
        raise ValueError("M7 offline evaluation requires one completed run")
    response = AgentDemoResponse.model_validate_json(
        json.dumps(run.terminal_response, ensure_ascii=False, separators=(",", ":"))
    )
    if (
        response.answer is None
        or response.answer.kind is not AgentAnswerKind.SHOPPING_SUMMARY
        or response.search_response is None
        or response.status.value != run.state.value
    ):
        raise ValueError("M7 offline evaluation requires a shopping result")
    request = search_request_from_durable_payload(run.request_payload)
    query = request.query
    top_k = request.top_k
    search = response.search_response
    products = tuple(
        M7ProductFact(
            product_id=result.product_id,
            title=result.title,
            category=result.category,
            landed_cost=f"{result.landed_cost.display} {result.landed_cost.currency}",
            reason=result.reason,
            unknowns=result.unknowns,
            evidence_ids=tuple(value.evidence_id for value in result.evidence),
        )
        for result in search.results
    )
    needs = tuple(
        M7NeedFact(
            label=criterion.source_span.text,
            kind=criterion.kind,
            importance=M7NeedImportance.REQUIRED,
            product_statuses=tuple(
                M7NeedStatus.VERIFIED
                if criterion.kind in result.matched_requirements
                else M7NeedStatus.UNVERIFIED
                for result in search.results
            ),
        )
        for criterion in search.interpreted_request.required
    ) + tuple(
        M7NeedFact(
            label=criterion.source_span.text,
            kind=criterion.kind,
            importance=M7NeedImportance.PREFERRED,
            product_statuses=tuple(
                M7NeedStatus.UNVERIFIED
                if f"未证实偏好：{criterion.source_span.text}"  # noqa: RUF001
                in result.unknowns
                else M7NeedStatus.VERIFIED
                for result in search.results
            ),
        )
        for criterion in search.interpreted_request.preferred
    )
    expected_product_ids = tuple(result.product_id for result in search.results)
    expected_evidence_ids = tuple(
        dict.fromkeys(value.evidence_id for result in search.results for value in result.evidence)
    )
    output_contract_valid = (
        response.selected_product_ids == expected_product_ids
        and response.evidence_ids == expected_evidence_ids
        and len(products) <= top_k
    )
    category_needs = tuple(value for value in needs if value.kind == "target_category")
    budget_needs = tuple(value for value in needs if value.kind.startswith("budget"))
    return M7EvaluationFacts(
        run_id=run.run_id,
        query=query,
        answer=response.answer.text,
        terminal_state=run.state.value,
        top_k=top_k,
        needs=needs,
        products=products,
        tool_names=tuple(value.tool_name.value for value in response.tool_summary),
        trace=()
        if trace is None
        else tuple(
            M7TraceFact(
                sequence=value.sequence,
                kind=value.draft.kind.value,
                operation=None if value.draft.operation is None else value.draft.operation.value,
                outcome=None if value.draft.outcome is None else value.draft.outcome.value,
                safe_code=value.draft.safe_code,
            )
            for value in trace.events
        ),
        # Category relevance must be positively established by the semantic
        # assertion below.  An absent target_category need is never a pass.
        category_integrity=bool(category_needs) and _all_verified(category_needs),
        budget_integrity=_all_verified(budget_needs),
        exclusion_integrity=output_contract_valid,
        evidence_integrity=output_contract_valid and all(value.evidence_ids for value in products),
        public_output_safe="\0" not in response.answer.text,
        output_contract_valid=output_contract_valid,
    )


def _all_verified(needs: tuple[M7NeedFact, ...]) -> bool:
    return not needs or all(
        all(status is M7NeedStatus.VERIFIED for status in need.product_statuses) for need in needs
    )


async def assert_category_integrity(
    *,
    facts: M7EvaluationFacts,
    semantic_assertion: SemanticAssertionPort,
) -> M7EvaluationFacts:
    """Bind M7's category P0 to a fresh semantic assertion over final products."""

    assertion = await semantic_assertion.verify(
        SemanticAssertionInput(
            query=facts.query,
            category=facts.query[:128],
            candidates=tuple(
                SemanticAssertionCandidate(
                    candidate_id=product.product_id,
                    title=product.title,
                )
                for product in facts.products
            ),
        )
    )
    relevant = set(assertion.relevant_candidate_ids)
    expected = {product.product_id for product in facts.products}
    return replace(facts, category_integrity=relevant == expected)


__all__ = ["assert_category_integrity", "facts_from_durable_run"]
