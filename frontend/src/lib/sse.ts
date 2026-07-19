/**
 * SSE (Server-Sent Events) 流式输出工具
 * 支持 POST 请求 + SSE 流式响应读取
 *
 * NOTE: chat/index.astro 使用内联 fetch + ReadableStream 实现 SSE，未引用本封装；
 * 但 admin/feedback.astro 已通过 streamChat 使用本封装。保留供 P4-3 统一
 * 迁移 chat 页面 SSE 实现到本封装时复用。
 */

import { API_BASE } from './api';

/** Token 在 localStorage 中的存储键名 */
const TOKEN_KEY = 'ekb_access_token';

/** SSE 事件数据 */
export interface SSEEvent {
  /** 事件类型（如 token, sources, done, error） */
  type: string;
  /** 事件数据 */
  data: unknown;
}

/** 从 localStorage 读取 Token */
function getToken(): string | null {
  if (typeof window === 'undefined') return null;
  return localStorage.getItem(TOKEN_KEY);
}

/**
 * 创建 SSE 流式读取
 * 通过 POST 请求发送数据，并以 SSE 方式读取流式响应
 *
 * @param url - 请求路径（相对路径会拼接 API_BASE）
 * @param body - 请求体数据
 * @returns AsyncGenerator，yield 解析后的 SSE 事件
 *
 * @example
 * ```typescript
 * for await (const event of createSSEStream('/api/v1/chat/stream', { query: '什么是知识库?' })) {
 *   if (event.type === 'token') {
 *     console.log(event.data); // 输出流式文本
 *   } else if (event.type === 'done') {
 *     console.log('流式输出完成');
 *   }
 * }
 * ```
 */
export async function* createSSEStream(
  url: string,
  body: unknown
): AsyncGenerator<SSEEvent, void, unknown> {
  const fullUrl = url.startsWith('http') ? url : `${API_BASE}${url}`;
  const token = getToken();

  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    Accept: 'text/event-stream',
  };

  if (token) {
    headers['Authorization'] = `Bearer ${token}`;
  }

  const response = await fetch(fullUrl, {
    method: 'POST',
    headers,
    body: JSON.stringify(body),
  });

  // 检查响应状态
  if (!response.ok) {
    const errorData = await response.json().catch(() => null);
    const message =
      (errorData as { message?: string })?.message ||
      `SSE 连接失败 (${response.status})`;
    throw new Error(message);
  }

  // 检查响应体是否存在
  if (!response.body) {
    throw new Error('响应流不可用');
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  try {
    while (true) {
      const { done, value } = await reader.read();

      if (done) {
        // 处理缓冲区中剩余的数据
        if (buffer.trim()) {
          const event = parseSSELine(buffer);
          if (event) {
            yield event;
          }
        }
        break;
      }

      // 将 Uint8Array 解码并追加到缓冲区
      buffer += decoder.decode(value, { stream: true });

      // 按换行符分割，逐行处理 SSE 事件
      // SSE 事件以空行分隔（两个连续换行）
      const lines = buffer.split('\n');
      buffer = lines.pop() || '';

      let currentEvent = '';
      let currentData = '';

      for (const line of lines) {
        const trimmed = line.trim();

        // 空行表示一个事件结束
        if (trimmed === '') {
          if (currentData || currentEvent) {
            const event = parseSSEData(currentEvent, currentData);
            if (event) {
              yield event;
            }
            currentEvent = '';
            currentData = '';
          }
          continue;
        }

        // 解析 SSE 字段
        if (trimmed.startsWith('event:')) {
          currentEvent = trimmed.slice(6).trim();
        } else if (trimmed.startsWith('data:')) {
          const dataLine = trimmed.slice(5).trim();
          currentData = currentData ? `${currentData}\n${dataLine}` : dataLine;
        }
      }
    }
  } finally {
    reader.releaseLock();
  }
}

/** 解析单行 SSE 数据（兼容旧格式） */
function parseSSELine(line: string): SSEEvent | null {
  const trimmed = line.trim();
  if (!trimmed.startsWith('data:')) return null;
  return parseSSEData('', trimmed.slice(5).trim());
}

/** 解析 SSE event + data 字段 */
function parseSSEData(event: string, data: string): SSEEvent | null {
  if (!data) return null;

  try {
    const parsed = JSON.parse(data);
    // 优先使用 SSE event 字段，其次使用数据中的 type 字段
    return {
      type: event || (parsed as { type?: string }).type || 'message',
      data: parsed,
    };
  } catch {
    // 非 JSON 数据，作为纯文本返回
    return {
      type: event || 'message',
      data,
    };
  }
}

/**
 * 简化的流式聊天函数
 * 提供回调式 API，便于在组件中使用
 *
 * @param url - SSE 接口地址
 * @param body - 请求体
 * @param callbacks - 事件回调
 */
export async function streamChat(
  url: string,
  body: unknown,
  callbacks: {
    onChunk?: (text: string) => void;
    onSources?: (sources: unknown[]) => void;
    onDone?: () => void;
    onError?: (error: Error) => void;
  }
): Promise<void> {
  try {
    for await (const event of createSSEStream(url, body)) {
      switch (event.type) {
        case 'token':
        case 'chunk':
          callbacks.onChunk?.((event.data as { text?: string }).text || String(event.data));
          break;
        case 'sources':
          callbacks.onSources?.(event.data as unknown[]);
          break;
        case 'done':
        case 'complete':
          callbacks.onDone?.();
          break;
        case 'error':
          throw new Error((event.data as { message?: string }).message || '流式输出错误');
      }
    }
    callbacks.onDone?.();
  } catch (error) {
    callbacks.onError?.(error instanceof Error ? error : new Error(String(error)));
  }
}
