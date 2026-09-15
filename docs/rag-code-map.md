# RAG 代码地图：数据处理（入库）与检索流程

> 用途：快速定位"要改什么 → 改哪个文件哪个函数"。
> 行号基于当前版本，改动后会漂移，请以函数名为准。
> 架构设计：[rag-architecture-design.md](rag-architecture-design.md)

---

## 一、代码结构（RAG 相关全部收在 `services/rag/`）

```
services/rag/
├── __init__.py     104 行   统一导出（from services.rag import ingest_guides, execute_query, …）
├── transform.py    845 行   唯一文本转换入口 es_info_transfer()：攻略 JSON → es_doc / vector_content
├── store.py        771 行   三库基础设施：PG 真相源 / ES 词法 / Qdrant 向量 + 依赖预检 + GuideStores
├── ingest.py       570 行   入库编排（函数化）：IngestOptions + ingest_guides()
├── retrieval.py    606 行   检索：多路召回 → RRF → 交叉编码重排 → PG 回填 + 排序打印
├── rerank.py       217 行   交叉编码重排序（dashscope qwen3-rerank / llm / none）
├── query.py        230 行   查询编排与展示（函数化）：QueryOptions + execute_query()
└── validate.py     300 行   最终总结结构校验（依据 skills/travel-summarizer.md）+ 转入库记录

# 根目录仅保留两个薄 CLI（argparse 参数与旧版完全一致）：
ingest_by_es_transfer.py   99 行   入库 CLI  → services/rag/ingest.py
rag_query.py              124 行   查询 CLI  → services/rag/query.py
seed_rag_test_data.py     832 行   4 篇示例攻略数据（入库默认输入）

# 仍在 services/ 顶层（RAG 之外也被引用）：
services/embedding.py     向量化（百炼 qwen3.7-text-embedding-flash，1024 维）
services/es_client.py     ES 客户端（HTTPS/认证/CA）；L242 起为旧单库路径，已弃用
services/env_bootstrap.py .env 加载 + Windows 控制台编码兜底
```

**已删除 / 已迁走**：`services/guide_ingest.py`、`services/triple_retrieval.py`、`services/rerank.py`、`utils/rag_tools.py`
（均已迁入 `services/rag/`）；`ingest_guide_template.py`、`test/es_doc.py`、`seed_triple_store.py`、`test/forRAGTest.py`；
Qdrant 集合 `guide_facts` / `guide_semantics` / `guide_embeddings`。

---

## 二、两种用法：函数 API 与 CLI

```python
# 程序内调用（推荐）
from services.rag import IngestOptions, QueryOptions, ingest_guides, execute_query, format_result, stats

ingest_guides(options=IngestOptions(reset_derived=True))              # 入库（缺省用示例数据）
ingest_guides(load_guides("example.json"), options=IngestOptions())
result = execute_query("上次去西安兵马俑怎么安排", QueryOptions(top_k=5), print_output=False)
print(format_result(result, QueryOptions(show_context=True)))
print(stats())
```

```powershell
# CLI（参数与旧版一致）
python ingest_by_es_transfer.py [--input P] [--title T] [--destination D] [--days N] [--summary S]
                                [--user-id U] [--username N] [--nickname N] [--trust V]
                                [--collection C] [--no-es] [--no-qdrant] [--reset-derived]
                                [--dry-run] [--stats] [--skip-service-check]
python rag_query.py ["查询" | -q 查询] [--top-k N] [--per-source N] [--context] [--context-chars N]
                    [--user-id U] [--public-only] [--verify] [--json] [--strict-embedding]
                    [--no-rerank] [--no-debug-ranking] [--skip-service-check]
```

---

## 三、数据处理（入库）流程

```
services/rag/ingest.py :: ingest_guides()  L426        ← 原 main 逻辑（函数化）
  ├─ load_guides() L100            读攻略 JSON（--input；缺省用 SEED_GUIDES）
  ├─ normalize_record() L131       归一化身份字段（title/destination/days/summary/owner/trust）
  ├─ transform.es_info_transfer()  ★ 唯一转换入口（services/rag/transform.py L581）
  │     ├─ es_doc          ES 文档（search_content / daily_plan[] / spots_catalog[] / …）
  │     ├─ vector_content  待向量化文本块（逐分区扁平数组）
  │     └─ qdrant_payload  Qdrant payload 基础字段
  ├─ [dry_run] build_preview() L367        只转换预览，不连库
  ├─ preflight_services()                   PG/ES/Qdrant 连通性预检
  └─ 逐篇写入：
       ① PG     pg_upsert_truth() L242        INSERT … content_json（JSON 在 json.dumps 处）
       ② ES     store.build_search_doc() → store.es_index_doc()
       ③ Qdrant typed_chunks() L189 → upsert_chunks() L329
       ④ rag_meta 登记 embedding 签名（查询时比对）
```

### 切块链路（三段，分别在两个文件）

| 阶段 | 位置 | 做什么 |
|------|------|--------|
| ① 切分并生成每块文本 | `services/rag/transform.py :: es_info_transfer()` **L581** | `_format_meta` L166 / `_format_period` L233 / `_format_day` L290 / `_format_daily_plan` L335 / `_format_trade_off_summary` L352 / `_format_spots_catalog` L411 / `_format_food_catalog` L435 / `_format_extra_recommendations` L459 / `_format_precautions_summary` L480 / `_format_budget_breakdown` L513；汇总成 `vector_content`（L688–701） |
| ② 给每块标类型 | `services/rag/ingest.py :: typed_chunks()` **L189** | 用 es_doc 里"同一份文本"反查 `chunk_type`（meta/trade_off/day_plan/spot/food/extra/precautions/budget） |
| ③ 向量化并写入 | `services/rag/ingest.py :: upsert_chunks()` **L329** | 先按 guide_id 清旧块 → `embedding.embed_texts()` 批向量化 → `store.qdrant_upsert()` L497 |

### 存储位置

| 数据 | 位置 |
|------|------|
| **攻略 JSON（真相源）** | PG `guides.content_json`（列定义 `store.py` DDL_GUIDES L104；写入 `ingest.py pg_upsert_truth()` L242） |
| **ES 检索文档** | `guide_search_docs`，字段由 `store.build_search_doc()` L408 生成（内部即 es_info_transfer 的 es_doc）；映射 `store.ES_MAPPING` L336；补字段 `store.es_apply_mapping()` L356 |
| **向量** | Qdrant `guide_chunks`（集合名由 `QDRANT_CHUNKS_COLLECTION` 或 `IngestOptions.collection` 决定），写入 `ingest.upsert_chunks()` L329 → `store.qdrant_upsert()` L497 |
| **元数据** | PG `rag_meta`（embedding 签名），`store.pg_set_meta()` L246 / `pg_get_meta()` L255 |

---

## 四、RAG 检索流程

```
services/rag/query.py :: execute_query()  L157     ← 预检 + 连接 + 检索 + 展示（原 main 逻辑）
  └─ run_query() L139 → retrieval.search()  L232   ★ 检索主函数
        ├─ check_embedding_consistency() L193      向量空间一致性（入库模型 vs 查询模型）
        ├─ embedding.embed_text(query)             query 向量化（失败退化纯词法）
        ├─ es_lexical_search()  L56                ① ES 词法（es_doc 各字段，cjk 分词 + highlight）
        ├─ qdrant_search()      L130               ② 对 VECTOR_COLLECTIONS 每个集合各召回一路
        ├─ rrf_fuse()           L159               RRF 融合（通道内按 guide_id 去重）
        ├─ store.pg_fetch_guides()                 按 guide_id 回 PG 取全文
        ├─ build_candidate_text() L433             构造 rerank 候选（命中证据优先）
        ├─ rerank.rerank()                         ★ 交叉编码重排（services/rag/rerank.py）
        ├─ finalize_ranking()   L469               final = W_RRF·归一化RRF + W_RERANK·归一化rerank
        ├─ print_channel_rankings() L373           打印各通道召回排序 + 分数构成
        └─ build_context()      L517               组装喂给 LLM 的 RAG 上下文
```

### 通道构成

| 通道 | 函数 | 匹配依据 |
|------|------|---------|
| ① ES 词法 | `retrieval.es_lexical_search()` L56 | `multi_match` over `title^3 / search_content^2.5 / summary^2 / overview^2 / destination^2 / keywords / spots_catalog^1.5 / food_catalog^1.5 / daily_plan / extra_recommendations / precautions_summary / budget_breakdown`（cjk bigram，AUTO fuzziness，带 highlight）；可选 `user_id`/`is_public` 过滤 |
| ② 向量通道 | `retrieval.qdrant_search()` L130 | 对 `store.VECTOR_COLLECTIONS`（默认 `guide_chunks`）每个集合独立召回，集合名即通道名；缺失/异常只告警不中断 |

### 环境变量

| 变量 | 默认 | 作用位置 |
|------|------|---------|
| `RAG_VECTOR_COLLECTIONS` | `guide_chunks` | 参与检索的 Qdrant 集合（兼容旧名 `RAG_EXTRA_COLLECTIONS`） |
| `RAG_RRF_K` | 60 | `rrf_fuse()` |
| `RAG_W_ES` / `RAG_W_<集合名>` | 1.0 | `retrieval.SOURCE_WEIGHTS` |
| `RAG_W_RRF` / `RAG_W_RERANK` | 0.4 / 0.6（**当前 `.env` 覆盖为 0.7 / 0.3**） | `finalize_ranking()` |
| `RAG_CHAT_VISIBILITY` | `own` | **历史模式**能检索到谁的攻略（`own` / `own_or_public` / `public` / `all`），见 `retrieval.resolve_visibility()`；公开模式固定 `own_or_public` |
| `RAG_USE_RERANK` | true | `search()` |
| `RAG_DEBUG_RANKING` | true | `print_channel_rankings()` |
| `RAG_RERANK_CANDIDATES` | max(2×top_k, 8) | 进入重排序的候选池 |
| `RERANK_PROVIDER` / `RERANK_MODEL` | dashscope / `qwen3-rerank` | `rerank.py` |
| `QDRANT_CHUNKS_COLLECTION` | `guide_chunks` | `ingest.py` |
| `EMBEDDING_MODEL` / `EMBEDDING_DIM` | `qwen3.7-text-embedding-flash` / 1024 | `services/embedding.py` |

---

## 五、"想改 X → 改哪里" 速查

| 想改什么 | 改哪里 |
|---------|--------|
| **攻略转成检索文本/分块规则**（最重要） | `services/rag/transform.py :: es_info_transfer()` L581 及其 `_format_*()` |
| 块类型判定 | `services/rag/ingest.py :: typed_chunks()` L189 |
| 向量化与写入集合 | `services/rag/ingest.py :: upsert_chunks()` L329；集合名 `.env` 的 `QDRANT_CHUNKS_COLLECTION` |
| **JSON 入库字段** | `services/rag/ingest.py :: pg_upsert_truth()` L242 |
| ES 文档字段 | `services/rag/store.py :: build_search_doc()` L408 |
| ES 索引映射/分词/权重 | `services/rag/store.py` 的 `ES_MAPPING` L336；查询语句 `retrieval.es_lexical_search()` L56 |
| 已有索引补字段 | `services/rag/store.py :: es_apply_mapping()` L356（入库时自动调用） |
| 入库选项（原 CLI 参数） | `services/rag/ingest.py :: IngestOptions` L70 |
| 检索参与哪些向量集合 | `.env` 的 `RAG_VECTOR_COLLECTIONS` → `store.VECTOR_COLLECTIONS` |
| 通道权重 / RRF 参数 | `retrieval.SOURCE_WEIGHTS`、`RAG_W_*` |
| **最终排序公式** | `retrieval.finalize_ranking()` L469（`score_parts` 可解释分数构成） |
| 排序明细打印 | `retrieval.print_channel_rankings()` L373（ES 高亮 + chunk 类型 + RRF/重排顺序 + 分数构成） |
| 重排序后端/模型/指令 | `services/rag/rerank.py`：`_provider()` L74 / `_rerank_dashscope()` L97 / `_rerank_llm()` L159 |
| 送进 rerank 的候选文本 | `retrieval.build_candidate_text()` L433（证据优先，勿把证据排末尾） |
| RAG 上下文格式 | `retrieval.build_context()` L517 |
| 查询选项（原 CLI 参数） | `services/rag/query.py :: QueryOptions` L39 |
| 结果展示格式 | `services/rag/query.py :: format_result()` L77 |
| 三库联动删除 | `services/rag/ingest.py :: delete_guide()` L550 |
| **总结结构 + JSON 格式校验** | `services/rag/validate.py`：`parse_summary_json()`（格式/简单修复）、`validate_summary()`（结构）、`prepare_summary_for_ingest()`（失败即抛异常） |
| 总结 → 入库记录 | `services/rag/validate.py :: summary_to_record()` |
| **总结生成后自动入库** | `services/summary_task.py :: save_summary_to_rag()`（后台任务里，SSE `done` 之后触发） |
| **聊天要不要走 RAG（三种模式）** | 见第八章：`services/chat_service.py :: resolve_chat_mode()` / `_resolve_rag()` + `services/rag_service.py :: MODE_VISIBILITY` |
| **攻略列表搜索（我的 / 公开）** | 后端 `services/es_client.py :: _guide_list_body(keyword)`（`multi_match` + `operator=and`，命中标题^3/目的地^2/关键词^2/摘要）；路由 `GET /api/guides/my?keyword=`、`GET /api/share/public?keyword=`、`GET /api/user/guides?keyword=`；前端 `ProfileView.vue` / `ShareView.vue`（300ms 防抖，total 为过滤后数量） |
| **谁能检索到谁的攻略（归属过滤）** | `services/rag/retrieval.py`：`resolve_visibility()` / `row_visible()` / `_es_owner_filter()` / `_qdrant_owner_filter()`；聊天默认 `own`（`RAG_CHAT_VISIBILITY`） |
| **攻略详情 / 评价 / 公开** | `controller/guide_routes.py :: get_guide_detail()`（本人或已公开）、`controller/share_routes.py`；前端 `vue/src/components/GuideDetailModal.vue`（「我的」「旅游分享」共用）。规则：**评价内容 10–500 字**（`models/schemas.py :: GuideRate`），**先评价才能公开**；字数要求/计数器/未达标原因都必须在界面上显示 |
| 前端错误提示文案 | `vue/src/lib/api.ts :: apiErrorMessage()` —— FastAPI 校验失败是 **422 且 `detail` 为对象数组**，直接渲染会变成 `[object Object]`；这里统一翻成「字段：至少需要 N 个字」这类中文 |
| 攻略文档 `_id` 与 `guide_id` 对不上（点详情 404） | `services/es_client.py`：`save_guide()` 显式指定 `_id`、`_resolve_doc_id()` 兜底解析、`_apply_new_mapping_fields()` 补映射 |
| 修复存量攻略归属（写进了 `rag_test_user`） | `fix_guide_owner.py`（默认 dry-run，`--apply` 才写 PG/ES/Qdrant 三处） |
| 回归探针 | `retrieval.PROBES` + `retrieval.verify()` L565 |
| 示例攻略数据 | `seed_rag_test_data.py` 或 `--input xxx.json` |

---

## 六、必须知道的坑

1. **`.env` 加载顺序**：模块级读环境变量的模块必须在读取前调用 `env_bootstrap.load_env()`（幂等）。`services/rag/*` 各模块均已处理；新模块照做，否则 `.env` 被静默忽略。
2. **ES 映射必须显式声明**：只改代码不补映射 → ES 动态映射成 standard 分词（中文逐字切分，实测"虚词刷分"）。入库时 `es_apply_mapping` 会自动补字段；分析器不一致只能删索引重建。
3. **读路径不做 DDL**：`rag_query.py` 不建表；`check_embedding_consistency()` 对缺失的 `rag_meta` 已容错。
4. **通道内去重**：`rrf_fuse(dedupe_per_channel=True)` 不能关，否则同一通道多块会把该通道权重隐性放大 N 倍。
5. **候选文本顺序**：`build_candidate_text()` 必须"命中证据优先 + 足够长度预算"，否则交叉编码器看不到关键证据（曾把最相关攻略判为无关）。
6. **换 embedding 模型**：必须重建集合（`IngestOptions(reset_derived=True)`）；查询侧有向量空间护栏，`QueryOptions(strict_embedding=True)` 可直接报错。
7. **Windows 控制台**：`env_bootstrap.setup_console()` 把 stdout `errors` 降级为 `replace`，否则 GBK 控制台打印 ✅/❌ 会抛 `UnicodeEncodeError`。
8. **包内引用**：`services/rag/*` 子模块顶层一律用 `services.rag.<module>` 绝对导入，不要 `from services.rag import x`，避免与 `__init__` 形成循环导入。
9. **子模块不能被同名函数遮蔽**：`services/rag/__init__.py` 重导出了函数 `rerank`，因此 `from services.rag import rerank as m` **和** `import services.rag.rerank as m` 都会把 `m` 绑到那个**函数**（不是模块），随后 `m.provider_info()` 报 `'function' object has no attribute 'provider_info'`，重排序被静默跳过、退回 RRF 顺序。正确写法：`from services.rag.rerank import provider_info, rerank`（按可调用对象导入）。同理适用于任何与子模块同名的重导出符号。
10. **外部依赖升级会静默打断链路**：`qdrant-client ≥1.13` 的 `AsyncQdrantClient` 删除了 `search()`（改用 `query_points()`，结果在 `.points`）；`memory_store.search_long_term_memories()` 曾因此让整个 `/api/chat/*` 返回 500。升级依赖后务必跑一次对话链路，不要只看单测。
11. **Redis 是聊天链路的硬依赖**：`memory_store.acquire_session_lock()` 走 Redis（`REDIS_URL`，默认 `localhost:6379`）。Redis 缺失时启动只记 warning 不会失败，但每次聊天都会 500。本机已用容器提供：`docker run -d --name redis -p 6379:6379 --restart unless-stopped redis:7-alpine`。
12. **检索不传 `user_id` 就等于"全库可见"**：`retrieval.search()` 只在拿到 `user_id`/`visibility` 时才过滤。聊天链路曾经漏传 `user_id`，导致把别人（以及测试写入的 `rag_test_user`）的攻略当成本人历史注入上下文。现在 `chat_service._resolve_rag()` 强制带 `user_id`，且**拿不到身份就跳过检索**（不会退回全库）。
13. **索引已存在时，"新加字段"不会生效**：`ensure_indices()` 对已存在的索引直接跳过 create，于是新字段走 ES 动态映射（`guide_id` 被当成 `text`）。现在 `_apply_new_mapping_fields()` 会补新增字段；但**已存在字段无法原地改类型**，只能靠查询兜底或重建索引。
14. **攻略文档 `_id` 必须等于文档里的 `guide_id`**：历史上 `save_guide()` 不带 id 索引，ES 自动生成 `_id`，而 `guide_id` 字段是另一个 UUID → "我的攻略列表里能看见，点详情却 404"。现在索引时显式用 `guide_id` 作 `_id`，且 `get_guide()/update_guide()` 走 `_resolve_doc_id()` 兜底（`term` + `match_phrase`，兼容已存的 text 映射老数据）。
15. **自动写入 RAG 的归属来自请求体**：`POST /api/summary/stream` 必须带 `user_id/username/nickname`（前端 `streamSummary(..., {owner})` 已经带上）；不带就会落到环境变量默认归属（`rag_test_user`），攻略不属于任何人，历史模式也就检索不到。存量错归属用 `fix_guide_owner.py` 修（默认 dry-run，`--apply` 才写入）。

---

## 七、最终总结 → 自动写入 RAG

```
POST /api/summary/stream  (SSE，只是"订阅"进度，真正的分析在后台任务里跑)
  └─ controller/summary_routes.py :: summary_stream()
        start_summary_task(task_id, owner)        ← 幂等：同一 task_id 只分析一次
        async for event in task.subscribe(from_index):   ← 先重放历史事件，再跟随新事件
            yield _sse(event["type"], event["data"])
  ↓ 后台任务（services/summary_task.py :: _run_summary_task）
        async for event in summary_from_notes(task_id, redis):   ← 分析 + 进度事件
            if event["type"] == "done": final_summary = event["data"]
            await task.emit(...)                  → 写入事件缓冲（任何订阅者都能重放）
        ↓ 全部事件发完后（auto_ingest 开启时）
        await task.emit("saving", {...})          ← 前端显示"正在写入知识库"
        report = await asyncio.to_thread(save_summary_to_rag, final_summary, owner)
        await task.emit("saved", {...})           ← 含 validation 与 ingested[].guide_id
        # 校验失败 → emit("validation_error")；其他异常 → emit("save_error")，都不影响已返回的总结
```

**关键**：分析不再跑在 HTTP 请求里，所以切页面 / 刷新 / 关标签页都不会中断它；
重新连上时用 `from_index=0` 从头重放，进度直接追平（详见第九章）。

`save_summary_to_rag()`（`services/summary_task.py`，`controller/summary_routes.py` 里保留同名重导出）内部：

| 步骤 | 函数 |
|------|------|
| ⓪ **JSON 格式校验与简单修复** | `services/rag/validate.py :: parse_summary_json()` —— 自动修 ``` 围栏 / `json:` 前缀 / 前后多余文字 / 尾随逗号 / 全角引号键名 / BOM；**截断、语法错误、顶层非对象 → 抛 `SummaryFormatError`（不写 RAG）** |
| ① 结构校验 | `services/rag/validate.py :: validate_summary()` —— 校验项对应 skill 的 8 条输出质量要求（天数/四时段覆盖、catalog 与 daily_plan 一致、经纬度、sources、预算数字与余额、extra 3~8 且不重复、景点去重等）；**有 error → 抛 `SummaryStructureError`（不写 RAG）** |
| ② 转入库记录 | `prepare_summary_for_ingest()` → `summary_to_record()`（destination 取 `meta.destinations`、days 取 `meta.total_days`、summary 取 `_format_meta(meta)`、content 为完整总结 JSON） |
| ③ 写入三库 | `services/rag/ingest.py :: ingest_guides()`（PG 真相源 + ES 文档 + Qdrant `guide_chunks`） |

**校验策略一句话**：简单格式问题自动修掉继续走；复杂问题（JSON 截断/结构不合规）**抛异常且绝不写 RAG**。

**开关**：`RAG_AUTO_INGEST=true|false`（默认 true）、`RAG_VALIDATE_STRICT=true|false`（默认 true：抛异常并跳过；false：只记 report 并跳过，不抛）；请求体可选传 `save_to_rag`、`user_id`、`username`、`nickname`、`is_public`（不传则用环境变量默认归属）。

**事件**：成功 → `saved`（含 `validation.{ok,errors,warnings,counts,json.repaired}` 与 `ingested[].guide_id`）；校验失败 → `validation_error`（`kind=format|structure` + `errors` + 完整 `report`）；其他异常 → `save_error`。

---

## 八、聊天三种模式：普通 / 历史（仅自己）/ 公开（自己 + 公开）

前端聊天框上方是三选一的模式切换（持久化在 `localStorage['chat_mode']`）：

| 模式 | 检索范围（visibility） | 是否做意图判定 | `rag_decision.source` |
|------|----------------------|---------------|----------------------|
| 💬 **普通**（默认） | 不检索 | 否 | `off` |
| 🧠 **历史模式** | `own` —— **只检索自己的攻略** | 是（关键词快筛 + LLM + `skills/rag-intent.md`） | `keyword` / `llm` / `heuristic` |
| 🌐 **公开模式** | `own_or_public` —— **自己的 + 他人已公开的** | 否（每次提问都检索） | `public` |

```
POST /api/chat/stream   { message, chat_mode, session_id, request_id }
                          chat_mode ∈ normal | history | public
  └─ controller/chat_routes.py :: send_message_stream()
        mode = chat_service.resolve_chat_mode(req.chat_mode, req.history_mode)   ← 兼容旧字段
        └─ services/chat_service.py :: stream_chat_with_rag(..., chat_mode)      ★ 接入点
              └─ _resolve_rag(message, mode, user_id)
                    ├─ normal  → {"need_rag": False, "source": "off"}            ← 直接让模型回答
                    ├─ history → rag_service.agent_rag_intent(query)             ★ 判断是否需要 RAG
                    │     ├─ extract_keywords_from_query() / keyword_prefilter()  关键词快筛（历史回忆词）
                    │     ├─ LLM + skills/rag-intent.md                          LLM 判定，输出严格 JSON
                    │     └─ _heuristic_decision()                               兜底启发式
                    │     → need_rag=True 才检索；visibility = own
                    └─ public  → 跳过意图判定，直接把问题当检索语句；visibility = own_or_public
                          └─ rag_service.search_related_guides_with_meta(query, user_id, visibility)
                                └─ services/rag/retrieval.py :: search()          ← 第四章完整检索链路
                          → build_rag_context(owner_user_id, mode)
                              · 每条攻略标注「我的攻略」/「公开攻略（作者：xxx）」
                              · 公开模式的说明文字明确要求"不要把别人的攻略当成用户自己的经历"
              ├─ start/内联事件携带 chat_mode + rag_decision，供前端展示判定依据
              └─ done.data 额外带 used_rag / rag_decision / related_guides
```

| 想改什么 | 改哪里 |
|---------|--------|
| **模式的检索范围** | `services/rag_service.py :: MODE_VISIBILITY`（normal/history/public → visibility） |
| 模式归一化（含 `history_mode` 兼容） | `services/chat_service.py :: resolve_chat_mode()` |
| **是否需要 RAG 的判定规则**（仅历史模式） | `skills/rag-intent.md`（skill 提示词：历史回忆 vs 一般旅游咨询、query 改写规则） |
| 判定所用模型 | `services/rag_service.py :: RAG_INTENT_MODEL`（默认 `deepseek-free`） |
| 历史回忆关键词 | `services/rag_service.py :: HISTORY_KEYWORDS`（如 上次/去年/我去过/我的攻略） |
| 上下文里的模式说明文字 | `services/rag_service.py :: MODE_CONTEXT_HEADER` |
| 注入上下文时的归属标注 | `services/rag/retrieval.py :: build_context(owner_user_id=...)` |
| 相关攻略精简结构（含 `is_mine`） | `services/chat_service.py :: _guide_brief()` |
| 前端模式切换与展示 | `vue/src/views/ChatView.vue`（三选一按钮 + 📚/🧭 判定 + 攻略卡片，公开的用绿色标出） |
| 前端请求 / SSE 解析 | `vue/src/lib/api.ts :: streamChat({ chatMode })` / `consumeSSE()` |

**接口契约**

- 请求：`ChatRequest.chat_mode: "normal" | "history" | "public" | None`（`models/schemas.py`）；
  `history_mode: bool` 为兼容字段，**仅当 `chat_mode` 未传（None）时生效**：`true` → history。
- SSE `start` 与 `done`：`chat_mode`、`history_mode`（兼容）、`used_rag`、
  `rag_decision{need_rag,intent_type,reason,query,keywords,source,visibility,mode}`、
  `related_guides[{guide_id,title,destination,score,channels,is_mine,is_public,author}]`
- 非流式 `POST /api/chat/message`：同一组字段（`ChatResponse`）

**可见性（重要）**：检索一律带 `user_id + visibility`，默认 `own`。不传 `user_id` 时不会退回"全库可见"，
而是直接跳过检索（避免把别人的或测试数据当成本人历史）。`RAG_CHAT_VISIBILITY` 只影响**历史模式**的默认范围，
公开模式固定 `own_or_public`（否则"公开模式"就没有意义了）。

**注意**：普通模式完全不碰 ES/Qdrant（`source="off"`），仅用模型自身知识 + 会话记忆回答；
历史模式命中不到自己的攻略时也会正常回答，只是没有引用（`used_rag=false`）。

---

## 九、攻略生成页：进度保活 与 后端日志

### 9.1 为什么以前"切到别的页面，进度就停了并重置"

两个原因叠加：

1. **前端**：`GuideView.vue` 把 `stage / events / finalSummary` 全放在组件本地 `ref` 里，
   路由切走 → 组件销毁 → 状态归零（`stores/plan.ts` 当时根本没被使用）；
2. **后端**：`summary_from_notes()` 的分析跑在 SSE **请求内部**，客户端一旦断开连接，
   FastAPI aclose 生成器 → 分析被取消（日志里能看到 `⚠️ [SSE] 客户端断开连接，事件流被取消`）。

### 9.2 现在的结构

```
前端                                  后端
──────────────────────────────────────────────────────────────────
stores/plan.ts  (唯一状态源)           services/summary_task.py
  stage/events/finalSummary              SummaryTask  (task_id → 事件缓冲)
  analyze()  ──POST /api/summary/stream──►  start_summary_task()  幂等，只分析一次
  resume()   ──POST /api/summary/stream──►  task.subscribe(from_index=0)
                 from_index=0                    → 先重放全部历史事件，再跟随新事件
  （切页面：store 与订阅都还活着，什么都不用做）
  （刷新页面：localStorage 恢复视图 → resume() 追平进度）
```

| 场景 | 行为 |
|------|------|
| 切到别的 Tab 再切回来 | store 不会被销毁，SSE 一直在收；`LayoutView` 顶部还会显示「分析中 n/m」小徽标 |
| 刷新页面（F5） | `store` 从 `localStorage['plan_state_v2']` 恢复 → `resume()` 重新订阅 → 后端重放全部事件，进度追平 |
| 关闭标签页 | 后端任务继续跑完（含 RAG 入库）；任务结果保留在缓冲区 6 小时（可调 TTL） |
| 后端重启过 | `GET /api/summary/task/{id}` 返回 404 → 前端提示"任务已失效，请重新识别"，不会卡在假进度上 |

| 想改什么 | 改哪里 |
|---------|--------|
| 页面阶段/状态机、进度事件处理 | `vue/src/stores/plan.ts`（`Stage` / `handleEvent()` / `_attach()`） |
| 攻略页渲染与加载动画 | `vue/src/views/GuideView.vue`、`components/PlannerForm.vue`（按钮转圈）、`components/AnalyzeProgress.vue`（进度条 + 时间线） |
| **重新识别**（改完输入再识别一次） | `IntentConfirm.vue`（可编辑输入框 + 🔄 按钮）→ `store.recognize()`（会先清空旧意图再请求） |
| 后端任务生命周期（TTL/缓冲上限/心跳） | `services/summary_task.py` 顶部常量 + 环境变量 `SUMMARY_TASK_TTL`、`SUMMARY_MAX_EVENTS`、`SUMMARY_MAX_TASKS`、`SUMMARY_SSE_HEARTBEAT` |
| 订阅/快照/取消接口 | `controller/summary_routes.py`：`POST /stream`、`GET /task/{id}`、`POST /cancel/{id}` |

**接口契约**：`POST /api/summary/stream` 请求体 `{task_id, from_index=0, user_id?, username?, nickname?, is_public?, save_to_rag?}`；
`GET /api/summary/task/{task_id}` 返回 `{status, index, events[], final_summary, ingest_report, error, elapsed}`。

### 9.3 看后端输出（三种方式）

后端用 `python run_backend.py` 启动时，会把 stdout/stderr（uvicorn 访问日志 + 项目 logger +
代码里所有 `print` 的进度信息）**双写**到控制台与日志文件：

```bash
python run_backend.py            # → 控制台 + logs/backend.log
```

| 方式 | 做法 |
|------|------|
| ① 前端页面（推荐） | 顶部 Tab「🖥️ 后端日志」→ 实时跟随（`GET /api/dev/logs/stream`，SSE），支持暂停/过滤/清屏 |
| ② 终端 | `Get-Content logs\backend.log -Wait -Tail 50` |
| ③ 直接看文件 | `logs/backend.log` |

| 想改什么 | 改哪里 |
|---------|--------|
| 日志文件路径 | 环境变量 `BACKEND_LOG_FILE`（默认 `<项目根>/logs/backend.log`） |
| 轮询间隔 / 关闭日志接口 | `DEV_LOG_POLL`（默认 1s）、`BACKEND_LOG_ENDPOINT=false` |
| 日志实时流实现 | `controller/dev_routes.py`（鉴权复用 `auth.get_current_user`）、`vue/src/views/LogsView.vue` |
| 日志 tee 逻辑 | `run_backend.py :: _Tee`（Windows GBK 控制台下自动降级为 `replace`，不会因 ✅ 崩） |
