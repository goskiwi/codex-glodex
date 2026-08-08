# Semantic CLI And Checkpoint Recovery Design

## Goal

Make the native shopping AgentLoop reachable through meaningful public commands and make its checkpoint contract honest and recoverable. Milestone-coded command names are removed without aliases.

## Public CLI

The only supported service and product-index commands are:

```text
glodex agent-api serve
glodex web-console serve
glodex product-index build
glodex product-index verify
```

`agent-api serve` starts the loopback durable Agent API on port 8766. `web-console serve` starts the loopback AG-UI console on port 8767. `product-index` owns BGE/OpenSearch product-index creation and validation. The old `m2b-serve`, `m2d-serve`, and `m2c-index` spellings are invalid CLI input; there are no aliases, warnings, or migration shims.

The CLI delegates composition to focused modules instead of rebuilding the dependency graph inside `cli.py`. Operator output uses responsibility-based stable codes such as `AGENT_API_UNAVAILABLE`, not milestone names.

## Agent Composition

The Agent API composition constructs `ReActAgentService` as the durable executor, injects the PostgreSQL/Redis stores, model-service retrieval dependencies, safe event projector, memory context, terminal memory writer, and observability dependencies, then passes it to `create_durable_agent_app`. No deterministic or legacy Agent selector is retained as a fallback.

Every root run derives its LangGraph `thread_id` from the durable execution context. Every child retains an independently allocated thread ID. Root and children continue to share the same system prompt and full native tool schema.

## Checkpoint Contract

Checkpoint payloads use one current schema only. A `SearchRequest` is restored through JSON-aware Pydantic validation so JSON arrays round-trip into typed tuples without weakening validation at normal API boundaries.

An AgentLoop checkpoint contains the LangGraph message/checkpoint identity plus the trusted `ShoppingToolSession` state required to continue without replaying completed tools. The durable coordinator passes the latest confirmed payload to `ReActAgentService`; the service rehydrates the session and graph before streaming. Unsupported, incomplete, or mismatched payloads fail closed. The current implementation does not accept legacy payload shapes.

## Testing

Contract tests prove the new commands parse and the old names fail. Composition tests prove `agent-api serve` constructs the native ReAct executor and `web-console serve` binds only loopback ports. Product-index tests cover build and verify dispatch.

Checkpoint tests cover JSON request round-trip, initial resume, resume after confirmed tool state without replay, mismatched identity rejection, and malformed payload rejection. Existing Agent tool, architecture, durable runtime, API, and CLI suites must remain green.

