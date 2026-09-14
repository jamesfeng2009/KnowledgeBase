"""add v_delivery_chain view

Revision ID: a9b0c1d2e3f4
Revises: d5e6f7a8b9c0
Create Date: 2026-09-12 11:00:00.000000

P2b 交付链视图：按 assistant 消息（交付锚点）串联
Intent（用户问题）→ Process（agent_event_logs / tool_audit_log 聚合）
→ Output（引用卡片）→ 反馈信号 → 沉淀产物。
只读视图，用于 badcase 归因与评测集回流。PostgreSQL DDL。
"""

from alembic import op

revision: str = "a9b0c1d2e3f4"
down_revision = "d5e6f7a8b9c0"
branch_labels = None
depends_on = None

_VIEW_SQL = """
CREATE OR REPLACE VIEW v_delivery_chain AS
SELECT
    m.id                          AS message_id,
    m.conversation_id,
    c.tenant_id,
    c.user_id                     AS conversation_user_id,
    c.title                       AS conversation_title,
    m.created_at                  AS answered_at,
    -- Intent：同会话中紧邻的前一条 user 消息
    q.id                          AS user_message_id,
    q.content                     AS user_question,
    -- Process 聚合（session_id 为 Conversation.id 的字符串形式）
    COALESCE(t.tool_calls, 0)     AS tool_calls,
    COALESCE(t.tool_errors, 0)    AS tool_errors,
    t.tool_names,
    COALESCE(e.node_runs, 0)      AS node_runs,
    COALESCE(e.max_iteration, 0)  AS max_iteration,
    e.total_latency_ms,
    e.total_tokens,
    e.has_error,
    -- Output
    LEFT(m.content, 500)          AS answer_excerpt,
    m.sources                     AS citations,
    m.model_used,
    m.token_count                 AS answer_tokens,
    -- 反馈信号
    COALESCE(f.feedback_count, 0) AS feedback_count,
    f.feedback_types,
    (f.feedback_types @> ARRAY['complaint']::varchar[] OR f.feedback_types @> ARRAY['bug']::varchar[]) AS is_badcase,
    -- 沉淀产物（经 chat_feedback 反馈回溯到资产）
    a.sediment_asset_ids,
    a.sediment_doc_ids
FROM messages m
JOIN conversations c ON c.id = m.conversation_id
LEFT JOIN LATERAL (
    SELECT qq.id, qq.content FROM messages qq
    WHERE qq.conversation_id = m.conversation_id
      AND qq.role = 'user'
      AND qq.created_at <= m.created_at
    ORDER BY qq.created_at DESC
    LIMIT 1
) q ON true
LEFT JOIN LATERAL (
    SELECT count(*)                AS tool_calls,
           count(*) FILTER (WHERE ta.status <> 'success') AS tool_errors,
           array_agg(DISTINCT ta.tool_name) FILTER (WHERE ta.tool_name IS NOT NULL) AS tool_names
    FROM tool_audit_log ta
    WHERE ta.session_id = m.conversation_id::text
      AND ta.created_at <= m.created_at
      AND ta.created_at >= m.created_at - INTERVAL '1 hour'
) t ON true
LEFT JOIN LATERAL (
    SELECT count(*) FILTER (WHERE el.event_type = 'node_end') AS node_runs,
           max(el.iteration) FILTER (WHERE el.event_type = 'node_end') AS max_iteration,
           sum(NULLIF(el.metadata->>'latency_ms', '')::bigint) AS total_latency_ms,
           sum(NULLIF(el.metadata->>'token_count', '')::bigint) AS total_tokens,
           bool_or(el.metadata ? 'error') AS has_error
    FROM agent_event_logs el
    WHERE el.session_id = m.conversation_id::text
      AND el.created_at <= m.created_at + INTERVAL '5 minutes'
      AND el.created_at >= m.created_at - INTERVAL '1 hour'
) e ON true
LEFT JOIN LATERAL (
    SELECT count(*) AS feedback_count,
           array_agg(DISTINCT fb.type) AS feedback_types
    FROM feedbacks fb
    WHERE fb.related_message_id = m.id
) f ON true
LEFT JOIN LATERAL (
    SELECT array_agg(DISTINCT ka.id) AS sediment_asset_ids,
           array_agg(DISTINCT ka.doc_id) FILTER (WHERE ka.doc_id IS NOT NULL) AS sediment_doc_ids
    FROM knowledge_assets ka
    WHERE ka.source_type = 'chat_feedback'
      AND ka.source_id IN (
          SELECT fb2.id FROM feedbacks fb2
          WHERE fb2.related_message_id = m.id
      )
      AND ka.deleted_at IS NULL
) a ON true
WHERE m.role = 'assistant'
"""


def upgrade() -> None:
    op.execute(_VIEW_SQL)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS v_delivery_chain")
