# Glodex M1d 全工具 AgentLoop 验证记录

本记录仅固化已完成的 M1d 最终离线门禁与一次人工 opt-in 组合 live smoke 的脱敏
证据。它不保存输入、prompt、Provider 响应、凭据、临时输出目录或终端原文，也不构成
Provider 可用性、目录覆盖、价格时效或推荐质量承诺。

关联文档：

- [M1d 全工具 AgentLoop 规格](./spec.md)
- [M1d 技术计划](./plan.md)
- [M1d 实施任务](./tasks.md)
- [项目运行说明](../../README.md)

## 验收身份

| 项目 | 结果 |
|---|---|
| 项目版本 | `0.1.0`（来自 `pyproject.toml`） |
| 工作分支 | `codex/glodex-m1d-agent-demo` |
| 记录日期 | `2026-07-30` |
| 最终离线总门禁 | `PASSED`（exit `0`） |
| 组合 live smoke | `PASSED`，安全终态为可信 `NO_MATCH` |

## 最终离线门禁

在项目根目录执行：

```bash
uv run --locked python scripts/verify_m1d.py
```

命令以 exit `0` 结束。它先完整执行 M1c（以及其 M0–M1b 前置门禁），再固定执行
M1d architecture + NFR、unit + contract、六项 black-box acceptance 和精确
traceability coverage 五步。M0 的批准性能工作负载也在该完整门禁中完成。

最终文档更新后，按任务要求又只复核了文档相关的 offline security、M1d runner
contract 与 `git diff --check`；未重新执行 live 或完整 runner。

## 组合 live smoke

在方向性 offline/contract 清单通过后，Operator 通过独立 CLI 显式启动一次组合
live smoke。最终这一次运行没有自动 retry；动态输出没有写入仓库。

| Provider | 已验证配置 | 请求计数 |
|---|---|---|
| DeepSeek Open Platform | `deepseek-v4-flash` | 动作请求 `8` |
| Tavily | 允许的有界 Web evidence 搜索 | `1` |
| DashScope | Category embedding | `1` batch / `1` query text |
| eBay Buy API | 固定 M1b 批准的 US phone marketplace profile | OAuth `1` + Browse `1` |

实际成功的购物工具为 `planner`、`web_search`、`category_insight`、`item_search`、
`item_picker`、`price_compare`、`shipping_calc` 与 `shopping_summary`；每个执行一次且
结果均为 `SUCCESS`。`chat_fallback` 不属于本次购物链路。最终 Canonical gate 未发布
推荐时，系统如实以 `NO_MATCH` 结束；这符合 fail-closed 语义，不以模型或 Provider
文本补写商品事实。

## 离线、Git 与数据保护

- 完整离线门禁不读取 `.env`、不执行 live smoke；普通 CLI/API/SSE 仍不触发外部
  Provider 调用。
- live 运行使用 checkout 外、权限为 `0700` 的输出目录；运行结束后已删除，未保存
  live snapshot、receipt、HAR、cassette、raw payload 或动态终端输出。
- `.env` 继续被忽略并保持 `0600`；最终 security/Git inventory 没有发现 secret、token
  或凭据进入 tracked/staged 文件。
- `项目架构/` 下 26 张 PNG 继续由 `.gitignore` 排除，未被 tracked 或 staged。
- `check_traceability.py --profile m1d` 的 references 与 coverage 均精确闭合为
  `6 P0 / 6 AC / 6 NFR`。

## 已知限制

- M1d 是固定工具、固定 Provider 的有界 Demo，不是动态工具系统、通用模型网关或
  完整 AG-UI 实现。
- 只有 eBay 具有 request-time marketplace adapter；其他三平台继续使用版本化 Demo
  Snapshot。Tavily Web evidence 不作为 marketplace 商品事实。
- eBay live 使用固定、M1b 已验证的 `smartphone` marketplace query，而不是完整的
  Operator 购物请求。
- 本次 smoke 仅证明记录时刻的一次有界链路成功，不能推导远端长期可用、实时库存、
  landed cost 完整性、性能 SLA 或推荐质量。
