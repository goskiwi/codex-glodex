# Glodex

Glodex 目前包含三个叠加的里程碑：

- **M0** 是本地、离线、确定性的跨市场购物检索业务基线。它接收中文购物请求，从版本化快照中聚合同款商品和各市场报价，使用 `Decimal` 计算到手价，在排序前执行预算、品类、库存、商品本体和证据硬门，最后返回最多 3 个可核验结果。
- **M1a** 在不改变 M0 业务合同与不变量的前提下，增加一个本地 FastAPI + SSE 服务化垂直切片。
- **M1b** 增加一个 operator-only、显式 opt-in 的 eBay Buy Browse Capture 链路，把一个有界的真实结果页归一化为现有 manifest v1 snapshot；搜索、API 和 SSE 仍只读取本地快照。

M0 仍是可保留的业务与合同基线；M1a 是单进程 Demo，不是生产 Agent 平台。M1b 只允许 Operator 通过独立命令显式连接一个固定 Provider，不把联网能力带入搜索请求路径。项目仍不连接真实 LLM 或网络数据库，也不保存长期用户画像。

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

## 环境与安装

需要 Python `>=3.12,<3.13` 和 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync --locked --dev
uv run --locked glodex --help
```

运行时依赖包含 FastAPI 和 Pydantic 2。本地服务命令使用 dev 依赖组中的 Uvicorn；pytest、pytest-socket、Ruff 和 mypy 也位于 dev 依赖组。

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

M0 只支持 `zh-CN`；币种必须为三位大写字母；`top_k` 必须在 1–3。未知配置键、无效值或缺失的显式配置都会 fail closed。业务配置不读取 `.env` 或密钥；只有 M1b Capture 会读取进程环境，或读取 Operator 通过 `--env-file` 明确指定的文件。

## 快照

[`data/snapshots/m0-v1`](./data/snapshots/m0-v1) 包含 manifest、products、offers、evidence 和 exchange rates。加载器验证文件哈希、原始记录数、路径、版本和 schema：

- 单条脏记录进入 quarantine，其他合法记录可继续；
- manifest、哈希、核心文件或版本错误是 fatal，不能产生部分可信结果；
- 输出证据不仅匹配 ID，还校验 entity、field path、provider、offer/product 和 snapshot 归属。

M1b 使用同一个 snapshot 合同。每条 eBay listing 保留独立且无损的 provider-scoped Product/Offer identity；库存缺失为 `UNKNOWN`，未披露费用为 `UnknownCost`，USD identity FX 只表示同币种恒等换算。它们不是“有货”“免运费/税费”或完整到手价的推断。

真实 Capture 是某一时刻、固定上限单页的快照，不代表 eBay 全量目录，也不保证之后仍然新鲜。M1b 不提供 Provider 可用性、商品覆盖率、推荐质量、价格时效或完整 landed cost SLA；搜索因 `UNKNOWN` 库存或未知费用返回可信 `NO_MATCH` 也符合合同。

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
```

`scripts/verify_m0.py` 依次检查锁文件、格式、lint、类型、离线/安全/架构、unit/contract/generated、全部 AC、Golden、20 进程确定性、traceability 和 20k/100 性能工作负载。任一步失败都会整体非零，门禁不会 skip/xfail 或自动更新 Golden。

`scripts/verify_m1a.py` 是 M1a 的一键总门禁：它先完整执行 M0，再检查 M1a 的架构、离线与安全边界、unit/contract、acceptance 和 exact traceability coverage。当前分支的分阶段证据与最终门禁记录见 [M1a 验证记录](./specs/001-glodex-m1-api/verification.md)。

`scripts/verify_m1b.py` 是默认禁网的一键总门禁：它先完整执行 M1a，再检查 M1b 的 architecture/NFR、unit/contract、acceptance 和 exact traceability coverage。它不读取真实凭据、不执行 live smoke，也不会自动生成或提交真实数据。当前分支的完整离线门禁与独立真实三步 smoke 证据见 [M1b 验证记录](./specs/002-glodex-m1b-provider/verification.md)。

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
- **没有真实智能体或 request-time Provider 集成**：AG-UI、WebSocket、前端和真实 LLM 均未实现；M1b 的 eBay Capture 是独立 operator 链路，不进入搜索/API/SSE 请求路径。

## 后续范围

M1a 已交付最小 FastAPI + SSE 服务化切片，M1b 已交付单 Provider 的 capture-first 切片；以下能力仍明确延期：

- **后续 M1**：真实 LLM Intent、request-time Provider fan-out、AG-UI 投影、Category Insight；
- **M2**：OpenSearch/Hybrid/cross-encoder、Postgres/checkpoint、Redis、长期记忆、User-only/Reflect、React UI；
- **范围外**：生产部署、实时/全量商品覆盖、Provider 可用性、价格时效、真实推荐质量承诺和开放式 AgentLoop。

## 规格与决策记录

- M0：[产品规格](./specs/000-glodex-mvp/spec.md) · [技术计划](./specs/000-glodex-mvp/plan.md) · [实施任务](./specs/000-glodex-mvp/tasks.md)
- M1a：[API 与实时事件规格](./specs/001-glodex-m1-api/spec.md) · [技术计划](./specs/001-glodex-m1-api/plan.md) · [实施任务](./specs/001-glodex-m1-api/tasks.md) · [验证记录](./specs/001-glodex-m1-api/verification.md)
- M1b：[Provider Capture 规格](./specs/002-glodex-m1b-provider/spec.md) · [技术计划](./specs/002-glodex-m1b-provider/plan.md) · [实施任务](./specs/002-glodex-m1b-provider/tasks.md) · [验证记录](./specs/002-glodex-m1b-provider/verification.md)
- [ADR-0001：确定性领域核心](./specs/000-glodex-mvp/adr/0001-deterministic-domain-core.md)
- [ADR-0002：金额、汇率与舍入](./specs/000-glodex-mvp/adr/0002-money-fx-and-rounding.md)
- [ADR-0003：硬门与排序降级](./specs/000-glodex-mvp/adr/0003-hard-gates-and-ranking-degradation.md)
