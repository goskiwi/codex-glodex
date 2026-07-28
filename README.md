# Glodex

Glodex M0 是一个本地、离线、确定性的跨市场购物检索垂直切片。它接收中文购物请求，从版本化快照中聚合同款商品和各市场报价，使用 `Decimal` 计算到手价，在排序前执行预算、品类、库存、商品本体和证据硬门，最后返回最多 3 个可核验结果。

M0 是可保留的领域与合同基线，不是生产 Agent 平台。当前不连接真实电商、LLM、网络、数据库、FastAPI、AG-UI，也不保存长期用户画像。

## 已实现

- 中文规则意图解析，并校验 Required/Preferred 的原文引用；
- manifest、SHA-256、记录数、路径、schema 和版本校验；
- Canonical Product 聚合与合法 Offer 守恒；
- 商品价、运费、税费、关税和版本化汇率组成的到手价；
- 商品级、报价级硬门，以及绝不自动放宽条件的 `NO_MATCH`；
- Query-first 确定性排序和全批次安全降级；
- Verified Claims、证据闭包、Top K 和最终不变量守卫；
- 不可变 `RunJournal`、阶段漏斗、诊断和唯一终态；
- Golden、跨进程确定性、Spec↔Test 追踪、禁网和安全门禁。

## 环境与安装

需要 Python `>=3.12,<3.13` 和 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync --locked --dev
uv run --locked glodex --help
```

运行时依赖只有 Pydantic 2；pytest、pytest-socket、Ruff 和 mypy 位于 dev 依赖组。

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

搜索、演示和快照校验在 stdout 输出一行 JSON。请求或配置被拒绝时，稳定的机器可读结果仍在 stdout，简短诊断写入 stderr。

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

M0 只支持 `zh-CN`；币种必须为三位大写字母；`top_k` 必须在 1–3。未知配置键、无效值或缺失的显式配置都会 fail closed。项目不读取 `.env` 或密钥。

## 快照

[`data/snapshots/m0-v1`](./data/snapshots/m0-v1) 包含 manifest、products、offers、evidence 和 exchange rates。加载器验证文件哈希、原始记录数、路径、版本和 schema：

- 单条脏记录进入 quarantine，其他合法记录可继续；
- manifest、哈希、核心文件或版本错误是 fatal，不能产生部分可信结果；
- 输出证据不仅匹配 ID，还校验 entity、field path、provider、offer/product 和 snapshot 归属。

## Golden、追踪与门禁

```bash
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

## M1 / M2

以下能力已明确延期，不应被描述为 M0 已完成：

- **M1**：FastAPI、真实 LLM Intent、Provider fan-out、AG-UI 投影、Category Insight；
- **M2**：OpenSearch/Hybrid/cross-encoder、Postgres/checkpoint、Redis、长期记忆、User-only/Reflect、React UI；
- **范围外**：生产部署、实时商品覆盖、真实推荐质量承诺和开放式 AgentLoop。

## 规格与决策记录

- [产品规格](./specs/000-glodex-mvp/spec.md)
- [技术计划](./specs/000-glodex-mvp/plan.md)
- [实施任务](./specs/000-glodex-mvp/tasks.md)
- [ADR-0001：确定性领域核心](./specs/000-glodex-mvp/adr/0001-deterministic-domain-core.md)
- [ADR-0002：金额、汇率与舍入](./specs/000-glodex-mvp/adr/0002-money-fx-and-rounding.md)
- [ADR-0003：硬门与排序降级](./specs/000-glodex-mvp/adr/0003-hard-gates-and-ranking-degradation.md)
