# Glodex M2d 验收记录

| 字段 | 值 |
|---|---|
| 里程碑 | M2d：AG-UI adapter、React 实时运行面与受控运营视图 |
| 验收日期 | 2026-07-31 |
| 状态 | Passed |
| 范围 | `GLO-SPEC-010 v0.1.0` 的 T1–T4 |

## 默认离线门禁

以下命令以成功状态结束：

```bash
uv run --locked python scripts/verify_m2d.py
```

它先完成既有 M0–M2c 的默认回归，再完成 M2d 的 socket-blocked Python 测试、格式和类型检查、
Spec↔Test traceability、锁定依赖的离线前端安装、React 测试、production build，以及源代码和
打包资源的隐私扫描。M0 的 20k 快照、100-request 本地性能工作负载也通过；本次测得总测试段
为 510.60 秒。M2d 专属 Python suite 为 19 passed，React suite 为 6 passed。

打包产物扫描确认没有 M2b private endpoint、M2c/GPU endpoint、provider credential marker 或录制
raw fixture；浏览器源代码也不使用 storage、外部 HTTP URL 或 console persistence。

## 显式本机浏览器验收

在既有 M2b durable prerequisite 已运行的前提下，先构建 `frontend/`，再由：

```bash
uv run --locked glodex m2d-serve --live
```

启动 `127.0.0.1:8767` 的 console。真实浏览器 smoke 确认 production React 资源由该 app 同源托管，
标准 AG-UI POST 创建一个 M2b durable run，并将安全 timeline、cursor 和终态展示到页面。网络只使用
同源 `/api/v1/m2d`；浏览器不直连 8766 或 18000。

一次真实 durable Agent 终态以稳定 safe code 显示为失败，UI 正确将其作为 durable business outcome，
而不是伪装为 M2d relay degradation；页面没有显示 query、tool arguments/output、reasoning、provider、
DB/Redis 或 GPU 信息。该 smoke 还暴露过一个未读取 SSE body 的 client 分支和一个错误的前端降级状态，
两者均已修复并在重建后复测。

## 保留的边界

M2d 只调用固定 `127.0.0.1:8766` 的 M2b public routes；没有 direct DB/Redis、OpenSearch、Provider、
M2c GPU、WebSocket、worker 或账户实现。`127.0.0.1:8765` 的 M1f static showcase 未被修改。这个里程碑
不宣称 provider-backed shopping run 必然成功；该结果仍受 M2b 已有 operator prerequisite 约束。
