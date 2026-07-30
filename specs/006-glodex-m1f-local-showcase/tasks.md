# Glodex M1f 本地离线 Showcase 实施任务

| 字段 | 值 |
|---|---|
| Task Set ID | `GLO-TASKS-006` |
| 版本 | `0.1.0` |
| 状态 | Approved |
| 实施状态 | Completed; static Showcase and M1e-first verification complete |
| 对应规格 | [`GLO-SPEC-006 v0.1.0`](./spec.md)（Approved） |
| 对应计划 | [`GLO-PLAN-006 v0.1.0`](./plan.md)（Approved） |
| 父基线 | `GLO-SPEC-004`、`GLO-SPEC-005` |
| 创建日期 | 2026-07-30 |
| 最后更新 | 2026-07-30 |
| 批准日期 | 2026-07-30 |

## 1. 执行约定

1. 任务精确为 `T-M1F-A01 → T-M1F-B01 → T-M1F-C01` 三个串行交付块；不按按钮、样式、
   工具名称、单项指标或单个测试再创建 Task ID。
2. 每块整体执行 RED→GREEN：先写能证明当前能力缺失的定向测试，再完整实现本块最小闭环；
   不为留痕拆成大量微型 commit、抽象层或前端框架。
3. M1f 新增 runtime dependency 为 **0**。禁止 Node、React、AG-UI、WebSocket、浏览器
   测试包、CDN、数据库、Provider、模型、配置读取或凭据读取。
4. `showcase/` 是唯一新增前端目录；`src/glodex/`、已有 CLI/API/SSE/Agent/benchmark 的
   代码和公开合同均不得改变。唯一允许读取 M1d DTO/M1e evaluator 的新增文件是显式离线
   validator `scripts/validate_m1f_showcase.py`。
5. `m1d-replay.v1.json` 必须是 schema-validated 的受控静态 replay；页面必须称其为“本地
   录制回放，非实时 Agent 运行”。不能用任何 UI 文案暗示当前 Agent、商品、价格、库存或
   平台请求真实发生。
6. `m1e-summary.v1.json` 只能是当前 committed artifact 的安全 CLI 白名单投影。不得包含
   ESCI query、商品文本、逐条 label、原始文件、本机路径或可导航外部链接。
7. 每个 pytest 子进程显式加载 `-p scripts.verify_m0`，禁止 `-k`、单节点选择、`--lf`、
   `--ff`、skip/xfail、自动更新或任何 live 开关。阶段 A/B 只跑方向性测试；完整 M1e-first
   runner 只在 C 跑一次。
8. 不提交 token、Cookie、`.env`、下载缓存、真实 Agent 输出、截图、ESCI 原始记录或
   `项目架构/` 下的 26 张 PNG；也不修改用户已有的无关工作区改动。

统一 pytest 形式：

```bash
uv run --locked python -m pytest -q -p scripts.verify_m0 <固定测试路径>
```

## 2. 依赖主链与不做的事

```mermaid
flowchart LR
    A["T-M1F-A01\n静态证据与 validator"]
      --> B["T-M1F-B01\n离线 UI 与 loopback 服务"]
    B --> C["T-M1F-C01\n追溯、最终门禁与说明"]
```

三个块共同明确不做：实时 Agent 面板、现有 Agent API/SSE 代理、AG-UI、React/Node、市场
adapter、模型/Provider、ESCI 原始数据浏览、数据库、部署、认证或公网服务。

## 3. Phase A：静态证据与 validator

### `T-M1F-A01` `[RED→GREEN] [GATE]` 安全 replay、M1e summary 与显式离线校验

- 状态：Completed（2026-07-30；静态 replay、M1e 白名单摘要与离线 validator 已通过定向验证）
- 依赖：无
- Primary：
  - `GLO-M1F-P0-002`、`GLO-M1F-P0-003`
  - `M1F-AC-002`、`M1F-AC-003`
  - `GLO-M1F-NFR-002`、`GLO-M1F-NFR-004`
- Supporting：`GLO-M1F-P0-004`、`GLO-M1F-NFR-001`
- 主要路径：
  - `showcase/assets/{m1d-replay.v1.json,m1e-summary.v1.json}`
  - `scripts/validate_m1f_showcase.py`
  - `tests/m1f/{unit/test_showcase_assets.py,contract/test_showcase_validator.py}`

#### RED

- replay 接受未知字段、错误 schema/version、非连续 sequence、非法 DTO、无 terminal、
  root/child/depth/round/fork 生命周期错误，或暴露 query、prompt、工具参数/结果、
  Observation、商品、价格、URL、路径、时间敏感事实或凭据；
- 能力清单并非精确 9 个业务工具加单列 `dispatch_tool`，或者 replay 把未执行工具伪装成
  已执行；
- summary 不等于 `benchmark_summary(data/benchmarks/esci-small-us-v1)` 的严格白名单投影，
  hash/revision/count/label/指标或分母任一漂移仍通过；
- summary 含未知/原始字段、资产超 256 KiB、编码/键序不确定，或者 validator 读取 config、
  credential、网络、Agent bootstrap 或 Provider。

#### GREEN

- 新增小型 canonical JSON replay；用现有 `AgentPublicEvent` camelCase DTO 及附加 lifecycle
  规则验证，所有展示文本留在客户端固定映射中；
- 新增严格 JSON benchmark summary，保留 status、benchmark/scorer/manifest/source 标识、
  counts、四种 label 分布和三项 metric 的 value/denominator/excluded，初始值来自当前
  `benchmark-esci` 安全输出；
- 新增显式 validator：只能在离线验证时 import `glodex.api.agent_events` 与
  `glodex.esci_benchmark`，比对 canonical 白名单投影、资产目录和总量，成功/失败均不回显
  原始数据或本机路径；
- 用受控临时 JSON mutation 覆盖 fail-closed 矩阵，不在测试中接触实际 Agent 或 Provider。

#### 完成条件

- 两份提交资产小、确定、可由 validator 完整验证；M1d replay 只显示公开安全事件形状，
  M1e 卡只显示已审计聚合证据；
- validator 没有网络/环境/config/Agent execution 副作用，且本块没有 HTML、CSS、JS 或
  HTTP server。

#### 方向性验证

```bash
uv run --locked python scripts/validate_m1f_showcase.py

uv run --locked python -m pytest -q -p scripts.verify_m0 \
  tests/m1f/unit/test_showcase_assets.py \
  tests/m1f/contract/test_showcase_validator.py

uv run --locked ruff check scripts/validate_m1f_showcase.py tests/m1f
uv run --locked mypy scripts/validate_m1f_showcase.py
```

## 4. Phase B：极薄本地 UI 与 loopback 服务

### `T-M1F-B01` `[RED→GREEN] [GATE]` 静态展示、回放控制与固定本地服务

- 状态：Completed（2026-07-30；静态页面、回放状态机和 127.0.0.1-only 服务已通过定向测试与实际 loopback 请求）
- 依赖：`T-M1F-A01`
- Primary：
  - `GLO-M1F-P0-001`、`GLO-M1F-P0-002`
  - `M1F-AC-001`、`M1F-AC-002`
  - `GLO-M1F-NFR-001`、`GLO-M1F-NFR-003`
- Supporting：`GLO-M1F-P0-004`、`GLO-M1F-NFR-002`、`GLO-M1F-NFR-004`
- 主要路径：
  - `showcase/{index.html,styles.css,app.js}`
  - `scripts/serve_m1f_showcase.py`
  - `tests/m1f/contract/test_showcase_static_contract.py`
  - `tests/m1f/acceptance/test_showcase_local_workflow.py`

#### RED

- 页面没有显著“本地录制回放，非实时 Agent 运行”声明，未包含回放、工具能力、M1e benchmark
  三个独立区域，或以实时/市场事实措辞误导；
- 浏览器需要外部 URL、CDN、字体、图片、iframe、Agent API/SSE、benchmark runtime、任意
  host `fetch`、环境凭据或动态代码；
- 播放/暂停/继续/重放不能从静态 JSON 得到连续、不重复的 sequence，键盘无法激活控件，或
  页面未区分“已执行”与“本次未执行”的 9+1 工具能力；
- 服务可绑定非 `127.0.0.1`、服务项目根/任意目录、写展示资产、自动开浏览器，或需要真实
  socket/网络才能在 pytest 中验证其配置。

#### GREEN

- 新增无外部资源的语义化 HTML/CSS/JS，使用相对路径读取 A 的两份 JSON；JS 只实现一个
  bounded `idle/running/paused/completed` 回放状态和固定中文 event/tool 映射；
- 页面展示 actual replay event、能力目录（精确九个业务工具加 `dispatch_tool`）以及 M1e
  的三项指标、样本/label/provenance 文本；未执行的能力必须有可读状态；
- 新增标准库静态 server，目录固定为 `showcase/`、host 写死为 `127.0.0.1`、默认端口
  `8765`，只允许安全端口覆盖；不自动启动浏览器；
- server CLI 与静态资源检查使用可注入 server factory/纯 source contract，在 pytest 禁网
  下证明 loopback 配置；不把浏览器自动化引入依赖。真实 loopback 请求仅留给 C 的手工
  smoke。

#### 完成条件

- 用户执行 `uv run --locked python scripts/serve_m1f_showcase.py` 可得到一个本地 URL；
  静态页面无需任何 credential 或后台服务；
- 页面能如实回放安全 trace，完整显示 9+1 能力和 M1e 聚合结果；没有网络、父 runtime
  import 或外部资源路径。

#### 方向性验证

```bash
uv run --locked python -m pytest -q -p scripts.verify_m0 \
  tests/m1f/contract/test_showcase_static_contract.py \
  tests/m1f/acceptance/test_showcase_local_workflow.py

uv run --locked ruff check \
  scripts/serve_m1f_showcase.py \
  tests/m1f
uv run --locked mypy scripts/serve_m1f_showcase.py
```

## 5. Phase C：追溯、最终门禁与交付说明

### `T-M1F-C01` `[RED→GREEN] [GATE]` 隔离证据、traceability、M1e-first DoD 与 README

- 状态：Completed（2026-07-30；M1f traceability、隔离/NFR/acceptance、README 与最终 M1e-first gate 已完成）
- 依赖：`T-M1F-B01`
- Primary：
  - `GLO-M1F-P0-004`
  - `GLO-M1F-NFR-001`、`GLO-M1F-NFR-002`、`GLO-M1F-NFR-003`、`GLO-M1F-NFR-004`
  - 全部 `M1F-AC-001` 至 `M1F-AC-004`
- Supporting：全部 `GLO-M1F-P0-*`
- 主要路径：
  - `scripts/{check_traceability.py,verify_m1f.py}`
  - `tests/m1f/{architecture/test_m1f_boundaries.py,nfr/test_m1f_offline_security.py,unit/test_verify_m1f_runner.py,acceptance/test_showcase_public_workflow.py}`
  - `README.md`
  - 既有 M1e verification 与架构/安全 suites

#### RED

- `m1f` profile 无法精确解析/收集 `4 P0 / 4 NFR / 4 AC`，误收集父 milestone tests，或
  M1f marker 不在唯一的 function-level acceptance 所有者上；
- `showcase/` 或服务 import `glodex`/config/env、使用 socket client/远程 URL，validator
  import 超出 M1d DTO + M1e evaluator 的最小集合，或者父 runtime 反向 import M1f；
- Git inventory 出现 secret/raw ESCI/缓存/截图/PNG，或静态服务/README 对回放和 benchmark
  作出 live 声明；
- `verify_m1f.py` 未恰好先跑一次 `verify_m1e.py`、命令不是固定 root/净化环境/参数数组/
  `shell=False`，或可以用 live/ignore/deselect/筛选参数绕过门禁；
- public acceptance 只测私有 helper，未同时从 validator、static entry 与 server config
  观察到 M1f 合同。

#### GREEN

- 仅扩展 `check_traceability.py` 的 M1f path、ID pattern、approved inventory 和
  `tests/m1f` profile；所有 12 个 requirement ID 都有精确 marker 证据；
- 增加 AST/import/static-resource/inventory/security tests，证明只允许展示目录、标准库
  server 与显式 validator 边界；
- 新增 `verify_m1f.py`，按固定顺序只跑一次完整 M1e parent gate、M1f boundary、unit/
  contract、acceptance、coverage，使用现有 `sanitized_environment()`；
- 在 README 增加最短的 M1f 启动和 validator 命令，并明确这是零凭据的本地录制回放与
  历史离线 benchmark；
- 最后用本机 loopback smoke（浏览器或 `curl`）检查 127.0.0.1 页面、播放控件和 1024px
  布局；不保存截图、不引入 browser dependency。

#### 完成条件

- `scripts/verify_m1f.py` 成为唯一 M1f DoD runner，且不削弱 M1e parent gate；
- 所有 4 P0 / 4 AC / 4 NFR 可由 `m1f` coverage 追溯；README 和页面没有范围夸大；
- 暂存内容仅含批准的静态资产、标准库脚本、tests 和文档，不含任何禁止资料。

#### 最终验证（只在此块执行一次）

```bash
uv run --locked python scripts/verify_m1f.py

uv run --locked python scripts/serve_m1f_showcase.py
# 另一个本机终端：curl --fail http://127.0.0.1:8765/

git diff --check
git status --short
```

## 6. 需求到任务证据映射

| Requirement | 主要任务 | 最终证据 |
|---|---|---|
| `GLO-M1F-P0-001` / `M1F-AC-001` | B | static contract、loopback factory、public workflow、手工 loopback smoke |
| `GLO-M1F-P0-002` / `M1F-AC-002` | A + B | replay DTO/lifecycle、工具 9+1、replay state 和键盘控件证据 |
| `GLO-M1F-P0-003` / `M1F-AC-003` | A | validator 对 committed artifact 的白名单 projection 和安全输出 |
| `GLO-M1F-P0-004` / `M1F-AC-004` | C | import/inventory/parent regression/`verify_m1f.py` 固定 gate |
| `GLO-M1F-NFR-001..004` | A + B + C | offline/security/static-source/asset-size/determinism/traceability tests |

## 7. Tasks Definition of Ready（进入 Implementation 前）

- [x] 本 Task Set 状态改为 `Approved` 并记录批准日期；
- [x] 用户确认按精确三个块 A（证据）→ B（UI）→ C（门禁）执行；
- [x] 用户确认 M1d 部分是安全静态 replay，M1e 部分是安全聚合 benchmark，而非实时系统；
- [x] 用户确认不新增依赖/框架/Provider/API/SSE/部署，也不改 `src/glodex/`；
- [x] 用户确认完整 M1e-first runner 仅在 C 阶段执行一次，之前只运行方向性验证；
- [x] 用户确认不上传凭据、raw ESCI、截图或架构 PNG。
