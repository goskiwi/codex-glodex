# Glodex M1b 单 Provider 采集规格

| 字段 | 值 |
|---|---|
| Spec ID | `GLO-SPEC-002` |
| 版本 | `0.2.1` |
| 状态 | Approved |
| 里程碑 | M1b：单 Provider 非变更型采集 → 不可变快照 |
| 实现候选 | `GLO-P1-003` 的前置垂直切片 |
| 父规格 | [`GLO-SPEC-000`](../000-glodex-mvp/spec.md)、[`GLO-SPEC-001`](../001-glodex-m1-api/spec.md) |
| 创建日期 | 2026-07-28 |
| 最后更新 | 2026-07-29 |
| 批准日期 | 2026-07-29 |

## 1. 文档目的与权威边界

本规格定义 Glodex 第一次接入真实商品数据源时，系统必须表现出的用户可见行为。
M1b 不直接实现多 Provider fan-out，而是先证明一条更小、更可信的链路：

```text
Operator 显式发起采集
    → 一个已批准的官方只读 Provider API
    → 严格归一化、隔离和证据闭包
    → 原子发布一个新的不可变快照
    → 复用现有 CLI / FastAPI / SSE / SearchService 搜索
```

权威顺序为：

1. 已批准的 [`GLO-SPEC-000`](../000-glodex-mvp/spec.md)；
2. 已批准的 [`GLO-SPEC-001`](../001-glodex-m1-api/spec.md)；
3. 本规格；
4. 已接受 ADR；
5. 本规格批准后的 Plan 与 Tasks；
6. 本地 `项目架构/` PNG 参考资料。

本地架构图只是目标态参考，不构成 Provider 授权或当前实现证明。

本规格描述产品行为和验收边界，并冻结 M1b 的 eBay Provider 选择。HTTP 客户端、
文件布局和通用资源数值上限仍在本规格批准后进入 Plan。

## 2. 问题与目标

### 2.1 当前问题

M0 已证明确定性的金额、硬门、排序和 Evidence 语义；M1a 已证明本地 API 与 SSE
运行时。但所有商品数据仍来自仓库内版本化 fixture，因此尚未证明：

- 能否把一个真实 Provider 的不可信响应安全映射为现有领域合同；
- Provider 缺字段、超时、限流或返回脏记录时，系统是否仍会诚实失败；
- 凭据、原始响应和第三方错误是否会泄漏；
- 真实数据是否会破坏 `snapshot_version` 与可复现性语义。

### 2.2 M1b 目标

M1b 交付一条 operator-controlled、默认关闭、可审计的采集链路：

1. 只连接一个明确批准的官方 Provider；
2. 每次 Capture 至多发出一个批准的认证请求和一个非变更型商品请求；
3. 把一次采集结果冻结为新的不可变快照；
4. 后续搜索不再访问 Provider，而是完整复用 M0/M1a；
5. 默认自动化门禁仍完全离线；
6. 另以显式 opt-in live smoke 证明至少一个真实 Product/Offer 可被归一化、发布并
   由现有搜索加载。

M1b 的价值不是“实时全网最低价”，而是建立第一个可替换、可验证的外部数据信任
边界。

## 3. 范围

### 3.1 In Scope

- 一个具体、文档化、许可允许的官方 Provider API；
- 一个 marketplace、一个币种和一个受控演示品类；
- 一个 operator-only、显式 opt-in 的 Provider 采集入口；
- 至多一个批准的认证请求和一个非变更型商品请求；
- 一个固定上限的结果页；M1b 不跟随分页；
- 严格的 Provider response 校验、字段映射和资源上限；
- provider-scoped 商品、报价、来源和 Evidence；
- 捕获时间、capture identity、不可变 snapshot identity 和安全采集回执；
- 原子发布 normalized snapshot，不保存原始响应；
- Provider 级失败、单记录 quarantine 和空结果语义；
- 使用采集快照运行现有 CLI、FastAPI、SSE 和 `SearchService`；
- 默认离线受控测试替身与单独的真实 live smoke；
- 凭据、日志、错误、事件和 Git 的安全边界。

### 3.2 Out of Scope

- 第二个 Provider、多 Provider fan-out、fork 或并行 Worker；
- 跨 Provider canonical merge、部分 Provider 成功降级或“全网最优”承诺；
- 每次用户搜索时实时访问 Provider；
- 多页采集、retry、未批准的隐式外呼或额外 endpoint；
- 由 HTTP 请求动态选择 Provider、origin、endpoint、认证方式或 marketplace；
- 抓取网页、浏览器自动化、绕过 Provider 官方 API 或违反服务条款；
- 购物车、下单、支付、账户、库存修改或其他业务写操作；
- 通用 Provider registry、插件系统、SDK 框架或配置 DSL；
- 自动调度、webhook、缓存、数据库、队列、checkpoint 或后台同步服务；
- 完整 circuit breaker、自适应重试、限流平台或生产 SLA；
- 实时 FX、税费、关税或运费推测；
- LLM Intent、翻译、Query 改写、ANN、OpenSearch 或 Category Insight；
- AG-UI、WebSocket、React、长期记忆、用户画像或 AgentLoop。

以上能力不能以空壳、隐藏配置或“以后会用”的依赖进入 M1b。

## 4. 继承且不得破坏的不变量

M1b 原样继承 M0 与 M1a 的已批准合同：

1. `SearchRequest`、`SearchResponse`、现有 search/demo/validate CLI 退出码、
   三个 HTTP 端点和七种 SSE 事件不发生未批准变化；
2. 所有金额继续使用 `Decimal` 和现有币种/舍入规则；
3. 商品本体、库存、预算、证据和费用完整性硬门继续先于排序；
4. 缺失 shipping、tax、duty 或其他费用时保持 `UnknownCost`，不得按 `0`、
   历史值或估算值补造；
5. Provider 只提供候选事实，不能直接决定推荐结果或绕过 final guard；
6. 每个公开事实继续绑定到字段级 Evidence；
7. `NO_MATCH` 不自动放宽条件，`FAILED` 不伪造部分可信结果；
8. 同一不可变 snapshot 的业务投影继续可复现；
9. M1a Run、SSE、重放和 transport-only `ABORTED` 语义不变；
10. 现有搜索组合继续只使用本地 snapshot，不替换 `CatalogGateway`；
11. 只有独立 Capture 边界获准出站；domain、application search、API、SSE 和默认
    组合继续禁止网络；
12. 未显式启用采集时，默认 CLI、API 和全部自动化测试仍为零外部调用。

## 5. 已冻结的产品决策

| ID | 决策 |
|---|---|
| `M1B-D-001` | M1b 使用 capture-first，不在用户 Run 内实时请求 Provider。 |
| `M1B-D-002` | M1b 恰好实现一个 Provider、一个 marketplace、一个币种和一个演示品类。 |
| `M1B-D-003` | Provider、认证 origin、API origin 和允许的 operation 由部署配置固定，用户输入不能改写。 |
| `M1B-D-004` | 每次成功采集发布一个新 snapshot；已有 snapshot 永不覆盖或就地更新。 |
| `M1B-D-005` | 搜索只读取已发布 snapshot，因此同一 snapshot 仍可复现；不同采集不承诺内容相同。 |
| `M1B-D-006` | 不保存原始 Provider payload、access token、client secret 或完整第三方错误。 |
| `M1B-D-007` | Provider 缺失事实保持 unknown；M1b 不新增估价、翻译或推断能力。 |
| `M1B-D-008` | Provider 级失败不发布 snapshot，也不静默回退到本地 fixture。 |
| `M1B-D-009` | M1b 的批准 Capture 边界是一个固定上限的结果页；完整收到该页时可发布，响应不完整或超限时失败。 |
| `M1B-D-010` | 自动门禁默认离线；真实 smoke 必须显式启用、可单独失败且不污染离线基线。 |
| `M1B-D-011` | 成功时 `capture_id == snapshot_version`，继续写现有 manifest v1；采集回执是独立审计结果，不宣称可从 snapshot 重建。 |
| `M1B-D-012` | live snapshot 写入 checkout 外的显式运行目录；现有搜索使用同一 resolved snapshot root。 |
| `M1B-D-013` | M1b 的唯一 Provider 固定为 eBay Buy Browse API，`provider_id=ebay-browse`。 |
| `M1B-D-014` | 目标环境固定为 production；认证使用 OAuth 2.0 Client Credentials 的一次 token POST，商品 operation 只允许一次 `GET /buy/browse/v1/item_summary/search`。 |
| `M1B-D-015` | marketplace 固定为 `EBAY_US`，币种固定为 `USD`，演示品类固定为手机：eBay `category_ids=9355` → canonical category `phone` → `PRIMARY_PRODUCT`。 |
| `M1B-D-016` | Browse `item_summary/search` 未提供库存事实时映射为有来源 Evidence 的 `UNKNOWN`，不得硬编码有货；现有库存硬门可因此产生可信 `NO_MATCH`。 |
| `M1B-D-017` | M1b 不做跨 listing canonical merge；完整 eBay `itemId` 经无 padding base64url 无损编码后，分别形成满足现有 M0 `Identifier` 的 provider-scoped Product/Offer identity，不删除 variant 信息。 |
| `M1B-D-018` | 只有 Provider 明确返回的 USD price/shipping 可成为 KnownCost；不从卖家反馈、标题、营销字段或 listing 存在性推断 rating、库存、销量或费用。 |

## 6. 术语

| 术语 | 含义 |
|---|---|
| Provider | 经用户批准、通过官方 API 提供商品事实的单一第三方。 |
| Capture | 一次 operator 发起、至多含一个认证请求和一个商品结果页的非变更型采集。 |
| Capture ID | 标识一次采集尝试的唯一、不透明 ID。 |
| Snapshot | 一次成功 Capture 归一化并原子发布的不可变 M0 Catalog 数据集。 |
| Provider-level failure | 无法证明单页批准边界完整的认证、传输、根合同或发布故障。 |
| Quarantine | 完整 Capture 内单条记录违反字段/证据合同后，被稳定隔离且不进入候选。 |
| Live smoke | 使用真实凭据和官方 endpoint 执行的一次显式、有界、脱敏验证。 |
| Operator | 拥有本机配置与 Provider 凭据、主动执行采集命令的项目操作者。 |

## 7. 用户旅程

### 7.1 成功采集并搜索

1. Operator 在进程环境或批准的 secret 注入机制中配置 Provider 凭据；
2. Operator 在固定演示品类内使用自由文本 query 发起一次采集；
3. 系统至多发出一个批准的认证请求和一个非变更型商品请求，不跟随下一页；
4. 系统校验、归一化并原子发布新 snapshot；
5. stdout 返回机器可读的安全采集回执；
6. Operator 把 `snapshot_version` 和 receipt 中的批准币种传给现有
   `glodex search` 或 M1a POST；预算若存在也使用该币种；
7. 现有 SearchService 从该 snapshot 产生 `COMPLETED`、`NO_MATCH` 或 `FAILED`。

### 7.2 合法空集合

Provider 成功返回空集合时，Capture 可以成功并发布空 snapshot。使用 receipt
中的批准币种搜索时返回 `NO_MATCH`，不自动切换 query、Provider 或本地 fixture。

### 7.3 Provider 级失败

认证失败、timeout、`429`、`5xx`、非法根 schema、未批准 redirect、响应超限或
单页响应不完整时：

- Capture 返回稳定安全失败；
- 不发布新 snapshot；
- 不保留半成品或原始 payload；
- 不自动调用第二 Provider；
- 不把第三方 body、凭据、路径或 traceback 返回给用户。

### 7.4 单记录隔离与单页边界

M1b 的“完整”只指完整收到一个固定上限的 Provider 结果页，不代表 Provider 全部
数据，也不承诺检索覆盖率。响应中的 next cursor/link 只作为不可信数据忽略，不能
触发第二个商品请求。完整单页内的个别坏记录进入 quarantine；若 deadline、记录或
byte 上限使该页无法完整接收/校验，则 Capture 失败且不发布部分 snapshot。刚好在
允许上限内完整结束仍可发布。

## 8. 采集入口与公共行为

M1b 增加一个 operator-only CLI 子命令，规范名称为：

```text
glodex capture-provider
```

具体参数布局进入 Plan，但公共行为固定如下：

- 必须显式提供非空自由文本 query；Provider 仍强制使用已批准的演示品类筛选，
  M1b 不使用 LLM 翻译、扩写或改写；
- Provider、origin allowlist、operation、marketplace、币种和品类来自独立的
  Capture 配置，不进入 `SearchRequest` 或现有业务 config fingerprint；
- live 输出 root 必须显式解析到 checkout 外；后续 CLI/API 以现有配置读取同一
  snapshot root；
- 缺配置、缺凭据、不安全输出 root 或不支持输入在任何外呼前拒绝；
- 每次 Capture 的实际外呼不超过两个：认证请求 `0..1`、商品请求 `0..1`；
  `PUBLISHED` 必须恰好完成一个商品请求；不跟随 redirect/next link，不 retry，
  也不允许隐式外呼；
- Provider record quarantine 必须在写 staging 前完成；staging 在最终可见前必须
  按现有 loader 与 aggregation 语义验证：`fatal_issues`、loader quarantine 和
  aggregation quarantine 全为空，物化 Product/Offer 数与 manifest、receipt
  计数一致；否则 `FAILED / SNAPSHOT_VALIDATION_FAILED`，不得发布；
- stdout 只输出一个版本化 JSON 回执，stderr 只输出安全简述。

Capture 是独立 operator 链路，不实现或替换 `CatalogGateway`，不修改
`SearchService`、默认组合、HTTP/SSE 路由或 DTO。M1b 可以只在 Capture
实现边界引入一个受控 outbound transport，并同步收窄架构门禁；
domain、application search、API、SSE 和默认搜索路径仍禁止网络。

状态和退出码固定为：

| 状态 | Exit | 语义 |
|---|---:|---|
| `PUBLISHED` | `0` | 单页响应完整，snapshot 已验证并原子发布。 |
| `FAILED` | `1` | Capture 已建立但失败；不发布 snapshot。 |
| `REJECTED` | `2` | 本地 preflight 拒绝；零外呼、无 Capture。 |

`capture_id` 只在 preflight 全部通过、即将第一次外呼时分配。成功时
`capture_id == snapshot_version`；该值必须满足继承的公共 `SnapshotVersion`
合同。`FAILED` 有 `capture_id` 但无 `snapshot_version`，`REJECTED` 两者都没有。

Capture query 会发送给获批 Provider，CLI help 与 README 必须告知 Operator。
除 query 和 operation 必需的受控筛选外，不发送 `thread_id`、`run_id`、用户画像、
历史对话或本机路径；query 不写入 snapshot、日志或回执。

### 8.1 Capture receipt v1

`schema_version` 固定为 `glodex.capture-receipt.v1`。每种状态的顶层 key 集合精确
如下；不适用字段直接缺席，不编码为 `null`：

| 状态 | 精确字段 |
|---|---|
| `REJECTED` | `schema_version`, `status`, `request_count`, `issues` |
| `FAILED` | 上述字段，加 `provider_id`, `capture_id`, `marketplace`, `currency`, `category`, `received_record_count` |
| `PUBLISHED` | `FAILED` 字段，加 `snapshot_version`, `captured_at`, `published_product_count`, `published_offer_count`, `quarantine_count` |

ID、Provider、marketplace、currency 和 category 是非空安全字符串；所有 count 是
非负整数且不能为 boolean；`captured_at` 是 UTC RFC 3339。`request_count` 统计
认证与商品请求的每个实际出站 attempt；`REJECTED` 必为 `0`。
`received_record_count` 是完整解码的单页 item 数；`quarantine_count` 按写
staging 前被排除的输入 item 计数，每个 item 最多一次；published count 是
loader 与 aggregation 验证后计数一致的最终 Product/Offer 数。

`issues` 是按 `code` 排序、同码合并的对象数组，每项精确为
`{"code": <string>, "count": <positive integer>}`。允许的稳定 code 与状态为：

| 状态 | Issue codes |
|---|---|
| `REJECTED` | `CAPTURE_CONFIG_INVALID`, `CAPTURE_CREDENTIALS_MISSING`, `CAPTURE_INPUT_INVALID` |
| `FAILED` | `PROVIDER_TARGET_REJECTED`, `PROVIDER_AUTH_REJECTED`, `PROVIDER_TIMEOUT`, `PROVIDER_RATE_LIMITED`, `PROVIDER_UNAVAILABLE`, `PROVIDER_RESPONSE_INVALID`, `PROVIDER_RESPONSE_INCOMPLETE`, `PROVIDER_RESPONSE_LIMIT`, `SNAPSHOT_VALIDATION_FAILED`, `SNAPSHOT_PUBLISH_FAILED` |
| `PUBLISHED` | 无 issue，或 `RECORD_QUARANTINED` |

回执不包含凭据、原始 query/body、绝对路径、第三方自由错误或 traceback。
`RECORD_QUARANTINED.count` 必须等于 `quarantine_count`。
PUBLISHED receipt 是独立审计结果；manifest v1 与 normalized snapshot 不保存
quarantine/count 统计，也不承诺能重建回执。

M1b 不新增 HTTP endpoint。采集后，现有搜索入口只接收返回的
`snapshot_version`，搜索阶段不再访问 Provider。

## 9. Snapshot 与数据信任合同

### 9.1 Identity 与不可变性

- 每次已建立的 Capture 都有新的 `capture_id`；
- 每次成功 Capture 的 `snapshot_version` 与 `capture_id` 相同且非恒定；
- 继续写现有 `glodex.snapshot-manifest.v1`，不要求 manifest v2；
- manifest 的 file hash 验证内容完整性，PUBLISHED receipt 把 snapshot ID 绑定到
  Provider、marketplace、币种、品类和采集时间；
- 禁止使用恒定 `live-v1` 指向不断变化的数据；
- 目标 snapshot 已存在时 fail closed，不覆盖、不 merge；
- snapshot 必须不可变、完整性可验证且所有内部版本一致；
- 发布必须原子：搜索只能看到完整旧版本或完整新版本；
- 合法空 snapshot 的 manifest provider/market/category inventories 按现有 v1
  规则为空，其采集 provenance 仅由 receipt 表达。

### 9.2 最小可信字段

进入 snapshot 的记录必须能够证明：

- 固定 `provider_id`；
- 稳定、provider-scoped 的 product/offer identity；
- marketplace 与币种；
- 商品标题、受控 canonical category、`entity_kind` 及属性；
- 报价金额；
- 明确的库存事实或现有 unknown 语义；
- 官方商品/报价 source；
- capture time；
- 所有公开字段所需的 Evidence。

eBay Browse 的 identity、category、`entity_kind` 和库存映射固定如下：

- 完整 `itemId` 的 UTF-8 bytes 使用 RFC 4648 URL-safe base64、去除尾部 `=`
  padding 后，分别以 `ebay-browse:product:<encoded>` 和
  `ebay-browse:offer:<encoded>` 建立 provider-scoped identity；编码必须可无损
  还原完整原值并满足现有 M0 `Identifier`/长度合同，否则 quarantine。M1b 将每个
  listing 作为独立 Product，不删除前缀、variant 或其他 identity 片段；
- 商品请求固定携带 `category_ids=9355`，canonical category 固定为 `phone`，
  `entity_kind` 固定为 `PRIMARY_PRODUCT`；该映射来自批准的 Provider category
  filter，不从标题临时猜测；
- `listingMarketplaceId` 若存在必须为 `EBAY_US`；`price.currency` 必须为 `USD`；
- `itemId`、非空 `title`、`itemWebUrl`、正数 `price.value` 或 `price.currency`
  缺失/非法时 quarantine 该 item；
- Browse ItemSummary 没有库存字段时映射为 `UNKNOWN`，Evidence 绑定该 item 的
  官方 source 与 Capture time；不能因为 listing 存在、`FIXED_PRICE` 或
  `buyingOptions` 而推断 `IN_STOCK`；
- Provider 明确返回零个 item 才是合法空集合；非空响应若没有至少一个合法
  Product 和 Offer，则 `FAILED / PROVIDER_RESPONSE_INVALID`，不能伪装为空集合。

### 9.3 费用与币种

M1b 只支持一个 marketplace 和其批准币种：

- Provider 明确返回的 price/shipping 才能成为 KnownCost；
- 未返回的 shipping、tax、duty 保持 UnknownCost；
- 不调用实时 FX，不抓取税率，不猜测重量或运费；
- 每个 snapshot（包括空 snapshot）都包含批准币种的 identity FX table：
  `base_currency == currency`、`base_per_unit == Decimal("1")`，minor units 来自
  批准配置；其 Evidence 使用 `provider_id=glodex-system`、
  `source_uri=urn:glodex:identity-fx:<currency>`，`captured_at` 等于 manifest
  `created_at`；这不是外部汇率估算；
- 不支持的币种在 Capture 或后续 Search 中按现有规则 fail closed；
- 后续 Search 必须显式使用 receipt 币种，预算若存在也必须同币种；否则保持现有
  `FAILED` 语义；
- 结果可能因此全部被硬门淘汰，这是可信行为，不是需要自动放宽的错误。

### 9.4 数据保留

- 应用只持久化 normalized snapshot；receipt 只写 stdout，由 Operator 决定是否
  在 checkout 外另行保存；
- 原始 response body 只在内存中受限处理，处理后丢弃；
- live snapshot 与持久化 receipt 都属于 checkout 外的本地运行数据，不进入 Git、
  Golden 或测试 fixture；
- 保留期限、删除要求和可展示字段必须服从最终批准 Provider 的许可与条款；
- M1b 不实现自动清理 scheduler；若条款要求自动删除，则该 Provider 不能在本
  里程碑获批，除非重新走规格变更。

## 10. P0 功能需求

| ID | 需求 | Fail-closed 条件 |
|---|---|---|
| `GLO-M1B-P0-001` | 只有 Operator 显式启用时才连接一个已批准 Provider；受控出站只存在于独立 Capture 边界，默认搜索继续使用本地 snapshot。 | Provider、origin、凭据或模式可由普通搜索请求改写，或 search/API/SSE 路径可外呼时验收失败。 |
| `GLO-M1B-P0-002` | 每次 Capture 外呼不超过两个：认证 `0..1`、商品 `0..1`；`PUBLISHED` 恰好完成一个商品请求；对 deadline、记录和 body 设置上限，不分页、不 retry。 | 未批准 redirect、next link、额外 endpoint、隐式外呼、业务写操作或不完整/超限响应被发布时验收失败。 |
| `GLO-M1B-P0-003` | 把完整单页严格映射为现有 manifest v1 snapshot；发布前 loader/aggregation 无 fatal 或新增 quarantine，Product/Offer 计数与 manifest/receipt 一致，再原子发布。 | 恒定/非法 version、覆盖旧版本、验证/计数不一致、半成品可见或原始 payload 落盘时验收失败。 |
| `GLO-M1B-P0-004` | 采集数据完整复用现有金额、硬门、排序、Evidence、终态、CLI、API 和 SSE 语义。 | Provider 绕过硬门、改变公共 DTO 或在搜索阶段再次外呼时验收失败。 |
| `GLO-M1B-P0-005` | Provider 级故障原子失败；坏记录稳定 quarantine；合法空集合可发布；每个 snapshot 均含单币种 identity FX 与 Evidence。 | 静默回退、全记录隔离后伪装空集合、伪造费用/库存、缺 identity FX 或泄露第三方详情时验收失败。 |
| `GLO-M1B-P0-006` | 提供默认离线的完整 M1b 门禁和独立 opt-in live smoke 合同，输出可审计且脱敏的证据。 | 默认测试需要凭据/公网、live payload 进入 Git，或 M0/M1a 回归未先通过时验收失败。 |

## 11. Given-When-Then 验收场景

### `M1B-AC-001` 默认路径完全不变

**Given** 未显式启用 Provider Capture，且没有凭据或公网

**When** 执行现有 CLI、FastAPI、SSE 和完整自动化门禁

**Then** Provider 调用为零，M0/M1a DTO、Golden、结果、事件和错误合同保持不变；
domain、application search、API、SSE 与默认组合继续禁止网络

覆盖：`GLO-M1B-P0-001`、`GLO-M1B-P0-006`

### `M1B-AC-002` 单 Provider 正常采集并复用搜索

**Given** 已批准 Provider 在一个 marketplace 返回字段完整、可证明来源的商品和报价

**When** Operator 执行一次 Capture，再使用返回的 snapshot 和 receipt 币种搜索；
预算若存在也使用该币种

**Then** 至多发生一个批准认证请求并恰好完成一个商品请求；staging snapshot
经 loader/aggregation 验证后无 fatal、无新增 quarantine，Product/Offer 数与
manifest/receipt 一致，再原子发布；搜索继续满足现有金额、硬门、排序和 Evidence
规则：至少一个候选通过硬门时为 `COMPLETED`，否则为诚实的 `NO_MATCH`

M1b 验证可信数据采集与现有搜索的闭环，不承诺真实 Capture 必然产生推荐结果。
若显示币种或预算币种不同，则继续按现有合同 `FAILED`。

覆盖：`GLO-M1B-P0-001`、`GLO-M1B-P0-002`、`GLO-M1B-P0-003`、
`GLO-M1B-P0-004`

### `M1B-AC-003` 空集合、单记录隔离与 unknown 可发布

**Given** Provider 完整单页明确返回零个 item，或返回至少一个合法 Product/Offer、
可独立隔离的坏记录、缺失的可选费用，以及缺失/明确 unknown 的库存状态

**When** 系统归一化、发布，并使用 receipt 币种搜索

**Then** Capture 为 `PUBLISHED`；系统发布空 snapshot 或只包含合法记录的
snapshot，quarantine 数稳定；每个 snapshot 都有 identity FX；shipping、tax、
duty 保持 `UnknownCost`，Browse 未提供或明确 unknown 的库存保留 `UNKNOWN`
Evidence，不因库存字段缺失单独 quarantine；现有库存硬门继续拒绝 UNKNOWN Offer，
搜索诚实返回 `NO_MATCH` 或 `COMPLETED`

覆盖：`GLO-M1B-P0-003`、`GLO-M1B-P0-004`、`GLO-M1B-P0-005`

### `M1B-AC-004` Provider 级故障诚实失败

**Given** A：本地 preflight 发现缺配置、缺凭据或不支持输入；或 B：Capture
建立后发生认证拒绝、timeout、`429`、`5xx`、非法根 schema、响应超限/不完整、
非空页全部记录不可信，或 staging snapshot 验证/发布失败

**When** Operator 发起 Capture

**Then** A 返回 `REJECTED`、exit `2`、无 `capture_id` 且零外呼；B 返回
`FAILED`、exit `1`、有 `capture_id`；两者都不发布 snapshot，也没有本地 fixture
回退、第二 Provider 调用、半成品、原始 body、凭据、绝对路径或 traceback

覆盖：`GLO-M1B-P0-002`、`GLO-M1B-P0-005`

### `M1B-AC-005` 出站目标与资源边界不可被输入突破

**Given** 恶意 query、未批准 redirect/next link、额外 endpoint 或超大 response

**When** Capture 边界处理该输入

**Then** query 不能改写 origin allowlist、operation、认证方式或受控筛选；系统
只访问批准的认证/API origin，至多执行一个认证请求和一个商品请求；不跟随
redirect/next link，不 retry，不调用额外 endpoint；完整单页在允许上限内结束时
可以发布，响应不完整或超限时停止且不发布；日志和回执不泄密

覆盖：`GLO-M1B-P0-001`、`GLO-M1B-P0-002`、`GLO-M1B-P0-005`

### `M1B-AC-006` 离线门禁与 live smoke 合同分轨

**Given** 无网络的固定 Provider-shaped response，以及独立的 opt-in live smoke
配置

**When** 执行 M1b 默认门禁，并验证 live smoke 命令的安全合同

**Then** 默认门禁无 socket、无 skip/xfail 并先完整通过经窄边界更新的 M1a；
live smoke 必须显式启用，至多一个认证请求和一个商品请求，且遵守 receipt v1 与
脱敏合同。真实非空映射、发布与加载成功是独立 DoD 证据，不是默认门禁的前置条件

覆盖：`GLO-M1B-P0-006`

## 12. 非功能需求

| ID | 类别 | 要求 |
|---|---|---|
| `GLO-M1B-NFR-001` | 兼容性 | M0/M1a 公共 DTO、CLI、12 个 M0 AC、10 个 M1a AC、Golden、API 与 SSE 不变；架构门禁只允许 Capture 窄出站边界。 |
| `GLO-M1B-NFR-002` | 可复现性 | 同一已发布 snapshot 的业务投影一致；不同真实 Capture 不宣称数据或排序完全相同。 |
| `GLO-M1B-NFR-003` | 数据与证据完整性 | 进入已发布 snapshot 的 identity、category、entity kind、金额、库存、marketplace、source、identity FX 和所需 Evidence 闭合率为 100%；不能证明即 unknown、quarantine 或 fail closed。 |
| `GLO-M1B-NFR-004` | 安全与隐私 | 只访问 allowlist 内的官方认证/API origin；凭据、token、完整 query、原始 payload、路径和第三方自由错误不得进入 Git、snapshot、日志、事件或公共错误。 |
| `GLO-M1B-NFR-005` | 有界性 | Capture 自身的 deadline、记录和 body 有明确可测试上限；request count 不超过两个，不分页、不 retry；完整单页刚好在上限内结束可发布。 |
| `GLO-M1B-NFR-006` | 可验证与可观测 | 自动测试使用可控响应且默认禁网；receipt v1 精确、稳定、脱敏，并准确记录状态、ID、请求/记录/发布/quarantine 计数和 issue code。 |

M1b 不定义跨机器吞吐、真实 Provider 可用性、商品覆盖率、推荐质量、价格时效或
完整 landed cost SLA。

## 13. Provider 选择门禁

### 13.1 已选 Provider 与真实探测

M1b 固定使用 [eBay Buy Browse API](https://developer.ebay.com/api-docs/buy/browse/overview.html)。
用户授权使用其本机 eBay application credentials；凭据只由当前进程环境或被 Git
忽略的本地 `.env` 注入，不进入版本化配置。

2026-07-29 在目标 production 环境完成两次有界只读探测：

- OAuth token POST 返回 `200`；
- `EBAY_US` 的 `GET /buy/browse/v1/item_summary/search` 返回 `200`；
- 固定 `category_ids=9355`、`q=smartphone`、`limit=10` 和
  `buyingOptions:{FIXED_PRICE}` 时返回 10 条 item、全部为 USD；
- 该单页响应为 20,239 bytes；10/10 有 price，10/10 有 shippingOptions，
  0/10 有 availability 字段；
- 原始 response、access token 和凭据未保存，以上只记录脱敏计数与字段覆盖。

真实探测证明目标账号当前可调用所选 operation，不把 Provider 可用性或数据字段
永久冻结为事实。默认测试仍使用人工构造的 Provider-shaped success/empty/failure
fixture；不得把 live payload 复制进 Git。

### 13.2 Provider 批准包

| 决策 | 已冻结值 |
|---|---|
| Provider、认证请求与商品 operation | `ebay-browse`；OAuth 2.0 Client Credentials；`POST https://api.ebay.com/identity/v1/oauth2/token`；`GET https://api.ebay.com/buy/browse/v1/item_summary/search`。 |
| 账号与环境 | 用户授权的 eBay application；production；凭据运行时注入，默认关闭。 |
| marketplace / currency / demo category | `EBAY_US` / `USD` / eBay category `9355` → `phone` → `PRIMARY_PRODUCT`。 |
| 使用边界 | M1b 仅用于本地课程/开发验证与只读商品检索；不实现 checkout、交易写操作、公开托管服务或第三方数据再分发。若扩大用途，必须重新核对 [Buy API requirements](https://developer.ebay.com/api-docs/buy/buy-requirements.html) 并修改规格。 |
| Provider 官方 page / response 特征 | [Browse search](https://developer.ebay.com/api-docs/buy/browse/resources/item_summary/methods/search)；固定单页、不分页；成功根对象的 `itemSummaries` 为数组；Plan 设置严格 deadline、record/body 上限。 |
| identity / category / entity kind 映射 | 完整 `itemId` 经无 padding base64url 无损编码后的 provider-scoped Product/Offer；固定 category filter `9355`；canonical `phone`；`PRIMARY_PRODUCT`。 |
| price / inventory / Evidence / unknown 映射 | USD price 必须存在且为正数；shipping 仅按明确字段；tax/duty unknown；库存缺失映射 `UNKNOWN`；不推断 rating、销量或库存。所有公开字段绑定 item source 与 Capture time。 |
| minor units 与 identity FX Evidence | USD minor units=`2`；`base_currency=USD`、`base_per_unit=Decimal("1")`；系统 Evidence URI=`urn:glodex:identity-fx:USD`。 |

Plan 必须把 probe 的实际 query page limit 固定为 `10`，并以合成、脱敏的
Provider-shaped fixture 覆盖非空、空集合、认证拒绝、`429`、`5xx`、timeout、
redirect、超限和非法 schema；live 探测不故意制造第三方失败。

## 14. 进入 Plan 的 Definition of Ready

- [x] capture-first、单 Provider、operator-only 和默认离线边界已冻结；
- [x] M0/M1a 公共搜索、API 和事件合同保持不变；
- [x] `REJECTED` / `FAILED` / `PUBLISHED`、单页、空集合、quarantine、unknown 与上限语义已定义；
- [x] 每个 `GLO-M1B-P0-*` 至少由一个 `M1B-AC-*` 覆盖；
- [x] manifest v1、receipt v1、identity FX、发布前验证和 Capture 窄网络边界已冻结；
- [x] 用户选定具体 Provider、认证/商品 operation、账号和目标环境；
- [x] 用户确认 marketplace、币种和演示品类；
- [x] 使用范围收窄为本地课程/开发验证；公开部署、交易和再分发不在 M1b；
- [x] Provider 官方 page/response 特征，以及 identity/category/entity kind、
  字段/Evidence 与保留规则已记录；应用采用更严格的具体数值上限并留到 Plan；
- [x] 真实非空单页已脱敏验证；空结果和主要失败的 Provider-shaped fixture 合同
  已冻结，精确 fixture 在 Plan/Tasks 实现；
- [x] 所有影响用户行为的开放问题已清零。

在以上项目全部勾选并由用户明确批准前，不进入 Plan。

## 15. Definition of Done

- [x] 本规格状态改为 `Approved`；
- [ ] Plan 和 Tasks 只设计本规格批准的单 Provider capture-first 能力；
- [ ] 一个真实 Provider 和一种非变更型商品 operation 可用；
- [ ] 采集成功时原子发布不可变 snapshot，失败时无半成品；
- [ ] staging snapshot 的 loader/aggregation 无 fatal 或新增 quarantine，物化
  Product/Offer 数与 manifest/receipt 一致；空 snapshot 也含 identity FX 并可
  返回 `NO_MATCH`；
- [ ] `M1B-AC-001` 至 `M1B-AC-006` 全部由默认离线自动化覆盖且无 skip/xfail；
- [ ] M1b exact traceability 为 `6 P0 / 6 AC / 6 NFR`；
- [ ] `verify_m1b.py` 先完整执行 `verify_m1a.py`，再执行 M1b 离线门禁；
- [ ] 架构门禁只为 Capture 实现边界放行受控出站，其余路径继续禁网；
- [ ] 独立执行一次脱敏的真实 live smoke：状态为 `PUBLISHED`，至少发布一个合法
  Product 和一个合法 Offer；使用 receipt 币种（预算若有也同币种）时，snapshot
  可由现有搜索加载并得到 `COMPLETED` 或可信 `NO_MATCH`；
- [ ] 凭据、token、原始 payload、live snapshot 和敏感 cassette 未进入 Git；
- [ ] README 诚实说明 opt-in、单 Provider、unknown 费用、数据时效与无 SLA；
- [ ] 没有第二 Provider、fan-out、写操作、通用插件、数据库、队列、缓存、
  scheduler、webhook、LLM、AG-UI 或前端空壳。

## 16. 后续候选

M1b 完成后，每项继续独立走 Spec → Plan → Tasks：

1. 第二 Provider 与有界 fan-out；
2. 跨 Provider canonical merge 和部分失败降级；
3. request-time live query 与 capture identity 合同；
4. snapshot-stable cursor/total-count 支持下的多页采集；
5. Provider-specific retention cleanup；
6. LLM Intent、Category Insight、AG-UI 或前端。

## 17. 变更治理

1. 改变 M0/M1a DTO、API、SSE、金额、Evidence 或终态语义，必须先修改父 Spec；
2. 改变 capture-first、单 Provider 或默认离线边界，必须先修改本 Spec；
3. 具体依赖、端口、目录布局和数字上限在 Spec 批准后进入 Plan；
4. Provider 官方 API、许可或字段变化时，必须重新检查 Provider 选择门禁；
5. 架构图中的技术不能绕过 Spec 审批进入实现；
6. 本规格批准前只允许调研和规格编辑，不允许提交 Provider 实现。
