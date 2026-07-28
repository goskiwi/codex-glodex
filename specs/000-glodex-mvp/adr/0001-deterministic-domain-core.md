# ADR-0001：M0 使用确定性领域核心

| 字段 | 值 |
|---|---|
| 状态 | Accepted |
| 日期 | 2026-07-23 |
| 对应规格 | [`GLO-SPEC-000`](../spec.md) |
| 对应计划 | [`GLO-PLAN-000`](../plan.md) |

## 背景

架构材料同时包含自主 ReAct AgentLoop、受控 LangGraph StateGraph 和传统确定性业务管线。M0 必须在无网络、无真实 LLM、无外部数据库的环境中证明硬约束、金额、证据和终态语义。

如果一开始以 Agent 框架承载这些业务规则：

- 测试会把确定性业务不变量与模型行为耦合；
- 零命中、降级和硬约束失败更难稳定复现；
- M0 会提前承担 checkpoint、工具路由和上下文管理复杂度；
- 后续难以判断改进来自领域逻辑还是模型随机性。

## 决策

M0 采用以下结构：

1. 领域规则实现为同步、无 I/O 的纯函数。
2. `SearchService` 负责固定阶段编排，应用入口为 async。
3. 外部变化点只通过 `typing.Protocol` 端口进入：
   - IntentInterpreter
   - CatalogGateway
   - QueryRanker
   - RunIdProvider
   - Clock
4. 阶段、漏斗和终态保存在应用内不可变 RunJournal，与响应原子组装；M0 不设置外部事件写入端口。
5. M0 适配器全部是本地确定性实现。
6. 不引入 LangGraph、LangChain、FastAPI、OpenSearch、数据库或模型 SDK。
7. 不为长期记忆、Category Insight、Provider fan-out 等未进入 M0 的能力创建空接口。

## 结果

正面影响：

- 硬约束、证据和金额规则可以离线、快速、完整测试；
- M0 形成可保留的领域内核，而不是一次性 demo；
- M1/M2 通过替换端口实现接入真实模型和基础设施；
- FastAPI 和 LangGraph 成为外围编排选择，不成为业务真相来源。

代价：

- M0 不展示完整 Agent 自主性；
- 应用层需要显式维护阶段和终态；
- M1 接入异步 Provider 时需要实现新的 CatalogGateway 适配器。

## 被否决的方案

### M0 直接使用 LangGraph

否决原因：它没有解决 M0 的任何必要外部依赖问题，却增加状态图、checkpoint 和框架测试面。

### 单文件脚本

否决原因：开发更快，但金额、证据、数据源和排序边界会耦合，M1 很可能需要重写。

### 预先复制完整目标目录

否决原因：会产生大量无实现空壳和错误抽象，掩盖真实完成度。

## 后续复审条件

满足以下任一条件时复审：

- M1 Spec 批准真实 LLM 或 Provider fan-out；
- 需要跨进程恢复或人工中断；
- 固定应用阶段无法表达已批准的有界恢复流程。

复审只决定应用编排框架，不允许改变本 ADR 已保护的领域不变量。
