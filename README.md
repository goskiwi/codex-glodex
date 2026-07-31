# Glodex

Glodex 目前包含十个叠加的里程碑：

- **M0** 是本地、离线、确定性的跨市场购物检索业务基线。它接收中文购物请求，从版本化快照中聚合同款商品和各市场报价，使用 `Decimal` 计算到手价，在排序前执行预算、品类、库存、商品本体和证据硬门，最后返回最多 3 个可核验结果。
- **M1a** 在不改变 M0 业务合同与不变量的前提下，增加一个本地 FastAPI + SSE 服务化垂直切片。
- **M1b** 增加一个 operator-only、显式 opt-in 的 eBay Buy Browse Capture 链路，把一个有界的真实结果页归一化为现有 manifest v1 snapshot；搜索、API 和 SSE 仍只读取本地快照。
- **M1c** 增加一个 operator-only、显式 opt-in 的固定 DeepSeek Intent 适配器；模型只产生待校验 Intent，完整搜索仍复用既有本地快照、硬门、排序、Evidence、CLI、API 与 SSE。
- **M1d** 增加一个 operator-only、显式 opt-in 的固定 DeepSeek AgentLoop，完整组装九个业务工具、`dispatch_tool`、最多四路子 Agent、Hybrid Category RAG、Tavily evidence 与 eBay live item search；最终商品仍必须通过既有 Canonical hard gates 与 Evidence closure。
- **M1e** 增加一个独立、公开、可复现的 ESCI 历史检索/重排 benchmark；它只评测固定候选池，不进入 Catalog、搜索、Agent、API 或 SSE。
- **M1f** 增加一个零凭据、本地静态 Showcase：它回放安全的 M1d 公开事件投影，并展示 M1e 的聚合 benchmark 证据；它不是实时 Agent 或 marketplace UI。
- **M2a** 增加一个 operator-only 的本机检索智能闭环：local OpenSearch Query Hybrid、显式 typed profile 的 User ANN 补充、固定 DashScope `qwen3-rerank` 与 Category Card rerank；它复用 M1d 的 AgentLoop 和最终 Hard Gates，但不改变默认 Agent/API/SSE。
- **M2b** 增加一个 operator-only 的本机 durable runtime：PostgreSQL 保存 Run、安全 SSE event、checkpoint 与 typed Profile；Redis 只缓存可重建的 retrieval/context 投影，OpenSearch User ANN 只接收当前 Profile revision 的可丢弃投影。
- **M2c** 增加一个 operator-only 的私有 GPU BGE retrieval model service：固定 loopback model verifier、独立 BGE Product/Card/Profile aliases 与 cross-encoder rerank；它复用 M1d 的可信发布 gates，但不改变 M2a DashScope 或 M2b durable backend。
- **M2d** 增加一个本机 AG-UI adapter 与 React Run Console：它只投影 M2b 已冻结的 public durable API，在 `127.0.0.1:8767` 提供同源交互、重连与受控 Cancel/Resume；它不读取 DB/Redis、不会把 M2c GPU 暴露给浏览器。

M0 仍是可保留的业务与合同基线；M1a 是单进程 Demo，不是生产 Agent 平台。M1b
Capture 与 M1c live Intent 都只能由 Operator 通过各自独立入口显式联网；默认
CLI/API 仍使用 Rule Intent 且只读取本地快照。M2a 的 profile 仅是 Operator 明确写入的本机
typed soft preference，不会保存聊天记录、隐式画像或跨设备用户数据。M1d 是独立 Agent 入口；
M1e 是本地 benchmark 入口；M1f 是独立静态展示入口；M2a 也不改变这些默认行为。
M2b 同样不会改变它们：只有 `m2b-*` 命令和独立 durable API factory 才会连接 PostgreSQL 或 Redis。M2c
也同样隔离：只有带 `--live` 的 `m2c-*` operator command 才会连接既有 loopback tunnel；M2d 是唯一
AG-UI/React 投影层，且固定只读取 M2b `8766` public routes。默认 M0–M2c、M1f 与 M2d 离线门禁
不会加载 GPU 依赖、读取 tunnel 或连接模型服务。

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

### M1a 本地 API

- FastAPI application factory；公开接口严格限制为三个端点：创建 Run、读取状态、订阅事件；
- 进程内 `RunRegistry`、同 Thread 活动 Run 冲突保护、显式 Run/Event/Subscriber 上限；
- SSE 实时事件、晚连接完整重放，以及通过 `Last-Event-ID` 进行同进程后缀重放；
- `COMPLETED`、`NO_MATCH`、`FAILED` 业务终态和 transport-only `ABORTED`；
- 版本化安全错误 envelope，不向客户端返回异常详情、请求内容、密钥或本机路径；
- 202 响应发送后才启动业务执行，Subscriber 断线不会取消 Runner。

### M1b Provider Capture

- `capture-provider --live` 是 operator-only 的显式联网入口，固定使用 eBay Buy Browse、`EBAY_US`、USD 和 phone 品类；
- Capture 最多发出一次认证请求和一次商品请求，不分页、不重试，也不跟随重定向；
- Provider item 先归一化、隔离坏记录，再经现有 loader 与 aggregation 反向验证后原子发布；
- Browse 未披露库存时保留 `UNKNOWN`；未披露的 shipping、tax 或 duty 保留 `UnknownCost`，不伪造为零；
- 每个已发布快照（包括合法空快照）都包含 USD identity FX 与对应 Evidence；
- Capture 只生成不可变本地快照，后续 `validate-snapshot`、`search`、API 和 SSE 不再访问 Provider。

### M1c DeepSeek Intent

- 默认 `demo`、`search` 和 API factory 继续使用 `RuleIntentInterpreter`，不读取模型凭据或访问模型网络；
- 只有 `search --live-intent` 与独立 live API factory 能启用固定
  DeepSeek Open Platform / `deepseek-v4-flash`；
- 执行顺序固定为 Rule Required baseline → 一次模型调用 → 既有 Intent validator
  → Required 等值完整性检查 → 既有 Catalog、hard gates、ranking 与 Evidence；
- Provider、解析或 Required 完整性故障在 Intent 阶段形成业务 `FAILED`，不回退
  Rule，也不进入 Catalog/Ranker；SSE 使用既有 `RUN_ERROR`，不是 transport-only
  `ABORTED`；
- 生产 adapter 下方可注入窄 Fake transport，因此默认自动化仍然确定、禁网且不读取
  真实凭据。

### M1d 全工具 AgentLoop

- 固定工具目录恰好包含九个业务工具：
  `planner`、`chat_fallback`、`web_search`、`category_insight`、`item_search`、
  `item_picker`、`price_compare`、`shipping_calc`、`shopping_summary`，另有一个
  `dispatch_tool` 元工具；
- root 采用显式有界 Think/Act/Observe 循环；`dispatch_tool` 可并发创建最多四个
  platform-scoped child，fork depth、数量、调用次数、Observation 和 deadline 均有
  固定上限；
- Category RAG 使用版本化本地 Cards、BM25、DashScope query embedding 与 RRF；
  四平台 Demo item search 使用版本化非空 Snapshot，只有 eBay 提供显式 live
  marketplace adapter；
- Tavily 结果只作为评测、指南和趋势证据，不能产生商品、价格、库存或费用事实；
- 跨平台候选经 Candidate Store、FX/包装归一、运费/关税状态与共享
  `EligibilityEvaluator` 后，最终仍由既有 `SearchService` 发布；
- 独立 Agent result、API 和 SSE 只暴露安全状态、工具名、fork 进度与 Canonical
  结果，不暴露 prompt、模型原文、工具正文或思维链。

### M1e ESCI 离线检索基准

- 只使用 Amazon Science Shopping Queries Dataset（ESCI）的固定、英文 US test-pool
  小样本，运行确定性 BM25 重排及 `Exact@10`、`MRR@10`、`nDCG@10`；
- 数据是 Apache-2.0 的历史离线 benchmark 证据，不是 Amazon live 搜索、当前商品价格、
  库存、配送或购物推荐；
- 原始 Parquet/CSV 必须保留在 checkout 外；派生 artifact 才可保留其 `LICENSE`、`NOTICE`
  和 attribution。

### M1f 本地离线 Showcase

- 只从版本化本地 JSON 回放 M1d 的安全事件，明确标注“本地录制回放，非实时 Agent 运行”；
- 固定展示九个业务工具和 `dispatch_tool` 元工具，并严格区分本次已执行与系统可用能力；
- 只展示已提交 ESCI artifact 的聚合指标和 provenance，不向浏览器提供 query、商品文本或逐条标签；
- 页面不调用模型、Provider、Agent API、SSE 或外部资源，静态服务只绑定 `127.0.0.1`。

### M2a 本机检索智能闭环

- `opensearchproject/opensearch:2.17.0` 仅以 single-node、`127.0.0.1:9200` 运行；没有远程
  endpoint、Dashboards、Postgres、Redis 或持久 Docker volume；
- Product/Card 索引只取已经 hash-closed 的 M1d assets。OpenSearch 返回不透明 identity/rank，
  商品、价格、Evidence 和 Card facts 均回读既有可信资产；
- 当前 Query 的 BM25 + k-NN Hybrid Top-30 始终受保护；显式 Profile 只可补充至多 10 个
  User ANN candidate，不能替换或修改当前 Required/Hard Gates；
- `qwen3-rerank` 仅在 `m2a-profile set --live` 或 `m2a-agent-demo --live` 的显式入口使用。
  默认测试、M0–M1f 命令、API、SSE 和 M1f Showcase 不会连接 OpenSearch 或读取云凭据。

## 环境与安装

需要 Python `>=3.12,<3.13` 和 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync --locked --dev
uv run --locked glodex --help
```

运行时依赖包含 FastAPI、Pydantic 2 和 HTTPX。本地服务命令使用 dev 依赖组中的 Uvicorn；pytest、pytest-socket、Ruff 和 mypy 也位于 dev 依赖组。

## CLI

所有子命令选项都写在子命令之后。`validate-snapshot` 的快照版本是位置参数。

### M1e ESCI benchmark

先从 [Amazon Science ESCI 官方仓库](https://github.com/amazon-science/esci-data) 获取数据，
并将 checkout 放在仓库外。构建器只读取这个本地目录，不下载或联网；Apache-2.0 的
`LICENSE`、`NOTICE` 与 attribution 会进入派生 artifact。

```bash
git clone https://github.com/amazon-science/esci-data.git /absolute/external/esci-data
ESCI_REVISION="$(git -C /absolute/external/esci-data rev-parse HEAD)"

uv run --group dev --locked python scripts/build_esci_benchmark.py \
  --source-root /absolute/external/esci-data \
  --source-revision "$ESCI_REVISION" \
  --output-root data/benchmarks/esci-small-us-v1

uv run --locked glodex benchmark-esci \
  --artifact-root data/benchmarks/esci-small-us-v1
```

这份输出只包含聚合检索指标；它不证明 Amazon 的实时搜索、价格、库存、配送或推荐质量。

### M1f 本地 Showcase

先验证静态证据，再启动仅限本机的静态服务：

```bash
uv run --locked python scripts/validate_m1f_showcase.py
uv run --locked python scripts/serve_m1f_showcase.py
```

然后在浏览器打开 `http://127.0.0.1:8765/`。页面是本地录制回放，不执行真实 Agent，
也不表示当前 marketplace 数据。

### M2a OpenSearch、Profile 与 Rerank

先由 Operator 手动启动/停止本机 OpenSearch；应用不会自动拉镜像或启动 Docker：

```bash
docker compose -f infra/m2a-opensearch.compose.yml up -d

uv run --locked glodex m2a-index --action build --snapshot m1d-demo-v1
uv run --locked glodex m2a-index --action verify --snapshot m1d-demo-v1
```

显式 profile 写入需要 DashScope embedding。它只写入 `profile_id/scope/kind/value` 的 typed
soft preference；`list` 只返回 opaque entry ID，避免把 preference 正文写进终端记录：

```bash
export DASHSCOPE_API_KEY="..."

uv run --locked glodex m2a-profile --action set --live \
  --profile local-demo --scope soft --kind preference --value "轻薄、长续航"
uv run --locked glodex m2a-profile --action list --profile local-demo
```

M2a Agent 是独立入口。它还需要既有的 DeepSeek selector 凭据；OpenSearch Query Hybrid 和
DashScope embedding/rerank 均实际执行，最后仍通过 M1d 的 Canonical、价格、运费、Hard Gates
和 Evidence closure：

```bash
export DEEPSEEK_API_KEY="..."

uv run --locked glodex m2a-agent-demo --live --profile local-demo \
  --query "在四个平台找手机，比较到手价" \
  --locale zh-CN --currency CNY --top-k 3 --snapshot m1d-demo-v1
```

如果已在仓库外构建 M1e 的 ESCI artifact，可显式比较本地 coarse 与 `qwen3-rerank` 聚合指标。
它先完成每个固定 candidate pool 的 rerank，之后才读取 labels；不把 ESCI 文本、labels 或结果
写入 OpenSearch/Catalog/Agent：

```bash
uv run --locked glodex m2a-eval-esci --live \
  --artifact-root data/benchmarks/esci-small-us-v1
```

离线回归不依赖 Docker 或 Provider；真实 smoke 需在 Docker/凭据可用时分别执行：

```bash
uv run --locked python scripts/verify_m2a.py
uv run --locked python scripts/verify_m2a_opensearch.py
uv run --locked python scripts/verify_m2a_dashscope.py

docker compose -f infra/m2a-opensearch.compose.yml down
```

M2a 仍是 8 商品/8 Card 的学生本机 demo，不承诺商业质量、召回率、生产吞吐、账号同步或完整
AG-UI；Redis/Postgres durable memory 已由 M2b 的显式本机 runtime 提供，固定私有 GPU BGE
retrieval service 由 M2c 的显式路径提供。

### M2b durable runtime 与长期记忆

M2b 使用独立的本机服务；应用不会自动拉镜像、启动 Docker 或删除数据。PostgreSQL named
volume 是 Run/Event/Checkpoint/Profile 的唯一持久真相；Redis 禁用 AOF/RDB，可随时丢失，最多只会
造成 cache miss。不要把卷、`*.env`、Profile value/vector、request/checkpoint 或 `项目架构/` 的
26 张 PNG 提交到仓库。

```bash
docker compose -f infra/m2b-durable.compose.yml up -d

uv run --locked glodex m2b-migrate --live
uv run --locked glodex m2b-verify --live
```

持久 Profile 只有 `soft/preference`，`list` 只输出 opaque entry ID 和 revision。写入 embedding
仍需显式 DashScope 凭据；不要在终端记录真实偏好正文：

```bash
export DASHSCOPE_API_KEY="..."

uv run --locked glodex m2b-profile --action set --live \
  --profile local-durable --value "轻薄、长续航"
uv run --locked glodex m2b-profile --action list --profile local-durable
```

运行真实 durable M2a Agent 前，先显式启动/构建 M2a OpenSearch index，并提供既有 DeepSeek 和
DashScope 凭据。M2b 先从 PostgreSQL 冻结 Profile revision，再投影到 OpenSearch User ANN；Query
Hybrid、rerank、Canonical、Evidence 和 Hard Gates 仍由既有 M2a/M1d 链路执行：

```bash
docker compose -f infra/m2a-opensearch.compose.yml up -d
uv run --locked glodex m2a-index --action build --snapshot m1d-demo-v1

export DEEPSEEK_API_KEY="..."
uv run --locked glodex m2b-durable-agent-demo --live --profile local-durable \
  --query "在四个平台找手机，比较到手价" --locale zh-CN --currency CNY --top-k 3
```

M2b 的核心本地 smoke 不调用 Provider：它使用 deterministic Agent 验证真实 PostgreSQL event
持久化、终态与 SSE suffix replay。离线门禁和本机服务 smoke 分开执行：

```bash
uv run --locked python scripts/verify_m2b.py
uv run --locked python scripts/verify_m2b_local.py
```

若要用 HTTP 访问已经验收的 durable Agent，而不是运行一次性 CLI，可在完成上述 migration、
OpenSearch index 和显式凭据注入后启动固定 loopback API。它只监听 `127.0.0.1:8766`；8765
仍是静态 Showcase，不是 durable API。服务不会自动迁移 schema 或自动读取 `.env`：

```bash
export DEEPSEEK_API_KEY="..."
export DASHSCOPE_API_KEY="..."
uv run --locked glodex m2b-serve --live
```

它只提供下列已冻结端点：`POST /api/v1/durable-agent-runs`、`GET
/api/v1/durable-agent-runs/{run_id}`、`GET /api/v1/durable-agent-runs/{run_id}/events`、`POST
/api/v1/durable-agent-runs/{run_id}/cancel`、`POST /api/v1/durable-agent-runs/{run_id}/resume`。
按 `Ctrl-C` 会停止 API，但不会删除任何 PostgreSQL volume。

停止服务不会删除 PostgreSQL named volume；需要清空 durable 数据时，只能由 Operator 明确执行
`docker compose -f infra/m2b-durable.compose.yml down -v`。平常停止请使用不带 `-v` 的 `down`。
当前恢复策略只允许没有进入外部 Agent 调用的已确认初始 checkpoint 重新执行；一旦整个 Agent
外部执行 fence 已进入 `REMOTE_PENDING`，重启/恢复会安全地 `ABORTED`，不会猜测或重放外部调用。

### M2d AG-UI / React Run Console

M2d 是实时交互面，不替代 M1f。三个本机入口的职责固定如下：`127.0.0.1:8765` 是静态录制
Showcase；`127.0.0.1:8766` 是 M2b durable public API；`127.0.0.1:8767` 才是 M2d 的 React
Console 与受限 AG-UI HTTP/SSE adapter。

首次准备前端依赖后，构建并启动 M2d。构建产物不提交；若产物缺失，8767 根页面只会安全提示构建
前提，而不会连接 M2b 或其他服务。

```bash
npm --prefix frontend ci
npm --prefix frontend run build

uv run --locked glodex m2d-serve --live
```

浏览器访问 `http://127.0.0.1:8767`。页面提交时只向同源 `/api/v1/m2d` 发请求；M2d 再通过固定
loopback `8766` 的 M2b public create/status/events/cancel/resume routes 工作。M2b 尚未启动时页面仍能
打开，但提交会诚实显示 `M2D_UPSTREAM_UNAVAILABLE`。M2d 不启动 Docker、不读取 `.env`、不直接访问
PostgreSQL/Redis/OpenSearch/Provider，也不访问 `18000` GPU service。

要进行真实 durable browser 验收，先按上一节准备并启动既有 M2b `m2b-serve --live`（包括它所需的
PostgreSQL、Redis、OpenSearch 和显式 credential 前提），再启动 M2d。页面只在内存保存当前表单；
`Reconnect` 从 durable cursor 重放，`Cancel`/`Resume` 只代理 M2b public operation，`New run` 只清
浏览器视图，不删除任何 durable 数据。页面不会展示或持久化 query history、profile、tool 参数/输出、
推理、Provider body、DB/Redis 数据、vector/score 或 GPU 信息。

默认门禁不启动服务、不连接网络或 GPU，并将 M2b browser smoke 与离线证据分开：

```bash
uv run --locked python scripts/verify_m2d.py
```

该门禁要求已有 npm cache，因为 frontend install 使用 `npm ci --offline`；首次依赖获取应在单独的
显式 `npm --prefix frontend ci` 中完成。M2d 仅兼容冻结的 AG-UI HTTP/SSE event subset，不宣称完整
AG-UI、WebSocket、worker queue、多用户/账号或生产监控能力。

### M2c A100 BGE retrieval model service

M2c 是受控 GPU 上的显式演示闭环，不会自动下载权重、创建 tunnel、拉起 Docker 或读取 `.env`。
GPU operator 将 private manifest 和权重保留在仓库外，先安装 GPU-only 依赖并启动新仓库提供的
service；应用侧只使用已经由 operator 建立的固定 loopback tunnel。不要把 manifest、权重、日志、
向量、profile value、Docker volume 或 `项目架构/` 的 26 张 PNG 提交到 Git。

```bash
# 在受控 GPU 环境；<private-manifest> 位于仓库外
uv sync --locked --group m2c-gpu
uv run --locked --group m2c-gpu glodex m2c-gpu-service --manifest <private-manifest>

# 在开发机；只核验固定 loopback service 的安全 identity summary
uv run --locked glodex m2c-model-verify --live
```

health 成功后，operator 可用已经验证的 M1d assets 重建**独立** M2c BGE aliases。此过程不覆盖
M2a aliases，也不会把 DashScope vector 或 M2b profile 转换为 BGE vector。OpenSearch 仍须由
operator 显式启动；命令不会自行启动或停止它：

```bash
docker compose -f infra/m2a-opensearch.compose.yml up -d
uv run --locked glodex m2c-index --action build --snapshot m1d-demo-v1 --live
uv run --locked glodex m2c-index --action verify --snapshot m1d-demo-v1 --live

# 只写入一条显式 typed soft preference；不要在终端历史中保存真实偏好正文
uv run --locked glodex m2c-profile --action set --live \
  --profile m2c-demo --value <soft-preference>
uv run --locked glodex m2c-profile --action list --live --profile m2c-demo
```

`m2c-agent-demo --live` 保持 M1d 的九工具、Canonical、Evidence 与 Hard Gates；DeepSeek 只负责既有
selector，BGE service 只负责 embedding/rerank。运行 Agent 前，operator 在当前 shell 提供既有的
DeepSeek credential，不把其值写入 README、命令历史或 Git：

```bash
uv run --locked glodex m2c-agent-demo --live --profile m2c-demo \
  --query "在四个平台找手机，比较到手价" \
  --locale zh-CN --currency CNY --top-k 3 --snapshot m1d-demo-v1
```

M2c service 只接收受限长度的当前 query、显式 soft preference 和由可信 assets 回读的 item/Card
文本；它不接收 credential、用户标识、持久 Run、最终商品事实或 OpenSearch document。它的 CLI
只输出状态、count、safe code、aggregate metric 和 manifest digest 前缀。M1e 对比也必须显式走
GPU cross-encoder；它先完成固定 candidate pool 的 rerank，之后才读取 labels，且只输出聚合指标：

```bash
uv run --locked glodex m2c-eval-esci --live \
  --artifact-root data/benchmarks/esci-small-us-v1
```

默认验证绝不连接 GPU、tunnel、OpenSearch 或 Provider；它先执行 M2b baseline，再以 socket-blocked
tests 验证 M2c fake contracts。无 GPU 或无受控 tunnel 时，只运行 M2a baseline，不将 M2c 标为可用：

```bash
uv run --locked python scripts/verify_m2c.py
```

停止 GPU service 使用该 service 前台进程的 `Ctrl-C`；tunnel 由建立它的 operator 在仓库外单独
停止。不要运行带 volume 删除的 Docker 命令，M2c 也不会自动清理 M2a/M2b 数据。README 不记录 remote host、模型路径/hash、SSH command、credential 或 service response。

M2d 已交付受限 AG-UI/React interaction layer；多 worker、生产 queue、完整 AG-UI 与远程部署仍未
实现。M2c 不承诺训练、模型泛化、商业召回率、高可用或生产吞吐。

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

M1c live Intent 必须由 Operator 显式开启。它会把完整的 trimmed query 与固定
`zh-CN` 发送给 DeepSeek，因此只应用于本地 Demo 的非敏感输入。它不发送
用户、Thread、Run、snapshot、商品或搜索结果数据；项目不承诺控制 Provider 的训练、
处理地域或保留策略。凭据只从当前进程的 `DEEPSEEK_API_KEY` 读取，不自动加载
`.env`：

```bash
read -r -s DEEPSEEK_API_KEY
export DEEPSEEK_API_KEY
uv run --locked glodex search --live-intent \
  --query "推荐 800 美元以内、有库存、适合出差的轻薄本" \
  --snapshot m0-v1 --currency USD --top-k 3
unset DEEPSEEK_API_KEY
```

目标固定为 `deepseek-v4-flash`：每个已建立 Run 至多一个非流式 POST，没有 retry、
fallback、tool call 或 cache；total deadline 为 15 秒，decoded response body
上限为 65,536 bytes，`max_tokens=1024`。模型输出仍是不可信输入，必须通过本地
schema、source span、语义与 Required 完整性校验。

M1d 使用独立的 `agent-demo` 命令，必须显式提供 `--live`。不带 `--live-data` 时，
商品数据固定来自本地 `m1d-demo-v1`：Amazon、Shopee、AliExpress 和 eBay 都是带
版本与来源的 Demo Snapshot，不是平台实时 API。该模式仍会调用 DeepSeek，并在
Category 或 Demo item 向量检索实际需要时调用一次 DashScope：

```bash
export DEEPSEEK_API_KEY="..."
export DASHSCOPE_API_KEY="..."

uv run --locked glodex agent-demo --live \
  --query "在四个平台找手机，比较到手价" \
  --locale zh-CN --currency CNY --top-k 3 \
  --snapshot m1d-demo-v1
```

`--live-data` 把数据模式切换为 live marketplace，进一步启用 Tavily evidence 和
eBay Buy Browse；它拒绝 `--snapshot`，并要求 checkout 外、已存在且权限恰为
`0700` 的绝对 `--output-root`。Amazon、Shopee 和 AliExpress 在该模式没有 live
adapter，也不会回落到网页抓取：

```bash
install -d -m 700 /absolute/external/glodex-m1d-live
export TAVILY_API_KEY="..."
export EBAY_APP_ID="..."
export EBAY_CERT_ID="..."

uv run --locked glodex agent-demo --live --live-data \
  --query "在 eBay 找手机，先分析品类并参考近期评测" \
  --locale zh-CN --currency CNY --top-k 3 \
  --output-root /absolute/external/glodex-m1d-live
```

`agent-demo` 不自动加载 `.env`，只读取当前进程中的四类 Provider 凭据。每轮
DeepSeek 会收到
完整 trimmed query、locale、display currency，以及有界的安全 Observation
（阶段、允许/已执行工具、平台、候选计数、不透明候选 ID 和安全 code）。DashScope
最多收到一个 batch、两个去重后的 Category/item query texts；启用且被动作实际选择
时，Tavily 收到有界 evidence query；eBay 只收到固定、经 M1b 验证的 US phone
marketplace query `smartphone`，不会收到完整原始购物请求。不会向这些 Provider
发送用户/Thread/Run 身份、完整商品正文、价格表、凭据或本机路径。不要在
query 中放入敏感信息；本项目不承诺控制远端 Provider 的训练、处理地域或保留策略。

M1b 的真实采集只供 Operator 显式执行。`--query` 的原文会发送给 eBay；它不会写入 snapshot 或 receipt，但仍应避免输入敏感信息。项目不会自动寻找 `.env`，使用本地凭据文件时必须显式传入 `--env-file`。`--output-root` 必须是已存在、权限恰为 `0700`、位于当前 checkout 之外的绝对目录：

```bash
install -d -m 700 /absolute/external/glodex-snapshots

uv run --locked glodex capture-provider --live \
  --env-file .env \
  --output-root /absolute/external/glodex-snapshots \
  --query "smartphone"
```

凭据键为 `EBAY_APP_ID` 和 `EBAY_CERT_ID`；进程环境优先于显式 env file。不要提交 `.env`、live snapshot、持久化 receipt 或原始 Provider 响应。

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

搜索、演示和快照校验在 stdout 输出一行 JSON。Capture 也只在 stdout 输出一行 `glodex.capture-receipt.v1` JSON，状态为 `PUBLISHED`、`FAILED` 或 `REJECTED`；请求或配置被拒绝时，稳定的机器可读结果仍在 stdout，简短诊断写入 stderr。

### 退出码

| 退出码 | 含义 |
|---:|---|
| `0` | 搜索为 `COMPLETED`/`NO_MATCH`，或快照有效 |
| `1` | 搜索为 `FAILED`，或快照无效 |
| `2` | CLI、配置或请求在运行前被拒绝 |

`NO_MATCH` 是成功执行后没有候选满足全部硬约束，不是系统错误。

Capture 使用同一组稳定退出码，但语义独立：

| 退出码 | Capture 状态 | 含义 |
|---:|---|---|
| `0` | `PUBLISHED` | 单页响应完整，snapshot 已验证并原子发布 |
| `1` | `FAILED` | Capture 已建立但失败，未发布 snapshot |
| `2` | `REJECTED` | 本地 preflight 拒绝，零外呼且未分配 Capture ID |

## 本地 API 与 SSE

在项目根目录启动一个单 Worker 服务：

```bash
uv run --locked uvicorn glodex.api.app:create_app --factory --host 127.0.0.1 --port 8000
```

上面的默认 factory 始终使用 Rule Intent。需要显式启用 M1c 时，先把
`DEEPSEEK_API_KEY` 注入当前进程，再启动独立 factory：

```bash
uv run --locked uvicorn glodex.api.live_app:create_live_app \
  --factory --host 127.0.0.1 --port 8000
```

live factory 在监听前校验凭据，并要求 API `run_timeout_seconds` 严格大于模型的
15 秒 deadline；启动阶段不向 Provider 发探测请求。HTTP request、header、query
或 SSE cursor 都不能启用 live，也不能修改 Provider、model 或 endpoint。

保持服务运行，在另一个终端创建 Run。下面的命令不依赖 `jq`，使用项目的 Python 从 202 响应中取出 `run_id`：

```bash
CREATE_RESPONSE="$(
  curl -sS -X POST http://127.0.0.1:8000/api/v1/runs \
    -H 'Content-Type: application/json' \
    --data-binary '{
      "thread_id": "thread-readme-001",
      "request": {
        "query": "推荐 800 美元以内、有库存、适合出差的轻薄本",
        "locale": "zh-CN",
        "display_currency": "USD",
        "top_k": 3,
        "snapshot_version": "m0-v1"
      }
    }'
)"

printf '%s\n' "$CREATE_RESPONSE"
RUN_ID="$(
  printf '%s' "$CREATE_RESPONSE" |
    uv run --locked python -c 'import json, sys; print(json.load(sys.stdin)["run_id"])'
)"
printf 'run_id=%s\n' "$RUN_ID"
```

读取 SSE。晚连接会先重放当前进程仍保留的事件，再跟随新事件；Run 到达终态后连接关闭：

```bash
curl -N -sS \
  -H 'Accept: text/event-stream' \
  "http://127.0.0.1:8000/api/v1/runs/$RUN_ID/events"
```

查询 Canonical 状态和最终 `SearchResponse`：

```bash
curl -sS "http://127.0.0.1:8000/api/v1/runs/$RUN_ID"
```

三个公开端点是：

| 方法 | 路径 | 作用 |
|---|---|---|
| `POST` | `/api/v1/runs` | 严格校验请求并立即返回 `202` Run 资源 URL |
| `GET` | `/api/v1/runs/{run_id}` | 读取当前状态、投影健康和 Canonical 响应 |
| `GET` | `/api/v1/runs/{run_id}/events` | 使用 `Accept: text/event-stream` 订阅或重放 SSE |

重连时可额外发送 `Last-Event-ID: <run_id>:<sequence>`，服务只重放该 sequence 之后仍保留的合法后缀。重放是同一进程内的有界能力，不是持久化或 exactly-once 保证。

### 独立 Agent API 与 SSE

M1d 提供 `glodex.api.agent_app:create_agent_app` 供 Operator 在进程启动时注入固定
`AgentExecutor` 与 `DataMode`。它不会被默认 `create_app()` 导入，也没有允许请求
切换 Provider 或 live data 的开关。当前没有隐式联网的 Uvicorn factory；使用该
library factory 的宿主必须自行完成凭据 preflight 和固定 composition。

它只暴露三个独立端点：

| 方法 | 路径 | 作用 |
|---|---|---|
| `POST` | `/api/v1/agent-runs` | 创建一个固定模式的 Agent Run |
| `GET` | `/api/v1/agent-runs/{run_id}` | 读取六态 Run 与 `AgentDemoResponse` |
| `GET` | `/api/v1/agent-runs/{run_id}/events` | 订阅或重放 `glodex.agent.event.v1` |

Agent SSE 只投影 model/tool/fork 的开始、结束、安全结果 code 和业务终态。它保留
202 后启动、同 Thread 单活动 Run、连续 event ID、晚连接重放、断线不取消和
timeout/shutdown `ABORTED` 等本地 Demo 行为，但不是完整 AG-UI 协议，也不提供
AG-UI SDK、WebSocket、自己的前端、持久事件或跨进程恢复；这些是独立 M2d/M2b interaction
composition 的职责，不能反向归因给这个旧 endpoint。

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
`.env` 或密钥。M1b Capture 可读取进程环境，或读取 Operator 通过 `--env-file`
明确指定的文件；M1c live 只读取进程环境中的 `DEEPSEEK_API_KEY`，不加载 `.env`。
M1d `agent-demo` 只在显式 `--live` 后读取进程中的 `DEEPSEEK_API_KEY` 与
`DASHSCOPE_API_KEY`；`--live-data` 还要求 `TAVILY_API_KEY`、`EBAY_APP_ID` 和
`EBAY_CERT_ID`。

## 快照

[`data/snapshots/m0-v1`](./data/snapshots/m0-v1) 包含 manifest、products、offers、evidence 和 exchange rates。加载器验证文件哈希、原始记录数、路径、版本和 schema：

- 单条脏记录进入 quarantine，其他合法记录可继续；
- manifest、哈希、核心文件或版本错误是 fatal，不能产生部分可信结果；
- 输出证据不仅匹配 ID，还校验 entity、field path、provider、offer/product 和 snapshot 归属。

M1b 使用同一个 snapshot 合同。每条 eBay listing 保留独立且无损的 provider-scoped Product/Offer identity；库存缺失为 `UNKNOWN`，未披露费用为 `UnknownCost`，USD identity FX 只表示同币种恒等换算。它们不是“有货”“免运费/税费”或完整到手价的推断。

真实 Capture 是某一时刻、固定上限单页的快照，不代表 eBay 全量目录，也不保证之后仍然新鲜。M1b 不提供 Provider 可用性、商品覆盖率、推荐质量、价格时效或完整 landed cost SLA；搜索因 `UNKNOWN` 库存或未知费用返回可信 `NO_MATCH` 也符合合同。

M1d 的 [`data/snapshots/m1d-demo-v1`](./data/snapshots/m1d-demo-v1) 是一个同时包含
四个平台的版本化 Demo Snapshot；[`data/agent/m1d-demo-v1`](./data/agent/m1d-demo-v1)
保存与其 hash 绑定的 Category Cards、item index、向量和运费规则。它们用于可复现
Demo，不代表实时库存、全量目录或价格新鲜度。`shipping_calc` 会区分
`EXACT`、`ESTIMATE` 和 `UNKNOWN`：估算值只用于 advisory/软排序，Unknown 不补零，
二者都不能让原本未通过 Canonical hard gate 的候选获得发布资格。

## M1b 三步 live smoke

以下是独立、人工 opt-in 的验证流程，不属于默认测试或 `verify_m1b.py`。当前工作树已于
2026-07-29 成功执行过一次，脱敏结果见
[M1b 验证记录](./specs/002-glodex-m1b-provider/verification.md)。复现时先准备 checkout
外的私有输出目录，再分别运行三条命令：

```bash
install -d -m 700 /absolute/external/glodex-snapshots

# 1. Capture：query 会发送给 eBay；stdout 只输出一条 receipt JSON
uv run --locked glodex capture-provider --live \
  --env-file .env \
  --output-root /absolute/external/glodex-snapshots \
  --query "smartphone"

# 从 PUBLISHED receipt 复制真实 snapshot_version
CAPTURE_ID="capture-..."

# 2. 使用现有加载器校验已发布 snapshot
GLODEX_DATA_DIR=/absolute/external/glodex-snapshots \
uv run --locked glodex validate-snapshot "$CAPTURE_ID" --currency USD

# 3. 使用 receipt 中的 USD 离线搜索；此步不会再次访问 eBay
GLODEX_DATA_DIR=/absolute/external/glodex-snapshots \
uv run --locked glodex search \
  --snapshot "$CAPTURE_ID" \
  --currency USD \
  --query "smartphone"
```

若搜索带预算，预算币种也必须与 receipt 一致。真实结果可能是 `COMPLETED` 或 `NO_MATCH`；不要为了得到推荐而把 `UNKNOWN` 库存或 `UnknownCost` 改写成已知值。

## M1c 独立 live smoke

M1c smoke 不属于 pytest 或 `verify_m1c.py`。必须先完成下面的完整离线门禁，再由
Operator 直接执行 CLI 章节中的 `search --live-intent` 命令。只接受
`COMPLETED` 或可信 `NO_MATCH`；不要把本次完整 query、prompt、响应、secret、
terminal 输出或其他 live artifact 保存到仓库。单次 smoke 只证明这次固定链路可
到达合法终态，不构成质量、可用性、延迟、成本、隐私保留或远程确定性 SLA。

## M1d 组合 live smoke

M1d smoke 不属于 pytest 或 `verify_m1d.py`。先通过固定的 Provider contract、
offline security 和 Git inventory 快速清单，再由 Operator 仅执行一次 CLI 章节中的
`agent-demo --live --live-data` 命令。接受 `COMPLETED` 或可信 `NO_MATCH`；失败时
不自动 retry。验证记录只保存固定 Provider/model、请求计数、实际工具名、安全终态
和离线门禁引用，不保存 query、prompt、Provider/工具正文、响应全文、credential、
外部 output root 或 terminal 输出。

该 smoke 的单次上限为：全树 DeepSeek 14 次、Tavily 1 次、DashScope 1 个至多两个
texts 的 batch、eBay 1 次 auth 加 1 次 Browse（最多 10 items），全部无 retry、
分页或隐藏 follow-up。root/child deadline 分别为 240/45 秒；单 Observation 为
8 KiB，全树累计 32 KiB，单 ToolResult 为 512 KiB，Candidate Store 为 2 MiB。
这些是安全上限，不是质量、可用性、延迟、成本或生产 SLA。

## Golden、追踪与门禁

```bash
# Golden 只检查，不写入
uv run --locked python scripts/update_goldens.py --check

# 检查全部 P0、AC 和 NFR 的正反向覆盖
uv run --locked python scripts/check_traceability.py --mode coverage

# 运行完整 M0 门禁
uv run --locked python scripts/verify_m0.py

# 运行完整 M1a 门禁；它会先运行完整 M0 门禁
uv run --locked python scripts/verify_m1a.py

# 运行完整 M1b 离线门禁；它会先运行完整 M1a 门禁
uv run --locked python scripts/verify_m1b.py

# 运行完整 M1c 离线门禁；它会先运行完整 M1b 门禁
uv run --locked python scripts/verify_m1c.py

# 运行完整 M1d 离线门禁；它会先运行完整 M1c 门禁
uv run --locked python scripts/verify_m1d.py

# 运行完整 M1e 离线门禁；它会先运行完整 M1d 门禁
uv run --locked python scripts/verify_m1e.py

# 运行完整 M1f 门禁；它会先运行完整 M1e 门禁
uv run --locked python scripts/verify_m1f.py
```

`scripts/verify_m0.py` 依次检查锁文件、格式、lint、类型、离线/安全/架构、unit/contract/generated、全部 AC、Golden、20 进程确定性、traceability 和 20k/100 性能工作负载。任一步失败都会整体非零，门禁不会 skip/xfail 或自动更新 Golden。

`scripts/verify_m1a.py` 是 M1a 的一键总门禁：它先完整执行 M0，再检查 M1a 的架构、离线与安全边界、unit/contract、acceptance 和 exact traceability coverage。当前分支的分阶段证据与最终门禁记录见 [M1a 验证记录](./specs/001-glodex-m1-api/verification.md)。

`scripts/verify_m1b.py` 是默认禁网的一键总门禁：它先完整执行 M1a，再检查 M1b 的 architecture/NFR、unit/contract、acceptance 和 exact traceability coverage。它不读取真实凭据、不执行 live smoke，也不会自动生成或提交真实数据。当前分支的完整离线门禁与独立真实三步 smoke 证据见 [M1b 验证记录](./specs/002-glodex-m1b-provider/verification.md)。

`scripts/verify_m1c.py` 先完整执行 M1b，再依次检查 M1c 的
architecture/NFR、unit/contract、acceptance 和 exact `6 P0 / 6 AC / 6 NFR`
coverage。它会清除模型凭据与代理变量并保持禁网，不执行 live smoke，也不更新
Golden。

`scripts/verify_m1d.py` 先完整执行 M1c，再依次检查 M1d 的
architecture/NFR、unit/contract、acceptance 和 exact `6 P0 / 6 AC / 6 NFR`
coverage。它同样清除全部 Provider 凭据与代理变量、保持禁网，不执行 live smoke，
不更新 Golden，也不保存任何 live artifact。

`scripts/verify_m1e.py` 先完整执行 M1d，再检查 ESCI artifact 的离线、安全、unit/contract、
acceptance 和 exact `4 P0 / 4 AC / 4 NFR` coverage。`scripts/verify_m1f.py` 先完整执行
M1e，再检查静态 Showcase 的隔离、资产、页面合同、acceptance 和相同规模的 coverage；二者都
不读取凭据、不启动 live Agent 或 Provider。

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

## M1a 边界

- **只能单进程、单 Worker 运行**：Run、事件和 Subscriber 都属于一个进程；不要使用 Uvicorn `--workers` 扩容此 Demo。
- **进程重启会丢失 Run**：没有数据库、checkpoint 或跨进程事件总线；重启后旧 `run_id` 返回未找到。
- **没有认证与授权**：不要暴露到不可信网络，也不要把请求或响应当作租户隔离数据。
- **没有持久化和用户取消 API**：超时、Runner 异常或 shutdown 使用 `ABORTED`，它不是业务 `FAILED`。
- **默认没有 CORS**：项目没有安装 CORS middleware，不提供跨源浏览器访问承诺。
- **没有生产 SLA**：容量值用于有界和可测试的本地行为，不代表吞吐、可用性或延迟承诺。
- **默认服务没有 Agent 路由或 request-time Provider**：M1d 的 AgentLoop、Agent
  SSE 与 eBay live item search 都位于独立 operator-only composition，不会改变
  M1a 默认三个 Search 端点。

## M1c 边界

- 只支持一个固定 Provider/model、`zh-CN` 与现有 Intent 语义，不是通用 LLM
  gateway；
- 远程模型跨调用不保证相同输出；通过验证后的 Intent 与既有本地 snapshot 的下游
  领域投影仍保持确定；
- 没有第二 Provider、动态 model/base URL、retry、fallback、cache、tool、
  AgentLoop 或 prompt 管理系统；
- 既有、经过验证的 `source_span.text` 摘录仍可进入 SearchResponse 与
  `STATE_SNAPSHOT`，但系统不记录或持久化本次完整 query、prompt 或原始模型响应；
- 本地 Demo 无生产认证、隔离或 SLA，不应暴露到不可信网络。

## M1d 边界

- 这是固定 `deepseek-v4-flash`、固定十 entry registry 和固定 Provider endpoint
  的有界购物 Agent，不是通用模型网关、动态工具系统或开放式代码执行环境；
- 只有 eBay 有 request-time marketplace adapter；Amazon、Shopee、AliExpress
  仅有明确标记的 `m1d-demo-v1` 数据，Tavily 网页证据不能充当商品事实；
- Agent 远端动作不保证跨调用确定；本地 schema、Candidate binding、金额、hard
  gates、Evidence closure 和最终 `SearchResponse` 继续 fail closed；
- 没有 retry、fallback model、长期记忆、checkpoint、数据库、Redis、任意网页抓取、
  外部向量库、认证、租户隔离或生产 SLA；
- Agent API/SSE 是单进程内存 Demo，`glodex.agent.event.v1` 不是 AG-UI；其自身没有
  AG-UI SDK、WebSocket 或 React UI。M2d 是独立的 M2b durable projection，而非对该 endpoint 的改写；
- `ESTIMATE`/`UNKNOWN` 会如实保留，不能被解释为精确费用、零费用或平台报价保证。

## 后续范围

M1a 已交付最小 FastAPI + SSE 服务化切片，M1b 已交付单 Provider 的 capture-first
切片，M1c 已交付固定 DeepSeek Intent 安全接入，M1d 已交付固定工具 AgentLoop 的
完整验收，M1e 已交付 ESCI 离线 benchmark，M1f 已交付本地静态 Showcase，M2d 已交付本机
AG-UI/React durable interaction layer；
以下能力仍明确延期：

- **后续 M1/M2**：eBay 之外的 live marketplace adapter、完整 AG-UI 与实时前端扩展；
- **M2**：OpenSearch/三塔/cross-encoder、Postgres/checkpoint、Redis、长期记忆与
  个性化；
- **范围外**：生产部署、实时/全量商品覆盖、Provider 可用性、价格时效、真实推荐
  质量承诺和开放式任意工具 Agent。

## 规格与决策记录

- M0：[产品规格](./specs/000-glodex-mvp/spec.md) · [技术计划](./specs/000-glodex-mvp/plan.md) · [实施任务](./specs/000-glodex-mvp/tasks.md)
- M1a：[API 与实时事件规格](./specs/001-glodex-m1-api/spec.md) · [技术计划](./specs/001-glodex-m1-api/plan.md) · [实施任务](./specs/001-glodex-m1-api/tasks.md) · [验证记录](./specs/001-glodex-m1-api/verification.md)
- M1b：[Provider Capture 规格](./specs/002-glodex-m1b-provider/spec.md) · [技术计划](./specs/002-glodex-m1b-provider/plan.md) · [实施任务](./specs/002-glodex-m1b-provider/tasks.md) · [验证记录](./specs/002-glodex-m1b-provider/verification.md)
- M1c：[DeepSeek Intent 规格](./specs/003-glodex-m1c-llm-intent/spec.md) · [技术计划](./specs/003-glodex-m1c-llm-intent/plan.md) · [实施任务](./specs/003-glodex-m1c-llm-intent/tasks.md) · [验证记录](./specs/003-glodex-m1c-llm-intent/verification.md)
- M1d：[全工具 AgentLoop 规格](./specs/004-glodex-m1d-agent-demo/spec.md) · [技术计划](./specs/004-glodex-m1d-agent-demo/plan.md) · [实施任务](./specs/004-glodex-m1d-agent-demo/tasks.md) · [验证记录](./specs/004-glodex-m1d-agent-demo/verification.md)
- M1e：[ESCI 离线检索规格](./specs/005-glodex-m1e-esci-retrieval-benchmark/spec.md) · [技术计划](./specs/005-glodex-m1e-esci-retrieval-benchmark/plan.md) · [实施任务](./specs/005-glodex-m1e-esci-retrieval-benchmark/tasks.md)
- M1f：[本地离线 Showcase 规格](./specs/006-glodex-m1f-local-showcase/spec.md) · [技术计划](./specs/006-glodex-m1f-local-showcase/plan.md) · [实施任务](./specs/006-glodex-m1f-local-showcase/tasks.md)
- M2a：[检索智能闭环规格](./specs/007-glodex-m2a-opensearch-hybrid-retrieval/spec.md) · [技术计划](./specs/007-glodex-m2a-opensearch-hybrid-retrieval/plan.md) · [实施任务](./specs/007-glodex-m2a-opensearch-hybrid-retrieval/tasks.md)
- M2d：[AG-UI / React Run Console 规格](./specs/010-glodex-m2d-agui-react-operations/spec.md) · [技术计划](./specs/010-glodex-m2d-agui-react-operations/plan.md) · [实施任务](./specs/010-glodex-m2d-agui-react-operations/tasks.md)
- [ADR-0001：确定性领域核心](./specs/000-glodex-mvp/adr/0001-deterministic-domain-core.md)
- [ADR-0002：金额、汇率与舍入](./specs/000-glodex-mvp/adr/0002-money-fx-and-rounding.md)
- [ADR-0003：硬门与排序降级](./specs/000-glodex-mvp/adr/0003-hard-gates-and-ranking-degradation.md)
