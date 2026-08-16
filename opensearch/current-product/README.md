# 合成面试商品 Hybrid 索引

这里是 04-1 的 OpenSearch 应用层，部署在 `glodex-a100`，不替换服务器上的 Faiss 商品召回。

该索引和 Gateway 只用于面试演示，不是实时商城镜像。926,710 条公开语义商品文本用于 Hybrid
检索，已有索引中的 CNY 金额只作为确定性的合成基准。Gateway v6 明确返回
`data_mode=SYNTHETIC_INTERVIEW`，并按 `synthetic-multiplatform-cny-v2` 将同一命中的商品投影为
Amazon/USD、Shopee/SGD、AliExpress/USD 或 eBay/EUR 报价。固定汇率为 CNY 1.00、USD 7.20、
EUR 7.85、SGD 5.30；每条汇率都带合成证据，Agent 统一换算为 CNY。所有平台、价格、库存、运费、
配送和汇率事实均不表示真实市场状态。

- 只读取当前 926,710 条商品的只读 SQLite catalog，以及当前 1024 维 Item-vector manifest；
- 严格经过 `formal vector row → formal ID sidecar → canonical document_id → catalog crosswalk/documents`；没有外部输入缓存、历史 crosswalk 或旧数据回退；
- 当前语义清洗候选索引为 `glodex-current-product-hybrid-semantic-clean-v5-20260809`；它完成全量校验后才会原子切换 alias `glodex-current-product-hybrid`；
- OpenSearch 固定回环 `127.0.0.1:9200`，不暴露网络、不用 Docker/Conda、不使用 systemd；`build.sh` 完成或失败都会停止它。
- 不复制 OpenSearch runtime：脚本默认复用服务器已有的 `/data3/sybai/glodex/product-search/runtime`，也不会改写其中的 `config/`。运行时参数强制为当前数据目录、`127.0.0.1:9200` 和单节点临时实例。

正式资产由 `current_product_binding.json` 精确固定：

- `/data3/sybai/glodex/product-search/current-product-catalog.sqlite3`；
- `/data3/sybai/glodex/current-model/item-vectors/manifest.json`，其 checksum、schema、矩阵和 ID sidecar 都会校验；
- catalog 仅以只读方式打开，builder 不会创建、补写或重建 SQLite。

服务器操作目录是 `/data3/sybai/glodex/opensearch/current-product/`。`build.sh` 必须显式给出动作：

```bash
./build.sh validate-assets  # 不启动 OpenSearch；校验完整 current catalog + formal vectors lineage
./build.sh verify           # 临时启动并校验已发布的 v3 mapping，完成后自动停止 OpenSearch
./build.sh build            # 显式全量构建 v3；若 v3 物理索引已存在会拒绝覆盖
```

`build` 没有默认值、没有 smoke 索引，也不会覆盖 v5 物理索引。它只会在新的 v5 全量索引通过
count、语义分类方法审计、四个固定商品/配件 canary、write block 和 Hybrid probe 后，以一个 alias
操作切换发布；此前的物理索引不会被改写。mapping `_meta` 必须精确为 v5 的当前 catalog checksum、
当前 Item-vector manifest checksum、`document_id` 和 `canonical_document_id_only`；任何旧 metadata
都会 fail closed。

服务器上验证当前已发布索引的精确命令（不会重建索引或改 alias）：

    cd /data3/sybai/glodex/opensearch/current-product
    ./build.sh verify

如服务器脚本被放到其他目录，仍显式复用同一份 runtime：

    ./build.sh --runtime-dir /data3/sybai/glodex/product-search/runtime verify

`start.sh` 是 `build.sh` 的内部前台启动器；日常不要直接运行它。`build.sh` 会拒绝复用已有的 9200 服务，并在任何退出路径只停止自己启动的进程。

已发布的 926,710 条商品索引保持不变。品类洞察迁移到独立的小型 CategoryInsight RAG
索引；更新品类知识不会重建商品索引。商品 Gateway 对全库执行 Hybrid 召回，不接受或使用
Category Card 过滤条件。

## 默认关闭的 Hybrid gateway

`current_product_hybrid_gateway.py` 是当前商品检索应用层 API。它不会启动 OpenSearch 或 RetrievalModel，
也没有默认端口；只有操作人显式运行
`serve --port <port>` 时才会绑定 `127.0.0.1`。因此它不能留下常驻服务或意外复用此前删除的
`18084`。

在临时前台 OpenSearch（`127.0.0.1:9200`）和最终 RetrievalModel（`127.0.0.1:18000`）均已由操作人启动后，
先作不绑定端口的校验。以下是服务器上显式同步该目录后的示例路径：

```bash
/home/sybai/miniforge3/envs/glodex/bin/python3.11 \
  /data3/sybai/glodex/opensearch/current-product/current_product_hybrid_gateway.py validate \
  --retrieval-model-manifest /data3/sybai/glodex/current-model/retrieval_model-manifest.json
```

校验会 fail-closed 地检查当前 catalog SQLite 的 metadata/926,710 条数、唯一 alias → 物理索引关系、
`document_id` mapping metadata、write block、1024 维向量 mapping、0.7/0.3 Hybrid
pipeline，以及 RetrievalModel `/v1/health` 和一次 tagged embedding → Hybrid probe。它不接受其他 alias
或非 canonical ID，也不会回退到 Faiss 或其他数据源。

需要短暂展示时再以前台方式运行（完成后 Ctrl-C 即停止）：

```bash
/home/sybai/miniforge3/envs/glodex/bin/python3.11 \
  /data3/sybai/glodex/opensearch/current-product/current_product_hybrid_gateway.py serve \
  --retrieval-model-manifest /data3/sybai/glodex/current-model/retrieval_model-manifest.json \
  --host 127.0.0.1 --port 18085
```

唯一查询入口是 `POST /v1/hybrid-search`。请求体包含 raw query、必填的报价 `platform`、
`top_k`（1–50）、1024 维归一化 `query_vector`，以及可空的 1024 维归一化
`preference_vector`。每个平台请求都通过固定的 `platform` term filter 检索对应平台报价；
不接受任意 OpenSearch DSL：

```json
{"query":"travel laptop","top_k":10,"platform":"amazon","query_vector":[...],"preference_vector":null}
```

两个向量都必须由正式 Agent 使用当前绑定的 RetrievalModel 生成。网关始终执行纯 query 召回；存在用户偏好时，
再以固定 0.80/0.20 权重生成个性化 query，并发执行第二条召回通道。两条通道都在 lexical + HNSW kNN
Hybrid 查询中硬过滤目标平台和落地成本，随后去重融合。调用方不能传 profile、history、任意 OpenSearch DSL
或商品 ID。Gateway 会把最多 50 个融合候选交给同一已验证 A100 BGE
cross-encoder，再用以 Hybrid 召回为主的 0.90/0.10 rank fusion 排序后返回 `top_k`；
结果不会返回 `item_vector`、`search_text` 或内部 Category Card ID。
