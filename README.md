# Glodex

Glodex 当前以原生 `ChatOpenAI` tool calling + LangGraph ReAct + 本地 A100 BGE 的 durable Agent 作为唯一正式 live 路径：

- **离线购物核心**从版本化快照聚合同款商品和报价，用 `Decimal` 计算到手价，并在排序前执行预算、品类、库存、商品本体和证据硬门。
- **AgentLoop 核心**提供九个业务原生 `@tool` 和两个 fork 控制工具。模型在每轮 `Think → Act → Observe → Reflect` 中选择下一步；独立平台可通过同质子 AgentLoop 并行检索。
- **检索基准**提供独立、可复现的 ESCI 历史检索/重排评测，只评测冻结候选池，不进入在线 Agent。
- **持久运行时**由 PostgreSQL 保存 Run、安全 SSE 事件和可恢复 checkpoint；Redis 只缓存可重建投影。
- **检索模型服务**是在固定 loopback 上运行的私有 GPU BGE embedding/cross-encoder rerank 服务，并绑定模型和索引身份。
- **Web 控制台**是本机 AG-UI/Vue 交互层，只投影持久 Agent API，在 `127.0.0.1:8767` 通过同源认证 WebSocket 提供提交、一次 suffix 重挂接与 Cancel；浏览器不能直连数据库、Redis、OpenSearch 或 GPU。
- **Agent 组合入口**固定为 `ChatOpenAI → @tool → create_agent → astream_events`，接入私有 BGE 检索服务、PostgreSQL/Redis 和 Web 控制台，不保留旧里程碑命令或适配器。
- **M4** 将真实购物动作收敛为原生、严格、单工具调用：模型只选择下一个工具，`ShoppingToolSession` 仍独占 query、候选、价格、预算和最终发布硬门。
- **M5** 补齐 loopback 本机账户、owner-scoped conversation history 与可管理 memory；Redis 仅是可重建 context projection。
- **M5b** 收紧长期记忆语义：单次购物条件不调用 Reflect、不写长期 Store；只有当前输入中的明确长期表达才触发一次真实 LLM Reflect，并写为 `explicit_reflect`。旧自动条目在迁移时直接删除。
- **M6** 为同一条正式路径加入受限 safe trace、真实 LLM usage receipt、版本化本机成本估算、每个依赖的 rolling breaker 与 owner trace/无身份 operations 面板；它不改变 Agent 决策、Hard Gate 或购物结果。
- **M7** 是显式内部离线评测：读取一个已完成 Run 的 Query、完整回答、商品证据和轨迹，用真实 OpenAI-compatible LLM 生成
  query-specific typed Rubric 并完成 P2 judge；在线搜索不等待评测。网页显示需求证据覆盖和所属 Run 的安全摘要。

M0 仍是可保留的业务与合同基线；durable API 与浏览器 console 的公共合同已经定义，
且只读取版本化快照；当前没有任何 marketplace 实时抓取 adapter。M5 的登录账户仅限本机，不是生产身份或跨设备服务；
它保存 owner-scoped history 与明确长期 memory，不从单次购物条件或隐式行为推断偏好。
持久化存储只由带 `--live` 的 `storage`、`agent-api` 与 `web-console` 命令及独立 durable API factory 使用。模型
loopback tunnel 只由带 `--live` 的 `model-service` 或 Agent API composition 使用；浏览器 console
仍是唯一 AG-UI/Vue 投影层，且固定只读取 Durable `8766` public routes。默认 M0–M4 与 Web console
离线门禁不会加载 GPU 依赖、读取 tunnel 或连接模型服务。

M6 trace 只保存 operation、受限耗时、safe code、LLM endpoint/model version、usage receipt 与本机成本状态，
不保存 query、prompt、memory、商品正文、LLM response body、cookie、credential、vector 或 score。PostgreSQL 是
trace、breaker、价格表和聚合的 truth；Redis 只缓存 5 分钟、无身份的 operations projection，缓存丢失会从
PostgreSQL 重建。operations 页面以固定安全码显示 breaker open、高失败率、receipt unavailable 与 retention
failure；它们不改变购物结果。默认 exporter 为 disabled，不读取外送 endpoint 或 credential，也不建立额外
socket。

M7 的完整评测不进入 PostgreSQL、SSE 或浏览器。操作者通过 `quality-evaluation run` 显式创建内部 JSON 报告；报告包含
Query、完整结构化回答、轨迹、Rubric、P0/P1/P2、reward 和版本。PostgreSQL 只保存不含这些私有上下文的安全
摘要，供所属账户在结果页读取。未证实的关键需求会确定性限制 P2 上限；Judge 无效时固定为 `UNSCORED`，P2 与
reward 为空，不 retry、fallback 或重跑 shopping Agent。

## 源码结构

运行时代码按能力命名，不使用 M3/M5 等里程碑名；这些编号仅存在于 `specs/` 和测试追踪标记中。

```text
src/glodex/
├── agent/           # ChatOpenAI、LangGraph ReAct、受控 tool session 与安全事件映射
├── tools/           # 九个业务工具的 typed 执行引擎
├── llm/             # OpenAI-compatible HTTP transport 与 LLM contract
├── retrieval/       # A100 BGE、reranker、OpenSearch、快照与 ESCI benchmark
├── memory/          # 本机身份、历史、长期记忆、Reflect、context 与 blacklist
├── runtime/         # durable Run、checkpoint、prompt cache 与 retrieval composition
├── quality/         # 离线 Rubric、完整 Judge facts、证据上限与内部报告
├── observability/   # trace、成本、breaker 与受限 exporter
├── infrastructure/  # PostgreSQL、Redis 与 forward-only migration
├── api/             # Durable API、AG-UI/WebSocket relay 与浏览器 DTO
├── application/     # M0 确定性检索业务基线
└── domain/          # 纯商品、证据、定价、资格与排序模型
```

## 已实现

### M0 业务基线

- 中文规则意图解析，并校验 Required/Preferred 的原文引用；
- manifest、SHA-256、记录数、路径、schema 和版本校验；
- Canonical Product 聚合与合法 Offer 守恒；
- 商品价、运费、税费、关税和版本化汇率组成的到手价；
- 商品级、报价级硬门，以及绝不自动放宽条件的 `NO_MATCH`；
- Query-first 确定性排序和全批次安全降级；
- Verified Claims、证据闭包、Top K 和最终不变量守卫；
- 不可变 `RunJournal`、阶段漏斗、诊断和唯一终态；
- Golden、跨进程确定性、Spec↔Test 追踪、禁网和安全门禁。

### M1d 原生 ReAct Agent

- `Think → Act → Observe → Reflect` 是四个概念阶段，而不是四个额外的模型调用：
  LangGraph 的 model 边界承担 Think/Reflect，tool 边界承担 Act/Observe。代码以不可变
  `AgentLoopDefinition` 集中声明每个 root/child loop 的 `thread_id`、checkpoint namespace、
  `tool_set` 与 `system_prompt`；root/child 共享行为定义，但拥有隔离的执行身份与 checkpoint；
- 业务目录保留九个业务 `@tool`：`planner`、`chat_fallback`、`web_search`、
  `category_insight`、`item_search`、`price_compare`、`shipping_calc`、`item_picker` 与
  `shopping_summary`；控制面额外提供 `dispatch_tool` 与 `parallel_dispatch_tool`。root 在每个
  Think 回合依据最新安全 observation 自主选择继续本地动作还是 fork；runtime 只验证 scope、预算和
  已观察引用，不把多平台或“剩余步骤”编译成固定下一动作；
- 根、子 AgentLoop 共享同一 system prompt、LLM 配置和完整原生工具 schema，但每个子循环有独立
  `thread_id`、LangGraph checkpoint 和 `ShoppingToolSession`。root 独占全局比较与最终用户发布；child
  在缩小的 scope 中可自主多轮行动，也可在继承 scope 与树级 child/depth guard 内继续 fork，完成时只回传结构化
  handoff，不回传消息历史。上下文隔离只会把获授权引用裁剪为私有 typed seed，不复制父循环 transcript；
  fanout、全树 child 数以及每个 Loop 的 fork 批次数都是可配置的防失控上限，不是 03-1 图规定的固定拓扑；
- fork 使用单一 v2 `ForkDemand` 契约，必填 `estimated_tool_calls`。仅当独立 scope 可并发、
  中间上下文需隔离，或预计至少三次工具调用时 fork；单步动作和直接总结由当前 Loop
  完成；
- `src/glodex/agent/llm.py` 只用 `ChatOpenAI` 建模，并以 strict + `parallel_tool_calls=False`
  绑定工具；`src/glodex/agent/graph.py` 用 `create_agent` 和
  `astream_events(version="v2")` 运行、投影安全生命周期事件；
- 每轮最多接受一个 native tool call，完整工具 schema 始终绑定给模型。生产购物 loop 不把原始模型
  直答发布给用户：在可信 terminal tool 前结束会安全失败；任何工具调用只在其自身的可信数据前置条件
  不满足时返回 bounded rejection，不会收到“下一工具”流程队列；
- `ShoppingToolSession` 保留原始 query、可信 intent、向量、候选归属、价格、费用、预算与
  eligibility。模型拿到的是有界的安全 observation（平台、计数、opaque ID、可信属性信号和 handoff
  摘要），不能写入事实或最后回答；已验证 category insight 可形成 runtime-owned 的受控检索 refinement；
- root 的 `shopping_summary` 必须经过 `SearchService` final gate；同名 child 调用只表示“scope 已完成，
  请把事实交回 root”。Tavily 仅能补充非商品事实，不能产生商品、价格、库存或费用事实；
- API/SSE 只映射安全的 Agent 生命周期和 Canonical 结果，从不转发 LangGraph 的 prompt、原始参数、
  工具正文或推理内容。

## 环境与安装

需要 Python `>=3.12,<3.13` 和 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync --locked --dev
uv run --locked glodex --help
```

运行时依赖包含 FastAPI、Pydantic 2 和 HTTPX。本地服务命令使用 dev 依赖组中的 Uvicorn；pytest、pytest-socket、Ruff 和 mypy 也位于 dev 依赖组。

## CLI

所有子命令选项都写在子命令之后。`validate-snapshot` 的快照版本是位置参数。

### 持久运行时与长期记忆

Durable 使用独立的本机服务；应用不会自动拉镜像、启动 Docker 或删除数据。PostgreSQL named
volume 是本机账户、thread、turn、memory 与 Run/Event/Checkpoint 的唯一持久真相；Redis 禁用
AOF/RDB，可随时丢失，最多只会造成 cache miss。不要把卷、`*.env`、memory 内容、request/checkpoint
或 `项目架构/` 的 26 张 PNG 提交到仓库。

```bash
docker compose -f infra/durable-runtime.compose.yml up -d

uv run --locked glodex storage migrate --live
uv run --locked glodex storage verify --live
```

M6 migration 同样由上面的 forward-only 命令执行。留存维护只可由 operator 显式启动，固定清理 14 天
trace 与 30 天 aggregate bucket；它不会在请求路径或启动阶段自动运行：

```bash
uv run --locked glodex operations retain --live
```

成本不是 LLM 服务账单。只有 OpenAI-compatible LLM response 的完整 `usage` 与 operator 明确导入、并在 `agent-api serve`
显式选择的精确 model/version 本机 price row 同时存在时才显示 `REPORTED`；否则固定是 `UNAVAILABLE`。导入
不会联网或读取 LLM 服务账单。下面是 operator 截至 2026-08-01 复核后录入的模型输入 cache-miss 与输出价格
（均为 CNY 每百万 token）的示例：

```bash
uv run --locked glodex model-prices import --live \
  --model "deepseek-v4-flash" \
  --price-table-version operator-cache-miss-2026-08-01 \
  --currency CNY \
  --input-micro-units-per-token 1 \
  --output-micro-units-per-token 2 \
  --source-label operator-reviewed-pricing-2026-08-01
```

当前 receipt 不含输入 cache-hit token 数，因此该表把全部输入按 cache-miss 计，是保守的本机估算，
不是账单金额。价格变动时必须人工复核后导入一个新版本，并在下次启动时显式选择它；不会自动抓取或
默默改用“最新”价格。

Web console 只 relay 两条固定 M6 路径：当前 owner 的
`GET /api/v1/web-console/m6/runs/{run_id}/trace`，以及登录后无身份聚合的
`GET /api/v1/web-console/m6/operations?window=1h|24h`。没有全局 trace 列表、任意筛选、breaker reset 或 exporter
HTTP 配置接口。

M7 不提供在线评测入口，也不会让搜索 SSE 等待 Judge。完成一次搜索后，operator 可以显式生成一个内部报告：

```bash
uv run --locked glodex quality-evaluation run --live \
  --run-id run-... \
  --output ./m7-report.json
```

报告路径必须是已存在目录下的 `.json` 文件；默认拒绝覆盖，确需替换时显式增加 `--overwrite`。完整报告不会被
前端读取；命令同时写入一个 owner-scoped 安全摘要，结果页可显示 P0/P1、三项 P2、reward、训练候选、模型和时间。

默认 `agent-api serve --live` 保持 exporter disabled。只有同时传入 `--m6-export-enabled`、HTTPS
`--m6-export-endpoint` 与仅含环境变量名的 `--m6-export-credential-env` 时，服务才会读取该环境变量并创建
一次性 HTTP exporter；失败只会记录 `TRACE_EXPORT_UNAVAILABLE`，不会重试或影响 shopping terminal。

M5b 只接受网页登录后的 session；旧匿名和 `profile_id` 路径没有迁移或认领入口。可信终态后，后端先用
deterministic prefilter 检查当前输入：`预算 800 元以内`、当前品类和临时排除项不会调用 Reflect；只有“平时、
通常、一直、以后、长期、偏好、不接受、绝不”等明确长期/default 表达才允许一次真实 LLM Reflect。逐字
引文、schema、source span 或 durable gate 任一失败均零写入，不 retry、repair 或规则兜底。

长期记忆只允许 `origin=manual` 与 `origin=explicit_reflect`；迁移直接删除旧自动条目。`GET /api/v1/memory`
只返回 `glodex.user-memory.memory-list.v3` 的 `activeEntries`，没有旧 DTO、双列表或前端 fallback。后续 run
仅对当前条目用本地 A100 BGE 重编码并至多选回 5 条，私有注入真实 LLM action。网页可以编辑/删除自己的
条目，但不会显示 vector、score、prompt、source span 或 LLM response body。
Query Hybrid、BGE rerank、Canonical、Evidence 和 Hard Gates 仍由既有可信链执行：

> 面试数据声明：这条购物链路的 `data_mode` 固定为 `SYNTHETIC_INTERVIEW`。926,710 条公开
> 语义商品文本只用于检索展示；平台、报价、库存、配送以及汇率均为确定性合成事实，不代表 Amazon、
> eBay、Shopee 或 AliExpress 的实时信息。Gateway 为同一商品生成 USD/EUR/SGD 源币种报价，Agent
> 使用 `synthetic-multiplatform-cny-v1` 中带证据的固定汇率换算，Web console 只接受并显示 CNY。
> 旧 `CURRENT_PRODUCT_CATALOG` 数据模式不再接受，也没有规则意图解析或旧 Gateway 回退。
> 商品清洗同样不使用标题关键词分类：全库只接受可信来源类目或正式 Item 向量的 Category Card
> 语义分类；Gateway 再通过已验证的 A100 BGE cross-encoder 清理 Hybrid 候选中的配件误召回。

```bash
docker compose -f infra/opensearch.compose.yml up -d
uv run --locked glodex model-service verify --live
uv run --locked glodex product-index verify --live

# Keep the verified current-product gateway in the foreground on the server.
python opensearch/current-product/current_product_hybrid_gateway.py serve \
  --retrieval-model-manifest /data3/sybai/glodex/current-model/retrieval-model-manifest.json \
  --host 127.0.0.1 --port 18085

# Native ReAct shopping Agent
uv run --env-file .env --locked glodex agent-api serve --live \
  --price-table-version official-cache-miss-2026-08-01
uv run --locked glodex web-console serve --live
```

Durable 的核心本地 smoke 不调用 Provider：它使用 deterministic Agent 验证真实 PostgreSQL event
持久化、终态与 durable SSE suffix replay。离线门禁和本机服务 smoke 分开执行：

```bash
uv run --locked python scripts/verify_durable_runtime.py
uv run --locked python scripts/verify_durable_runtime_local.py
```

若要用 HTTP 访问已经验收的 durable shopping Agent，而不是运行一次性 CLI，可在完成上述 migration、
Retrieval model service index 与显式 `DEEPSEEK_API_KEY` 后启动固定 loopback API。它只监听 `127.0.0.1:8766`；
服务不会自动迁移 schema 或自动读取 `.env`：

```bash
# .env 必须包含 DEEPSEEK_API_KEY、TAVILY_API_KEY 和 LANGGRAPH_AES_KEY
uv run --env-file .env --locked glodex agent-api serve --live
```

`agent-api serve` 需要在监听 8766 之前完整校验一次商品目录、Category Card、OpenSearch
mapping/count/source hash 与 A100 模型身份，并锁定校验通过的物理索引。校验失败则服务不启动；校验
成功后，所有搜索只复用这份不可变身份，不再逐请求解压目录或重算全量哈希。切换目录、索引或模型
版本必须重启服务并重新校验，没有旧的逐请求校验兼容路径。

它只提供下列已冻结端点：`POST /api/v1/durable-agent-runs`、`GET
/api/v1/durable-agent-runs/{run_id}`、`GET /api/v1/durable-agent-runs/{run_id}/events`、`POST
/api/v1/durable-agent-runs/{run_id}/cancel`、`POST /api/v1/durable-agent-runs/{run_id}/resume`。
按 `Ctrl-C` 会停止 API，但不会删除任何 PostgreSQL volume。

停止服务不会删除 PostgreSQL named volume；需要清空 durable 数据时，只能由 Operator 明确执行
`docker compose -f infra/durable-runtime.compose.yml down -v`。平常停止请使用不带 `-v` 的 `down`。
每个 root/child AgentLoop 都使用 AES 加密的 PostgreSQL LangGraph checkpoint，并同时保存严格的 typed action journal。
恢复会重建已确认动作、复用相同 scope digest 的既有 child，并继续未完成的并行分支；只有
`REMOTE_PENDING`、失败子分支或 composition identity 漂移会安全地 `ABORTED`。

### Web console AG-UI / Vue WebSocket Run Console

Web console 是唯一交互面：`127.0.0.1:8766` 是 Durable public API；`127.0.0.1:8767` 是 Web console 的
Vue Console 与受限 AG-UI WebSocket relay。

首次准备前端依赖后，构建并启动 Web console。构建产物不提交；若产物缺失，8767 根页面只会安全提示构建
前提，而不会连接 Durable 或其他服务。

```bash
pnpm --dir frontend install --frozen-lockfile
pnpm --dir frontend run build

uv run --locked glodex web-console serve --live
```

浏览器访问 `http://127.0.0.1:8767`。页面只连接同源 `/api/v1/web-console/ws`；Web console 再通过固定
loopback `8766` 的 Durable public create/status/events/cancel/resume routes 工作。Durable 尚未启动时页面仍能
打开，但提交会诚实显示 `WEB_CONSOLE_UPSTREAM_UNAVAILABLE`。Web console 不启动 Docker、不读取 `.env`、不直接访问
PostgreSQL/Redis/OpenSearch/Provider，也不访问 `18000` GPU service。

要进行真实 durable browser 验收，先按上一节准备并启动 `agent-api serve --live`（包括 PostgreSQL、Redis、
OpenSearch、已验证的 Retrieval Model service 和 `DEEPSEEK_API_KEY`），再启动 Web console。页面必须先注册或
登录本机账户；Web console 只透传 HttpOnly session cookie，永不读取 token 或数据库。当前 tab 的 `New conversation`
创建一个新的内存 thread ID；WebSocket 意外关闭时页面最多新建一次连接，以最后完整消费的 durable cursor 发送
`ATTACH` 重放 suffix，运行中可发送 `CANCEL`。旧的 Web console HTTP/SSE adapter 路由和 `/proxy-ws` 客户端已删除，
没有 SSE fallback。
Durable 的 `resume` public route 仍由后端使用，但当前工作台不显示 Resume 按钮。历史和 memory 管理面板只显示当前 owner 的数据；不会显示 Prompt、tool/LLM response body、vector、
score 或 GPU 信息，也不使用 localStorage。

M5 将完整 conversation turn 作为 PostgreSQL truth。超过最近窗口时，真实 LLM 生成受限 summary；
Redis 只缓存可重建的 summary/recent-window 投影。Redis 丢失不会改变 durable truth、商品事实或恢复语义。
Summary 按连续 ordinal 分批推进，必须与最近三个完整 user/assistant 对无缝衔接；任何摘要失败或历史空洞都会
安全终止本轮上下文构建，不能静默丢掉中间约束。DeepSeek Prompt Cache 当前能力明确为 unsupported；Agent
只在本地 callback metadata 记录 `cache_supported/cache_attempted/cache_hit=false`，不会向 Provider 发送未经
验证的 `cache_control` 字段，也不宣称缓存命中、token 或费用节省。

M4 不增加工具、市场数据或第二个模型。真实购物 action 由 `ChatOpenAI` 的 strict native tool call 表达；
`create_agent` 负责 root/child 同质的 Think → Act → Observe 循环，`astream_events` 只投影安全事件。
`dispatch_tool` 与 `parallel_dispatch_tool` 保留为受限控制面：模型可提出结构化、已观察 scope，runtime
只能授权或拒绝，不能把 child 降成固定 Worker。本机 A100 BGE embedding/reranker 与 OpenSearch 仍只服务
既有 snapshot retrieval。

默认门禁不启动服务、不连接网络或 GPU，并将 Durable browser smoke 与离线证据分开：

```bash
uv run --locked python scripts/verify_web_console.py
uv run --locked python scripts/verify_agent_composition.py
uv run --locked python scripts/verify_m5b.py
```

M5b 的真实闭环必须在当前代码的 8766、PostgreSQL、Redis、OpenSearch 和 A100 Retrieval model service 均已启动，且启动
8766 的同一终端已显式提供 `DEEPSEEK_API_KEY` 后执行 `uv run --locked python scripts/accept_m5b_live.py --live`。验收不得保存或打印
query、逐字引文、memory content、source span、cookie、Prompt、provider body、credential、vector 或 score。

该门禁要求已有 pnpm store，因为 frontend install 使用
`pnpm --dir frontend install --frozen-lockfile --offline --ignore-scripts`；首次依赖获取应在单独的
显式 `pnpm --dir frontend install --frozen-lockfile` 中完成。`frontend/pnpm-lock.yaml` 是唯一锁文件，
不接受 `package-lock.json`。Web console 仅实现冻结的 AG-UI event subset 与同源认证 WebSocket 协议，不宣称完整
AG-UI、worker queue、跨设备多用户或生产监控能力。

### Retrieval model service A100 BGE retrieval model service

Retrieval model service 是受控 GPU 上的显式演示闭环，不会自动下载权重、创建 tunnel、拉起 Docker 或读取 `.env`。
GPU operator 将 private manifest 和权重保留在仓库外，先安装 GPU-only 依赖并启动新仓库提供的
service；应用侧只使用已经由 operator 建立的固定 loopback tunnel。不要把 manifest、权重、日志、
向量、Docker volume 或 `项目架构/` 的 26 张 PNG 提交到 Git。

```bash
# 在受控 GPU 环境；<private-manifest> 位于仓库外
uv sync --locked --group retrieval-model-gpu
uv run --locked --group retrieval-model-gpu glodex model-service serve --manifest <private-manifest>

# 在开发机；只核验固定 loopback service 的安全 identity summary
uv run --locked glodex model-service verify --live
```

health 成功后，operator 可用已经验证的 M1d assets 重建**独立** Retrieval model service BGE aliases。此过程不覆盖
其他检索 aliases，也不会把任何历史向量转换为 BGE vector。M5 对当前用户 memory 在实际 run 中重新
BGE 编码。OpenSearch 仍须由 operator 显式启动；命令不会自行启动或停止它：

```bash
docker compose -f infra/opensearch.compose.yml up -d
uv run --locked glodex product-index build --live
uv run --locked glodex product-index verify --live

```

`agent-api serve --live` 现已由正式 composition root 构造 native ReAct executor。监听 8766
之前，它会依次核验 LLM 配置、128 张 Category Card、Item-vector manifest、Retrieval model service 身份、
current-product gateway/OpenSearch lineage、PostgreSQL、LangGraph checkpoint schema、Redis 与 Tavily
凭据；随后一次性建立 Card 向量索引。
任一身份或服务不一致都会输出 `AGENT_API_PREFLIGHT_FAILED` 并退出，不会回退到 Demo Snapshot、
规则品类映射或伪造购物结果。

Retrieval model service service 只接收受限长度的当前 query 和由可信 assets 回读的 item/Card 文本；它不接收 credential、
用户标识、持久 Run、最终商品事实或 OpenSearch document。它的 CLI
只输出状态、count、safe code、aggregate metric 和 manifest digest 前缀。默认验证绝不连接 GPU、tunnel、
OpenSearch 或 Provider；它先执行 Durable baseline，再以 socket-blocked
tests 验证 Retrieval model service fake contracts。无 GPU 或无受控 tunnel 时，不将 Retrieval model service 标为可用：

```bash
uv run --locked python scripts/verify_retrieval_model.py
```

停止 GPU service 使用该 service 前台进程的 `Ctrl-C`；tunnel 由建立它的 operator 在仓库外单独
停止。不要运行带 volume 删除的 Docker 命令，Retrieval model service 也不会自动清理 OpenSearch/Durable 数据。README 不记录 remote host、模型路径/hash、SSH command、credential 或 service response。

Web console 已交付受限 AG-UI/Vue interaction layer；多 worker、生产 queue、完整 AG-UI 与远程部署仍未
实现。Retrieval model service 不承诺训练、模型泛化、商业召回率、高可用或生产吞吐。

```bash
# 校验本地快照
uv run --locked glodex validate-snapshot m0-v1

# 运行固定的离线演示请求
uv run --locked glodex demo

# 提交自定义搜索
uv run --locked glodex search \
  --query "推荐 800 美元以内、有库存、适合出差的轻薄本" \
  --snapshot m0-v1 \
  --currency USD \
  --top-k 3
```

从任意非项目目录执行时，使用 uv 的 `--project`，并显式传入配置文件：

```bash
export GLODEX_PROJECT=/absolute/path/to/glodex
cd /tmp

uv run --project "$GLODEX_PROJECT" --locked glodex \
  validate-snapshot m0-v1 \
  --config "$GLODEX_PROJECT/glodex.toml"

uv run --project "$GLODEX_PROJECT" --locked glodex \
  demo \
  --config "$GLODEX_PROJECT/glodex.toml"

uv run --project "$GLODEX_PROJECT" --locked glodex \
  search \
  --config "$GLODEX_PROJECT/glodex.toml" \
  --query "推荐 800 美元以内、有库存、适合出差的轻薄本" \
  --snapshot m0-v1 \
  --currency USD \
  --top-k 3
```

搜索、演示和快照校验在 stdout 输出一行 JSON；请求或配置被拒绝时，稳定的机器可读结果仍在 stdout，简短诊断写入 stderr。

### 退出码

| 退出码 | 含义 |
|---:|---|
| `0` | 搜索为 `COMPLETED`/`NO_MATCH`，或快照有效 |
| `1` | 搜索为 `FAILED`，或快照无效 |
| `2` | CLI、配置或请求在运行前被拒绝 |

`NO_MATCH` 是成功执行后没有候选满足全部硬约束，不是系统错误。

## 配置

默认配置为 [`glodex.toml`](./glodex.toml)：

```toml
[app]
data_dir = "data/snapshots"
default_snapshot = "m0-v1"

[search]
default_locale = "zh-CN"
default_currency = "USD"
default_top_k = 3
```

配置文件选择顺序是：

1. 子命令的 `--config`；
2. `GLODEX_CONFIG`；
3. 包位置对应项目根下的 `glodex.toml`，不搜索当前工作目录。

环境覆盖项为 `GLODEX_DATA_DIR`、`GLODEX_DEFAULT_SNAPSHOT`、`GLODEX_DEFAULT_LOCALE`、`GLODEX_DEFAULT_CURRENCY` 和 `GLODEX_DEFAULT_TOP_K`。请求参数再覆盖对应默认值。相对 `data_dir` 以配置文件所在目录为基准。

M0 只支持 `zh-CN`；币种必须为三位大写字母；`top_k` 必须在 1–3。未知配置键、无效值或缺失的显式配置都会 fail closed。普通业务配置和默认搜索/API 不读取
`.env` 或密钥。正式 Agent live 路径只读取 `DEEPSEEK_API_KEY`；embedding 与 rerank 固定调用已验证的本地 BGE 服务。

## 快照

[`data/snapshots/m0-v1`](./data/snapshots/m0-v1) 包含 manifest、products、offers、evidence 和 exchange rates。加载器验证文件哈希、原始记录数、路径、版本和 schema：

- 单条脏记录进入 quarantine，其他合法记录可继续；
- manifest、哈希、核心文件或版本错误是 fatal，不能产生部分可信结果；
- 输出证据不仅匹配 ID，还校验 entity、field path、provider、offer/product 和 snapshot 归属。

## Golden、追踪与门禁

```bash
# 当前交付门禁：覆盖 Durable–M7 与 M5b 语义修正，不连接 live 服务
uv run --locked python scripts/verify_m5b.py

# 所有显式 live 前置条件健康后，按本仓库的固定服务合同执行人工 browser 验收。

# Golden 只检查，不写入
uv run --locked python scripts/update_goldens.py --check

# 检查全部 P0、AC 和 NFR 的正反向覆盖
uv run --locked python scripts/check_traceability.py --mode coverage

# 运行完整 M0 门禁
uv run --locked python scripts/verify_m0.py

```

`scripts/verify_m0.py` 依次检查锁文件、格式、lint、类型、离线/安全/架构、unit/contract/generated、全部 AC、Golden、20 进程确定性、traceability 和 20k/100 性能工作负载。任一步失败都会整体非零，门禁不会 skip/xfail 或自动更新 Golden。

Golden 的 `--write` 只应在已批准的语义变更后人工执行，并先审阅 diff。

### 性能结果如何解释

性能门禁固定生成 20,000 个商品，预热 10 次，再顺序测量 100 次请求，以 nearest-rank 计算 p50/p95/max。绝对耗时会随 CPU、Python 构建和机器负载变化：

- 普通本地运行完整工作负载并输出信息性指标；
- 只有明确标记的参考 CI 才强制 p95 不超过 2 秒：

```bash
GLODEX_REFERENCE_CI=1 uv run --locked python scripts/verify_m0.py
```

不要在配置不同的本地机器上设置该标识并据此比较绝对秒数。

## 常见问题

- **Python 版本不匹配**：使用 Python 3.12，并重新执行 `uv sync --locked --dev`。
- **锁文件不一致**：不要绕过 `--locked`；先确认是否真的发生了已批准的依赖变更。
- **`CONFIG_NOT_FOUND`**：检查显式 `--config` 或 `GLODEX_CONFIG` 指向的文件。
- **`CONFIG_INVALID`**：检查未知键、`top_k > 3`、小写币种和不受支持的 locale。
- **从其他目录找不到数据**：使用上面的 `--project` 与绝对 `--config` 写法。
- **快照或币种失败**：先运行 `validate-snapshot`，查看 JSON 中的 `issues`。
- **结果为 `NO_MATCH`**：查看 filter funnel 和 reason counts；系统不会自动放宽预算或其他 Required 条件。
- **结果为 `FAILED`**：查看 `diagnostics.issues`；它表示输入、数据合同或执行不再可信。

## Agent 边界

- 这是由 `DEEPSEEK_API_KEY` 激活、固定 `deepseek-v4-flash` 和九工具目录的
  有界购物 Agent，不是通用模型网关、动态工具系统或开放式代码执行环境；
- 商品检索只接受启动时完成身份校验的 current-product 索引；Tavily 网页证据不能充当商品事实；
- Agent 远端动作不保证跨调用确定；本地 schema、Candidate binding、金额、hard
  gates、Evidence closure 和最终 `SearchResponse` 继续 fail closed；
- 单独调用业务工具不自带存储；正式 Durable runtime 为 root/child run 保存独立 checkpoint，
  `REMOTE_PENDING` 与身份漂移一律 fail closed，绝不重放不确定的外部动作；
- 没有 retry、fallback model、任意网页抓取、认证、租户隔离或生产 SLA；
- Agent public SSE 使用严格的 `glodex.agent.event.v4`；它不是 AG-UI，且没有
  AG-UI SDK、WebSocket 或 Vue UI。Web console 是独立的 Durable projection，而非对该 endpoint 的改写；
- `ESTIMATE`/`UNKNOWN` 会如实保留，不能被解释为精确费用、零费用或平台报价保证。

## 后续范围

M1a/M1b/M1c 的独立旧路径均已归档并从运行时移除；M1d 已交付原生 `ChatOpenAI` + `@tool` +
`create_agent` 的完整验收，Web console 已交付本机 AG-UI/Vue durable interaction layer，M4
已交付 strict serial native-tool 调用与 state-owned hard gates，M5 已交付 owner-scoped history/memory，M5b 已将未来消费收紧为 active-only 明确长期记忆，
M6 已交付受限观测与运行保护，
M7 已交付离线真实 OpenAI-compatible LLM Rubric/P2 judge、deterministic P0/P1、前端证据覆盖与安全摘要；
以下能力仍明确延期：

- **后续 M1/M2**：如取得正式授权后再评估 live marketplace adapter、完整 AG-UI 与实时前端扩展；
- **后续方向**：隐式偏好学习、训练型三塔/融合、raw dataset/SFT/RL、已验证的 LLM Prompt Cache、完整实时通信或生产监控；
- **范围外**：生产部署、实时/全量商品覆盖、Provider 可用性、价格时效、真实推荐
  质量承诺和开放式任意工具 Agent。

## 规格与决策记录

- [确定性领域核心](./specs/000-glodex-mvp/spec.md)
- [持久化 Agent Runtime](./specs/008-glodex-durable-agent-runtime/spec.md)
- [检索模型服务](./specs/009-glodex-retrieval-model-service/spec.md)
- [Web Console](./specs/010-glodex-web-console/spec.md)
- [Agent 组装](./specs/011-glodex-agent-composition/spec.md)
- [真实购物动作可靠性](./specs/013-glodex-m4-real-shopping-reliability/spec.md)
- [用户身份、历史与记忆](./specs/014-glodex-m5-user-memory-identity/spec.md)
- [长期记忆语义](./specs/019-glodex-m5b-memory-semantics/spec.md)
- [可观测性、成本与运行保护](./specs/015-glodex-m6-observability-operations/spec.md)
- [离线质量评测](./specs/016-glodex-m7-rubric-quality-loop/spec.md)
- [WebSocket 实时通信](./specs/018-glodex-m9-websocket-realtime/spec.md)
- [ADR-0001：确定性领域核心](./specs/000-glodex-mvp/adr/0001-deterministic-domain-core.md)
- [ADR-0002：金额、汇率与舍入](./specs/000-glodex-mvp/adr/0002-money-fx-and-rounding.md)
- [ADR-0003：硬门与排序降级](./specs/000-glodex-mvp/adr/0003-hard-gates-and-ranking-degradation.md)
