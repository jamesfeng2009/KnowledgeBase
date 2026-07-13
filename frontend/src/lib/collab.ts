/**
 * Yjs 协同 Provider 管理
 * 负责 Yjs 文档、WebSocket 连接、离线缓存与协作者光标颜色分配
 */
import * as Y from 'yjs';
import { WebsocketProvider } from 'y-websocket';
import { IndexeddbPersistence } from 'y-indexeddb';

/** 协作者光标颜色池（12 种稳定颜色） */
export const CURSOR_COLORS = [
  '#FF6B6B', '#4ECDC4', '#45B7D1', '#FFA07A',
  '#98D8C8', '#F7DC6F', '#BB8FCE', '#85C1E2',
  '#F1948A', '#82E0AA', '#F8B739', '#AED6F1',
];

/** 文档评论数据结构 */
export interface Comment {
  id: string;
  doc_id: string;
  user_id: string;
  user_name: string;
  content: string;
  created_at: string;
}

/**
 * 基于用户 ID 哈希分配稳定的光标颜色
 * 同一用户在任何设备上都会得到相同颜色
 *
 * @param userId - 用户唯一标识
 * @returns 16 进制颜色字符串
 */
export function getUserColor(userId: string): string {
  let hash = 0;
  for (let i = 0; i < userId.length; i++) {
    hash = ((hash << 5) - hash) + userId.charCodeAt(i);
    hash |= 0;
  }
  return CURSOR_COLORS[Math.abs(hash) % CURSOR_COLORS.length];
}

/**
 * 创建协同编辑 Provider
 * 包含 Yjs 文档、WebSocket 连接与 IndexedDB 离线缓存
 *
 * @param docId - 文档 ID，作为 Yjs 房间名
 * @param wsUrl - WebSocket 服务基础地址
 * @param token - JWT 认证 Token
 * @returns ydoc 与 provider
 */
export function createCollabProvider(
  docId: string,
  wsUrl: string,
  token: string
): { ydoc: Y.Doc; provider: WebsocketProvider } {
  const ydoc = new Y.Doc();

  const provider = new WebsocketProvider(
    `${wsUrl}/ws/collab`,
    docId,
    ydoc,
    {
      params: { token },
      connect: true,
      // 重连最大间隔 30s（指数退避），对应 y-websocket 的 maxBackoffTime 选项
      maxBackoffTime: 30000,
      resyncInterval: 5000,
    }
  );

  // 离线持久化缓存，断网时仍可编辑，恢复后自动同步
  new IndexeddbPersistence(docId, ydoc);

  return { ydoc, provider };
}

/**
 * 订阅文档新评论通知
 * 通过独立 WebSocket 连接接收评论事件，与协同编辑连接分离
 *
 * @param docId - 文档 ID
 * @param wsUrl - WebSocket 服务基础地址
 * @param token - JWT 认证 Token
 * @param onNewComment - 收到新评论时的回调
 * @returns 清理函数，调用后关闭连接
 */
export function subscribeComments(
  docId: string,
  wsUrl: string,
  token: string,
  onNewComment: (comment: Comment) => void
): () => void {
  const params = new URLSearchParams({ doc_id: docId, token });
  const ws = new WebSocket(`${wsUrl}/ws/comments?${params.toString()}`);

  ws.onmessage = (event: MessageEvent<string>) => {
    try {
      const msg = JSON.parse(event.data) as { type: string; data: Comment };
      if (msg.type === 'new_comment') {
        onNewComment(msg.data);
      }
    } catch {
      // 忽略无法解析的消息
    }
  };

  return () => {
    if (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING) {
      ws.close();
    }
  };
}

export default {
  CURSOR_COLORS,
  getUserColor,
  createCollabProvider,
  subscribeComments,
};
