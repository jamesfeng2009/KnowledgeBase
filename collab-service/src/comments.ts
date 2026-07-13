/**
 * @file comments.ts
 * @description 文档评论通知服务。处理 /ws/comments 路径的 WebSocket 连接，
 *              管理每个文档的评论订阅者，并在新评论到达时广播给所有订阅者。
 *
 * 协议（JSON 文本消息）：
 *  客户端 → 服务端：
 *   { type: 'subscribe',   data: { doc_id } }
 *   { type: 'unsubscribe', data: { doc_id } }
 *   { type: 'new_comment',     data: { doc_id, comment_id, author, content, created_at } }
 *   { type: 'resolve_comment', data: { doc_id, comment_id, ... } }
 *   { type: 'delete_comment',  data: { doc_id, comment_id, ... } }
 *  服务端 → 客户端：
 *   { type: 'subscribed',       data: { doc_id } }
 *   { type: 'new_comment',      data: { doc_id, comment_id, author, content, created_at } }
 *   { type: 'comment_resolved', data: { doc_id, comment_id } }
 *   { type: 'comment_deleted',  data: { doc_id, comment_id } }
 */

import { WebSocket } from 'ws';
import type { IncomingMessage } from 'http';
import type { CommentMessage, CommentData, JwtPayload } from './types.js';

/**
 * 文档评论 WebSocket 处理器。维护按文档分组的订阅者集合，
 * 并提供新评论广播能力。
 */
export class CommentsService {
  /** 每个文档的评论订阅者：docId → 连接集合 */
  private subscribers = new Map<string, Set<WebSocket>>();

  /**
   * 处理新的评论 WebSocket 连接。
   * @param conn WebSocket 连接
   * @param _req HTTP 升级请求（含 JWT，已在入口校验）
   * @param user 已认证的用户信息（可选）
   */
  handleConnection(conn: WebSocket, _req: IncomingMessage, user?: JwtPayload): void {
    // 记录连接关联的用户，便于审计日志
    const username = user?.name ?? user?.sub ?? 'unknown';
    conn.on('message', (raw: Buffer | ArrayBuffer | Uint8Array | string) => {
      try {
        const text = typeof raw === 'string' ? raw : Buffer.from(raw as Buffer).toString('utf8');
        const message = JSON.parse(text) as CommentMessage;
        this.handleMessage(conn, message, username);
      } catch (err) {
        console.error('[comments] 消息解析失败:', err);
        this.send(conn, { type: 'comment_deleted', data: { doc_id: '' } }); // error sentinel
      }
    });

    conn.on('close', () => this.handleClose(conn));
    conn.on('error', (err: Error) =>
      console.error('[comments] WebSocket 错误:', err.message)
    );
  }

  /**
   * 处理客户端评论消息。
   */
  private handleMessage(conn: WebSocket, message: CommentMessage, username: string): void {
    const { type, data } = message;
    const docId = (data as { doc_id?: string })?.doc_id;
    if (!docId) {
      console.warn('[comments] 缺少 doc_id:', type);
      return;
    }

    switch (type) {
      case 'subscribe':
        this.subscribe(docId, conn);
        this.send(conn, { type: 'subscribed', data: { doc_id: docId } });
        console.log(`[comments] 用户 ${username} 订阅文档 ${docId}`);
        break;
      case 'unsubscribe':
        this.unsubscribe(docId, conn);
        break;
      case 'new_comment':
        // 广播给该文档所有订阅者（含发送者，便于多端同步）
        this.broadcast(docId, { type: 'new_comment', data: data as CommentData });
        console.log(
          `[comments] 新评论 (doc=${docId}) 作者=${(data as CommentData).author}`
        );
        break;
      case 'resolve_comment':
        this.broadcast(docId, {
          type: 'comment_resolved',
          data: data as CommentData,
        });
        break;
      case 'delete_comment':
        this.broadcast(docId, {
          type: 'comment_deleted',
          data: data as CommentData,
        });
        break;
      default:
        console.warn('[comments] 未知消息类型:', type);
    }
  }

  /**
   * 订阅文档评论。
   */
  private subscribe(docId: string, conn: WebSocket): void {
    let set = this.subscribers.get(docId);
    if (!set) {
      set = new Set();
      this.subscribers.set(docId, set);
    }
    set.add(conn);
  }

  /**
   * 取消订阅文档评论。
   */
  private unsubscribe(docId: string, conn: WebSocket): void {
    this.subscribers.get(docId)?.delete(conn);
  }

  /**
   * 广播消息给文档的所有订阅者。
   * @param docId 文档 ID
   * @param message 评论消息
   */
  private broadcast(docId: string, message: CommentMessage): void {
    const set = this.subscribers.get(docId);
    if (!set) return;
    const payload = JSON.stringify(message);
    for (const conn of set) {
      if (conn.readyState === WebSocket.OPEN) {
        conn.send(payload);
      }
    }
  }

  /**
   * 处理连接关闭：从所有订阅集合中移除该连接，并清理空集合。
   */
  handleClose(conn: WebSocket): void {
    for (const [docId, set] of this.subscribers) {
      if (set.delete(conn) && set.size === 0) {
        this.subscribers.delete(docId);
      }
    }
  }

  /**
   * 向单个连接发送 JSON 消息。
   */
  private send(conn: WebSocket, message: CommentMessage): void {
    if (conn.readyState === WebSocket.OPEN) {
      conn.send(JSON.stringify(message));
    }
  }
}
