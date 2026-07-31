# Glodex M1f 本地离线 Showcase 展示台规格

| 字段 | 值 |
|---|---|
| Spec ID | `GLO-SPEC-006` |
| 版本 | `0.1.0` |
| 状态 | Approved |
| 里程碑 | M1f：M1d/M1e 的本地离线展示台 |
| 父规格 | [`GLO-SPEC-004`](../004-glodex-m1d-agent-demo/spec.md)、[`GLO-SPEC-005`](../005-glodex-m1e-esci-retrieval-benchmark/spec.md) |
| 创建日期 | 2026-07-30 |
| 最后更新 | 2026-07-30 |
| 批准日期 | 2026-07-30 |

## 1. 目的与结论

M1f 为学生答辩/演示提供一个可在本机直接打开的可视化展示台：它清楚展示 M1d 已有的
Agent 事件投影、九个业务工具与 `dispatch_tool`，以及 M1e 已有的 ESCI 离线 benchmark
聚合结果。

它不是新的 Agent 前端，更不是完整 AG-UI。默认页面只播放经审计的本地**录制回放**，
不调用模型、Provider、Agent API 或市场数据；页面必须明确标注“本地录制回放，非实时
Agent 运行”。因此演示不会要求 DeepSeek、DashScope、Tavily、eBay 或任何其他凭据。

```text
M1d 已公开、安全的 Agent 事件形状 ─┐
                                   ├→ 版本化、静态、脱敏展示数据 → 浏览器 Showcase
M1e 已有 CLI 聚合 benchmark 结果 ───┘                         ↑
                                               loopback-only 本地静态服务
```

M1f 不改变 M0–M1e 的搜索、Agent、API、SSE、benchmark、Provider 或默认命令行为。

## 2. 范围

### 2.1 In Scope

- 不依赖 Node、React 或第三方 CDN 的静态 `HTML + CSS + JavaScript` 单页展示；
- 一个只绑定 `127.0.0.1` 的显式本地静态服务命令，供浏览器查看已提交的展示资产；
- M1d 的固定、有限、安全事件回放：时间线可开始、暂停、继续和重新播放；
- 清晰区分“本次录制实际执行的工具”与“系统可用能力”；能力区固定展示恰好九个业务工具
  `planner`、`chat_fallback`、`web_search`、`category_insight`、`item_search`、
  `item_picker`、`price_compare`、`shipping_calc`、`shopping_summary`，并将
  `dispatch_tool` 单独标为元工具；
- M1e 的静态、已审计聚合摘要：benchmark 版本、来源 revision/manifest 标识、样本计数、
  label 分布、`Exact@10`、`MRR@10`、`nDCG@10` 与 scorer 版本；
- 展示资产、回放顺序和 benchmark 摘要的确定性校验，以及不泄露数据和不改动 M0–M1e 的
  自动化证据；
- 中文可读的离线演示文案、键盘可操作的回放控件和不依赖外网的基础视觉样式。

### 2.2 Out of Scope

- AG-UI 协议/SDK、WebSocket、React/Node 构建链、前端框架、远程字体、分析脚本或 CDN；
- 调用或代理现有 `/api/v1/agent-runs`、Agent SSE、`AgentExecutor`、`agent_bootstrap`，
  也不增加 CORS、认证、会话、数据库、持久事件或实时运行控制；
- 新模型、Provider、API key、环境变量读取、市场 API、网页抓取、实时商品/价格/库存/运费
  或新的 live marketplace adapter；
- 将 ESCI 原始 query、商品文本、逐行 label、原始文件、本机路径或数据集下载逻辑暴露给
  浏览器，或把 ESCI 接回 Search/Agent/推荐发布链；
- 改动九个业务工具、`dispatch_tool`、M1d 事件合同、M1e evaluator、M0–M1e CLI/API/SSE
  的默认行为；
- 自动部署、公网托管、产品级账号体验或生产 SLA。

## 3. 设计约束与运行边界

1. 展示台的运行依赖只能是版本化静态文件和 Python 标准库静态服务；服务进程不得导入
   `agent_bootstrap`、`esci_benchmark`、配置、Provider 或环境凭据。
2. 回放数据只允许使用 M1d 已公开 SSE 投影可表达的安全字段：事件版本、固定 run 标识、
   序号、阶段、允许的工具名、状态、fork 标识和受限的本地说明。不得含用户 query、prompt、
   模型文本/推理、工具参数、Observation、工具结果、商品事实、URL、价格、凭据或时间敏感
   市场信息。
3. `dispatch_tool` 是第十个 registry entry，但不是第十个业务工具。页面必须同时保证：
   能力目录中为“9 个业务工具 + 1 个元工具”，录制时间线只陈述实际发生的事件，不能暗示
   全部十个工具都在一次回放中执行。
4. M1e 摘要由显式、离线的构建/验证步骤从既有 `benchmark-esci` 结果和 manifest provenance
   得到；浏览器运行时不得读取 artifact、执行 evaluator 或加载原始 JSONL。
5. 所有页面文本、样式、脚本和展示 JSON 都必须随仓库版本化，且不请求外部网络。浏览器
   可通过本地静态相对路径读取展示 JSON，但不得请求任意远程 URL。
6. 展示层只能向下依赖静态资产：`M1d/M1e 既有实现 → 显式离线生成/校验 → 展示资产 → 浏览器`；
   不允许产生反向 runtime import 或改变父规格的依赖图。
7. 页面和终端帮助必须如实说明该展示为历史、离线、录制的工程证据，不是当前真实购物
   建议、实时 Agent 会话或 ESCI 的实时 Amazon 检索结果。

## 4. 需求

| ID | 要求 |
|---|---|
| `GLO-M1F-P0-001` | **离线本地展示台**：仓库必须提供不依赖 Node/第三方包的静态页面、样式、脚本和显式 loopback-only 服务入口。页面启动不读取凭据、不创建网络连接、不访问既有 Agent/API/benchmark runtime，并在页面显著声明“本地录制回放，非实时 Agent 运行”。 |
| `GLO-M1F-P0-002` | **诚实的 M1d 可视化**：回放必须采用已验证的公开 `glodex.agent.event.v1` 事件形状和单调序号，支持开始、暂停、继续和从首事件重新播放。时间线只渲染回放数据允许的安全字段；能力目录固定、完整地列出九个业务工具及单独的 `dispatch_tool`，并显式区分“已执行”与“可用但本次未执行”。 |
| `GLO-M1F-P0-003` | **可复核的 M1e 证据卡**：页面必须显示一个由既有版本化 `esci-small-us-v1` artifact 和 `benchmark-esci` 聚合输出交叉校验的静态摘要。摘要仅含 benchmark/version/source revision/manifest 标识、聚合计数、label 分布、三个规定指标与 scorer version；页面不携带 query、商品文本、逐条 judgement 或原始数据。 |
| `GLO-M1F-P0-004` | **父规格隔离**：M1f 不修改 M0–M1e 的公开合同和默认行为。静态服务与浏览器都不能导入/调用 Agent、Provider、ESCI evaluator 或 artifact；现有离线、格式、类型、测试和架构门禁继续通过。 |

## 5. 验收场景

### `M1F-AC-001` 零凭据本地打开

在干净环境执行显式展示命令后，服务仅监听 `127.0.0.1`，浏览器能打开页面；页面不需要
API key，不向公网或现有本地 Agent API 发请求，并显示录制回放声明。

### `M1F-AC-002` 回放与工具边界

加载固定回放后，事件按序号稳定渲染；开始、暂停、继续和重新播放不会重排或重复事件。
工具能力区恰有九个业务工具及一个单列元工具，且能看出哪些在此录制中实际发生、哪些只是
系统能力，页面不包含 prompt、CoT、参数、Observation、商品或市场事实。

### `M1F-AC-003` benchmark 摘要一致性

展示 JSON 的 benchmark ID、provenance、样本/label 聚合和 `Exact@10`、`MRR@10`、
`nDCG@10` 与对同一提交 artifact 执行既有 CLI 得到的安全聚合输出完全一致；验证器拒绝
缺字段、未知字段、原始文本字段、格式错误或不匹配的 hash/指标。

### `M1F-AC-004` 隔离和回归

在静态服务进程和页面资源检查中可证明没有 Agent、Provider、benchmark runtime import 或
外部 URL；M0–M1e 的既有 tests、lint、类型、离线与架构证据不因 M1f 改变。

## 6. 非功能需求

| ID | 要求 |
|---|---|
| `GLO-M1F-NFR-001` | 展示台离线可用：服务只绑定 loopback，静态运行时零凭据、零 Provider、零远程网络、零数据库；首次打开不依赖预热或下载。 |
| `GLO-M1F-NFR-002` | 展示数据最小化且安全：提交的回放与 benchmark 摘要只保留第 3 节允许字段，合计不超过 256 KiB；不得提交 token、Cookie、日志、截图中可读的敏感信息、ESCI 原始内容或 26 张架构 PNG。 |
| `GLO-M1F-NFR-003` | 页面不加载远程资源；在宽度 1024px 及以上保持可读，回放控件可通过键盘操作并有可见状态文本。 |
| `GLO-M1F-NFR-004` | 同一提交下，静态资产、事件顺序、展示的指标和校验输出完全确定；新增展示层不得放宽 M0–M1e 的格式、lint、类型、架构、离线和测试门禁。 |

## 7. Definition of Ready（进入 Plan 前）

- [x] 本规格状态改为 `Approved` 并记录批准日期；
- [x] 用户确认 M1f 是本地**录制回放**展示，而不是隐藏凭据后执行真实 Agent；
- [x] 用户确认只展示九个业务工具与 `dispatch_tool` 的能力/事件投影，不扩展为完整 AG-UI；
- [x] 用户确认 M1e 只展示经审计的聚合 benchmark 证据，不向浏览器提供 ESCI 原始记录；
- [x] 用户确认不引入 React/Node、WebSocket、数据库、Provider、市场 API 或公网部署；
- [x] `4 P0 / 4 AC / 4 NFR` 均有唯一、可自动验证的证据路径。

## 8. 后续候选与变更治理

真实 Agent 会话面板、实时 SSE、跨会话存储、AG-UI、远程发布、对外数据源或任何可交互的
购物操作都不是被删除的功能，但它们会引入凭据、网络、运行时真相与安全边界，必须另开
Spec 并重新评估 M1d/M1e 的合同。

改变回放允许字段、将回放表述为 live、把 ESCI 记录交给浏览器、接入现有 Agent API，或
改动 M0–M1e 的默认命令/API/SSE/benchmark，也都必须先更新并重新批准本规格。
