/**
 * 通知中心 API 封装
 * 对接后端 notifications.py 路由
 * 功能：通知列表 / 未读计数 / SSE 实时推送 / 标记已读
 */
import { getData, putData, postData } from '../api';

const BASE = '/api/v1/notifications';

// ===== 类型定义 =====

export type NotificationType = 'digest' | 'update' | 'gap';

export interface Notification {
  id: string;
  user_id: string;
  type: NotificationType;
  title: string;
  content: string;
  source_id: string | null;
  source_type: string | null;
  read: boolean;
  created_at: string;
}

// ===== API 方法 =====

/** 获取通知列表 */
export function getNotifications(params: { unread_only?: boolean; limit?: number } = {}): Promise<Notification[]> {
  return getData<Notification[]>(BASE, params as Record<string, string | number | boolean>);
}

/** 获取未读通知数量 */
export function getUnreadCount(): Promise<number> {
  return getData<number>(`${BASE}/unread-count`);
}

/** 标记单条通知已读 */
export function markAsRead(notificationId: string): Promise<void> {
  return putData<void>(`${BASE}/${notificationId}/read`);
}

/** 标记所有通知已读 */
export function markAllAsRead(): Promise<void> {
  return putData<void>(`${BASE}/read-all`);
}

/** 手动触发知识日报 */
export function triggerDigest(): Promise<unknown[]> {
  return postData<unknown[]>(`${BASE}/trigger-digest`);
}

/** 手动触发知识缺口预警 */
export function triggerGapAlert(): Promise<{ notified: number }> {
  return postData<{ notified: number }>(`${BASE}/trigger-gap-alert`);
}

// ===== SSE 实时推送 =====

/**
 * 创建 SSE 通知推送连接
 * 后端 30 秒心跳保活，返回 EventSource 和清理函数
 */
export function createNotificationStream(
  onMessage: (notification: Notification) => void,
  onError?: (error: Event) => void
): { close: () => void } {
  const token = localStorage.getItem('ekb_access_token');
  const apiBase = import.meta.env.PUBLIC_API_BASE || 'http://localhost:8000';

  const eventSource = new EventSource(`${apiBase}${BASE}/stream?token=${token}`);

  eventSource.onmessage = (event) => {
    try {
      const data = JSON.parse(event.data);
      if (data.type === 'notification' && data.data) {
        onMessage(data.data as Notification);
      }
    } catch {
      // 忽略心跳等非 JSON 消息
    }
  };

  if (onError) {
    eventSource.onerror = onError;
  }

  return {
    close: () => eventSource.close(),
  };
}
