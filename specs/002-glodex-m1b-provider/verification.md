# Glodex M1b Provider Capture 验证记录

本文记录 M1b 当前工作树的完整离线门禁与一次独立、人工 opt-in 的真实三步
Capture smoke。它不是规格替代品，也不把单次 Provider 结果写成覆盖率、时效或
可用性承诺。

关联文档：

- [M1b Provider Capture 规格](./spec.md)
- [M1b 技术实施计划](./plan.md)
- [M1b 实施任务](./tasks.md)
- [项目运行说明](../../README.md)

## 源码身份

| 项目 | 值 |
|---|---|
| 项目版本 | `0.1.0`（来自 `pyproject.toml`） |
| 工作分支 | `codex/glodex-m1b-provider` |
| 记录日期 | `2026-07-29` |
| M1a 基线 HEAD | `30b9f4a768eb347d56c15823fa1fe75a06284665` |
| 验证对象 | 上述 HEAD 加当前未提交、未暂存的 M1b 工作树 |
| Python | `3.12.13`（uv 项目环境） |
| uv | `0.11.7` |
| 环境 | `macOS 15.3 / Darwin 24.3.0 / arm64` |
| 最终离线总门禁 | `PASSED`（exit `0`） |
| 独立真实三步 smoke | `PASSED`（三条命令均 exit `0`） |

M1a 仍是 API、SSE 与本地搜索回归基线。M1b 只增加 Operator 显式启动的单
Provider Capture，不把网络或凭据读取带入普通 CLI、API、SSE 或搜索路径。

## Phase A–C 证据

| Phase | 已落地范围 | 主要自动化证据 |
|---|---|---|
| A：边界与合同 | Capture DTO、严格 preflight、显式 env-file、固定 eBay profile、唯一 HTTP transport | `tests/m1b/unit/test_capture_config.py`、`tests/m1b/contract/test_ebay_http_contract.py` |
| B：映射 | provider-scoped identity、Evidence、`UNKNOWN` 库存、`UnknownCost`、逐项 quarantine | `tests/m1b/unit/test_ebay_mapping.py`、`tests/m1b/contract/test_ebay_mapping_contract.py` |
| C：发布与编排 | manifest v1 反向校验、单进程原子 rename、Service、CLI receipt、现有 validate/search 复用 | `tests/m1b/unit/test_snapshot_publisher.py`、`test_capture_service.py`、`tests/m1b/acceptance/test_m1b_ac_002_005.py` |

## 完整离线门禁

在项目根目录执行：

```bash
uv run --locked python scripts/verify_m1b.py
git diff --check
```

两条命令均以 exit `0` 结束。`verify_m1b.py` 固定项目 cwd、使用参数数组、清理
Provider/代理环境并首错停止；第一步完整执行 M1a，因此也完整包含 M0。实际摘要：

```text
M0 locked dependency graph: passed
M0 format: 135 files already formatted
M0 lint: passed
M0 mypy: 50 source files passed
M0 architecture/offline/security: 16 passed
M0 unit/contract/generated: 784 passed
M0 AC-001–012: 29 passed
M0 Golden: 3 scenarios current
M0 twenty-process determinism: 1 passed
M0 exact traceability: valid=true, coverage_issues=[]
M0 performance: 5 passed in 510.68s
M0 verification passed (all final gates).
M1a architecture/offline/security: 14 passed
M1a unit/contract: 115 passed
M1-AC-001–010: 12 passed
M1a exact traceability: valid=true, coverage_issues=[]
M1a verification passed (complete M0 and M1a gates).
M1b architecture/offline/security: 9 passed
M1b unit/contract: 106 passed
M1B-AC-001–006: 8 passed
M1b exact traceability: 6 P0 / 6 AC / 6 NFR,
  valid=true, coverage_issues=[]
M1b verification passed (complete M1a and M1b gates).
```

性能负载按批准的 20,000 商品、10 次预热和 100 次测量完整执行。本机信息性结果为
`p50=4629.730ms`、`p95=4750.053ms`、`max=5029.658ms`；未设置
`GLODEX_REFERENCE_CI=1`，因此这些本机绝对耗时不作为跨配置 SLA。

## 独立真实三步 smoke

离线总门禁通过后，使用本机已忽略且权限为 `0600` 的显式 `.env`，在 checkout
外新建权限为 `0700` 的临时输出目录。未把凭据、token、raw Provider payload、
receipt 或 snapshot 写入仓库。

三步直接使用 README 中的生产 CLI，没有 wrapper、fixture 或第二套 JSON：

1. `capture-provider --live` 返回 `PUBLISHED`，`request_count=2`，
   `received_record_count=10`、`published_product_count=10`、
   `published_offer_count=10`、`quarantine_count=0`、`issues=[]`。
2. `validate-snapshot` 返回 `valid=true`，重新加载后仍为 10 个 Product、
   10 个 Offer、0 个 quarantine，`issues=[]`。
3. `search` 正常完成并返回可信 `NO_MATCH`；10 个 Offer 均因 Provider 未披露
   库存而保持 `UNKNOWN`，没有被改写成可用库存，诊断无 issue。

Capture ID、临时目录和真实商品内容不记录在仓库。该结果只证明记录时刻的一次
有界单页链路成功，不证明 Provider 长期可用、目录覆盖、价格时效、库存真实性、
landed cost 完整性或推荐质量。

## 默认离线与 Git 保护

- 普通 CLI、API 和 SSE 的 fresh-process 验收证明不会导入 Capture，也不会触发
  外部调用；真实联网必须同时经过独立命令和显式 `--live`。
- M1b 测试默认禁用 socket，并移除 `EBAY_*` 与代理环境；完整离线门禁不读取
  `.env`，也不执行 live smoke。
- 验证时 `git ls-files '*.png'` 与暂存 PNG 计数均为 `0`；本地
  `项目架构/` 下 26 张 PNG 继续由 `.gitignore` 排除。
- `.env` 由 `.gitignore` 排除且权限为 `0600`。Git inventory 与独立终审均未发现
  secret、token、live snapshot/receipt、HAR、cassette 或 raw payload。
- 本次验证没有创建提交，也没有推送远程。

## 已知限制

- 仅支持一个固定 eBay Buy Browse profile、一个有界结果页和单 Operator。
- Publisher 的原子性前提是同一文件系统、单进程 writer；没有跨进程锁或恢复协议。
- Capture 不进入 request-time 搜索/API/SSE，不提供自动刷新或 Provider fallback。
- Provider 未披露库存时保持 `UNKNOWN`；未披露费用保持 `UnknownCost`。
- USD identity FX 只表示同币种恒等换算，不表示 landed cost 完整。
- 不保存 raw Provider payload、query、凭据或 token，因此不能用仓库数据重放真实
  HTTP 会话。
- 不提供 Provider 可用性、商品覆盖、推荐质量、价格时效、吞吐或延迟 SLA。
