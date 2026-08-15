"""检索不变量 — 所有召回路必须满足的确定性约束（单一事实来源）。

三层防御架构（Phase 0 堵漏，GAP-2 修复）：

    第 1 层 · Pushdown
        向量 / 全文 / 跨模态三路通过 ``pushdown()`` 统一注入
        doc_status=published 子句，由 filter_builder 下推到后端；
        图谱路在 graph_service.py 的 Cypher WHERE 中直接过滤源头。

    第 2 层 · Final Gate
        ``final_gate()`` 在合并去重后、注入生成上下文前逐条复检：
        文档状态 + 密级 + 知识库归属（含租户），任何一项查不到
        即剔除（fail-closed，与 permission_service 既有语义一致）。
        即使某一路的 pushdown / Cypher 被未来重构遗漏，
        半成品与越权文档也在注入前被拦下。

    第 3 层 · 契约测试
        tests/test_retrieval_invariants.py 以 parametrize 锁定
        ALL_CHANNELS 注册表中全部通道：草稿 / 越权 / 跨租户候选
        断言不出现。新增检索通道必须注册进 ALL_CHANNELS 并通过
        契约测试，否则 CI 红灯。

使用方式::

    from app.rag.retrieval_invariants import RetrievalInvariants

    # 检索层下推（替代各处就地覆盖 doc_status）
    effective_filters = RetrievalInvariants.pushdown(
        "vector", kb_ids, caller_filters
    )

    # 注入前复检（perm_svc 为 PermissionService 实例）
    safe_docs = await RetrievalInvariants.final_gate(
        merged_results, perm_svc=permission_svc
    )
"""

from __future__ import annotations

from typing import Any

from app.utils.logger import get_logger

log = get_logger(__name__)

# 已注册的检索通道 — 新增通道必须注册到此表并通过契约测试
# （tests/test_retrieval_invariants.py parametrize 本列表）。
# "constraint" 为契约测试专用桩通道，验证不变量逻辑与具体后端解耦。
ALL_CHANNELS: tuple[str, ...] = (
    "vector",
    "fulltext",
    "cross_modal",
    "graph",
    "constraint",
)


class RetrievalInvariants:
    """检索不变量 — 每条召回路在任何输入下都必须成立的约束。

    不变量清单（违反任意一条 = 数据泄漏事故）：

        I1_PUBLISHED  文档状态必须为 published — 半成品（draft /
                      pending_review / archived）不可被在线检索看到。
                      即使向量化任务已对草稿跑完，也不允许进入召回。
        I2_TENANT     租户隔离 — 与缓存 key 同构，跨租户文档互不可见。
        I3_CLEARANCE  文档密级 ≤ 用户密级，fail-closed（查不到密级
                      一律剔除，宁可少召回不可越权）。
        I4_KB_SCOPE   kb_id ∈ 用户可访问知识库集合；空集合必须短路
                      不检索（空列表传给底层会被解释为"不过滤"）。
    """

    I1_PUBLISHED = "doc_status == published（半成品不可见）"
    I2_TENANT = "租户隔离（与缓存 key 同构）"
    I3_CLEARANCE = "classification ≤ 用户密级，fail-closed"
    I4_KB_SCOPE = "kb_id ∈ 可访问集合（空集合短路不检索）"

    # I1 的唯一合法取值 — 其余状态（draft / pending_review / archived）
    # 一律不可见。该常量是 doc_status 过滤的唯一权威定义。
    PUBLISHED = "published"

    # ------------------------------------------------------------------
    # 第 1 层：Pushdown — 检索层下推过滤
    # ------------------------------------------------------------------

    @classmethod
    def pushdown(
        cls,
        channel: str,
        kb_ids: list[str] | None,
        base: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """生成检索层下推过滤子句 — I1 在召回阶段的第一道防线。

        行为契约：
            - 返回新 dict，不修改调用方传入的 ``base``；
            - 强制注入 ``doc_status=published`` — 调用方传入的
              doc_status 会被覆盖（安全优先于灵活性）；
            - ``kb_ids`` 参数当前仅为签名对齐（kb 范围过滤由
              retriever 单独下推），保留参数使未来新增 per-channel
              下推约束无需改动调用方签名。

        Args:
            channel: 通道名（ALL_CHANNELS 之一，用于日志归因）。
            kb_ids: 检索限定的知识库 ID 列表（可为 None 表示不限定）。
            base: 调用方传入的原始过滤条件（如 wiki 层级过滤）。

        Returns:
            含 doc_status=published 的过滤字典，透传给
            VectorStoreBase.search / OpenSearch bool.filter。
        """
        filters: dict[str, Any] = dict(base) if base else {}
        if filters.get("doc_status") != cls.PUBLISHED:
            log.debug(
                "retrieval_invariants.pushdown_override",
                channel=channel,
                original_status=filters.get("doc_status"),
            )
        filters["doc_status"] = cls.PUBLISHED
        return filters

    # ------------------------------------------------------------------
    # 第 2 层：Final Gate — 注入前逐条复检
    # ------------------------------------------------------------------

    @classmethod
    async def final_gate(
        cls,
        results: list[dict[str, Any]],
        kb_ids: list[str] | None = None,
        perm_svc: Any = None,
    ) -> list[dict[str, Any]]:
        """合并去重后、注入生成上下文前的最后一道门。

        逐条复检三项不变量（I1 状态 + I3 密级 + I2/I4 归属与租户）：
        复检通过 PermissionService.filter_retrieval_candidates 以
        DB 为权威数据源执行 — 索引（向量库 / OpenSearch / Neo4j）
        数据可能滞后或被越权写入，DB 真实状态是最终裁决依据。
        任何一项查不到 → 剔除并告警（fail-closed）。

        Args:
            results: 合并去重后的候选列表（HybridRetriever 返回格式）。
            kb_ids: 调用方限定的知识库范围（记录用途；归属复检由
                perm_svc 按 DB 权限执行，比调用方声明更权威）。
            perm_svc: PermissionService 实例（携带请求级用户上下文
                与 DB session）。为 None 时无法复检 — 保持调用方
                既有行为（原样返回并告警），不因缺权限服务而
                拖垮无权限场景的检索链路。

        Returns:
            复检通过的候选子集（保持原顺序）。

        Raises（不抛）:
            复检内部异常时 fail-closed 返回空列表 — 权限复检失败
            的结果集宁可全弃，不可放行未验证内容。
        """
        if not results:
            return results
        if perm_svc is None:
            log.warning(
                "retrieval_invariants.final_gate_skipped",
                reason="no_permission_service",
                count=len(results),
                kb_ids=kb_ids or [],
            )
            return results
        try:
            return await perm_svc.filter_retrieval_candidates(results)
        except Exception as exc:
            log.error(
                "retrieval_invariants.final_gate_error",
                error=str(exc),
                error_type=type(exc).__name__,
                count=len(results),
            )
            return []
