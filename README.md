# Glodex

Glodex 目前包含五个叠加的里程碑：

- **M0** 是本地、离线、确定性的跨市场购物检索业务基线。它接收中文购物请求，从版本化快照中聚合同款商品和各市场报价，使用 `Decimal` 计算到手价，在排序前执行预算、品类、库存、商品本体和证据硬门，最后返回最多 3 个可核验结果。
- **M1a** 在不改变 M0 业务合同与不变量的前提下，增加一个本地 FastAPI + SSE 服务化垂直切片。
- **M1b** 增加一个 operator-only、显式 opt-in 的 eBay Buy Browse Capture 链路，把一个有界的真实结果页归一化为现有 manifest v1 snapshot；搜索、API 和 SSE 仍只读取本地快照。
- **M1c** 增加一个 operator-only、显式 opt-in 的固定 DeepSeek Intent 适配器；模型只产生待校验 Intent，完整搜索仍复用既有本地快照、硬门、排序、Evidence、CLI、API 与 SSE。
- **M1d** 增加一个 operator-only、显式 opt-in 的固定 DeepSeek AgentLoop，完整组装九个业务工具、`dispatch_tool`、最多四路子 Agent、Hybrid Category RAG、Tavily evidence 与 eBay live item search；最终商品仍必须通过既有 Canonical hard gates 与 Evidence closure。

M0 仍是可保留的业务与合同基线；M1a 是单进程 Demo，不是生产 Agent 平台。M1b
Capture 与 M1c live Intent 都只能由 Operator 通过各自独立入口显式联网；默认
CLI/API 仍使用 Rule Intent 且只读取本地快照。项目不连接网络数据库，也不保存长期
用户画像。M1d 是独立 Agent 入口，不改变这些默认行为。

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

## 环境与安装

需要 Python `>=3.12,<3.13` 和 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync --locked --dev
uv run --locked glodex --help
```

运行时依赖包含 FastAPI、Pydantic 2 和 HTTPX。本地服务命令使用 dev 依赖组中的 Uvicorn；pytest、pytest-socket、Ruff 和 mypy 也位于 dev 依赖组。

## CLI

所有子命令选项都写在子命令之后。`validate-snapshot` 的快照版本是位置参数。

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
AG-UI SDK、WebSocket、前端、持久事件或跨进程恢复。

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
- Agent API/SSE 是单进程内存 Demo，`glodex.agent.event.v1` 不是完整 AG-UI；
  没有 AG-UI SDK、WebSocket 或 React UI；
- `ESTIMATE`/`UNKNOWN` 会如实保留，不能被解释为精确费用、零费用或平台报价保证。

## 后续范围

M1a 已交付最小 FastAPI + SSE 服务化切片，M1b 已交付单 Provider 的 capture-first
切片，M1c 已交付固定 DeepSeek Intent 安全接入，M1d 已交付固定工具 AgentLoop 的
完整验收；
以下能力仍明确延期：

- **后续 M1**：eBay 之外的 live marketplace adapter、完整 AG-UI 与前端；
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
- [ADR-0001：确定性领域核心](./specs/000-glodex-mvp/adr/0001-deterministic-domain-core.md)
- [ADR-0002：金额、汇率与舍入](./specs/000-glodex-mvp/adr/0002-money-fx-and-rounding.md)
- [ADR-0003：硬门与排序降级](./specs/000-glodex-mvp/adr/0003-hard-gates-and-ranking-degradation.md)
