# Glodex M1a 验证记录

本文记录 M1a 当前分阶段证据、最终门禁入口和已知边界。它不是规格替代品，也不把局部测试结果写成最终发布结论。

关联文档：

- [M1a API 与实时事件规格](./spec.md)
- [M1a 技术实施计划](./plan.md)
- [M1a 实施任务](./tasks.md)
- [项目运行说明](../../README.md)

## 源码身份

| 项目 | 值 |
|---|---|
| 项目版本 | `0.1.0`（来自 `pyproject.toml`） |
| 工作分支 | `codex/glodex-m1-api` |
| 记录日期 | `2026-07-28` |
| M0 基线提交 | `7a957f3d57e510392873c468f5341d96e7254bfc` |
| 验证对象 | 上述 HEAD 加当前未提交、未暂存的 M1a 工作树 |
| Python | `3.12.13`（uv 项目环境） |
| uv | `0.11.7` |
| 环境 | `macOS 15.3 / Darwin 24.3.0 / arm64` |
| 最终总门禁 | `PASSED`（exit `0`） |

M0 仍是业务、DTO、Golden 和确定性的回归基线；M1a 只在其上增加 FastAPI、进程内 Run runtime、SSE、重放和 transport 错误合同。

## Phase A–C 当前证据

| Phase | 已落地范围 | 主要自动化证据 |
|---|---|---|
| A：合同与 Walking Skeleton | 严格 HTTP/Event DTO、三个 OpenAPI 路径、API settings、版本化安全错误 | `tests/m1a/contract/test_http_api_contract.py`、`tests/m1a/contract/test_sse_contract.py` |
| B：应用缝与 Run Runtime | 显式 Run ID、同步 observer、EventProjector、RunRegistry、Coordinator、容量与生命周期 | `tests/m1a/unit/test_search_service_observer.py`、`test_event_projection.py`、`test_run_registry.py` |
| C：HTTP、SSE 与验收 | 202 后启动、状态查询、实际 body limit、SSE 在线事件/重放/游标/断线、`M1-AC-001`–`009` | `tests/m1a/unit/test_body_limit.py`、`test_sse_cursor.py`、`tests/m1a/contract/`、`tests/m1a/acceptance/` |

2026-07-28 实际执行了下面的 Phase A–C 定向命令。它显式排除尚属 Phase D 的 `test_verify_m1a_runner.py`：

```bash
uv run --locked python -m pytest -q -p scripts.verify_m0 \
  --ignore=tests/m1a/unit/test_verify_m1a_runner.py \
  tests/m1a/unit tests/m1a/contract tests/m1a/acceptance
```

实际结果：

```text
120 passed in 1.22s
```

这条结果只证明当时工作树上的 Phase A–C 定向集合。它没有执行完整 M0、Phase D architecture/NFR/security、`M1-AC-010`、M1a exact coverage 或最终 runner，因此不能替代最终门禁。

## 最终门禁执行

在项目根目录、Python 3.12 环境中执行：

```bash
uv sync --locked --dev
uv run --locked python scripts/verify_m1a.py
git diff --check
```

`scripts/verify_m1a.py` 的成功结果必须同时证明：

1. 完整 `scripts/verify_m0.py` 先通过；
2. M1a architecture、offline 和 security 检查通过；
3. M1a unit、contract 和 acceptance 全部通过；
4. `M1-AC-001`–`010` 均有有效黑盒证据；
5. M1a 的 `9 P0 / 10 AC / 10 NFR` exact traceability coverage 通过；
6. 没有 skip、xfail、真实外部调用或自动 Golden 改写。

### 最终总门禁结果

`PASSED`。2026-07-28 在上述工作树执行：

```bash
uv sync --locked --dev
uv run --locked python scripts/verify_m1a.py
git diff --check
```

三条命令均以 exit `0` 结束。`verify_m1a.py` 的原始摘要为：

```text
M0 locked dependency graph: passed
M0 format: 108 files already formatted
M0 lint: passed
M0 mypy: 40 source files passed
M0 architecture/offline/security: 16 passed
M0 unit/contract/generated: 784 passed
M0 AC-001–012: 29 passed
M0 Golden: 3 scenarios current
M0 twenty-process determinism: 1 passed
M0 exact traceability: valid=true, coverage_issues=[]
M0 performance: 5 passed in 509.64s
M0 verification passed (all final gates).
M1a architecture/offline/security: 14 passed
M1a unit/contract: 115 passed
M1-AC-001–010: 12 passed
M1a exact traceability: valid=true, coverage_issues=[]
M1a verification passed (complete M0 and M1a gates).
```

性能负载按批准的 20,000 商品、10 次预热和 100 次测量完整执行。本机信息性结果为
`p50=4632.435ms`、`p95=4689.582ms`、`max=4833.519ms`；未设置
`GLODEX_REFERENCE_CI=1`，因此这些本机绝对耗时不作为跨配置 SLA。

## 本地 API/SSE Smoke

自动门禁通过后，实际启动 README 中的单 Worker Uvicorn 命令，并依次执行
POST、SSE GET 和状态 GET。HTTP 状态分别为 `202`、`200`、`200`，摘要为：

```json
{"accepted_state":"ACCEPTED","event_count":15,"first_event":"RUN_STARTED","last_event":"RUN_FINISHED","result_count":3,"status_state":"COMPLETED"}
```

服务随后通过 `SIGINT` 正常关闭，application shutdown 完成。

## Git 与架构图保护

验证时 `git ls-files '*.png'` 和 `git diff --cached --name-only -- '*.png'` 均为
`0`。本地 `项目架构/` 下的 26 张 PNG 仍由 `.gitignore` 排除，没有被追踪或暂存。
本次验证没有创建提交，也没有推送远程。

## 已知限制

- 仅支持单进程、单 Worker；多 Worker 之间没有共享 Run 或事件状态。
- Run、事件和 Subscriber 只在内存中有界保留；进程重启后旧 Run 不可恢复。
- 没有认证、授权、持久化、用户取消 API 或跨进程消息系统。
- 默认没有 CORS middleware；没有跨源浏览器访问承诺。
- `ABORTED` 只表示 transport runner 未产生可信业务终态，不等同于 `FAILED`。
- SSE 重放只覆盖同一进程仍保留的合法事件前缀，不承诺 exactly-once。
- 没有生产吞吐、延迟、可用性或数据恢复 SLA。
- AG-UI、WebSocket、前端、真实 LLM 和真实电商 Provider 均未实现。
