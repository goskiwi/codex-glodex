# Glodex M1b 单 Provider Capture 技术实施计划

| 字段 | 值 |
|---|---|
| Plan ID | `GLO-PLAN-002` |
| 版本 | `0.2.0` |
| 状态 | Approved |
| 对应规格 | [`GLO-SPEC-002` v0.2.1](./spec.md) |
| 父基线 | [`GLO-SPEC-000`](../000-glodex-mvp/spec.md)、[`GLO-SPEC-001`](../001-glodex-m1-api/spec.md) |
| 最后更新 | 2026-07-29 |
| 批准日期 | 2026-07-29 |

## 1. 计划目标

在不改变 M0/M1a 搜索、API 或 SSE 合同的前提下，增加一条独立的 Operator
Capture 垂直切片：

```text
glodex capture-provider --live
    → 本地 preflight
    → eBay OAuth（最多一次）
    → eBay Browse item_summary/search（恰好一次）
    → 严格解码、映射与 quarantine
    → staging manifest v1 snapshot
    → 现有 loader + aggregation 反向验证
    → 同文件系统原子发布
    → glodex.capture-receipt.v1
```

发布完成后，现有 `glodex search`、FastAPI、SSE 与 `SearchService` 只读取该
snapshot，不再访问 eBay。

本 Plan 只设计：

- 一个 eBay Browse Provider；
- `EBAY_US / USD / category 9355 / phone`；
- 一个 OAuth token 请求和一个单页商品请求；
- Provider response 到现有 manifest v1 的可信映射；
- 原子发布、receipt、默认离线门禁和独立 live smoke。

本 Plan 不设计第二 Provider、fan-out、分页、retry、实时用户搜索外呼、库存详情
补查、通用 Provider SDK、数据库、队列、缓存、scheduler、LLM、AG-UI 或前端。

## 2. 当前代码基线与扩展缝

现有实现提供以下可复用能力：

- [`LocalSnapshotCatalog`](../../src/glodex/adapters/local_snapshot.py) 已严格验证
  manifest v1、路径、文件大小、hash、record count、JSON、Evidence closure、
  币种和 snapshot version；
- [`aggregate_catalog_batch`](../../src/glodex/domain/catalog.py) 已负责 Product
  合并、Offer identity、Evidence 与 aggregation quarantine；
- [`Product`、`Offer`、`StockStatus`、`ExchangeRateTable`](../../src/glodex/domain/catalog.py)
  已完整表达 M1b normalized 数据；
- [`EvidenceRef` 与 `FieldEvidence`](../../src/glodex/domain/evidence.py) 已冻结字段级
  Evidence 所有权；
- [`canonical_exact_amount`](../../src/glodex/domain/pricing.py) 已提供金额 canonical
  JSON 表示；
- [`cli.py`](../../src/glodex/cli.py) 已保证单 JSON stdout、稳定退出码和一次
  `asyncio.run()`；
- [`verify_m1a.py`](../../scripts/verify_m1a.py) 已实现父里程碑优先的最终门禁；
- pytest 全局 `--disable-socket` 与环境清理已保证默认测试离线。

需要新增的扩展缝只有：

1. 当前没有获准出站的运行时模块；
2. 当前只有 snapshot reader，没有 production snapshot publisher；
3. 当前 CLI outcome 不包含 Capture receipt；
4. 当前 traceability checker 只识别 M0/M1a；
5. 当前公共 `Identifier` 不接受 eBay 原始 `itemId` 中常见的 `|`。

第 5 项已在 `GLO-SPEC-002 v0.2.1` 修正为无 padding base64url 无损编码；本 Plan
不得放宽 M0 `Identifier`。

## 3. 技术选择与固定资源预算

### 3.1 技术选择

| 项目 | 选择 | 原因 |
|---|---|---|
| Python | 保持 `>=3.12,<3.13` | 复用现有工程与标准库。 |
| HTTP transport | 标准库 `http.client.HTTPSConnection` | 单 Provider、两次请求，不新增 runtime 依赖或 eBay SDK。 |
| URL 编码 | 标准库 `urllib.parse.urlencode` | query 只成为已编码参数，不能改写 origin/path。 |
| Receipt | 三个 strict、frozen Pydantic DTO | 精确表达三种不相同的顶层 key 集合。 |
| Provider 解码 | 标准库 `json` + 必要 schema 检查 | 不保存 raw payload，不实现自定义 JSON parser。 |
| 金额 | `Decimal` + 现有 canonical helper | 禁止 float 与隐式舍入。 |
| Snapshot | 现有 manifest v1、四个数据文件 | 不引入 manifest v2。 |
| 发布 | 同一 output root 内 staging + `os.rename` | 搜索只能观察完整旧目录或完整新目录。 |
| 发布验证 | 现有 loader + aggregation | writer 不能自我宣称输出合法。 |
| 自动测试 | fake client/stream，保持全局禁网 | 默认门禁不需要凭据或公网。 |

`pyproject.toml` 与 `uv.lock` 不新增依赖。`httpx` 继续只用于 M1a 的进程内 HTTP
测试；M1b production Capture 不依赖它。

### 3.2 固定资源预算

| 边界 | 上限 |
|---|---:|
| Provider page `limit` | `10` |
| 完整解码的 item 数 | `10` |
| query | trim 后 `1..100` Unicode code points，禁止 NUL/CR/LF 与 `*` |
| OAuth response body | `64 KiB` |
| Search response body | `256 KiB` |
| OAuth request timeout | `10s` |
| Search request timeout | `15s` |
| Capture 总 deadline | `30s` |
| redirect / retry / next-link follow / pagination | `0 / 0 / 0 / 0` |

边界值恰好等于上限时允许继续；超过一项即 fail closed。总 deadline 使用
monotonic clock，在每次 request 前计算剩余预算。

## 4. 分层与依赖方向

```text
glodex.cli
  └── 仅 capture-provider 分支惰性导入 glodex.capture.bootstrap
          │
          ▼
glodex.capture.service
  ├── receipt contracts / preflight profile
  ├── provider-specific ports
  ├── eBay pure mapper
  └── snapshot publisher port
          ▲                         ▲
          │                         │
glodex.capture.ebay_http   glodex.capture.snapshot_publisher
  唯一网络实现              唯一 Capture 文件写实现
          │                         │
          └──────────┬──────────────┘
                     ▼
              glodex.domain
                     ▲
                     │
       LocalSnapshotCatalog + aggregation
```

依赖规则：

- `domain`、`application`、`api` 不导入 `glodex.capture`；
- `SearchService`、`build_service()`、FastAPI 和 SSE 不获得 Capture port；
- `glodex.capture.service` 只依赖自己的 contracts/ports、纯 mapper 与 domain；
- production composition 只存在于 `glodex.capture.bootstrap`；
- `http.client` 只允许出现在 `glodex.capture.ebay_http`；
- 文件写入只允许出现在 `glodex.capture.snapshot_publisher`；
- Capture 包不提供 registry、动态 Provider 名称或可替换 endpoint DSL；
- 普通 `search`、`demo`、`validate-snapshot` 不导入或实例化 live transport。

## 5. CLI、配置、凭据与 Preflight

### 5.1 CLI 形状

批准的命令形状为：

```bash
uv run --locked glodex capture-provider \
  --live \
  --query "smartphone" \
  --env-file .env \
  --output-root /absolute/external/glodex-snapshots
```

参数：

- `--live`：必需的显式 opt-in；
- `--query`：必需，自由文本会发送给 eBay；
- `--env-file`：可选、显式的本地 secret 文件；不提供时只读进程环境；
- `--output-root`：必需、绝对路径、checkout 外的 snapshot root。

不提供 `provider`、`origin`、`environment`、`marketplace`、`currency`、
`category`、`operation`、`limit` 或 `sort` 参数。

Capture 分支必须在现有 `load_config()` 前独立分发，避免 Provider 配置进入
`GlodexConfig.fingerprint`。其他命令的 parser、payload 和输出保持原样。

### 5.2 固定 eBay Profile

`EbayCaptureProfile` 是代码内 frozen 常量，不读取用户 URL：

| 字段 | 固定值 |
|---|---|
| `provider_id` | `ebay-browse` |
| HTTPS host | `api.ebay.com:443` |
| auth path | `/identity/v1/oauth2/token` |
| search path | `/buy/browse/v1/item_summary/search` |
| OAuth scope | `https://api.ebay.com/oauth/api_scope` |
| marketplace header | `EBAY_US` |
| currency | `USD` |
| category parameter | `9355` |
| canonical category | `phone` |
| entity kind | `PRIMARY_PRODUCT` |
| buying option filter | `buyingOptions:{FIXED_PRICE}` |
| offset / limit | `0 / 10` |
| sort | 不发送，使用 Provider 默认结果页 |

### 5.3 凭据

只读取两个名称：

- `EBAY_APP_ID`
- `EBAY_CERT_ID`

读取优先级为进程环境高于显式 `--env-file`。不读取 `LIVE_PROVIDERS`，不从其他
文件或仓库回退。

显式 env-file parser 只接受简单的单行 `KEY=VALUE`：

- 不执行 shell；
- 不做变量、命令或 escape 展开；
- 只提取两个批准的 key，空值失败；
- 文件必须是显式指定、可读的普通文件；
- 未请求 `--env-file` 时不自动寻找 `.env`。

`EbayCredentials` 使用 frozen dataclass，secret 字段 `repr=False`。错误边界只返回
稳定 code，不序列化 Pydantic validation input、Authorization header 或
exception text。

### 5.4 Output root

Preflight 要求 output root：

- 已存在、可写、为绝对路径和真实目录；
- 经 `resolve()` 后位于当前 checkout 外；
- 不是 symlink，并由 Operator 以 `0700` 创建。

任何失败都返回 `REJECTED / CAPTURE_CONFIG_INVALID`，不分配 `capture_id`、
不外呼。

### 5.5 Preflight 顺序

固定顺序为：

1. `--live`；
2. query trim、字符和长度；
3. output root；
4. env-file 与凭据存在；
5. frozen profile 自检。

全部通过后、第一次出站 attempt 前，生成
`capture-<32 lowercase hex>`。该值同时满足现有 `SnapshotVersion` 和成功时的
`snapshot_version`。

## 6. eBay HTTP 合同

### 6.1 Transport

`EbayHttpClient` 对每个 operation 新建一个默认验证 CA、hostname 与 SNI 的
`HTTPSConnection`：

- 不读取代理环境；
- 不发送 cookie；
- `Accept-Encoding: identity`；
- `Connection: close`；
- 不 follow redirect；
- 不 retry；
- 不跟随 response 的 `next`、`prev`、cursor 或 item href；
- 不调用 `getItem` 或任何第二商品 endpoint。

production class 接受一个窄 `HttpsConnectionFactory` 注入点，仅用于离线测试。
业务调用方不能传 host、scheme 或 path。

### 6.2 OAuth request

```text
POST /identity/v1/oauth2/token
Authorization: Basic base64(EBAY_APP_ID + ":" + EBAY_CERT_ID)
Content-Type: application/x-www-form-urlencoded
Accept: application/json

grant_type=client_credentials&
scope=https%3A%2F%2Fapi.ebay.com%2Foauth%2Fapi_scope
```

token response 必须：

- HTTP 2xx；
- body 完整且不超过 `64 KiB`；
- root 为 object；
- `access_token` 为非空 string。

access token 不缓存到下一次 Capture，不进入 receipt、snapshot 或日志。

### 6.3 Search request

```text
GET /buy/browse/v1/item_summary/search
    ?q=<urlencoded-query>
    &category_ids=9355
    &limit=10
    &offset=0
    &filter=buyingOptions%3A%7BFIXED_PRICE%7D
Authorization: Bearer <token>
X-EBAY-C-MARKETPLACE-ID: EBAY_US
Accept: application/json
```

query 参数通过 `urlencode()` 从固定 key/value tuple 构造，不能拼接 raw URL。

search response 必须：

- HTTP 2xx；
- body 完整且不超过 `256 KiB`；
- root 为 object，`itemSummaries` 明确存在且为 list；
- `itemSummaries` 长度不超过 10；
- `next`、`prev`、cursor、href 仅视为不可信附加字段，不触发请求。

root/传输无法证明完整时是 Provider-level failure；完整 root 内单条 item 的错误
只 quarantine 该 item。

### 6.4 稳定失败映射

| 条件 | Receipt issue |
|---|---|
| 3xx、非批准 target/redirect | `PROVIDER_TARGET_REJECTED` |
| OAuth 400/401/403 或 token 合同非法 | `PROVIDER_AUTH_REJECTED` |
| deadline/socket timeout | `PROVIDER_TIMEOUT` |
| 429 | `PROVIDER_RATE_LIMITED` |
| 5xx、DNS/TLS/connect failure | `PROVIDER_UNAVAILABLE` |
| 非预期 4xx、JSON/root/itemSummaries 合同非法 | `PROVIDER_RESPONSE_INVALID` |
| EOF 截断、Content-Length 与实际 body 不一致 | `PROVIDER_RESPONSE_INCOMPLETE` |
| body 或 record 超限 | `PROVIDER_RESPONSE_LIMIT` |

错误映射忽略第三方 body、header 与 `str(exception)`。

## 7. Provider Item 映射

### 7.1 Record 处理顺序

完整页解码后：

1. `received_record_count = len(itemSummaries)`；
2. 每个输入 ordinal 至多产生一次 quarantine；
3. duplicate `itemId` 的所有出现均 quarantine，不保留“第一个”；
4. 合法 item 按编码后的 Product ID 排序，再赋 snapshot ordinal；
5. 非空页若最终没有合法 Product/Offer，Provider-level
   `FAILED / PROVIDER_RESPONSE_INVALID`；
6. 明确 `itemSummaries=[]` 才是合法空集合。

quarantine 原因仅用于内部测试和计数，公共 receipt 合并为一个
`RECORD_QUARANTINED` issue。

### 7.2 Identity

完整 eBay `itemId` 的 UTF-8 bytes 使用 URL-safe base64，去除尾部 `=`：

```text
product_id = ebay-browse:product:<unpadded-base64url>
offer_id   = ebay-browse:offer:<unpadded-base64url>
```

规则：

- 编码必须能重新补 padding 并无损解码为完全相同的 UTF-8 原值；
- 编码只使用现有 `Identifier` 允许字符；
- Product/Offer ID 必须各自不超过 128 code points；
- 不能删除 eBay prefix、variant 或其他片段；
- 任一检查失败时 quarantine。

Evidence ID 不重复拼接原始 itemId，而使用
`ev-ebay-<sha256(itemId)[:24]>-<field-suffix>`，同时满足现有长度合同。

### 7.3 Source URI

`itemWebUrl` 只作为 Evidence/source，不抓取：

- scheme 必须是 `https`；
- userinfo、fragment 禁止；
- host 必须是 `www.ebay.com` 或 `ebay.com`；
- 归一化时移除 query/fragment，只保留 HTTPS origin 与 path；
- path 必须非空，完整 URI 服从现有 4,096 code point 上限。

`itemHref`、图片、seller、营销价格和其他链接不进入 snapshot。

### 7.4 Product

每个合法 listing 独立形成一个 Product：

| 字段 | 来源 |
|---|---|
| `provider_id` | 固定 `ebay-browse` |
| `product_id` | 无损编码后的 provider-scoped ID |
| `source_uri` | 已验证/归一化的 `itemWebUrl` |
| `title` | 非空、最多 2,000 code points 的 `title`，使用更严格的现有 `SearchResult` 下游上限 |
| `category` | 固定 filter 映射 `phone` |
| `entity_kind` | 固定 filter 映射 `PRIMARY_PRODUCT` |
| `attributes` | M1b 为空 |

`title`、`category`、`entity_kind` 各有独立 FieldEvidence/EvidenceRef。固定 category
映射的 Evidence 仍绑定该 listing source 与统一 Capture time，不从标题猜测。

### 7.5 Offer

| 字段 | 映射 |
|---|---|
| `provider_id` | `ebay-browse` |
| `offer_id` / `product_id` | 同一个完整 itemId 的两个命名空间 |
| `market` | `listingMarketplaceId` 缺失时使用批准请求事实 `EBAY_US`；存在时必须等于 `EBAY_US` |
| `stock_status` | 固定 `UNKNOWN`；Browse 未披露库存，不 quarantine、不推断有货 |
| `item_price` | `price.value` 的正数 Decimal，currency 必须 USD |
| `shipping` | 仅一个明确、非负、USD `shippingCost` 时 Known；缺失、多值、计算型或歧义时 Unknown |
| `tax` / `duty` | `UnknownCost(reason=NOT_DISCLOSED)` |
| `captured_at` | 完整 search body 收到后的统一 UTC instant |

金额规则：

- 不使用 float；
- USD 金额最多两位小数，不能通过 round/quantize 修复非法精度；
- 正数使用 `canonical_exact_amount()`；
- Known 零运费按现有 loader 合同序列化为 `"0.00"`；
- UnknownCost 不绑定不存在的金额 Evidence。

Offer 必须为 `market`、`inventory`、`cost_components.currency`、item price 和可选
Known shipping 建立正确的 Evidence closure。listing 存在、FIXED_PRICE、
`buyingOptions`、seller feedback 或 rating 都不能证明库存、销量或质量。

### 7.6 Identity FX

每个 snapshot（包括空 snapshot）写入：

```text
base_currency = USD
currency = USD
base_per_unit = Decimal("1")
minor_units = 2
source_uri = urn:glodex:identity-fx:USD
```

FX Evidence 的 captured_at 与 manifest `created_at`、receipt `captured_at` 使用同一
UTC instant。

## 8. Receipt v1 与 Capture 状态机

### 8.1 DTO

`capture/contracts.py` 定义：

- `CaptureIssue`；
- `RejectedCaptureReceipt`；
- `FailedCaptureReceipt`；
- `PublishedCaptureReceipt`。

三个 receipt 分开建模，不使用一个包含大量 Optional 的 DTO。所有模型：

- `extra="forbid"`、`frozen=True`、`strict=True`；
- count 为 strict non-negative int，拒绝 bool；
- 状态使用 Literal discriminator；
- 顶层 key 与 Spec 精确一致；
- `issues` 按 code 排序并同码合并；
- stdout 使用 compact JSON 单行；
- stderr 只使用静态状态/code 简述。

### 8.2 状态机

```text
CLI parsed
  ├── preflight fail ───────────────→ REJECTED / request_count=0
  └── preflight pass
        → allocate capture_id
        → auth attempt (+1)
          ├── fail ─────────────────→ FAILED / request_count=1
          └── token
                → search attempt (+1)
                  ├── fail ─────────→ FAILED / request_count=2
                  └── complete page
                        → map/quarantine
                        → stage/validate/publish
                          ├── fail ─→ FAILED / request_count=2
                          └── done ─→ PUBLISHED / request_count=2
```

attempt counter 在调用 transport 前递增，保证 timeout/connect failure 也被计数。
`capture_id` 在首次 attempt 前分配。失败后不重用 ID。

`received_record_count`：

- root 未完整、未合法解码时为 0；
- 完整 `itemSummaries` 解码后为其数组长度；
- 后续 mapping/publish 失败仍保留该值。

PUBLISHED：

- 无 quarantine 时 `issues=[]`；
- 有 quarantine 时只含
  `{"code":"RECORD_QUARANTINED","count":quarantine_count}`；
- published Product/Offer count 使用发布前 loader/aggregation 的最终计数。

## 9. Snapshot 构建、验证与原子发布

### 9.1 Canonical 文件

publisher 只写：

```text
<capture_id>/
├── manifest.json
├── products.jsonl
├── offers.jsonl
├── evidence.jsonl
└── exchange_rates.json
```

JSON 使用 UTF-8、`sort_keys=True`、compact separators 和单个结尾换行。records
按 normalized ID 稳定排序。manifest generator：

```json
{"name":"glodex-m1b-ebay-capture","version":"1.0.0"}
```

非空 snapshot inventories：

```text
providers  = ["ebay-browse"]
markets    = ["EBAY_US"]
categories = ["phone"]
currencies = ["USD"]
```

合法空 snapshot 的前三项为空，`currencies=["USD"]`，并仍包含 identity FX。

### 9.2 Staging

在最终 output root 的同一文件系统创建：

```text
<output-root>/.glodex-staging-<random>/<capture_id>/
```

- staging directory mode `0700`，文件 mode `0600`；
- data 文件先写，manifest 最后写，关闭文件后再验证；
- 不把 query、token、raw response 或 quarantine raw item 写入文件。

M1b 是本地单 Operator、单进程 writer；并发 publisher 不在本里程碑范围。

### 9.3 反向验证

发布前执行：

1. `LocalSnapshotCatalog(staging_parent).load(capture_id, display_currency="USD")`；
2. `fatal_issues == ()`；
3. loader `quarantine_issues == ()`；
4. `aggregate_catalog_batch(batch)`；
5. aggregation `quarantine_issues == ()`；
6. materialized Product/Offer count 与 writer、manifest、receipt 预期完全一致；
7. snapshot/FX/Evidence version 与 capture ID 完全一致。

这里的“零新增 quarantine”不包含 Provider mapping 阶段已计入 receipt、且从未写入
staging 的 item。

### 9.4 发布

- 验证后对 `<output-root>/<capture_id>` 执行最终 `lstat`；已存在时 fail closed；
- 将 staging 内层 `<capture_id>` 同文件系统 `os.rename()` 到最终路径；
- 禁止 `os.replace`，不 overwrite/merge；
- **成功 rename 是唯一 commit point**：此前失败保证 final 不可见；此后 cleanup
  只做 best effort，不能把 receipt 从 `PUBLISHED` 降为 `FAILED`；
- rename 前失败只清理本次创建的 staging parent；
- 目标冲突或 rename 失败映射 `SNAPSHOT_PUBLISH_FAILED`；
- loader/aggregation/count 失败映射 `SNAPSHOT_VALIDATION_FAILED`。

最终目录只在一次 rename 后可见。publisher 不修改 `LocalSnapshotCatalog`。

## 10. 与现有 CLI、搜索和 API 的集成

`src/glodex/cli.py` 只做以下窄改动：

1. parser 增加 `capture-provider`；
2. parser error 携带实际 subcommand；Capture 的缺参/未知参数也映射
   `REJECTED / CAPTURE_INPUT_INVALID`，不能落入 M0 `RequestRejected`；
3. parse 完成后，capture 分支在 `load_config()` 前独立分发；
4. 分支内部惰性导入 `glodex.capture.bootstrap`；
5. 使用 Capture 专属 emit/exit code，不把 receipt 包装成 M0
   `RequestRejected`；
6. 其他命令继续使用现有 `_payload()`、`build_service()` 和 `_emit()`。

Capture receipt 的 snapshot 可按现有方式搜索：

```bash
GLODEX_DATA_DIR=/absolute/external/glodex-snapshots \
uv run --locked glodex search \
  --query "推荐一部手机" \
  --snapshot <capture_id> \
  --currency USD
```

现有 Rule Intent、hard gates、ranking、CLI DTO、FastAPI 与 SSE 不修改。由于库存、
tax 和 duty 为 unknown，搜索返回可信 `NO_MATCH` 是预期行为。

## 11. 文件布局

### 11.1 新增

```text
src/glodex/capture/
├── __init__.py
├── bootstrap.py
├── config.py
├── contracts.py
├── ebay_http.py
├── ebay_mapping.py
├── ports.py
├── service.py
└── snapshot_publisher.py

scripts/
└── verify_m1b.py

tests/m1b/
├── __init__.py
├── conftest.py
├── fixtures/
│   ├── ebay_search_empty.json
│   └── ebay_search_success.json
├── unit/
├── contract/
├── acceptance/
├── architecture/
└── nfr/
```

fixture 全部人工构造，只保留 Provider-shaped 字段，不复制 live payload、真实 item
ID、URL、标题、seller 或第三方错误 body。

### 11.2 修改

| 文件 | 计划改动 |
|---|---|
| `specs/002-glodex-m1b-provider/spec.md` | 保持 `v0.2.1 / Approved`，不再新增行为。 |
| `src/glodex/cli.py` | 增加独立 capture 分发。 |
| `scripts/check_traceability.py` | 新增 M1b ID regex、exact inventory 和 profile。 |
| `scripts/verify_m0.py` | sanitizer 增加 `EBAY_`；历史 whole-suite phase 显式 ignore `tests/m1b`，最终 M0 定向根不变。 |
| `tests/conftest.py` | sanitizer 增加 `EBAY_` 前缀。 |
| `tests/architecture/test_import_boundaries.py` | 只为 `capture/ebay_http.py` 放行 `http.client`；其他规则不变。 |
| `tests/unit/scripts/test_verify_m0_runner.py` | 锁定新增的凭据清理与 M1b milestone 隔离。 |
| `README.md` | 增加 opt-in Capture、query disclosure、external root、UNKNOWN/NO_MATCH 和 live smoke 说明。 |

不修改：

- `src/glodex/application/ports.py`
- `src/glodex/application/search_service.py`
- `src/glodex/bootstrap.py`
- `src/glodex/api/**`
- M0/M1a 公共 contracts 和 Golden
- `pyproject.toml` runtime dependency 集合
- `uv.lock`

若实现发现必须修改上述不修改清单中的公共行为，先回到 Spec/Plan，不在 Tasks 中
静默扩展。

## 12. 架构与安全门禁

门禁必须证明：

- runtime dependencies 仍精确为 FastAPI 与 Pydantic；
- `http.client` 只出现在 `capture/ebay_http.py`；
- `urllib.parse` 可以用于编码，但其他网络模块仍禁止；
- domain/application/API/SSE/default adapters 不导入 Capture；
- normal CLI/API composition 不实例化 transport；
- 默认 pytest 仍 `--disable-socket`；
- `EBAY_`、代理、数据库、模型等环境变量在默认测试 collection 前被移除；
- `verify_m0.py` 创建的子进程环境也移除 `EBAY_`，历史 whole-suite phase 不收集
  `tests/m1b`，M0/M1a 的定向最终门禁不变；
- `.env`、live snapshot、receipt、raw/cassette 不在 Git inventory；
- 26 张架构 PNG 继续不 tracked、不 staged；
- credential/token/raw body 不出现在 stdout、stderr、repr、snapshot 或 Git；
- Provider origin、path、market/category/currency/filter 不能被 query/CLI/env 改写；
- output root 与 staging 清理只作用于本次创建的精确路径。

原有 M0 import checker 调整为“默认全禁，唯一精确文件例外”，而不是删除网络规则。
反例测试必须证明其他 Capture 文件或 adapter 导入 `http.client` 仍失败。

## 13. 测试与 Traceability

### 13.1 测试分层

- unit：preflight、env-file parser、receipt、ID 编码、mapper、Evidence、identity
  FX、publisher；
- contract：OAuth/Search method/path/header/body、response cap/root schema、
  manifest v1、CLI 精确 JSON key/exit code；
- acceptance：`M1B-AC-001` 至 `M1B-AC-006`，全部 fake transport；
- architecture：唯一网络文件、依赖方向、default composition、Git inventory；
- NFR：deadline/request/body/item 上限和公开输出/Git 脱敏；
- regression：完整 `verify_m1a.py`；
- live smoke：pytest/默认 runner 外单独执行。

重点边界：

- 10 条完整页可发布，11 条失败；
- body 恰好上限可解码，多一个 byte 失败；
- `next` 存在但请求数仍为 2；
- 缺库存得到 UNKNOWN，绝不得到 IN_STOCK；
- 非空页全部 quarantine 不能伪装为空集合；
- duplicate itemId 的所有记录均 quarantine；
- success 可完整加载；target 已存在、一次验证失败和 rename 失败均无最终半成品；
  rename 后 cleanup 失败仍保持 PUBLISHED；
- empty snapshot 仍有 identity FX；
- search/demo/API/SSE 测试中 live client call count 为 0。

### 13.2 M1b Traceability profile

`check_traceability.py` 增加：

- `M1B_SPEC_PATH`；
- `GLO-M1B-P0-*`、`GLO-M1B-NFR-*`、`M1B-AC-*` definition/reference shape；
- exact inventory `6 P0 / 6 AC / 6 NFR`；
- test root 只收集 `tests/m1b`；
- unknown、missing、duplicate、skip、skipif、xfail 均 fail closed；
- M0 与 M1a profile/inventory 保持原样。

### 13.3 P0 追踪

| P0 | 组件 | 主要 AC |
|---|---|---|
| `GLO-M1B-P0-001` | preflight、CLI、composition、architecture | `M1B-AC-001`、`002`、`005` |
| `GLO-M1B-P0-002` | eBay HTTP、deadline、request/body/item budget | `M1B-AC-002`、`004`、`005` |
| `GLO-M1B-P0-003` | mapper、publisher、loader/aggregation gate | `M1B-AC-002`、`003` |
| `GLO-M1B-P0-004` | manifest v1、现有 search regression | `M1B-AC-001`、`002`、`003` |
| `GLO-M1B-P0-005` | error mapping、quarantine、empty、identity FX | `M1B-AC-003`、`004`、`005` |
| `GLO-M1B-P0-006` | offline gates、receipt、live wrapper | `M1B-AC-001`、`006` |

### 13.4 NFR 追踪

| NFR | 主要验证 |
|---|---|
| `GLO-M1B-NFR-001` | 完整 verify_m1a、CLI/API/Golden/architecture regression |
| `GLO-M1B-NFR-002` | 已发布 snapshot reload 后业务投影一致 |
| `GLO-M1B-NFR-003` | mapper、Evidence closure、loader/aggregation 零新增 quarantine |
| `GLO-M1B-NFR-004` | origin allowlist、env sanitizer、脱敏、Git inventory |
| `GLO-M1B-NFR-005` | request/deadline/body/item/redirect/retry/pagination 边界 |
| `GLO-M1B-NFR-006` | receipt exact schema、计数、traceability、runner |

## 14. 实施阶段

### Phase A：合同、Preflight 与架构窄缝

- RED：receipt 精确 key、退出码、env sanitizer、M1b traceability、唯一网络文件；
- GREEN：capture contracts/config/ports、CLI receipt/argument contract tests、
  M1b profile；实际 parser 与分派在 Phase C 一次性接入，避免暴露未闭环命令；
- Gate：M1b 定向 contract/architecture 与 traceability references。

### Phase B：eBay Transport 与纯映射

- RED：OAuth/Search 形状、request/deadline/body/item cap、错误映射、ID/Evidence/
  UNKNOWN/UnknownCost/FX；
- GREEN：`ebay_http.py` 与 `ebay_mapping.py`；
- Gate：无 socket 的 transport contract + mapper unit/contract。

### Phase C：Snapshot 发布与 CLI 闭环

- RED：canonical files、hash/manifest、代表性发布失败、loader/aggregation、receipt；
- GREEN：publisher、service、bootstrap 与 capture CLI；
- Gate：`M1B-AC-002` 至 `M1B-AC-005`。

### Phase D：完整门禁、文档与 Live Smoke

- 完成 AC-001/006、architecture/NFR、安全和 Git inventory；
- 新增 `verify_m1b.py`，先完整执行 `verify_m1a.py`；
- 使用 `capture-provider --live`、`validate-snapshot` 与 `search` 独立完成真实 smoke；
- README 记录命令、query disclosure、external root、数据时效与 UNKNOWN/NO_MATCH；
- 完成 verification evidence；
- Gate：M1b Definition of Done。

每个 Phase 必须 RED → GREEN → Gate。Plan 批准后，Tasks 再拆成可审查、单链依赖的
提交单元；本 Plan 不预先创建实现空壳。

## 15. 验证命令

快速离线门禁：

```bash
uv run --locked python -m pytest -q -p scripts.verify_m0 tests/m1b
uv run --locked python scripts/check_traceability.py --profile m1b --mode references
```

最终离线门禁：

```bash
uv run --locked python scripts/verify_m1b.py
```

`verify_m1b.py` 固定顺序：

1. 完整 `verify_m1a.py`；
2. M1b architecture/NFR；
3. M1b unit/contract；
4. `M1B-AC-001` 至 `M1B-AC-006`；
5. M1b exact coverage。

runner 固定项目 cwd、参数数组、`shell=False`、sanitized environment，禁止自动更新
Golden、skip、xfail 或 live socket。

独立 live smoke：

```bash
install -d -m 700 /absolute/external/glodex-snapshots

uv run --locked glodex capture-provider --live \
  --env-file .env \
  --output-root /absolute/external/glodex-snapshots \
  --query "smartphone"

# 从上一条 receipt 取得真实值
CAPTURE_ID="capture-..."

GLODEX_DATA_DIR=/absolute/external/glodex-snapshots \
uv run --locked glodex validate-snapshot "$CAPTURE_ID" --currency USD

GLODEX_DATA_DIR=/absolute/external/glodex-snapshots \
uv run --locked glodex search \
  --snapshot "$CAPTURE_ID" \
  --currency USD \
  --query "smartphone"
```

三条命令分开执行：Capture 自身仍只输出一个 receipt JSON；后两条复用现有公共
合同验证加载与搜索。真实 smoke 不属于默认 runner 或 pytest skip 分支。

## 16. 主要风险

| 风险 | 计划控制 |
|---|---|
| eBay itemId 破坏公共 Identifier | 无 padding base64url 无损编码；编码后长度 fail closed。 |
| search ItemSummary 无库存 | UNKNOWN + Evidence；不硬编码有货，允许可信 NO_MATCH。 |
| 多 shipping option 造成低估 | 只有唯一明确 USD cost 才 Known，其余 Unknown。 |
| query 改写 endpoint/filter | host/path/filter 常量；只对 q 使用 urlencode。 |
| http.client 扩散到搜索路径 | 唯一文件 allowlist + 反例 import tests + lazy composition。 |
| 自动测试读取本机凭据 | collection 前清除 `EBAY_`，fake client，全局禁 socket。 |
| token/body/异常泄漏 | secret repr=False；边界只映射 enum；不输出第三方 text。 |
| raw response 被当 fixture 提交 | synthetic fixture + Git inventory gate。 |
| body/JSON 资源攻击 | bounded read + item cap + 必要 root schema 检查。 |
| 全记录 quarantine 被当空结果 | 非空输入零合法记录固定 FAILED。 |
| writer 与 loader 规则漂移 | 发布前用现有 loader + aggregation 反向验证。 |
| staging 半成品可见 | hidden staging、发布前验证、同 FS rename。 |
| FAILED 但 snapshot 已可见 | rename 是唯一 commit point；commit 后错误不再降级 receipt。 |
| target 碰撞 | UUID snapshot、0700 root、最终 lstat；M1b 明确为单 writer。 |
| live 数据无法产生推荐 | M1b 验证可信采集；UNKNOWN 库存/费用导致 NO_MATCH 是合同内结果。 |
| Provider API/条款变化 | live smoke 与 Provider 门禁重审；不静默扩为公开服务。 |

## 17. Plan 审批门禁

进入 Tasks 前必须确认：

- [x] `GLO-SPEC-002 v0.2.1` 已 Approved；
- [x] 用户接受不新增 runtime 依赖，只在一个文件使用标准库 HTTPS；
- [x] 用户接受 `--live + --query + --output-root`，以及可选显式 `--env-file`；
- [x] 用户接受 page=10、OAuth=64 KiB、Search=256 KiB、10s/15s/30s 上限；
- [x] 用户接受缺库存映射 UNKNOWN，tax/duty unknown，live 搜索可为 NO_MATCH；
- [x] 用户接受 base64url ID、provider item 独立 Product 和不做 canonical merge；
- [x] 用户接受 staging → loader/aggregation → rename 的发布顺序；
- [x] 用户接受独立 `tests/m1b`/traceability/verify runner 和默认禁网；
- [x] 用户接受 live smoke 独立执行，不进入默认 CI；
- [x] 文件布局和 Phase A–D 顺序获批；
- [x] 没有第二 Provider、分页、retry、getItem、数据库、队列、缓存、LLM、AG-UI
  或前端任务。

## 18. 调研依据

- eBay Browse API overview：
  <https://developer.ebay.com/api-docs/buy/browse/overview.html>
- Browse item summary search：
  <https://developer.ebay.com/api-docs/buy/browse/resources/item_summary/methods/search>
- eBay OAuth authorization：
  <https://developer.ebay.com/api-docs/static/oauth-credentials.html>
- eBay Buy API requirements：
  <https://developer.ebay.com/api-docs/buy/buy-requirements.html>
- Python `http.client`：
  <https://docs.python.org/3.12/library/http.client.html>
- Python `os.rename`：
  <https://docs.python.org/3.12/library/os.html#os.rename>

2026-07-29 的真实 probe 只用于确认 production auth/search、单页大小和字段覆盖；
其脱敏结论已记录在 Spec，raw response 不属于本 Plan 或测试资产。
