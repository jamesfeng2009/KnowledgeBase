/**
 * @file types.ts
 * @description Yjs 协作服务类型定义。包含文档状态、协作者 awareness、
 *              协作消息与文档评论通知消息的核心类型。
 */

import type { WebSocket } from 'ws';
import type * as Y from 'yjs';

/**
 * 文档运行时状态。缓存所有活跃文档的 Yjs Doc、连接集合与最后修改时间。
 */
export interface DocState {
  /** Yjs CRDT 文档实例，承载文档全量内容 */
  ydoc: Y.Doc;
  /** 当前订阅该文档的 WebSocket 连接集合 */
  connections: Set<WebSocket>;
  /** 文档最后一次被修改的时间戳（ms） */
  lastModified: number;
}

/**
 * 协作者光标 / 选区状态。用于在多端实时显示其他协作者位置。
 */
export interface AwarenessState {
  /** Yjs 客户端 ID（与 Y.Doc.clientID 对应） */
  clientId: number;
  /** 协作者展示名 */
  name: string;
  /** 协作者头像/标识颜色（HEX，如 #4f46e5） */
  color: string;
  /** 光标坐标，无光标时为 null */
  cursor: { x: number; y: number } | null;
  /** 文本选区范围，无选区时为 null */
  selection: { start: number; end: number } | null;
}

/**
 * 协作消息类型。payload 取决于 type：
 *  - 'sync'      → Yjs 同步二进制（Uint8Array）
 *  - 'awareness' → 协作者状态（AwarenessState）
 *  - 'query'     → 查询字符串（如查询在线协作者）
 */
export interface CollabMessage {
  type: 'sync' | 'awareness' | 'query';
  payload: Uint8Array | AwarenessState | string;
}

/**
 * 文档评论数据负载。
 */
export interface CommentData {
  /** 文档 ID */
  doc_id: string;
  /** 评论 ID */
  comment_id: string;
  /** 评论作者（用户名） */
  author: string;
  /** 评论正文 */
  content: string;
  /** 创建时间（ISO 8601 字符串） */
  created_at: string;
}

/**
 * 文档评论通知消息。
 * 客户端 → 服务端：subscribe / unsubscribe / new_comment / resolve_comment / delete_comment
 * 服务端 → 客户端：new_comment / comment_resolved / comment_deleted / subscribed
 */
export interface CommentMessage {
  type:
    | 'subscribe'
    | 'unsubscribe'
    | 'new_comment'
    | 'resolve_comment'
    | 'delete_comment'
    | 'comment_resolved'
    | 'comment_deleted'
    | 'subscribed';
  /** 消息负载，结构随 type 变化 */
  data: CommentData | CommentSubscribeData;
}

/**
 * 评论订阅数据负载。
 */
export interface CommentSubscribeData {
  /** 要订阅 / 取消订阅的文档 ID */
  doc_id: string;
}

/**
 * 已解码的 JWT 用户信息。签名验证由 APISIX 网关完成，
 * 本服务仅解析 payload 以获取用户身份。
 */
export interface JwtPayload {
  /** 用户 ID（sub 字段） */
  sub?: string;
  /** 用户邮箱 */
  email?: string;
  /** 用户展示名 */
  name?: string;
  /** 用户角色（admin/editor/viewer 等） */
  role?: string;
  /** 租户 ID */
  tenant_id?: string;
  /** 过期时间（Unix 秒） */
  exp?: number;
  /** 签发时间（Unix 秒） */
  iat?: number;
}
