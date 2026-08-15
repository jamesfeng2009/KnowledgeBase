-- ============================================================================
-- 约束注入通道 — 运营手动标注示例（Phase 1）
--
-- 设计：constraint-recall-design §4 / §11.2
-- 用途：Phase 1 无自动打标（Phase 2 才有），约束规则由运营手动 INSERT，
--       先让通道跑起来、审计落起来。本文件是可直接执行的示例模板。
--
-- 硬约束（务必遵守）：
--   1. 禁 DELETE — 修订走「旧规则 status='retired' + 新规则 superseded_by 指旧」，
--      历史条款永久可审计回放（§4 版本链）。
--   2. 状态机只有 pending_review → active → retired，无其他取值。
--   3. severity 取值 block | confirm | warn；status='active' 才会被通道注入。
--   4. normalized 为 JSONB 机器执行结构（statement / condition / required_mentions /
--      forbidden_patterns / amount_limits），Phase 3 ConstraintVerifier 消费。
--   5. kb.category 是 T4 高风险域默认注入的判定依据，存量 NULL 不命中 T4。
--
-- 执行前提：迁移 c7d8e9f0a1b2 已应用（constraint_rules 等表已建）。
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 0. 前置查询 — 拿到真实 kb_id / document_id（替换下方所有占位符）
-- ----------------------------------------------------------------------------
-- SELECT id, name FROM knowledge_bases WHERE name LIKE '%财务%';
-- SELECT id, chunk_id, title FROM documents WHERE kb_id = '<KB_ID>' LIMIT 10;


-- ----------------------------------------------------------------------------
-- 1. T4 前置 — 把财务 KB 标记为高风险域（该 KB 全部 active 规则无条件进候选）
-- ----------------------------------------------------------------------------
UPDATE knowledge_bases
SET    category = 'finance'
WHERE  name LIKE '%财务制度%'
AND    category IS NULL;          -- 幂等：已标记的不动

-- 域取值须 ∈ CONSTRAINT_HIGH_RISK_DOMAINS（默认 finance/legal/security/hr，
-- 见 app/config.py；改配置需重启服务）


-- ----------------------------------------------------------------------------
-- 2. T2 实体触发 — 红线规则（severity=block，超预算也全量注入）
-- ----------------------------------------------------------------------------
INSERT INTO constraint_rules (
    id, kb_id, document_id, chunk_id, rule_text, normalized,
    severity, trigger_entities, effective_from, status,
    classifier_confidence, reviewed_by
) VALUES (
    gen_random_uuid(),
    '<KB_ID>',                          -- ← 替换：财务制度 KB
    '<DOC_ID>',                         -- ← 替换：报销制度文档
    '<CHUNK_ID>',                       -- ← 替换：对应 chunk（图谱 HAS_CHUNK 边同源）
    '单笔金额超过 5000 元的报销必须双人签批，缺一不可。',
    '{
        "statement": "报销超5000元须双人签批",
        "condition": {"amount_gt": 5000},
        "required_mentions": ["双人签批"],
        "forbidden_patterns": [],
        "amount_limits": {"single_max": null}
    }'::jsonb,
    'block',
    ARRAY['报销', '签批', '差旅报销'],   -- T2：查询命中任一实体即注入
    DATE '2026-01-01',                   -- 生效窗起点（NULL = 无界）
    'active',                            -- 运营已人审 → 直接 active
    1.0,                                 -- 手动标注置信度记满
    '<REVIEWER_USER_ID>'                 -- ← 替换：人审账号
);

-- confirm 级示例 — 供应商新增需确认（占预算，超 CONSTRAINT_BUDGET_MAX_TOKENS 截断）
INSERT INTO constraint_rules (
    id, kb_id, document_id, chunk_id, rule_text, normalized,
    severity, trigger_entities, status, classifier_confidence
) VALUES (
    gen_random_uuid(),
    '<KB_ID>', '<DOC_ID>', '<CHUNK_ID>',
    '新增供应商前必须先查询重复供应商名录，确认无重名后再提交。',
    '{"statement": "新增供应商须先查重"}'::jsonb,
    'confirm',
    ARRAY['供应商', '新增供应商'],
    'active',
    1.0
);

-- warn 级示例 — 无生效窗（NULL/NULL = 永久有效）
INSERT INTO constraint_rules (
    id, kb_id, document_id, chunk_id, rule_text, normalized,
    severity, trigger_entities, status, classifier_confidence
) VALUES (
    gen_random_uuid(),
    '<KB_ID>', '<DOC_ID>', '<CHUNK_ID>',
    '所有对外付款建议保留审批截图作为附件。',
    '{"statement": "对外付款保留审批截图"}'::jsonb,
    'warn',
    ARRAY['付款', '对外付款'],
    'active',
    1.0
);


-- ----------------------------------------------------------------------------
-- 3. 版本链修订 — 条款改了不许 UPDATE 覆盖，也不许 DELETE
--    三步：旧规则 retire → 新规则 INSERT → 回填 superseded_by
-- ----------------------------------------------------------------------------
-- 3.1 旧规则退休（保留全文，审计可回放）
UPDATE constraint_rules
SET    status = 'retired',
       updated_at = now()
WHERE  id = '<OLD_RULE_ID>';

-- 3.2 新版本 INSERT（trigger_entities / 生效窗按新版填写）
INSERT INTO constraint_rules (
    id, kb_id, document_id, chunk_id, rule_text, normalized,
    severity, trigger_entities, effective_from, effective_to,
    status, classifier_confidence
) VALUES (
    gen_random_uuid(),
    '<KB_ID>', '<DOC_ID>', '<CHUNK_ID>',
    '单笔金额超过 8000 元的报销必须双人签批并附预算编号。',
    '{"statement": "报销超8000元须双人签批+预算编号", "condition": {"amount_gt": 8000}}'::jsonb,
    'block',
    ARRAY['报销', '签批'],
    DATE '2026-09-01',                   -- 新版 9 月起生效（旧版自然进 expired 审计）
    NULL,
    'active',
    1.0
);

-- 3.3 回填版本链指针
UPDATE constraint_rules
SET    superseded_by = '<NEW_RULE_ID>'
WHERE  id = '<OLD_RULE_ID>';


-- ----------------------------------------------------------------------------
-- 4. 灰度期运维查询 — observe 模式跑一周后的对比分析
-- ----------------------------------------------------------------------------
-- 4.1 每日注入决策分布（injected / skipped_observe / filtered_perm / expired）
-- SELECT action,
--        count(*)                AS records,
--        count(DISTINCT rule_id) AS rules
-- FROM   constraint_audit_records
-- WHERE  created_at >= now() - interval '7 days'
-- GROUP  BY action
-- ORDER  BY records DESC;

-- 4.2 observe 模式下「如果放开会注入哪些」→ 人工抽查规则质量
-- SELECT r.rule_text, r.severity, a.triggers, count(*) AS hits
-- FROM   constraint_audit_records a
-- JOIN   constraint_rules r ON r.id = a.rule_id
-- WHERE  a.action = 'skipped_observe'
-- AND    a.created_at >= now() - interval '7 days'
-- GROUP  BY r.rule_text, r.severity, a.triggers
-- ORDER  BY hits DESC
-- LIMIT  50;

-- 4.3 权限链误杀排查（规则文档被 filtered_perm 拦下的高频规则）
-- SELECT r.rule_text, count(*) AS blocked
-- FROM   constraint_audit_records a
-- JOIN   constraint_rules r ON r.id = a.rule_id
-- WHERE  a.action = 'filtered_perm'
-- GROUP  BY r.rule_text
-- ORDER  BY blocked DESC;

-- 4.4 生效窗过期仍被路由命中的条款（应全部落 expired 审计、零注入）
-- SELECT r.rule_text, r.effective_to, count(*)
-- FROM   constraint_audit_records a
-- JOIN   constraint_rules r ON r.id = a.rule_id
-- WHERE  a.action = 'expired'
-- GROUP  BY r.rule_text, r.effective_to;


-- ----------------------------------------------------------------------------
-- 5. 放开注入前的开关顺序（app/config.py / 环境变量）
-- ----------------------------------------------------------------------------
--   第 1 步（灰度）: CONSTRAINT_ENABLED=True + CONSTRAINT_INJECT_MODE=observe
--                     → 只审计不注入，跑一周看 §4 对比
--   第 2 步（放开）: CONSTRAINT_INJECT_MODE=enforce → 实际注入 prompt 红线段
--   回滚（一键）  : CONSTRAINT_ENABLED=False → 通道短路，回到现状，零残留
