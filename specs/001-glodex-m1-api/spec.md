# Glodex M1a API 与实时事件规格

| 字段 | 值 |
|---|---|
| Spec ID | `GLO-SPEC-001` |
| 版本 | `0.3.2` |
| 状态 | Approved |
| 里程碑 | M1a：FastAPI + SSE 垂直切片 |
| 实现候选 | `GLO-P1-002` |
| 父规格 | [`GLO-SPEC-000`](../000-glodex-mvp/spec.md) |
| 最后更新 | 2026-07-28 |

## 1. 文档目的与权威边界

本规格定义 M0 确定性搜索核心的第一个 HTTP 入口。客户端能够创建一次搜索
Run、查询状态，并通过 Server-Sent Events（SSE）观察执行进度。

本规格描述“系统必须表现出什么行为”，不规定具体目录、锁、队列、并发库、
清理算法或依赖版本。规格批准后，以上实现细节进入 Plan。

权威顺序为：

1. 已批准的 [`GLO-SPEC-000`](../000-glodex-mvp/spec.md)；
2. 本规格；
3. 已接受 ADR；
4. Plan 与 Tasks；
5. 本地架构图。

架构图是目标参考，不自动批准其中的 WebSocket、React、文件接口或生产基础设施。

## 2. 问题与目标

### 2.1 当前问题

M0 只有 Python 应用入口和 CLI。调用方无法：

- 通过稳定 HTTP 合同启动搜索；
- 在搜索完成前观察阶段进度；
- 晚连接或短暂断线后继续读取事件；
- 在不导入内部 Python 模块的情况下接入后续界面。

### 2.2 M1a 目标

M1a 交付一条本地可运行、可测试的服务化垂直切片：

1. FastAPI 作为外围适配器调用现有 `SearchService`；
2. 合法请求异步创建 Run，不阻塞到业务终态；
3. 状态接口返回可信的运行状态和最终 `SearchResponse`；
4. SSE 在阶段发生时发送版本化事件；
5. 同进程内支持晚连接和基于游标的简单重放；
6. HTTP、SSE 和 CLI 的业务结果保持一致；
7. 默认测试仍完全离线。

## 3. 范围

### 3.1 In Scope

- `POST /api/v1/runs` 创建 Run；
- `GET /api/v1/runs/{run_id}` 查询 Run；
- `GET /api/v1/runs/{run_id}/events` 订阅 SSE；
- 进程内 Run 状态与事件缓存；
- Glodex 自有 `glodex.event.v1` 事件合同；
- `Last-Event-ID` 同进程重放；
- 并发 Run 和 Subscriber 隔离；
- 统一、安全的 HTTP 错误 envelope；
- M0 CLI、业务验收和离线门禁回归。

### 3.2 Out of Scope

- 标准 AG-UI SDK、decoder、`HttpAgent` 或完整协议兼容；
- WebSocket；
- 用户取消、checkpoint、恢复和可靠任务队列；
- Postgres、Redis、多进程或多 Worker 状态共享；
- React/Vite 前端、文件上传或下载；
- 认证、租户、计费和公网生产部署；
- 真实 LLM、Provider fan-out、OpenSearch 或向量召回；
- 长期记忆、自主 AgentLoop 或子 Agent fork；
- 生产 SLA 和跨机器性能承诺。

这些能力不能以空壳或隐藏配置进入 M1a。

## 4. 继承的 M0 不变量

M1a 必须原样继承：

1. query、locale、top_k 和币种格式在创建 Run 前校验；拒绝时不创建 Run，
   不调用 Intent、Catalog 或 Ranker。
2. snapshot 和币种兼容性错误属于带 `run_id` 的业务 `FAILED`。
3. Required、source span、硬门、排序、金额、Evidence 和 Verified Claims
   规则不得因 HTTP 或事件展示而改变。
4. `NO_MATCH` 是正常业务终态，不得自动放宽条件。
5. `SearchResponse` 仍只有 `COMPLETED`、`NO_MATCH`、`FAILED`。
6. `RunJournal` 与 `SearchResponse` 是业务真相；SSE 只是只读投影。
7. 相同请求、配置、snapshot 和算法版本产生相同业务结果与顺序。
8. domain 不得导入 FastAPI、SSE、HTTP 或网络框架。

## 5. 产品决策

| ID | 决策 |
|---|---|
| `M1-D-001` | M1a 只实现 `GLO-P1-002` 的最小服务化切片。 |
| `M1-D-002` | 使用 FastAPI 提供版本化 HTTP API。 |
| `M1-D-003` | 实时传输只使用 SSE，不同时实现 WebSocket。 |
| `M1-D-004` | 事件使用自有 `glodex.event.v1`；名称参考常见 AG-UI 事件，但不声明兼容。 |
| `M1-D-005` | Run、事件和结果只保存在当前进程内，不承诺跨重启恢复。 |
| `M1-D-006` | 断开 Subscriber 不取消业务 Run。 |
| `M1-D-007` | M1a 没有用户取消状态；非业务 runner 中断使用 transport-only `ABORTED`。 |
| `M1-D-008` | 服务默认面向本机开发与测试，CORS 默认关闭。 |

## 6. 用户旅程

### 6.1 创建并观察运行

1. 客户端提交 Thread 与现有 `SearchRequest`。
2. 服务完成 pre-run 校验。
3. 合法请求获得唯一 `run_id`，服务返回 `202 Accepted`。
4. 客户端连接 `events_url`，先读取缓存事件，再接收新事件。
5. Run 完成后，客户端从状态接口取得完整 `SearchResponse`。

### 6.2 请求被拒绝

非法输入返回结构化 4xx。服务不分配 `run_id`、不创建任务、不发送事件，
也不调用业务下游。

### 6.3 晚连接与重连

事件先写入 Run 缓存，再通知 Subscriber。首次连接从第一条事件开始；
重连携带 `Last-Event-ID`，只补发游标之后的事件，不重新执行搜索。

### 6.4 业务失败与传输中断

- `FAILED` 来自现有 `SearchService`，包含可信 `SearchResponse`；
- `ABORTED` 表示 runner 在无法产生可信 `SearchExecution` 时中断，
  `response` 为 `null`，不能伪造成业务 `FAILED`；
- 硬进程崩溃可能直接丢失 Run，重启后查询返回未找到。

## 7. HTTP 合同

全部端点使用 `/api/v1`。HTTP DTO 使用 `snake_case`，SSE 事件使用
`camelCase`。未知字段不得被静默接受为合同的一部分。

### 7.1 创建 Run

`POST /api/v1/runs`

请求：

```json
{
  "thread_id": "thread-demo-001",
  "request": {
    "query": "推荐 800 美元以内、有库存、适合出差的轻薄本",
    "locale": "zh-CN",
    "display_currency": "USD",
    "top_k": 3,
    "snapshot_version": "m0-v1"
  }
}
```

规则：

- `thread_id` 可省略；省略时由服务生成；
- 客户端提供的 `thread_id` 复用现有 `Identifier` 长度与字符规则；
- `request` 复用 M0 `SearchRequest` 与 pre-run 校验；
- 校验通过后才能分配 `run_id` 和登记 runner；
- `run_id` 由服务生成且不包含冒号，供 SSE event ID 无歧义使用；
- 同一 Thread 同时最多一个活动 Run；
- 创建操作非幂等，M1a 不承诺 `Idempotency-Key`。

成功返回 `202 Accepted`：

```json
{
  "schema_version": "glodex.run.v1",
  "thread_id": "thread-demo-001",
  "run_id": "run-001",
  "state": "ACCEPTED",
  "status_url": "/api/v1/runs/run-001",
  "events_url": "/api/v1/runs/run-001/events"
}
```

### 7.2 查询 Run

`GET /api/v1/runs/{run_id}`

```json
{
  "schema_version": "glodex.run.v1",
  "thread_id": "thread-demo-001",
  "run_id": "run-001",
  "state": "RUNNING",
  "projection_status": "OK",
  "last_event_id": "run-001:4",
  "response": null,
  "error": null
}
```

`state` 只能是：

- `ACCEPTED`
- `RUNNING`
- `COMPLETED`
- `NO_MATCH`
- `FAILED`
- `ABORTED`

业务终态时，`response` 是完整 `SearchResponse`，其 `run_id` 与 `status`
必须和 Run Resource 一致。`ABORTED` 时 `response=null`，`error` 为安全的
transport error。活动状态下二者均为 `null`。

`projection_status` 为 `OK` 或 `DEGRADED`。投影失败不能改写业务状态或响应；
此时状态接口是恢复业务结果的权威入口。尚无公开事件时
`last_event_id=null`。

未知、过期或重启后丢失的 Run 返回 `404 RUN_NOT_FOUND_OR_EXPIRED`。

### 7.3 订阅事件

`GET /api/v1/runs/{run_id}/events`

请求 `Accept` 必须包含 `text/event-stream`。事件按 SSE 编码：

```text
id: run-001:4
data: {"type":"STEP_FINISHED","schemaVersion":"glodex.event.v1","threadId":"thread-demo-001","runId":"run-001","sequence":4,"timestamp":1785227400123,"stepName":"ranking"}

```

规则：

1. 未提供 `Last-Event-ID` 时从第一条事件开始；
2. 合法游标只补发游标之后的事件；
3. 非法、超前或属于其他 Run 的游标返回 `400 INVALID_EVENT_CURSOR`；
4. 重连不创建新 Run，也不重新执行搜索；
5. 活动 Run 在重放后继续等待，终态 Run 在重放完成后关闭；
6. Subscriber 断线、变慢或发送失败不取消、不阻塞业务；
7. projection degraded 后允许关闭事件流，状态接口仍可返回最终业务结果；
8. 重放只在当前进程和 Run 保留期内有效，不承诺 exactly-once。

### 7.4 错误合同

除成功 DTO 与业务 `SearchResponse` 外，HTTP 4xx/5xx 使用：

```json
{
  "schema_version": "glodex.error.v1",
  "type": "api_error",
  "error": {
    "code": "REQUEST_REJECTED",
    "message": "Request validation failed.",
    "field_errors": []
  }
}
```

最低稳定 code：

| 条件 | HTTP | code |
|---|---:|---|
| 请求无法解析或格式不支持 | `400`/`415` | `BAD_REQUEST` |
| 实际请求体超过配置上限 | `413` | `REQUEST_BODY_TOO_LARGE` |
| M0 或 transport 字段校验失败 | `422` | `REQUEST_REJECTED` |
| 同一 Thread 已有活动 Run | `409` | `RUN_ALREADY_ACTIVE` |
| 新 Run 达到服务容量上限 | `503` | `CAPACITY_EXCEEDED` |
| 单 Run 达到 Subscriber 上限 | `429` | `CAPACITY_EXCEEDED` |
| Run 未知、过期或重启后丢失 | `404` | `RUN_NOT_FOUND_OR_EXPIRED` |
| SSE Accept 不兼容 | `406` | `NOT_ACCEPTABLE` |
| SSE cursor 非法 | `400` | `INVALID_EVENT_CURSOR` |
| 未映射内部错误 | `500` | `INTERNAL_SERVER_ERROR` |

`code` 是机器合同；`message` 只提供安全、可读说明。错误不得包含 traceback、
绝对路径、秘密、请求回显或框架默认自由 `detail`。

所有 transport-only `ABORTED` 原因在状态资源的 `error` 与最终 `RUN_ERROR`
事件中统一使用 `code="RUN_ABORTED"` 和
`message="Run execution was aborted."`。M1a 不向客户端区分 timeout、
响应发送失败、runner 异常或服务关闭等内部原因。

## 8. 事件合同

### 8.1 公共字段

所有 `glodex.event.v1` 事件包含：

- `type`
- `schemaVersion`
- `threadId`
- `runId`
- `sequence`
- `timestamp`

`sequence` 在一个 Run 内从 1 连续递增。SSE `id` 为
`{runId}:{sequence}`。`timestamp` 为 UTC Unix epoch 毫秒；排序和去重依赖
sequence，不依赖 timestamp。

### 8.2 事件类型

| 事件 | 业务字段 | 含义 |
|---|---|---|
| `RUN_STARTED` | 无 | Run 开始 |
| `STEP_STARTED` | `stepName` | 阶段开始 |
| `STEP_FINISHED` | `stepName` | 阶段完成 |
| `STEP_DEGRADED` | `stepName`、`issueCodes` | 阶段降级但业务继续 |
| `STATE_SNAPSHOT` | `snapshot.response` | 完整公共 `SearchResponse` |
| `RUN_FINISHED` | `result.status` | `COMPLETED` 或 `NO_MATCH` |
| `RUN_ERROR` | `code`、`message` | 业务 `FAILED` 或 transport `ABORTED` |

事件名称只属于 Glodex 合同。未来接入 AG-UI 时通过独立 adapter 转换。

### 8.3 顺序与终态

projection 正常时：

1. 第一条事件是 `RUN_STARTED`；
2. 每个 `STEP_FINISHED` 或 `STEP_DEGRADED` 对应一个已开始的 step；
3. `COMPLETED`、`NO_MATCH`、`FAILED` 在终态前发送完整 `STATE_SNAPSHOT`；
4. `COMPLETED`、`NO_MATCH` 最后发送 `RUN_FINISHED`；
5. `FAILED` 最后发送 `RUN_ERROR`；
6. `ABORTED` 不伪造 snapshot，最后发送 `RUN_ERROR`；
7. 终态后不再发送业务事件。

投影失败时，`projection_status=DEGRADED`，已发送事件必须仍是合法连续前缀，
但不要求伪造事件终态。Canonical `SearchResponse` 保持权威。

事件不得增加完整 request/query、密钥、宿主机路径或内部异常。
`STATE_SNAPSHOT` 可包含公共 `SearchResponse` 已批准的 source-span 文本。

## 9. P0 功能需求

| ID | 需求 | 失败或降级行为 |
|---|---|---|
| `GLO-M1-P0-001` | 提供三个版本化 Run API，并复用 M0 pre-run 校验。 | 非法请求无 Run、无事件、无下游调用。 |
| `GLO-M1-P0-002` | 合法请求异步建立唯一 Run，并返回状态与事件 URL。 | 同 Thread 冲突或容量耗尽时 fail closed，不覆盖已有 Run。 |
| `GLO-M1-P0-003` | 后台执行调用同一个 `SearchService`，保存原样 `SearchResponse`。 | 业务失败保持 `FAILED`；runner 中断不得伪造业务响应。 |
| `GLO-M1-P0-004` | 在运行过程中投影 `glodex.event.v1` 事件。 | 投影失败只标记 degraded，不改变业务结果。 |
| `GLO-M1-P0-005` | 支持首次重放和 `Last-Event-ID` 同进程补发。 | 非法游标被拒绝；不得重新执行业务。 |
| `GLO-M1-P0-006` | 隔离 Thread、Run 与 Subscriber 生命周期。 | 跨 Run 数据、双重执行或 Subscriber 取消业务均使验收失败。 |
| `GLO-M1-P0-007` | 所有资源有界，HTTP 错误安全且机器可读。 | 超限 fail closed；进程重启诚实返回 Run 丢失。 |
| `GLO-M1-P0-008` | 保持 API、事件、Python 入口和 CLI 的业务语义一致。 | 金额、证据、排序、漏斗或终态差异使验收失败。 |
| `GLO-M1-P0-009` | 提供默认离线的 API、SSE、并发与 M0 回归门禁。 | 测试访问公网、真实模型、Provider 或数据库时失败。 |

## 10. Given-When-Then 验收场景

### `M1-AC-001` API 与应用入口等价

**Given** 固定请求、配置、snapshot、时钟和 run ID

**When** 分别通过 API 与直接 `SearchService` 执行

**Then** 最终 `SearchResponse`、商品顺序、金额、Evidence、漏斗、warnings
和业务终态完全一致。

覆盖：`GLO-M1-P0-001`、`GLO-M1-P0-003`、`GLO-M1-P0-008`

### `M1-AC-002` 非法请求不建立 Run

**Given** query、locale、币种、top_k 或 Thread 非法

**When** 创建 Run

**Then** 返回结构化 4xx，不产生 `run_id`、任务或事件，下游调用次数为零。

覆盖：`GLO-M1-P0-001`、`GLO-M1-P0-007`

### `M1-AC-003` 异步创建与真实进度

**Given** SearchService 在可控阶段闩锁处暂停

**When** 客户端创建 Run 并订阅事件

**Then** 服务先返回 202；Subscriber 在释放闩锁前读到已完成阶段事件，
证明进度不是终态后的批量伪造。

覆盖：`GLO-M1-P0-002`、`GLO-M1-P0-004`

### `M1-AC-004` 晚连接与重连不丢失、不重算

**Given** Run 已产生多个事件，客户端处理到一个已知 event ID 后断开

**When** 首次连接或携带 `Last-Event-ID` 重连

**Then** 服务按连续 sequence 重放正确后缀，最终响应不变，下游调用次数不增加。

覆盖：`GLO-M1-P0-004`、`GLO-M1-P0-005`

### `M1-AC-005` 三种业务终态正确映射

**Given** 分别产生 `COMPLETED`、`NO_MATCH`、`FAILED` 的固定场景

**When** 查询状态并读取完整事件流

**Then** 状态、`SearchResponse`、snapshot 和最终事件一致；`NO_MATCH`
使用 `RUN_FINISHED`，`FAILED` 使用 `RUN_ERROR`。

覆盖：`GLO-M1-P0-003`、`GLO-M1-P0-004`

### `M1-AC-006` 并发隔离与同 Thread 冲突

**Given** 不同 Thread 并发运行，同时两个请求竞争同一 Thread

**When** 读取各自状态和事件

**Then** 不同 Run 无数据串流；同 Thread 恰好一个请求成功，另一个返回 409，
且只执行一次业务。

覆盖：`GLO-M1-P0-002`、`GLO-M1-P0-006`

### `M1-AC-007` Subscriber 与 Projection 故障隔离

**Given** 一个 Subscriber 断线，或共享事件投影失败

**When** SearchService 继续执行

**Then** Subscriber 断线不改变 Run；投影失败只标记 degraded；状态接口仍返回
同一个 Canonical `SearchResponse`。

覆盖：`GLO-M1-P0-004`、`GLO-M1-P0-006`

### `M1-AC-008` 有界资源、Runner 中断与进程丢失

**Given** 达到 Plan 规定的 Run、事件或 Subscriber 上限，runner 在产生可信
`SearchExecution` 前中断，或服务进程重启

**When** 创建、订阅或查询 Run

**Then** Run 容量返回 `503`、Subscriber 容量返回 `429`，均不驱逐活动 Run；
事件上限停止新增投影、保留合法前缀并标记 `DEGRADED`；runner 中断进入
`ABORTED` 且 `response=null`；重启后旧 Run 返回
`RUN_NOT_FOUND_OR_EXPIRED`，不伪造业务终态。

覆盖：`GLO-M1-P0-003`、`GLO-M1-P0-007`

### `M1-AC-009` 错误和事件不泄密

**Given** 代表性非法请求、内部异常和每种事件

**When** 检查 HTTP body、SSE 与捕获日志

**Then** schema 版本和 code 稳定，不含 traceback、绝对路径、密钥、完整请求体
或未批准字段。

覆盖：`GLO-M1-P0-004`、`GLO-M1-P0-007`

### `M1-AC-010` M0 回归与离线边界

**Given** 未配置网络、真实 LLM、Provider、数据库或 Redis

**When** 执行 M1a 一键门禁

**Then** M0 全部 AC、Golden、determinism、security、CLI smoke 继续通过，
新增 API/SSE/并发测试通过，外部调用次数为零。

覆盖：`GLO-M1-P0-008`、`GLO-M1-P0-009`

## 11. 非功能需求

| ID | 维度 | 要求 |
|---|---|---|
| `GLO-M1-NFR-001` | 兼容性 | M0 公共 DTO、CLI、Golden 与 12 个 AC 不发生未批准变化。 |
| `GLO-M1-NFR-002` | 一致性 | Journal、状态资源与 SearchResponse 一致率为 100%；projection 正常时最终事件也一致。 |
| `GLO-M1-NFR-003` | 顺序 | projection 正常时 sequence 连续、首事件固定、终态唯一，跨 Run 混入率为 0。 |
| `GLO-M1-NFR-004` | 实时性 | 创建响应不等待业务终态；阶段事件在运行过程中可观察。 |
| `GLO-M1-NFR-005` | 重放 | 保留期内按 cursor 补发且不重新执行；不宣称 exactly-once。 |
| `GLO-M1-NFR-006` | 有界性 | 请求、Run、事件和 Subscriber 均有显式配置上限；活动 Run 不被普通保留策略驱逐。 |
| `GLO-M1-NFR-007` | 安全与隐私 | 默认无 CORS；错误、事件和日志不泄露秘密、路径、traceback 或完整请求体。 |
| `GLO-M1-NFR-008` | 架构 | FastAPI 与 SSE 仅存在于 adapter/composition 层；domain 保持纯净。 |
| `GLO-M1-NFR-009` | 可验证性 | 使用进程内 ASGI、固定时钟和可控闩锁验证顺序，不以脆弱 sleep 或机器性能判定正确性。 |
| `GLO-M1-NFR-010` | 离线默认 | 默认运行和全部验收不访问公网、真实模型、Provider 或外部数据库。 |

Plan 必须选择明确、可配置且可测试的资源默认值，但本 Demo Spec 不把某台机器的
吞吐数字或绝对延迟写成产品正确性。

## 12. 架构资料决策

| 架构图主题 | M1a 决策 |
|---|---|
| FastAPI 任务接口 | Keep：实现三个 Run API。 |
| 实时事件 | Change：M1a 使用 SSE 与 `glodex.event.v1`。 |
| AG-UI | Defer：只借用直观事件名称，未来通过 adapter 接入。 |
| WebSocket | Defer：M1a 不同时维护第二套实时协议。 |
| React/Vite | Defer：先稳定后端合同。 |
| 文件接口 | Defer：不进入搜索 Run 垂直切片。 |
| Postgres/checkpoint/取消 | Defer：进入持久任务独立 Spec。 |
| LLM/Provider/AgentLoop | Defer：不与 API 传输切片混做。 |

## 13. 风险与缓解

| 风险 | 缓解 |
|---|---|
| POST 后订阅丢失早期事件 | 事件先缓存，Subscriber 再读取。 |
| 最终回放冒充实时进度 | 用可控闩锁验收运行中事件。 |
| Subscriber 断线取消业务 | 将订阅与业务生命周期分离。 |
| 投影失败污染业务终态 | Journal/Response 权威，单列 projection 状态。 |
| 同 Thread 并发覆盖 | 原子预留；冲突返回 409。 |
| 进程内状态被误当可靠队列 | 明确重启丢失和 404 语义。 |
| 传输层改变金额或证据 | API 等价验收和 domain import boundary。 |
| 首版范围再次膨胀 | WebSocket、前端、持久化和 Agent 能力全部独立立项。 |

## 14. 进入 Plan 的 Definition of Ready

只有满足以下条件才进入 Plan：

- [x] 用户批准三个 API、SSE 和自有事件合同；
- [x] 用户接受单进程、无持久恢复、无用户取消的 M1a 边界；
- [x] 每个 `GLO-M1-P0-*` 至少有一个验收场景；
- [x] 所有影响用户行为的开放问题已清零；
- [x] AG-UI、WebSocket、React、文件和数据库明确不进入 M1a。

## 15. Definition of Done

- [x] 本规格状态已改为 `Approved`；
- [x] Plan 只设计本规格批准的能力；
- [x] Tasks 将全部 P0、NFR 与 AC 映射到代码和测试；
- [x] 三个 HTTP 端点、错误 envelope 和七种事件已实现；
- [x] `M1-AC-001` 至 `M1-AC-010` 全部自动化且无 skip/xfail；
- [x] M0 完整门禁、CLI、Golden、security 和 determinism 继续通过；
- [x] 提供一条 M1a 验证命令和一条本地 API/SSE 演示命令；
- [x] README 只描述真实能力，并注明单进程、无认证、无恢复；
- [x] 没有 WebSocket、取消、文件、React、LLM、Provider 或数据库空壳。

## 16. 后续候选

M1a 完成后，每项继续独立走 Spec → Plan → Tasks：

1. LLM Intent；
2. Provider fan-out；
3. Category Insight；
4. 持久任务、取消与恢复；
5. AG-UI adapter 与 React 界面。

## 17. 变更治理

1. 改变 API、事件、状态或用户可见失败语义，必须先修改本 Spec；
2. 只改变内部实现的选择进入 Plan 或 ADR；
3. 架构图中的新技术不能直接进入实现；
4. M1a 完成前，后续能力不得破坏 M0 业务不变量。
