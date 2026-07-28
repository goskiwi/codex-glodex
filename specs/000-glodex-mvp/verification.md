# Glodex M0 验证证据

本文件记录 Spec v0.1.2 对应的 M0 验收结果。验证日期为 2026-07-28，执行环境为本地 macOS；该环境未标记为 reference CI。

## 1. 验证身份

| 字段 | 值 |
|---|---|
| Python | `3.12.13` |
| uv | `0.11.7 (9d177269e 2026-04-15 aarch64-apple-darwin)` |
| OS / architecture | `Darwin 24.3.0 arm64` |
| Git | `main` unborn branch；当前没有 commit |
| Workspace tree SHA-256 | `81f33a9f61ae4692bfc3670fb5e76f8f437a163f360207eeffdf227cfe5aa452` |
| `uv.lock` SHA-256 | `541a24c788633d06e8899b0392d06095d47bd1dbaa45d5cdef142b3c2fd14bc2` |
| `m0-v1/manifest.json` SHA-256 | `aa1ca1e389cf1fcd86dead85b6937d7c7991e9ed381c5d190161f86bbcfce576` |

未擅自创建 Git commit。为使当前未提交工作区仍可准确识别，Workspace tree hash 对 `rg --files --hidden` 返回的非忽略文件逐文件计算 SHA-256，按路径排序后再次计算 SHA-256，并排除本验证文件以避免自引用：

```bash
/usr/bin/env LC_ALL=C LANG=C rg --files --hidden \
  -g '!.git/**' \
  -g '!.venv/**' \
  -g '!**/__pycache__/**' \
  -g '!.pytest_cache/**' \
  -g '!.mypy_cache/**' \
  -g '!.ruff_cache/**' \
  -g '!specs/000-glodex-mvp/verification.md' |
  /usr/bin/env LC_ALL=C LANG=C sort |
  while IFS= read -r file_name; do
    /usr/bin/env LC_ALL=C LANG=C shasum -a 256 "$file_name"
  done |
  /usr/bin/env LC_ALL=C LANG=C shasum -a 256
```

## 2. 完整 M0 门禁

执行命令：

```bash
uv run --locked python scripts/verify_m0.py
```

结果：退出码 `0`，11/11 门禁通过。

| 门禁 | 结果 |
|---|---|
| 锁文件 | 通过 |
| Ruff format | 81 files already formatted |
| Ruff lint | 通过 |
| mypy | 31 source files，无问题 |
| Architecture / offline / security | 16 passed |
| Unit / contract / generated | 783 passed |
| AC-001–AC-012 | 29 passed |
| Golden | 3 scenarios current |
| 20 进程确定性 | 1 passed |
| Spec↔Test coverage | valid |
| 20k/100 性能 | 5 passed |

门禁使用严格 pytest 插件拒绝 skip、xfail 和 xpass；没有自动更新 Golden，也没有通过 ignore、deselect 或 `-k` 绕开测试。

README、CLI help 和 SDD 完成状态在完整门禁后落盘。对应的增量门禁再次通过：

- `uv lock --check`；
- Ruff format / lint；
- mypy 31 source files；
- CLI acceptance、verify runner 和 security 目标测试：35 passed；
- Golden 3 场景仍为 current；
- 从非项目 cwd 执行真实 CLI smoke。

## 3. 性能

本次执行未设置 `GLODEX_REFERENCE_CI=1`，因此结果是当前机器的完整信息性基线，不声称满足 reference CI 的 2 秒阈值。

```json
{
  "algorithm_version": "phase-d-v1",
  "max_ms": 4933.401,
  "os": "Darwin 24.3.0",
  "p50_ms": 4610.368,
  "p95_ms": 4849.879,
  "python": "3.12.13",
  "requests": 100,
  "snapshot_hash": "b00cd7ea1021fd4b42ad38b42c730a1837705f187bfb364a31fb0d365d2d00ee",
  "snapshot_products": 20000,
  "warm_up_requests": 10
}
```

完整性能测试耗时 `511.83s`，5 个性能测试全部通过。若未来在固定 reference CI 上执行，应显式使用：

```bash
GLODEX_REFERENCE_CI=1 uv run --locked python scripts/verify_m0.py
```

只有该环境才把 p95 `≤ 2000ms` 作为硬门。

## 4. Golden

检查命令：

```bash
uv run --locked python scripts/update_goldens.py --check
```

结果：3 个场景全部与当前语义投影一致。

| 文件 | SHA-256 |
|---|---|
| `completed.json` | `09caf83d92adf6add7278c05ffc169ccc17c48aaab9d247f6da271738a33f632` |
| `no-match.json` | `c2fba1ce6d189443cede382d281b75efff1f3a933f70883641164efbc2c92539` |
| `ranker-degraded.json` | `0bc3dd2d53114c0a3abd3cece7dcc326c153507dc651762814a09b1c858f1c1c` |

## 5. Traceability

检查命令：

```bash
uv run --locked python scripts/check_traceability.py --mode coverage
```

| 类别 | 结果 |
|---|---|
| P0 | 12/12 |
| AC | 12/12，均有 acceptance 黑盒测试 |
| NFR | 11/11 |
| 收集测试 | 834 |
| Reference issues | 0 |
| Coverage issues | 0 |
| 总体 | valid |

## 6. 非项目目录 CLI smoke

工作目录：`/private/tmp`。所有命令均使用项目绝对路径、`uv --project`、`--locked` 和显式配置。

| 命令 | 退出码 | 稳定摘要 |
|---|---:|---|
| `glodex --help` | 0 | 包含 `demo`、`search`、`validate-snapshot`，demo 标识为 M0 |
| `validate-snapshot m0-v1` | 0 | `valid=true`，34 products，37 offers，7 quarantined |
| `demo` | 0 | `COMPLETED`，`m0-v1`，3 results |
| `search` | 0 | `COMPLETED`，`m0-v1`，3 results |

验证记录只保存稳定业务字段，不保存随机 `run_id`、UTC 时间或阶段耗时。

## 7. M0 DoD 闭合矩阵

| DoD | 证据 |
|---|---|
| 所有 P0 已实现并可追踪 | Traceability P0 12/12；unit/contract/generated 与 AC 门禁 |
| AC-001–AC-012 自动化并通过 | Traceability AC 12/12；29 个 acceptance 测试通过 |
| 默认无真实网络、LLM、数据库 | Architecture / offline / security 16 passed |
| 硬约束、证据、去重、确定性满足 NFR | AC、generated、Golden、20 进程确定性与完整性能报告 |
| 一条全量命令和一条 demo 命令 | `scripts/verify_m0.py` 与 `glodex demo` |
| 结果包含版本、run ID、漏斗、终态 | CLI smoke、SearchResponse 合同和 RunJournal 测试 |
| 无隐藏降级、条件放宽、无证据陈述 | AC-002/003/007/011、Golden 与 final guard |
| README 描述真实完成度 | 根目录 README 与非项目 cwd 命令实测 |

## 8. 已说明的边界

- 当前仓库没有 commit；本文件以可复算的 Workspace tree hash 标识已验证源码，未伪造 commit SHA。
- 当前机器不是 reference CI；已执行完整 20k/100 工作负载，但 2 秒硬阈值未在本机启用。
- FastAPI、真实 LLM、Provider fan-out、OpenSearch、AG-UI、长期记忆、React UI 和生产部署属于 M1/M2 或范围外，不是 M0 失败。

除这些已批准边界外，本次 M0 验收没有未说明失败、skip、xfail 或自动放宽。
