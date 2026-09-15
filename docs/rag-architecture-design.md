# RAG 架构设计方案：PostgreSQL 真相源 + ES 词法检索 + Qdrant 双通道语义检索

> 状态：设计稿（待评审）
> 适用：TravelAgent 旅游攻略 RAG（skills/travel-summarizer 输出结构）
> 配套：seed_rag_test_data.py（改造后作为端到端种子/验证）

---

## 1. 目标与背景

现状：攻略完整 JSON + embedding 全部存在 ES 单索引，混合检索只对"整篇摘要"向量化，
导致两类问题：
1. **语义粒度粗**——用户问"住宿怎么选/节奏适不适合带娃"这类体验问题时，
   单条整篇向量难以命中；
2. **真相源与检索层耦合**——ES 既当数据库又当检索引擎，结构修改、回填、多副本成本高。

本方案改为**三库分层 + 双通道向量化**：

| 层 | 职责 | 谁 |
|----|------|-----|
| PostgreSQL | 真相源：攻略完整 JSON，增删改唯一入口 | 业务库 |
| Elasticsearch | 词法/过滤检索（关键词、用户、公开状态、标题/摘要/目的地） | 检索 |
| Qdrant | 语义向量检索（双通道：结构化事实 + LLM 语义总结） | 检索 |

检索时 ES 与 Qdrant 并行查询 → 融合去重得到 `guide_id` 列表 → **回 PostgreSQL 取全文**。

---

## 2. 总体数据流

```
        ┌─────────── 写入侧 ───────────┐        ┌──── 查询侧 ────┐
用户输入/攻略生成                          │        │ 用户 query
   │                                    │        │   │
   ▼                                    │        │   ▼
PostgreSQL guides (JSONB, 真相源) ──commit─►    触发判断(should_use_rag)
   │ ①构建 search document               │        │   │
   ▼                                    │        ├─► ES 词法检索 (top N) ──┐
Elasticsearch guide_search_docs          │        │                          │
   │ ②入队向量化任务                      │        ├─► Qdrant A 结构化向量 ──┤→ RRF/加权融合
   ▼                                    │        ├─► Qdrant B LLM语义向量 ──┤   → 去重 guide_id 列表
异步 Worker（Redis 队列）                 │        │                          ▼
   ├─ 通道A：结构化事实 → 直接向量 → Qdrant A  │        │   按 id 批量查 PostgreSQL
   └─ 通道B：LLM 总结(住宿/行程期望等) →      │        ▼
       向量 → Qdrant B                    │   组装 RAG 上下文 → LLM
        └───────────────┘
```

**核心原则**：ES、Qdrant 都是**可重建的派生索引**（derived index），PostgreSQL 是唯一真相源。
任一索引损坏/丢失，都能从 PG 全量重建——因此 ES 中**不再存向量**（消除双份向量同步问题）。

---

## 3. 存储设计

### 3.1 PostgreSQL —— 真相源

```sql
CREATE EXTENSION IF NOT EXISTS pgcrypto;          -- gen_random_uuid

CREATE TABLE guides (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       TEXT        NOT NULL,            -- 兼容现有 ES 用户体系(user_id 字符串)
    username      TEXT        NOT NULL,
    nickname      TEXT        NOT NULL,
    title         TEXT        NOT NULL,
    destination   TEXT        NOT NULL,
    days          INTEGER,
    summary       TEXT,                            -- 展示摘要；也是通道A向量化底座
    content_json  JSONB       NOT NULL,            -- 攻略完整 JSON（travel-summarizer schema）
    keywords      TEXT[]      NOT NULL DEFAULT '{}',
    rating        SMALLINT,                        -- 1-5
    rating_text   TEXT,
    is_public     BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    search_doc_version INTEGER NOT NULL DEFAULT 0  -- 派生索引版本号（重建用）
);

CREATE INDEX idx_guides_user_id  ON guides (user_id);
CREATE INDEX idx_guides_public   ON guides (is_public);
CREATE INDEX idx_guides_dest     ON guides (destination);
CREATE INDEX idx_guides_gin_key  ON guides USING GIN (keywords);
```

- `id`（UUID）即 guide_id：ES 文档 `_id` = id，Qdrant payload `guide_id` = id，三库同键。
- 迁移范围说明：`users` / 聊天记录仍留在 ES（不在本期范围），仅 `guides` 上 PG。

### 3.2 Elasticsearch —— 词法/过滤层（不再存向量）

索引名沿用 `travel_guides`（避免业务代码大改）或新建 `guide_search_docs`（彻底隔离）。
推荐**新建 `guide_search_docs`**，便于灰度与回滚。

```jsonc
// guide_search_docs mapping（精简：只服务词法与过滤）
{
  "mappings": {
    "properties": {
      "guide_id":    {"type": "keyword"},              // = PG id
      "user_id":     {"type": "keyword"},
      "is_public":   {"type": "boolean"},
      "title":       {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
      "destination": {"type": "keyword", "fields": {"text": {"type": "text"}}},
      "summary":     {"type": "text"},
      "days":        {"type": "integer"},
      "keywords":    {"type": "keyword"},
      "updated_at":  {"type": "date"}
      // ⚠ 无 embedding 字段 —— 向量全部交给 Qdrant
    }
  }
}
```

检索职责：`multi_match`（title^3/summary^2/destination^2/keywords）+ `user_id`/`is_public` 过滤 + 分页。

### 3.3 Qdrant —— 语义层（双通道，两个 collection）

| Collection | 内容 | 每攻略点数 | payload |
|-----------|------|-----------|---------|
| `guide_facts` | 通道A：结构化事实向量 | 1 | `{guide_id, channel:"facts", destination}` |
| `guide_semantics` | 通道B：LLM 语义总结向量 | 2~5（按 section） | `{guide_id, channel:"semantics", section, text}` |

- 距离度量：`Cosine`；向量维度 = 所选 embedding 模型输出维度（全链路同一模型，两 collection 维度一致）。
- 全量数据按 `guide_id` 用 payload 过滤做删除（`delete by filter`）。

---

## 4. 双通道向量化

### 4.1 通道 A —— 结构化事实直接向量化（确定性、可重入）

把攻略的结构化字段拼成一条**规范文本**（顺序固定 → 同内容同向量）：

```
模板：
{destination}{days}天攻略 | 节奏:{pace} | 预算:{budget_amount}元/预估{budget_estimated}元
景点: {spot.name}({spot.type},{spot.cost})、…
美食: {food.name}(人均{food.avg_cost})、…
备选: {extra.name}
避坑: {precautions_summary.tickets/transport/... 摘要}
```

- 拼串后一次 `embed_text(structured_text)` → 写入 `guide_facts`。
- 该向量描述"这篇攻略有什么"，供**事实型/列举型**查询命中（"哪些景点/多少钱/几天"）。
- 可选演进：后期按 spot 拆分多条向量提升细粒度召回（本期不做）。

### 4.2 通道 B —— LLM 语义总结后向量化（体验语义）

由 worker 用现成 LLM（DeepSeek/Kimi，OpenAI 兼容 gateway）对 `content_json` 生成**固定 section 的语义画像**：

```json
// 请求：content_json(可截断) + 固定输出 schema
[
  {"section": "accommodation", "text": "…住宿怎么选、住哪区最方便…"},
  {"section": "itinerary_style", "text": "…整体节奏/每天强度/适合天数…"},
  {"section": "fit_for", "text": "…适合带娃/老人/文青/吃货…"},
  {"section": "selling_point", "text": "…这条线最值得去的核心卖点…"}
]
```

要求：每段 50~150 字、只讲一个主题、可独立检索（含关键体验词），
输出严格 JSON。每段各自 `embed_text` → 按 `guide_id + section` 写 `guide_semantics`（upsert）。

- 该通道解决**体验型查询**：如"带小孩节奏不赶的海边度假""想住景区里看夜景"——
  查询词未必出现在标题/摘要里，必须靠语义画像命中。
- LLM 总结在**入库后异步**执行（不阻塞保存接口），幂等键 = `guide_id`（重复运行覆盖）。

---

## 5. 写入流水线（一致性）

```
① 业务层保存 → PG: INSERT/UPDATE guides (唯一真相源)
② 提交成功后 → 构建 search doc → ES: index(id=guide_id)（同步，失败重试3次）
③ 入队向量化任务 → Redis list: vec:{guide_id}
④ worker 消费：
   A 通道: 拼结构化文本 → embed → qdrant.upsert(guide_facts)
   B 通道: LLM 总结(可并行4段) → embed → qdrant.upsert(guide_semantics)
⑤ 全部成功 → guides.search_doc_version += 1
```

**删除/公开状态**：
- 删除攻略：PG 删行 → ES delete doc → Qdrant 两 collection `delete(filter guide_id=)`。
- 公开/取消公开：只改 PG + ES（词法层需要 is_public 过滤），Qdrant 不动。

**失败处理**：ES/Qdrant 写入失败进入重试队列并告警；提供
`tools/rebuild_derived.py --from-pg` 全量重建（扫 PG 逐条重建 search doc + 双通道向量），
派生索引随时可重建是设计底线。

---

## 6. 查询流水线（融合检索）

```
1. 触发判断：沿用 should_use_rag 词表/LLM（保留"上次/去年…"语义）
2. 并行检索：
   ES:        多字段词法 + user/is_public 过滤      → top N=10 (含 _score)
   Qdrant A:  query(facts,      limit=10)          → 命中 {guide_id, score}
   Qdrant B:  query(semantics,  limit=10)          → 命中 {guide_id, score, section}
3. 融合：三种来源各自按 rank 归一，RRF 融合
   score(g) = Σ_source  w_s / (k + rank_s(g))      // k=60, w 可调(默认等权)
4. 去重 → 有序 guide_id 列表（保留命中来源与分数，供调试/加权）
5. 回 PG：SELECT ... WHERE id = ANY($ids) 按列表顺序取 content_json/summary/title
6. 组装 RAG 上下文：title/destination/summary +（可选）相关 content 片段，截断 max_chars
7. 结果里附 used_rag + 命中通道统计（A/B/ES 各几条），便于观测与调权
```

**通道权重可调**：事实型查询可提高通道 A 权重、体验型查询提高 B 权重
（后续可用 LLM 判断查询类型或直接手动配置）。

---

## 7. 代码改造映射

| 现状模块 | 改造 |
|---------|------|
| `services/es_client.py` | 拆分：ES 仅保留 users/chat + 新建 guide_search_docs 客户端；移除 GUIDE 的 embedding 相关 |
| `services/guide_service.py` | 保存改走 **PG**（新增 `guide_pg_store.py`）；保存后同步 ES doc + 入队向量任务 |
| `services/rag_service.py` | `search_related_guides` 改为：ES + Qdrant 双路 → 融合 → PG 取数 |
| `services/embedding.py` | 复用（单模型全链路） |
| `services/chat_service.py` | 基本不变（消费 rag_service 新接口） |
| 新增 | `guide_pg_store.py`（PG CRUD）、`search_doc.py`（构建/同步 ES doc）、`qdrant_client.py`、`vector_worker.py`（双通道）、`fusion.py`（RRF/加权）、`tools/rebuild_derived.py` |
| 配置 | `docker-compose.yml` 增 postgres:16 + qdrant:latest；`.env` 增 `PG_DSN`、`QDRANT_URL`、`RAG_*` 权重/阈值 |
| 依赖 | `asyncpg`(或 psycopg[binary])、`qdrant-client` |

---

## 8. 种子数据与端到端验证（改造 seed_rag_test_data.py）

- 写入路径改为：**先插 PG → 触发流水线**（不再是直插 ES）。
- 保留 4 份攻略种子（成都/西安/杭州+乌镇/三亚）。
- 探针分两类，验证**双通道各自价值**：
  - 词法型："上次去西安，兵马俑怎么安排" → 应经 ES 命中
  - 语义型："想找带娃不赶、有海有沙滩的攻略" → **关键词无"三亚/亚龙湾"，只能靠 Qdrant B 命中三亚**（验证 LLM 语义通道必要）
  - 融合型："去年 3 天预算 2000 内双城" → 验证跨通道融合 + PG 回填
- 每探针断言：期望 guide_id 出现在融合 top-k，并打印命中通道分布。

---

## 9. 迁移与回填

1. 建 PG 表 + ES 新索引 + Qdrant collections（幂等脚本）。
2. `tools/backfill_from_es.py`：扫旧 `travel_guides`（content_json 已全量），
   逐条插 PG → 走标准流水线重建 ES doc + 双通道向量。
3. 灰度：`rag_service` 加开关 `RAG_STRATEGY=legacy|triple`，默认 legacy，验证通过后切换。
4. 验证通过后下线旧索引与旧逻辑。

---

## 10. 运维要点

- 备份：PG（pg_dump/WAL）、Qdrant（快照卷），ES 可重建可不重点备份。
- 重建演练：定期执行 `rebuild_derived.py --dry-run` 校验派生索引与 PG 行数一致。
- 成本：LLM 总结每攻略约 2~4K tokens（DeepSeek 极低）；embedding 用中文强模型
  （阿里云百炼 text-embedding-v4 / 智谱 embedding-3，见前期调研），单模型贯穿两通道。

---

## 11. 风险与开放问题

| 项 | 说明/建议 |
|----|----------|
| 双通道与用户体系的绑定 | `user_id` 目前是字符串（ES users 仍驻留 ES）；若用户也要迁 PG，另立一期 |
| LLM 总结质量 | 生成内容不稳定时，用固定 JSON schema + 温度 0 + 重试 1 次兜底 |
| 融合权重 | 先等权上线，用命中通道统计调参；可扩展为按查询类型动态加权 |
| 逐点向量（per-spot） | 通道 A 现为攻略级 1 条；后期如需细粒度召回，按 spot 拆分是自然的扩展点 |
| 与 Cloud Agent 的关系 | 本方案仅涉 TravelAgent 本地 ES/PG/Qdrant 栈；云端 dsh 项目不受影响 |
