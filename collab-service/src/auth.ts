/**
 * @file auth.ts
 * @description 协作服务的鉴权与授权。
 *
 * ## 为什么需要这个模块（安全修复背景）
 *
 * 修复前 `index.ts` 里的 `verifyToken()` **只做 base64 解码，不做签名验证**，
 * 注释写的是"签名由 APISIX 验证"。这句话有两处不成立：
 *
 * 1. `infra/apisix/apisix.yml` 里只配了 `/api/*` 一条路由，**没有**到 yjs-server
 *    的 WebSocket 路由，也没有任何 JWT 校验插件 —— 网关根本没有参与鉴权；
 * 2. `docker-compose.yml` 把 `8001:8001` 直接发布到宿主机，攻击者可以完全
 *    绕过网关直连协作服务。
 *
 * 两者叠加的后果：**任何人构造 `{"sub":"<任意用户名>"}` 的伪造 JWT 就能
 * 打开任意文档的协同编辑连接**（既不验签，也不校验文档归属，IDOR）。
 *
 * ## 现在的做法
 *
 * - `verifyJwt()`：本地 HS256 验签（与后端 `app/utils/crypto.py` 同一 SECRET_KEY），
 *   常量时间比较，拒绝 `alg: none` / 非 HS256 / 过期 token；
 * - `authorizeDocument()`：连 PG 查文档 + 知识库 + 成员 + 用户密级，按后端
 *   `PermissionService.check_write` 的同一套规则判定，杜绝跨租户 / 越权 IDOR。
 *
 * 密钥缺失时服务**拒绝所有连接**（fail-closed），不再是"默认放行"。
 */

import { createHmac, timingSafeEqual } from 'node:crypto';
import type { Pool } from 'pg';
import type { JwtPayload } from './types.js';

/** 与后端保持一致：密级权重越大越敏感 */
const CLEARANCE_ORDER: Record<string, number> = {
  public: 0,
  internal: 1,
  confidential: 2,
  secret: 3,
};

/** 允许写协作文档的成员角色（对齐后端 PermissionService.check_write） */
const WRITE_ROLES = new Set(['admin', 'editor']);

/** JWT 校验结果 */
export type VerifyResult =
  | { ok: true; payload: JwtPayload }
  | { ok: false; reason: string };

/**
 * base64url 解码为 Buffer。
 * @param input base64url 字符串
 */
function b64urlDecode(input: string): Buffer {
  return Buffer.from(input, 'base64url');
}

/**
 * 校验 HS256 JWT 签名与有效期。
 *
 * 只接受 HS256：若允许 `none` 或非对称算法，攻击者可以换算法头绕过验签
 * （经典 JWT 算法混淆攻击）。
 *
 * @param token JWT 字符串
 * @param secret 与后端共享的 SECRET_KEY；为空时 fail-closed
 * @param nowMs 当前时间（毫秒），便于测试注入
 * @returns 校验结果；失败时 reason 说明原因（不含 token 内容，避免进日志）
 */
export function verifyJwt(
  token: string | null,
  secret: string | undefined,
  nowMs: number = Date.now()
): VerifyResult {
  if (!secret) {
    return { ok: false, reason: 'server_misconfigured: SECRET_KEY 未设置' };
  }
  if (!token) {
    return { ok: false, reason: 'missing_token' };
  }

  const parts = token.split('.');
  if (parts.length !== 3) {
    return { ok: false, reason: 'malformed_token' };
  }
  const [headerB64, payloadB64, signatureB64] = parts;

  let header: { alg?: string; typ?: string };
  try {
    header = JSON.parse(b64urlDecode(headerB64).toString('utf8')) as {
      alg?: string;
      typ?: string;
    };
  } catch {
    return { ok: false, reason: 'malformed_header' };
  }

  if (header.alg !== 'HS256') {
    return { ok: false, reason: `unsupported_alg: ${String(header.alg)}` };
  }

  const expected = createHmac('sha256', secret)
    .update(`${headerB64}.${payloadB64}`)
    .digest();
  const actual = b64urlDecode(signatureB64);

  // 长度不等时 timingSafeEqual 会抛异常，先比长度（长度本身不是秘密）
  if (actual.length !== expected.length) {
    return { ok: false, reason: 'bad_signature' };
  }
  if (!timingSafeEqual(actual, expected)) {
    return { ok: false, reason: 'bad_signature' };
  }

  let payload: JwtPayload;
  try {
    payload = JSON.parse(b64urlDecode(payloadB64).toString('utf8')) as JwtPayload;
  } catch {
    return { ok: false, reason: 'malformed_payload' };
  }

  const nowSec = nowMs / 1000;
  if (typeof payload.exp === 'number' && nowSec > payload.exp) {
    return { ok: false, reason: 'token_expired' };
  }
  // 时钟偏斜容忍 60s，防止刚签发就判过期
  if (typeof payload.nbf === 'number' && nowSec + 60 < payload.nbf) {
    return { ok: false, reason: 'token_not_yet_valid' };
  }

  return { ok: true, payload };
}

/**
 * 从 Cookie 头中读取 access_token。
 *
 * 只认 Cookie，不认 URL query：浏览器 WebSocket 无法自定义请求头，Cookie 是
 * 唯一"不进访问日志"的通道（query 里的 token 会被 nginx/网关 access_log 记下来）。
 * 前端的 `?token=` 代码路径已删除，两端契约以 Cookie 为唯一事实来源。
 *
 * @param cookieHeader HTTP Cookie 头
 * @returns JWT 字符串；不存在时 null
 */
export function getCookieToken(cookieHeader: string | undefined): string | null {
  if (!cookieHeader) return null;
  const match = cookieHeader.match(/(?:^|;\s*)access_token=([^;]+)/);
  return match ? decodeURIComponent(match[1]) : null;
}

/** 文档授权结果 */
export interface DocAuthorization {
  /** 是否允许建立协作连接 */
  allowed: boolean;
  /** 拒绝原因（用于 close code 与日志，不含敏感信息） */
  reason?: string;
  /** 文档所属租户（用于日志） */
  tenantId?: string | null;
}

/** UUID 形态校验 —— 非 UUID 直接拒绝，避免把脏参数送进 SQL 的 ::uuid 转换 */
const UUID_RE = /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/;

/**
 * 校验用户对文档的访问权限。
 *
 * - ``mode='write'``（协同编辑）：规则与后端 ``PermissionService.check_write`` 对齐 ——
 *   admin / 知识库 owner / kb_members.role ∈ {admin, editor} 才放行；
 * - ``mode='read'``（评论订阅）：在上述基础上追加"只读成员 + public 库 + 同部门 dept 库"，
 *   对齐后端 ``check_function``。
 *
 * 两条路径都要过密级与租户校验，避免"能连上就能看"的越权。
 *
 * @param pool PG 连接池
 * @param docId 文档 ID（y-websocket 的 room 名）
 * @param userId JWT 的 sub
 * @param mode 权限类型
 * @returns 授权结果
 */
export async function authorizeDocument(
  pool: Pool | null,
  docId: string,
  userId: string | undefined,
  mode: 'read' | 'write' = 'write'
): Promise<DocAuthorization> {
  if (!userId || !UUID_RE.test(userId)) {
    return { allowed: false, reason: 'invalid_user_id' };
  }
  // "default" 是历史遗留的无 doc 参数兜底房间，不对应任何真实文档 —— 直接拒绝
  if (!UUID_RE.test(docId)) {
    return { allowed: false, reason: 'invalid_doc_id' };
  }
  if (!pool) {
    // PG 不可用时无法判权：fail-closed，宁可协作不可用也不放行越权连接
    return { allowed: false, reason: 'authorization_unavailable' };
  }

  const { rows } = await pool.query(
    `SELECT d.id,
            d.deleted_at,
            d.classification,
            d.tenant_id            AS doc_tenant_id,
            kb.owner_id            AS kb_owner_id,
            kb.visibility          AS kb_visibility,
            kb.dept_id             AS kb_dept_id,
            u.role                 AS user_role,
            u.clearance_level      AS user_clearance,
            u.tenant_id            AS user_tenant_id,
            u.dept_id              AS user_dept_id,
            km.role                AS member_role
       FROM documents d
       JOIN knowledge_bases kb ON kb.id = d.kb_id AND kb.deleted_at IS NULL
       LEFT JOIN users u ON u.id = $2::uuid
       LEFT JOIN kb_members km ON km.kb_id = kb.id AND km.user_id = $2::uuid
      WHERE d.id = $1::uuid`,
    [docId, userId]
  );

  const row = rows[0];
  if (!row) {
    return { allowed: false, reason: 'document_not_found' };
  }
  if (row.deleted_at !== null) {
    return { allowed: false, reason: 'document_deleted' };
  }
  if (!row.user_role) {
    // 用户已被删除但 token 未过期 —— 不能再编辑任何文档
    return { allowed: false, reason: 'user_not_found' };
  }

  // 跨租户拒绝（doc 有租户、用户有租户且不一致）
  if (
    row.doc_tenant_id &&
    row.user_tenant_id &&
    String(row.doc_tenant_id) !== String(row.user_tenant_id)
  ) {
    return { allowed: false, reason: 'tenant_mismatch' };
  }

  const isAdmin = row.user_role === 'admin';
  const isKbOwner = String(row.kb_owner_id) === userId;
  const memberRole = row.member_role ? String(row.member_role) : null;
  const isWriter = memberRole !== null && WRITE_ROLES.has(memberRole);

  let permitted = isAdmin || isKbOwner || isWriter;
  if (!permitted && mode === 'read') {
    // 读权限额外分支（对齐后端 check_function）
    const visibility = String(row.kb_visibility ?? 'private');
    const sameDept =
      row.kb_dept_id !== null &&
      row.user_dept_id !== null &&
      String(row.kb_dept_id) === String(row.user_dept_id);
    permitted = memberRole !== null || visibility === 'public' || (visibility === 'dept' && sameDept);
  }

  if (!permitted) {
    return {
      allowed: false,
      reason: `forbidden: mode=${mode} member_role=${memberRole ?? 'none'}`,
    };
  }

  // 密级：admin 不受限制，其他用户按权重比较
  if (!isAdmin) {
    const need = CLEARANCE_ORDER[String(row.classification ?? 'internal')] ?? 1;
    const have = CLEARANCE_ORDER[String(row.user_clearance ?? 'internal')] ?? 1;
    if (need > have) {
      return { allowed: false, reason: 'insufficient_clearance' };
    }
  }

  return { allowed: true, tenantId: row.doc_tenant_id ? String(row.doc_tenant_id) : null };
}
