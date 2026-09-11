# P4 · 公网混合检索设计（Deep Research 双路取证）

> 状态：设计草案（待 review）
> 关联范围：Deep Research（P2-11 / P2-13）检索能力补强
> 技术底线：**只取理念、不动技术栈**。不引入 LangGraph / Chroma / FastMCP 标准协议；不新增网页抓取层与任务级临时索引。

---

## 1. 背景

`DeepResearchService` 现有取证仅依赖 `HybridRetriever.search()`（四路 `gather_paths`：vector / fulltext / cross_modal / graph），**全部为内部源**。对"课题 / 竞品 / 行业动态"类研究，内部库天然缺失"新东西"，导致结论偏旧、信息缺口（`status="gap"`）频发。

本设计为该场景补充**公网搜索返回的 snippet 级引用**，与内部证据并发检索、归一化合并、打源标记，解决"内部 + 公网异构量纲合并"问题，并保持引用可溯源（`[内部]/[网络]`）。

## 2. 目标

- Deep Research 单子课题取证从"只查内部"变为"内部 + 公网并发"，产生带 `source_type` 标签的引用。
- 合并排序量纲统一：内部向量分与公网搜索分各自归一化后可比，`boost` 参数效果可观察。
- 单源失败优雅降级（不阻塞，回落纯内部行为）。
- 引用可溯源：每条引用标注来源类型；`source_type` 随 `EvidenceCard.to_dict()` 一路透出。

## 3. 非目标（明确不做）

| 不做 | 原因 |
|---|---|
| 网页正文抓取 / HTML 抽取 / 分片 | 只需引用级溯源；抓全文层留待"用户要引原文"时二期做 |
| 任务级临时索引 + TTL | snippet 方案无需临时向量库 |
| 新增 `HybridRetriever.gather_paths` web 分支 | 检索下沉到 Service 层并行，避免触碰既有四路 |
| 引入 LangGraph / Chroma / FastMCP 标准协议 | 四条既定约束 |

## 4. 设计原则

1. **依赖注入**：`WebSearchProvider` 构造注入，可替换 `MockProvider`（对齐项目"可 Mock"约束）。
2. **并发 ≤ 2**：外部请求并发收敛到 ≤ 2（对齐爬虫防 IP 屏蔽偏好）。
3. **成本可视**：公网搜索按租户配额 + 配置开关，可一键关闭降级纯内部。
4. **单源降级**：公网异常 = 空结果，不抛错、不阻塞。
5. **内部优先放生成阶段**：不在检索排序强行加权破坏公平合并，而是靠 Prompt 约束"内部优先引用"。

## 5. 架构总览

```
DeepResearchService.research(goal, kb_ids)
 └─ _decompose_goal → [subtopic]
 └─ per subtopic: _gather_evidence(topic)                     [M2]
     ├─ asyncio.gather( return_exceptions=True, 并发≤2 )
     │    ├─ internal: self._retriever.search(topic, kb_ids, top_k)   // 四路内部，不动
     │    └─ web:      web_provider.search(topic, max_results)         // 公网 snippet
     ├─ merge_and_rank(internal_hits, web_hits, ...)                   // N2 纯函数
     └─ → EvidenceCard(citations=[{source_type, doc_id|url, title, snippet, score}…])  [M1]
 └─ _summarize / _summary_prompt                                       [M3 内部优先约束]
```

## 6. 新增模块

### 6.1 `backend/app/rag/web_search.py`

```python
class WebHit(TypedDict):        # 统一公网命中结构
    title: str
    url: str
    snippet: str                # 直接作为引用 snippet，无需抓正文
    score: float                # 提供商相关性分（另一套量纲，需归一化）

class WebSearchProvider(Protocol):
    async def search(self, query: str, max_results: int = 5) -> list[WebHit]: ...

class TavilyProvider(WebSearchProvider): ...   # 计划接入，含超时/限流
class MockProvider(WebSearchProvider): ...     # 测试与降级

def dedup_web_by_url(hits: list[WebHit]) -> list[WebHit]: ...
```

要点：`MockProvider` 供单测与无 Key 时的显式降级；snippet 直用，**不触网页**。

### 6.2 `backend/app/rag/merge_rank.py`

```python
def merge_and_rank(
    internal_hits, web_hits, *,
    k_internal, k_web,                     # 每源配额上限
    boost,                                 # 内部加权系数，默认 1.2
    min_internal=2, min_web=2,             # 保底：两源证据都出现
    total_budget=6,
) -> list[dict]:  # [{source_type, title, url_or_doc_path, snippet, score}]
```

**算法（保底 + 竞争，非固定配额）**：

```
internal_norm = min_max_normalize(internal.score)   # 各源自归一化
web_norm      = min_max_normalize(web.score)
ranked = [(internal, s*boost) for internal] + [(web, s) for web]
ranked.sort(desc by score)
picked = internal[:min_internal] + web[:min_web]                 # 第一刀：保底
flex   = internal[min_internal:k_internal] + web[min_web:k_web]  # 第二刀：剩余候选池
flex.sort(desc)
picked += flex[: max(0, total_budget - len(picked))]             # 全局竞争
picked.sort(desc)
```

**为何不用 PRD 式固定配额（内部 Top-N + 网络 Top-M 直拼）**：配额锁死谁入选后，改 `INTERNAL_KB_BOOST_FACTOR` 对入选集无影响，与"boost 效果可观察"的验收自相矛盾。保底 + 竞争让 boost 真正参与排序。

## 7. 数据模型变更

### `backend/app/services/deep_research_service.py` → `EvidenceCard.citations[]`

现结构 → 新增 `source_type`：

```python
# 改动前
"citations": [{ "doc_id", "title", "snippet", "score" }]
# 改动后
"citations": [{
    "doc_id": doc_id_or_url,   # web 命中用 url 作 doc_id
    "title": title,
    "snippet": snippet,        # internal=content[:200]，web=snippet
    "score": score,
    "source_type": "internal" | "web",   # 新增
}]
```

`source_type` 随 `to_dict()` 一路透出（报告/任务结果天然携带）。

## 8. 配置项（`backend/app/config.py` `get_settings()`）

| Key | 类型 | 默认 | 说明 |
|---|---|---|---|
| `WEB_SEARCH_ENABLED` | bool | false | 总开关；关则回落纯内部 |
| `WEB_SEARCH_PROVIDER` | str | "mock" | tavily / bing / mock |
| `WEB_SEARCH_API_KEY` | str | "" | 无 Key → MockProvider 降级 |
| `MERGE_K_INTERNAL` | int | 5 | 内部配额上限 |
| `MERGE_K_WEB` | int | 5 | 公网配额上限 |
| `INTERNAL_KB_BOOST_FACTOR` | float | 1.2 | 内部可信加权 |
| `MERGE_MIN_INTERNAL` | int | 2 | 内部保底 |
| `MERGE_MIN_WEB` | int | 2 | 公网保底 |
| `MERGE_TOTAL_BUDGET` | int | 6 | 单子课题引用总预算 |
| `WEB_SEARCH_CONCURRENCY` | int | 2 | 外部并发上限（≤2 约束） |

## 9. Prompt 变更（与 web 集成同一次改）

`_CONCLUDE_PROMPT` / `_SUMMARY_PROMPT` 增加生成期约束：

- 内部证据已清晰回答 → 优先引用内部；
- 网络来源仅在内部不足或需补充最新动态时引用；
- 每个论点后用引文编号标注，并区分 `[内部]/[网络]`。

> 内部优先落在**生成阶段**而非检索排序（设计原则 5），调参直观、可观察。

## 10. 校验与编号（inline）

- 来源去重：合并前仅 web 源内按 URL 去重，绝不跨源合并去重。
- 编号：引用顺序 = 报告编号；悬空编号清理策略沿用 P3 的 `evidence_ref` / 引用校验通道（本期仅保证 `source_type` 贯通，编号级清理留待呈现层一并上线）。

## 11. 涉及文件改动清单

| 文件 | 动作 |
|---|---|
| `backend/app/rag/web_search.py` | 新增：Provider 抽象 + Tavily/Mock 实现 + URL 去重 |
| `backend/app/rag/merge_rank.py` | 新增：归一化 + 保底/竞争合并 |
| `backend/app/config.py` | 新增配置项（§8） |
| `backend/app/services/deep_research_service.py` | M1 citations 加 `source_type`；M2 `_gather_evidence` 双路并行 + merge；M3 Prompt；`__init__` 注入 `web_provider` |
| `backend/app/services/deep_research_service.py`（并发闸口） | `asyncio.Semaphore(WEB_SEARCH_CONCURRENCY)` 约束 ≤ 2 |
| `backend/tasks/deep_research_tasks.py` | 组装 Provider 后传给 service；透传 `source_type` |

**明确不改**：`backend/app/rag/retriever.py`（`HybridRetriever.gather_paths` 四路保持原样）。

## 12. 验收标准

1. **boost 可观察**：同一 `goal` 重跑，`INTERNAL_KB_BOOST_FACTOR`=1.2 vs 0.2，报告内部引用占比应有可观察变化。
2. **单源降级**：`web_provider` 抛异常 / 无 Key / 空结果时，仍产出纯内部报告，回归现有行为。
3. **并发约束**：外部请求并发峰值 ≤ 2（可由日志/压测确认）。
4. **引用溯源**：`EvidenceCard.citations[]` 每条含非空 `source_type`；`[内部]/[网络]` 在呈现层可见。
5. **merge_rank 单测**：不同量纲（如 0~1 vs 0~100）归一化后可比；保底与竞争两条路径分别覆盖；`boost` 改变确实重排。

## 13. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 外部搜索成本 / 限流 | `WEB_SEARCH_ENABLED` 总开关 + 租户配额 + 并发 ≤ 2 + 缓存同日去重 |
| 公网质量 / 版权 | 提供商默认质量排序 + 白名单域可选 + 引用仅到 snippet 级不引全页 |
| 内容注入 / 污染 | snippet 仅作引用展示与 prompt 输入，经 sanitize 后再入上下文（对齐注入防护） |
| 内部占比被稀释 | 保底 `min_internal` 保两源，生成期"内部优先"Prompt 兜底 |

## 14. 分期

**POC（0.5 周）**：`merge_rank.py` 纯函数 + 单测 + boost 可观察实验（Mock 数据），验证算法。
**正式集成（1–1.5 周）**：`web_search.py` Provider + `backend` Service 接入 + 配置 + 降级回归 + 前端/task 结果透出 `source_type`（视 D1 结论）。

## 15. 待确认项（D1）

Deep Research 当前**无前端报告页**（`frontend/src` 无对应页面），结果走 Celery `to_dict()` 任务轮询透出。`[内部]/[网络]` 标签的**呈现层**归属需产品确认：

- A. 本期仅后端贯通 `source_type`，呈现层后续补；
- B. 本期一并做"任务结果/看板"的来源标签展示。

## 16. 引用来源

本文档基于外部文章《Research Agent：企业研究报告自动生成系统》的**设计理念**提炼（HITL 澄清、双源归一化合并、生成期内部优先、引用可溯源），**不采用其技术栈**（LangGraph / Chroma / FastMCP）。理念落地详见上方各节。