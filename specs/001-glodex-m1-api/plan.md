# Glodex M1a API 技术实施计划

| 字段 | 值 |
|---|---|
| Plan ID | `GLO-PLAN-001` |
| 版本 | `0.1.2` |
| 状态 | Completed |
| 对应规格 | [`GLO-SPEC-001` v0.3.2](./spec.md) |
| 父基线 | [`GLO-SPEC-000`](../000-glodex-mvp/spec.md) |
| 最后更新 | 2026-07-28 |
| 批准日期 | 2026-07-28 |

## 1. 计划目标

在不改变 M0 领域语义的前提下，增加一个单进程 FastAPI 入站适配器：

```text
POST 创建 Run
    → 后台调用同一个 SearchService
    → RunJournal 增量投影为 glodex.event.v1
    → GET 状态 / GET SSE
```

本 Plan 只设计 M1a 已批准的三个 API、进程内 Run、SSE、重放、错误合同和门禁。
不设计 AG-UI、WebSocket、取消、持久化、前端、LLM 或 Provider。

## 2. 当前代码基线

现有实现提供了可复用基础：

- [`SearchService`](../../src/glodex/application/search_service.py) 返回原子
  `SearchExecution(response, journal)`；
- [`RunJournal`](../../src/glodex/application/journal.py) 已有连续 sequence、
  stage 配对和唯一业务终态；
- [`bootstrap.py`](../../src/glodex/bootstrap.py) 已集中处理 pre-run 校验、
  service 构建、Clock 和 Run ID；
- [`contracts.py`](../../src/glodex/contracts.py) 已提供 strict DTO、
  `Identifier`、`SearchRequest`、`RequestRejected` 与 `SearchResponse`；
- [`verify_m0.py`](../../scripts/verify_m0.py) 已提供离线、无 skip/xfail 的完整门禁。

需要解决两个扩展缝：

1. `SearchService.execute()` 当前在内部生成 `run_id`，API 无法在后台执行前返回同一 ID；
2. Journal 当前只在执行结束后返回，无法证明阶段事件在运行过程中可见。

## 3. 技术选择

| 项目 | 选择 | 原因 |
|---|---|---|
| Python | 保持 `>=3.12,<3.13` | 复用现有工程与 `asyncio`。 |
| HTTP | FastAPI `>=0.135,<1` | 内建 `EventSourceResponse` 与 Pydantic 集成。 |
| SSE | `fastapi.sse` | 不增加 `sse-starlette` 或自写 wire encoder。 |
| ASGI Server | Uvicorn，仅 dev 依赖 | 只用于本地人工演示。 |
| HTTP 测试 | HTTPX，仅 dev 依赖 | 普通请求使用进程内 ASGI transport。 |
| 实时流测试 | 小型直接 ASGI harness | 避免 HTTPX 缓冲流和真实 socket。 |
| 状态存储 | 进程内 `RunRegistry` | 符合 M1a 无数据库、无恢复范围。 |
| 并发模型 | 单 ASGI event loop + tracked tasks | 不引入线程池、队列服务或多 Worker 协调。 |
| 事件合同 | 自有 `glodex.event.v1` | 不引入 AG-UI SDK 或 decoder。 |

依赖改动：

```toml
[project]
dependencies = [
    "fastapi>=0.135,<1",
    "pydantic>=2.12,<3",
]

[dependency-groups]
dev = [
    "httpx>=0.28,<1",
    "mypy>=1.18,<2",
    "pytest>=9,<10",
    "pytest-socket>=0.7,<1",
    "ruff>=0.14,<1",
    "uvicorn>=0.35,<1",
]
```

`uv.lock` 冻结实际解析版本。M1a 不增加 `fastapi[standard]`、AG-UI、
`pytest-asyncio`、`asgi-lifespan` 或额外 SSE 客户端。

## 4. 分层与依赖方向

```text
glodex.api
  ├── FastAPI / SSE / HTTP DTO
  ├── RunRegistry / RunCoordinator
  └── EventProjector
          │
          ▼
glodex.application
  ├── SearchService
  ├── RunJournal
  └── framework-free RunEventObserver port
          │
          ▼
glodex.domain
```

约束：

- `api` 可以依赖 FastAPI、application、contracts、config；
- application 只增加框架无关 observer port，不能导入 `api`、FastAPI 或 Starlette；
- domain 保持当前纯净边界；
- HTTP 层不能重新计算价格、过滤候选、排序、理由或证据；
- `SearchResponse` 始终由 `SearchService` 生成，API 只序列化。

## 5. SearchService 扩展缝

### 5.1 预分配 Run ID

保持 `SearchService.execute(request)` 的现有签名与行为不变，增加一个明确供已预分配
Run 使用的应用入口：

```python
async def execute_run(
    request: SearchRequest,
    *,
    run_id: str,
    observer: RunEventObserver | None = None,
) -> SearchExecution:
    ...
```

- 现有 `execute(request)` 在完成同样的类型检查后，从 `RunIdProvider` 取得 ID，
  再委托给 `execute_run`；
- API 先由 Coordinator 生成并预留 ID，再传给 `execute_run`；
- 显式 ID 必须通过现有 `Identifier`，且不能再次调用 provider；
- `execute_run` 不收紧通用 `Identifier`；Coordinator 在 API 生成边界另外拒绝
  含 `:` 的 ID。默认继续使用符合规则的 `run-<uuid-hex>`；
- `SearchResponse.run_id`、Journal 和 Run Resource 必须使用同一值。

`execute(request)` 与 `search(request)` 的现有签名、校验顺序和公开行为均保持不变。
不使用“可选 run ID”参数，避免同时存在“调用方提供”和“内部生成”的模糊组合。

### 5.2 实时观察口

在 application ports 增加同步、只读、每次执行可选的 `RunEventObserver`：

```python
class RunEventObserver(Protocol):
    def on_event(self, event: RunEvent) -> None: ...
```

- `RUN_STARTED`、`STAGE_STARTED`、`STAGE_COMPLETED`、`STAGE_DEGRADED`
  在对应 Journal 转换后立即通知；
- observer 不能改变 Journal，也不能驱动业务状态；
- API composition 使用隔离包装器捕获投影错误、标记该 Run
  `projection_status=DEGRADED`，不让异常返回 SearchService；
- Journal 业务终态不在 observer 中直接公开。Coordinator 收到完整
  `SearchExecution` 后，再一次提交最终 snapshot 与 terminal event。

非终态 Journal 转换集中通过小型 helper 通知 observer，避免在业务阶段中散落
HTTP 或 SSE 逻辑。

`RunJournal` 的数据结构、状态机、sequence 和零 I/O 特性不做任何修改。

## 6. API 运行时

### 6.1 RunResource

`RunResource` 是 API adapter 内部的可变记录，至少保存：

- thread ID、run ID；
- transport state 与 projection status；
- `SearchResponse | None`；
- 安全 `ApiError | None`；
- 已验证公开事件列表；
- Subscriber 通知句柄；
- 创建、终态和过期所需时间。

它不是领域实体，不保存 `SearchRequest` 或 query，也不进入 `SearchResponse`
或 M0 config fingerprint。

### 6.2 RunRegistry

Registry 由一个 ASGI event loop 独占。所有修改方法均同步完成且内部不 `await`，
从而保证以下操作原子：

- 在 create、status、subscribe 和容量检查入口惰性清理已过期终态；
- 检查活动容量；
- 检查同 Thread 活动冲突；
- 生成/预留 Run；
- 登记 runner。

行为：

- 活动 Run 不因 TTL 或终态容量被驱逐；
- 终态 Run 按终态时间整体过期/驱逐，不截断仍存在 Run 的事件前缀；
- 每次 terminal commit 同步执行终态数量驱逐，避免一批 Run 同时完成后长期超限；
- 单 Run 到达事件上限后停止新增投影并标记 `DEGRADED`，不丢弃已有事件前缀；
- Run 进入任一终态时释放 Thread 活动预留；
- 新进程使用空 Registry，旧 ID 返回 404。

TTL 清理由注入 Clock 驱动，不增加周期 sweeper。GET 仍是业务只读操作，但其
Registry 读取入口会先做同一临界区内的过期 housekeeping，确保过期 ID 立即变成
404。

### 6.3 通知与慢 Subscriber

公开事件列表是重放真相。每个 Subscriber 只持有一个容量为 1 的通知队列：

1. 按自己的 index 读取共享事件列表；
2. 追平后等待通知 token；
3. 新事件到达时使用 `put_nowait`；已有 token 时不重复堆积；
4. 断开时只注销自己的 token queue。

因此慢连接不会产生无界 per-subscriber event queue，也不会阻塞业务执行。

### 6.4 RunCoordinator

Coordinator：

1. 接收已校验请求，预留 Run，并创建一个先等待 start gate 的 tracked task；
2. POST 通过 FastAPI `BackgroundTasks` 为 `202` Response 登记轻量回调；ASGI
   响应体发送完成后，回调只负责释放 start gate；
3. gate 释放后才把资源改为 `RUNNING` 并调用 `SearchService.execute_run`，因此即使
   service double 立即返回，业务也不能先于 `202` 响应完成；
4. 整个 runner 受配置 timeout 约束，响应发送失败导致 gate 未释放时也会有界进入
   `ABORTED`，不会永久占用 Thread 或活动容量；
5. 成功时提交业务 state、原样 response 和最终事件；
6. timeout 或未映射 runner 异常时提交 `ABORTED`，不构造 `SearchResponse`；
7. task 完成后消费异常、从 tracked set 移除并释放对请求闭包的引用。

Registry 永不保存原始请求或 query。已验证请求只由活动 runner 闭包短期持有，
结束后不得由 task、done callback 或异常 traceback 继续保留。

FastAPI lifespan 关闭时停止接单，取消并回收 tracked tasks；进程仍可提交状态时，
活动 Run 进入 `ABORTED`。硬 kill 仍按 Spec 视为资源丢失。

## 7. EventProjector

Projector 使用独立 public sequence，不复用 Journal sequence：

| Journal / Execution | `glodex.event.v1` |
|---|---|
| `run_started` | `RUN_STARTED` |
| `stage_started` | `STEP_STARTED` |
| `stage_completed` | `STEP_FINISHED` |
| `stage_degraded` | `STEP_DEGRADED` |
| 最终 response | `STATE_SNAPSHOT` |
| `COMPLETED` / `NO_MATCH` | `RUN_FINISHED` |
| 业务 `FAILED` | `RUN_ERROR` |
| transport `ABORTED` | `RUN_ERROR`，无 snapshot |

实现顺序：

1. 先构造 strict event DTO；
2. 再执行 JSON-mode 序列化验证；
3. 最后一次性写入 Resource 并通知 Subscriber。

业务终态的 `STATE_SNAPSHOT + terminal` 必须先完整构造，再作为一个同步提交写入；
任何失败都只标记 projection degraded，response 仍提交。`RUN_ERROR` 的业务
code/message 从首个安全 ERROR issue 确定化选择，没有时使用固定安全 fallback。

`STEP_*` 的 `stepName` 直接复制 `RunEvent.stage`，不做展示别名映射。当前
`SearchService` 实际发出的名字固定为 `intent`、`snapshot`、`aggregation`、
`eligibility`、`ranking`、`result_assembly`；合同测试锁定这些值，并明确拒绝
把 `ranking` 改写为 `rank`。

## 8. FastAPI 入口

提供 `create_app(...) -> FastAPI` factory，测试可注入固定 Clock、Run ID、
SearchService double 和小容量 settings。生产模块导入时不加载配置、不启动任务。

路由职责：

- `POST /api/v1/runs`：解析 wrapper，调用现有 `validate_search_request`，
  成功后交给 Coordinator；
- `GET /api/v1/runs/{run_id}`：只读取 RunResource snapshot；
- `GET /api/v1/runs/{run_id}/events`：校验 Accept/cursor，返回
  `EventSourceResponse`。

SSE 使用 `ServerSentEvent(data=..., id=...)`。心跳使用 comment，不占 sequence。
generator 在 `finally` 注销 Subscriber；取消检查点只结束流，不传播给 runner。

统一 exception handlers 将 request validation、404/405 和未映射错误转换为
`glodex.error.v1`。debug traceback、FastAPI 默认 `detail` 和请求 echo 不得公开。
CORS middleware 默认不安装。

## 9. API Settings

API 限制使用独立 strict `ApiSettings`，不加入 `GlodexConfig`，因此不会改变
M0 `config_fingerprint`。

开发默认值：

| 设置 | 默认值 |
|---|---:|
| 最大活动 Run | `16` |
| 最大保留终态 Run | `128` |
| 终态 TTL | `900s` |
| 单 Run 最大事件 | `128` |
| 单 Run 最大 Subscriber | `4` |
| 最大请求体 | `16384 bytes` |
| Run timeout | `30s` |
| SSE heartbeat | `15s` |

这些值是有界开发默认值，不是跨机器性能门槛。所有设置都是拒绝 bool/coercion 的
strict positive integer；零、负数或非整数在启动时 fail closed。app factory
可注入合法小值供测试。默认演示固定单 Worker、绑定 `127.0.0.1`。

请求体限制在 JSON 解析前由 API ASGI middleware 强制，不能只信任
`Content-Length`。

## 10. 文件布局

```text
src/glodex/
├── application/
│   ├── ports.py                  # 新增 RunEventObserver
│   └── search_service.py         # 新增 execute_run / observer
├── api/
│   ├── __init__.py               # 导出 create_app
│   ├── settings.py               # ApiSettings
│   ├── contracts.py              # HTTP DTO / ApiError
│   ├── events.py                 # event DTO / projector / cursor
│   ├── runtime.py                # RunResource / Registry / Coordinator
│   ├── routes.py                 # 三个端点
│   └── app.py                    # factory / lifespan / handlers / middleware
└── bootstrap.py                  # observer-aware service composition

tests/m1a/
├── conftest.py
├── unit/
│   ├── test_run_registry.py
│   ├── test_event_projection.py
│   ├── test_sse_cursor.py
│   ├── test_traceability_m1a.py
│   └── test_verify_m1a_runner.py
├── contract/
│   ├── test_http_api_contract.py
│   └── test_sse_contract.py
├── architecture/test_m1a_boundaries.py
├── nfr/test_m1a_offline_security.py
└── acceptance/
    ├── test_m1_ac_001_005.py
    └── test_m1_ac_006_010.py

scripts/
└── verify_m1a.py
```

M1a 测试独立放在 `tests/m1a`，并配合显式 test roots / ignore 规则，保持
`verify_m0.py` 的测试选择语义不变。

## 11. 架构与依赖门禁调整

现有架构测试会在任何源码位置拒绝 FastAPI，必须改为分层允许：

- domain/application/adapters 继续禁止 FastAPI、Starlette 与 HTTP 框架；
- 只有 `glodex.api` 可以导入 FastAPI；
- `httpx` 只能出现在 tests；
- `starlette` 加入受控框架前缀，防止绕过 FastAPI 边界；
- application → adapter/api 依赖继续禁止；
- 直接 runtime dependency allowlist 从 `{pydantic}` 更新为
  `{fastapi, pydantic}`；
- requests、数据库和模型 SDK 仍必须被反例测试拒绝。

pytest 的全局 `--disable-socket` 保持不变。自动验收不启动 Uvicorn。

## 12. 测试策略

### 12.1 测试层次

- unit：Registry 状态、容量/TTL、projector、cursor、通知队列；
- contract：三个 HTTP DTO、错误 envelope、OpenAPI、SSE framing；
- acceptance：`M1-AC-001`–`010`；
- architecture/NFR：依赖边界、禁网、CORS、日志/事件脱敏；
- regression：完整 `verify_m0.py`。

普通 HTTP 使用 HTTPX ASGI transport。实时 AC 使用直接 ASGI harness：

1. app task 把 `http.response.body` chunk 写入测试 queue；
2. SearchService double 在可控 `asyncio.Event` 暂停；
3. 测试在释放闩锁前读到阶段事件；
4. 不使用 sleep，不打开真实端口。

### 12.2 Traceability

`check_traceability.py` 增加 profile：

```bash
python scripts/check_traceability.py --profile m0 --mode coverage
python scripts/check_traceability.py --profile m1a --mode coverage
```

- M0 exact inventory 保持 `12 P0 / 12 AC / 11 NFR`；
- M1a exact inventory 为 `9 P0 / 10 AC / 10 NFR`；
- m0 profile 排除 `tests/m1a`；
- m1a profile 只收集 `tests/m1a`；
- unknown、missing、duplicate、skip、skipif、xfail 均 fail closed。

最终 `_M0_STEPS` 已显式选择现有 M0 test roots，继续保持该选择；历史 phase
profile 中仍使用 `pytest tests` 的步骤增加 `--ignore=tests/m1a`。`verify_m0.py`
调用 traceability 时显式使用 `--profile m0`，避免加入 M1a 后改变任何 M0
门禁的收集集合。

### 12.3 P0 追踪

| P0 | 组件 | 主要验证 |
|---|---|---|
| `GLO-M1-P0-001` | routes/contracts | HTTP contract、AC-001/002 |
| `GLO-M1-P0-002` | Registry/Coordinator | AC-003/006 |
| `GLO-M1-P0-003` | SearchService seam/Coordinator | AC-001/005/008 |
| `GLO-M1-P0-004` | observer/projector | AC-003/005/007/009 |
| `GLO-M1-P0-005` | cursor/subscription | AC-004 |
| `GLO-M1-P0-006` | Registry/subscriber | AC-006/007 |
| `GLO-M1-P0-007` | settings/errors/lifecycle | AC-002/008/009 |
| `GLO-M1-P0-008` | response projection/regression | AC-001/010 |
| `GLO-M1-P0-009` | verify_m1a/offline gates | AC-010 |

NFR 在 Tasks 阶段逐项映射到 architecture、NFR 或 acceptance 自动化测试。

## 13. 实施阶段

### Phase A：合同与 Walking Skeleton

- RED：HTTP DTO、错误 envelope、依赖/导入边界、M1 traceability profile；
- GREEN：依赖锁、`ApiSettings`、空 Registry、`create_app` 与三个占位端点；
- Gate：M0 定向架构回归 + M1a contract skeleton。

### Phase B：实时应用缝与 Run Runtime

- RED：显式 run ID、observer 实时性、同 Thread 原子竞争、状态转换；
- GREEN：SearchService 新增 `execute_run`、Registry、Coordinator、Projector；
- Gate：SearchService Golden 等价 + Registry/projector unit tests。

### Phase C：SSE、重放与错误

- RED：首次重放、cursor 后缀、在线事件、断线隔离、容量与 degraded；
- GREEN：三个正式路由、EventSourceResponse、统一错误和 body limit；
- Gate：HTTP/SSE contract + `M1-AC-001`–`009`。

### Phase D：质量闭环与文档

- 扩展 architecture/offline/security；
- 完成 M1 exact traceability；
- 新增 `verify_m1a.py`，先完整运行 M0，再运行 M1；
- 完成 `M1-AC-010`、README 与本地 curl/SSE Demo；
- Gate：完整 M1a Definition of Done。

每个 Phase 必须先 RED、再 GREEN；Plan 批准后 Tasks 才拆到具体提交单元。

## 14. 验证命令

日常快速门禁：

```bash
uv run --locked python -m pytest -q -p scripts.verify_m0 tests/m1a
uv run --locked python scripts/check_traceability.py --profile m1a --mode references
```

最终门禁：

```bash
uv run --locked python scripts/verify_m1a.py
```

`verify_m1a.py` 必须依次执行：

1. 完整 `verify_m0.py`；
2. M1a architecture/offline/security；
3. M1a unit/contract；
4. `M1-AC-001`–`010`；
5. M1a traceability coverage。

门禁固定 cwd、使用参数数组与 `shell=False`，不自动更新 Golden，也不允许
skip/xfail。

人工演示：

```bash
uv run --locked uvicorn glodex.api.app:create_app \
  --factory \
  --host 127.0.0.1 \
  --port 8000
```

README 再给出一个 POST、一个 `curl -N` SSE 和一个状态 GET。

## 15. 主要风险

| 风险 | 计划控制 |
|---|---|
| API Run ID 与 SearchResponse ID 分叉 | Coordinator 预分配并传入 `execute_run`；合同测试检查 provider 调用。 |
| 通用 Identifier 允许冒号 | API 生成边界额外拒绝冒号；不收紧 M0 合同。 |
| 极快 runner 在 POST 返回前完成 | start gate 只由 202 Response 的发送后 callback 释放。 |
| 实时事件变成终态批量回放 | observer + 闩锁验收。 |
| observer 异常改变业务结果 | composition 隔离包装器，失败只标记 projection degraded。 |
| snapshot 已发但 terminal 未发 | 最终二事件先构造、再同步批量提交。 |
| 慢 Subscriber 造成无界内存 | 共享有界事件列表 + 单 token 通知队列。 |
| FastAPI 侵入 application/domain | 分层 import gate 与反例测试。 |
| M1 marker 破坏 M0 traceability | 独立 test root/profile，M0 exact inventory 保持。 |
| 终态 Run 只写不清导致 TTL 失效 | create/status/subscribe 入口统一惰性清理。 |
| ASGI 测试误判实时性 | 直接捕获 body chunk，不用 HTTPX 缓冲流。 |
| Demo 变成生产服务承诺 | 单 Worker、本机绑定、无认证/恢复限制写入 README。 |

## 16. Plan 审批门禁

进入 Tasks 前必须确认：

- [x] 用户批准依赖、文件布局和四阶段实施顺序；
- [x] 用户接受新增 `SearchService.execute_run` 与每次执行 observer；
- [x] 用户接受单 event-loop Registry 与进程内 task 模型；
- [x] 用户接受 API settings 只是开发默认值，不是性能 SLA；
- [x] 用户接受 M1a 测试独立于 M0，并由最终门禁串联；
- [x] 没有 AG-UI、WebSocket、取消、数据库或前端实现任务。

## 17. 调研依据

- FastAPI 从 `0.135.0` 起内建 SSE，`ServerSentEvent` 支持 JSON data、id 和
  comment：<https://fastapi.tiangolo.com/tutorial/server-sent-events/>
- FastAPI 官方建议用 lifespan 管理 startup/shutdown 资源，并在测试中显式进入
  lifespan：<https://fastapi.tiangolo.com/advanced/testing-events/>
- FastAPI 官方异步测试使用 HTTPX ASGI transport；该方式用于普通 HTTP，
  M1a 实时流另用直接 ASGI harness：
  <https://fastapi.tiangolo.com/advanced/async-tests/>
