# Glodex M1e ESCI 公开商品检索基准规格

| 字段 | 值 |
|---|---|
| Spec ID | `GLO-SPEC-005` |
| 版本 | `0.1.0` |
| 状态 | Approved |
| 里程碑 | M1e：基于公开 ESCI 数据的离线检索/重排基准 |
| 父规格 | [`GLO-SPEC-000`](../000-glodex-mvp/spec.md)、[`GLO-SPEC-004`](../004-glodex-m1d-agent-demo/spec.md) |
| 创建日期 | 2026-07-30 |
| 最后更新 | 2026-07-30 |
| 批准日期 | 2026-07-30 |

## 1. 目的与结论

M1e 为学生可运行的 Glodex 增加一份真实、公开、可复现的商品检索证据，而不要求注册
Amazon、Shopee 或 AliExpress 的商业账号。

首发数据源固定为 Amazon Science 发布的 **Shopping Queries Dataset（ESCI）**：它提供
query、商品文本和 `Exact`、`Substitute`、`Complement`、`Irrelevant` 人工相关性标签，
并以 Apache-2.0 发布。[官方仓库](https://github.com/amazon-science/esci-data)

这不是 live marketplace adapter，也不是新的可发布商品目录。ESCI 没有实时价格、库存、
运费、税费或可核验商品页，因此把它装入 `CatalogBatch` 或让它发布购物推荐会破坏现有
Canonical Hard Gates。M1e 的唯一用户可见产物是**离线检索/重排评测报告**。

```text
Operator 已下载的 ESCI 原始文件
  → 确定性导入与许可/哈希校验
  → 小型、版本化 benchmark artifact
  → 纯离线候选排序与指标
  → 安全 JSON 摘要
```

M1e 不改变 M0–M1d 的搜索、Agent、API、SSE、eBay live 或默认 Demo 行为。

## 2. 固定数据语义

### 2.1 唯一首发来源

| 项目 | 固定规则 |
|---|---|
| 来源 | `amazon-science/esci-data` 的 Shopping Queries Dataset |
| 许可 | Apache-2.0；发布派生小样本时保留上游 `LICENSE` 与 `NOTICE` |
| 数据版本 | `small_version` 的一个固定 `us` locale 子集 |
| 选取 | 只使用 `split == "test"`；抽样规则、固定 seed、上游 revision 与三个输入文件 SHA-256 必须写入 manifest |
| 规模 | 至多 500 个 query；每个 query 至多保留 20 个上游候选；派生 artifact 总大小不超过 20 MiB |
| 本地化 | 首发只评测英语 `us` 商品文本；不声称中文检索质量 |

导入器只接受 Operator 已经下载到 checkout 外的源目录。普通 CLI、pytest、API、SSE
和 Agent 都不得下载、clone、联网读取或自动更新该数据集。

### 2.2 可使用与不可使用的字段

可使用的事实仅为上游提供的 `query`、`product_id`、`product_title`、
`product_description`、`product_bullet_point`、`product_brand`、`product_color`、
`product_locale` 与 ESCI label。`Exact`、`Substitute`、`Complement`、`Irrelevant`
是**离线评测标签**，不是给 Agent、Picker、Ranker 或用户结果的运行时商品事实。

不得推断、补写或展示以下事实：当前价格、折扣、库存、卖家、配送、税费、关税、评分、
商品页 URL、实时 Amazon 可用性或跨平台同款关系。

## 3. 范围

### 3.1 In Scope

- 一个显式 Operator-only 的 ESCI 导入/构建入口，输入为本地原始文件，输出为固定
  `esci-small-us-v1` benchmark artifact；
- 由 `queries`、去重 `products`、query-product `judgements`、严格 manifest 和
  Apache-2.0/NOTICE 构成的只读 JSONL artifact；
- 纯本地、确定性的文本候选重排：每个 query 只对其上游给出的候选池排序；
- 一个显式 `benchmark-esci` CLI，输出版本、来源、样本量、label 分布和固定指标的单行
  JSON 摘要；
- `Exact@10`、`MRR@10` 与 `nDCG@10`：固定 gain 为 `Exact=3`、`Substitute=2`、
  `Complement=1`、`Irrelevant=0`；`MRR@10` 只以 `Exact` 为正例；
- 导入的 hash、上游 revision、许可、抽样规则和构建器版本都可由 manifest 复核；
- 导入、损坏数据、确定性、指标、CLI、离线安全和 M0–M1d 不回归的自动化证据。

### 3.2 Out of Scope

- Amazon/Shopee/AliExpress/eBay 的 live API、登录、凭据、网页抓取、代理或第三方
  数据服务；
- 将 ESCI 数据转为 `CatalogBatch`、`SearchResponse`、Agent Candidate、价格比较、
  运费计算、`EligibilityEvaluator` 输入或购物推荐；
- 改动九个业务工具、`dispatch_tool`、Agent registry、DeepSeek、DashScope、Tavily、
  M1b Capture、默认 CLI/Search API/SSE；
- 向量数据库、ANN/HNSW、数据库、缓存、训练、微调、远程 embedding、LLM 评测或
  新的长期服务；
- Amazon Reviews 2023、Olist、WDC Products 的导入或再分发。它们保留为未来候选，
  需要分别进行许可和语义审查；
- 使用 ESCI 的测试标签训练、调参、重排或出现在任何用户购物结果中。

## 4. 设计约束与运行边界

1. M1e 是一个独立 benchmark slice，不引入新的购物平台 enum、DataMode、Provider
   抽象、Agent 工具或泛化插件系统。
2. 测试与默认命令保持 socket 禁用；只有显式的本地导入命令读取用户指定目录，且不
   发出网络请求。
3. 原始 Parquet/CSV 和全量源数据不进入 Git。仅可提交经过大小门限的派生 JSONL
   小样本、manifest、上游 LICENSE/NOTICE 和必要的 attribution。
4. 排序器不得读取 ESCI label；label 只由 evaluator 在排序完成后使用。
5. 同一原始文件、revision、抽样规则和 seed 必须生成逐字节相同的 artifact 与相同
   指标；稳定 ID 是所有并列分数的最终 tie-breaker。
6. CLI 不回显完整 query、商品文本或标签行；它只输出聚合指标和不可变版本/哈希标识。
7. 这份 benchmark 证明的是给定历史候选池上的离线检索质量，不证明 Amazon 搜索、
   商品价格、库存、配送、推荐质量或生产 SLA。

## 5. 需求

| ID | 要求 |
|---|---|
| `GLO-M1E-P0-001` | **来源、许可与派生数据可复核**：构建前必须验证输入文件集合、SHA-256、source revision、Apache-2.0、NOTICE 和固定选择规则。artifact manifest 必须精确记录这些非动态元数据；缺任一项或出现未知字段则拒绝构建。README 必须给出来源、归属、许可、下载方式与“历史离线 benchmark”声明。 |
| `GLO-M1E-P0-002` | **严格、确定性的 benchmark artifact**：导入器必须只接受上游三个规定数据文件的严格列集合，拒绝重复 query/product identity、无效 locale、未知 label、丢失产品文本、越界样本或不匹配的 query-product 关系。输出只包含第 2.2 节许可的字段，并受 500 query、20 candidate/query、20 MiB 的上限约束。 |
| `GLO-M1E-P0-003` | **不泄漏标签的离线检索评测**：评测必须用固定的本地文本 scorer 为每个 query 的候选池排序，不能读取 label、使用网络、模型或随机数。所有指标使用第 3.1 节的固定定义，并对无 `Exact` 的 query 明确排除 `MRR@10` 分母而不将其伪造为零命中。 |
| `GLO-M1E-P0-004` | **与购物发布链严格隔离**：ESCI artifact 不得被 `LocalSnapshotCatalog`、`SearchService`、Agent、API 或 SSE 加载。普通 `search`、`agent-demo`、`validate-snapshot`、M1b/M1c/M1d 命令和全部既有 Golden bytes 保持不变；M1e 的唯一入口为显式 benchmark CLI。 |

## 6. 验收场景

### `M1E-AC-001` 真实源小样本导入

从已校验的本地 ESCI 输入生成 `esci-small-us-v1`；manifest、LICENSE/NOTICE、计数、
SHA-256 和选择规则闭合。

### `M1E-AC-002` 可重现构建

两次使用同一输入的构建产物逐字节相同；改变输入 hash、seed 或列集会安全拒绝。

### `M1E-AC-003` 无标签泄漏评测

构造小候选池可证明 scorer 的排序与 label 无关；固定 gain 得到精确的 `Exact@10`、
`MRR@10`、`nDCG@10`。

### `M1E-AC-004` 公开 CLI 与隔离

`benchmark-esci` 只输出聚合 JSON；默认 M0–M1d 的 CLI/API/SSE 仍不加载 artifact、
不联网且结果不变。

## 7. 非功能需求

| ID | 要求 |
|---|---|
| `GLO-M1E-NFR-001` | 默认运行、pytest、API、SSE、Agent 和 benchmark evaluation 零网络、零凭据；构建入口也只能读取本地源目录。 |
| `GLO-M1E-NFR-002` | 派生 artifact ≤20 MiB；不跟踪原始 source、全量数据、Parquet、缓存、token、Cookie 或下载日志。 |
| `GLO-M1E-NFR-003` | 错误、日志与 CLI 输出不含完整 query、商品正文、原始输入行、凭据或本机 source 路径；Git 只允许包含已批准的派生 artifact、attribution 与 manifest。 |
| `GLO-M1E-NFR-004` | 在同一版本化 artifact 上，跨进程指标与排序结果完全确定；M0–M1d 的格式、lint、类型、架构、离线和测试门禁继续通过。 |

## 8. Definition of Ready（进入 Plan 前）

- [x] 本规格状态改为 `Approved` 并记录批准日期；
- [x] 用户确认 M1e 只交付 ESCI 离线检索/重排 benchmark，不承诺 live marketplace、
      当前价格、库存或推荐；
- [x] 用户确认首发固定为 `small_version`、`us` locale、小型 versioned subset，且
      原始源数据不提交到仓库；
- [x] 用户确认 Apache-2.0 attribution、LICENSE 与 NOTICE 随派生 artifact 保留；
- [x] 用户确认不导入 Amazon Reviews 2023、Olist 或 WDC Products；
- [x] `4 P0 / 4 AC / 4 NFR` 均有唯一、可自动验证的证据路径。

## 9. 后续候选与变更治理

Amazon Reviews 2023 的历史价格/评价、Olist 的历史运费履约、WDC 的跨站同款匹配，
都不是被删除的功能；但每一个都存在独立的许可、规模或事实语义问题，必须另开 Spec。

将 ESCI 接入购物发布链、加入实时 Provider、增加语种、扩大样本、改变指标、允许标签
参与排序、添加远程模型/embedding，或改变默认搜索/Agent 合同，都必须先更新并重新
批准本规格。
