# ADR-0003：硬门、Query-first 与排序降级

| 字段 | 值 |
|---|---|
| 状态 | Accepted |
| 日期 | 2026-07-23 |
| 对应规格 | [`GLO-SPEC-000`](../spec.md) |
| 对应计划 | [`GLO-PLAN-000`](../plan.md) |

## 背景

Glodex 的核心风险不是“相关商品排得不够好”，而是“返回了违反预算、品类、库存或商品本体要求的商品”。语义 scorer、向量检索或未来的用户偏好都不应拥有恢复已过滤候选的权限。

排序器还可能异常、缺失分数、返回未知 ID 或输出非法数值。M0 必须定义统一、确定性的降级语义。

## 决策

### 硬门顺序

商品级：

```text
品类 → 商品本体 → 明确排除项 → 必需商品证据
```

报价级：

```text
来源 → 库存 → 费用完整性 → 汇率 → 预算
```

只有至少存在一个合格 Offer 的商品才能进入排序。

`IN_STOCK` 和完整 Landed Cost 是 M0 的无条件报价资格规则；它们不是在用户未明说时合成的 Required。预算 gate 仅在请求包含 BudgetMax 时启用。

### Query-first 边界

1. scorer 只接收硬门后的唯一商品集合。
2. scorer 可以返回分数，不能修改商品、增加 ID、删除 ID 或恢复候选。
3. Preferred 只能影响顺序，不能影响合格集合。
4. M0 不读取长期记忆或用户画像。
5. 最终 invariant guard 在输出前重新检查全部硬约束。

### 正常排序

M0 使用 `lexical-v1`，稳定键为：

```text
(
  -query_score,
  -verified_preference_coverage,
  selected_landed_cost_exact,
  product_id,
)
```

### 整批降级

出现以下任一情况时，整个 scorer 批次作废：

- scorer 异常或超时；
- 输入 ID 缺失；
- 重复 ID；
- 出现输入集合外 ID；
- 非整数或非有限分数；
- 候选内容被修改。

降级时使用：

```text
(snapshot_ordinal, product_id)
```

响应记录 `ranker_degraded`，但继续返回硬门后可信结果。

### 最终失败

最终 guard 发现以下任一问题时：

- 违反 Required；
- 重复 product ID；
- selected offer 不属于 eligible offers；
- 金额或证据不可追溯；
- 结果数量超过 top_k；
- 终态或 schema 不一致；

则清空全部结果并返回 `FAILED`，不能输出部分可信结果。

## 结果

正面影响：

- 排序质量问题不能演变成硬约束违规；
- M1/M2 替换 OpenSearch、embedding 或 reranker 时仍受同一合同保护；
- scorer 故障可以稳定降级，不必把可信搜索变成系统失败；
- 结果集成员变化与顺序变化可以独立评测。

代价：

- scorer 的部分有效分数不会被保留；
- 最终 guard 会重复一部分硬门计算；
- snapshot ordinal 必须稳定且进入快照合同。

## 被否决的方案

### Query 分数与 User 分数直接加权

否决原因：长期偏好可能挤出当前查询候选，且违反 M0 Query-first 范围。

### scorer 返回任意 Top K

否决原因：无法证明它没有恢复不合格候选。

### 只忽略非法单项分数

否决原因：会产生难以解释的部分排序，并让不同故障位置影响结果。

### 最终 guard 只记录告警

否决原因：硬约束或证据违规时继续输出会产生半可信推荐。

## 后续复审条件

- M2 Spec 批准 Query Hybrid、cross-encoder 或 User-only 补充；
- 评测证明整批降级显著影响可用性；
- 新排序器需要明确的多阶段候选合同。

复审不得允许排序器越过硬门，也不得取消最终 invariant guard。
