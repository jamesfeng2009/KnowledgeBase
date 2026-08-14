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
 * 后端 30 秒心跳保活，返回清理函数
 *
 * 安全说明：Token 不再拼在 URL 查询参数（会进入访问日志 / 浏览器历史）。
 * 原生 EventSource 不支持自定义请求头，且后端仅接受 Authorization: Bearer
 * 头鉴权（OAuth2PasswordBearer），故改用 fetch + ReadableStream 实现 SSE。
 * 注意：fetch 实现无浏览器自动重连，断线后由调用方决定是否重建连接。
 */
export function createNotificationStream(
  onMessage: (notification: Notification) => void,
  onError?: (error: Event) => void
): { close: () => void } {
  const apiBase = import.meta.env.PUBLIC_API_BASE || 'http://localhost:8000';

  // 通过 AbortController 实现 close()，中止进行中的流读取
  const abortController = new AbortController();

  void (async () => {
    try {
      // P0 安全修复：认证通过 HttpOnly Cookie 自动携带
      const res = await fetch(`${apiBase}${BASE}/stream`, {
        headers: {
          Accept: 'text/event-stream',
        },
        credentials: 'include',
        signal: abortController.signal,
      });

      if (res.status === 401) {
        // 与 api.ts 行为对齐：401 跳转登录页，Token 由服务端 HttpOnly Cookie 管理
        if (window.location.pathname !== '/auth/login') {
          window.location.href = '/auth/login';
        }
        return;
      }
      if (!res.ok || !res.body) {
        throw new Error(`通知推送流连接失败 (${res.status})`);
      }

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      // 事件解析状态必须跨 reader.read() 保持（SSE 事件可能被 TCP 分包拆到多次 read 中）
      let currentData = '';

      // 空行表示一个事件结束，派发累积的 data 内容
      const dispatchEvent = () => {
        if (!currentData) return;
        const raw = currentData;
        currentData = '';
        try {
          const data = JSON.parse(raw);
          if (data.type === 'notification' && data.data) {
            onMessage(data.data as Notification);
          }
        } catch {
          // 忽略心跳等非 JSON 消息
        }
      };

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() || '';
        for (const line of lines) {
          const trimmed = line.trim();
          if (trimmed === '') {
            dispatchEvent();
          } else if (trimmed.startsWith('data:')) {
            const d = trimmed.slice(5).trim();
            currentData = currentData ? `${currentData}\n${d}` : d;
          }
        }
      }
      dispatchEvent(); // 冲刷流末尾未派发的事件
    } catch (err) {
      // 主动 close() 触发的中止不属于错误
      if (!abortController.signal.aborted) {
        console.warn('[Notifications] SSE 通知流异常:', err);
        onError?.(new Event('error'));
      }
    }
  })();

  return {
    close: () => abortController.abort(),
  };
}
