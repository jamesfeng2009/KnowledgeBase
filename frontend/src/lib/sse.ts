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

  // 401 未授权：清除 Token 并跳转登录页（与 api.ts 的 request() 行为对齐）
  if (response.status === 401) {
    console.warn('[SSE] 401 未授权，清除 Token 并跳转登录页');
    if (typeof window !== 'undefined') {
      localStorage.removeItem(TOKEN_KEY);
      if (window.location.pathname !== '/auth/login') {
        window.location.href = '/auth/login';
      }
    }
    throw new Error('登录已过期，请重新登录');
  }

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
  // 事件解析状态必须跨 reader.read() 保持：
  // 一个 SSE 事件可能被 TCP 分包拆到多次 read() 中，若状态声明在循环内会被重置，导致事件静默丢失
  let currentEvent = '';
  let currentData = '';

  try {
    while (true) {
      const { done, value } = await reader.read();

      if (done) {
        // 冲刷缓冲区中最后一行（可能是不含换行的事件行）
        const lastLine = buffer.trim();
        if (lastLine.startsWith('event:')) {
          currentEvent = lastLine.slice(6).trim();
        } else if (lastLine.startsWith('data:')) {
          const dataLine = lastLine.slice(5).trim();
          currentData = currentData ? `${currentData}\n${dataLine}` : dataLine;
        }
        // 冲刷未派发完的最后一个事件
        if (currentData || currentEvent) {
          const event = parseSSEData(currentEvent, currentData);
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
 * P0-6: 扩展回调支持 thinking / retrieve / tool_call / quality 事件。
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
    onThinking?: (data: { content?: string; iteration?: number }) => void;
    onRetrieveStart?: (data: { query?: string; iteration?: number }) => void;
    onRetrieveEnd?: (data: { doc_count?: number; iteration?: number }) => void;
    onToolCallStart?: (data: { tool_name: string; tool_use_id: string; arguments?: unknown }) => void;
    onToolCallEnd?: (data: { tool_use_id: string; tool_name?: string; result?: string; duration_ms?: number; status?: string }) => void;
    onApprovalRequired?: (data: { approval_id: string; tool_name: string; tool_use_id: string; arguments?: unknown; reason?: string; irreversible?: boolean; session_id?: string }) => void;
    onQuality?: (data: { low_confidence?: boolean; total_score?: number; message?: string }) => void;
    /** P2-B: 查询重写事件 — 展示检索前的查询优化过程 */
    onQueryRewrite?: (data: {
      original: string;
      rewritten: string;
      expanded_terms: string[];
      sub_queries: string[];
      hyde_document: string | null;
      strategy: string[];
      latency_ms: number;
      cache_hit: boolean;
      search_query: string;
    }) => void;
    onDone?: () => void;
    onError?: (error: Error) => void;
  }
): Promise<void> {
  // 防止 onDone 被调用两次：服务端 'done' 事件触发一次，流结束兜底一次
  let doneFired = false;
  const fireDone = () => {
    if (!doneFired) {
      doneFired = true;
      callbacks.onDone?.();
    }
  };
  try {
    for await (const event of createSSEStream(url, body)) {
      switch (event.type) {
        case 'token':
        case 'chunk':
        case 'message':
          callbacks.onChunk?.((event.data as { text?: string }).text || String(event.data));
          break;
        case 'sources':
          callbacks.onSources?.(event.data as unknown[]);
          break;
        case 'thinking':
          callbacks.onThinking?.(event.data as { content?: string; iteration?: number });
          break;
        case 'retrieve_start':
          callbacks.onRetrieveStart?.(event.data as { query?: string; iteration?: number });
          break;
        case 'retrieve_end':
          callbacks.onRetrieveEnd?.(event.data as { doc_count?: number; iteration?: number });
          break;
        case 'tool_call_start':
          callbacks.onToolCallStart?.(event.data as { tool_name: string; tool_use_id: string; arguments?: unknown });
          break;
        case 'tool_call_end':
          callbacks.onToolCallEnd?.(event.data as { tool_use_id: string; tool_name?: string; result?: string; duration_ms?: number; status?: string });
          break;
        case 'approval_required':
          callbacks.onApprovalRequired?.(event.data as { approval_id: string; tool_name: string; tool_use_id: string; arguments?: unknown; reason?: string; irreversible?: boolean; session_id?: string });
          break;
        case 'quality':
          callbacks.onQuality?.(event.data as { low_confidence?: boolean; total_score?: number; message?: string });
          break;
        case 'query_rewrite':
          callbacks.onQueryRewrite?.(event.data as {
            original: string;
            rewritten: string;
            expanded_terms: string[];
            sub_queries: string[];
            hyde_document: string | null;
            strategy: string[];
            latency_ms: number;
            cache_hit: boolean;
            search_query: string;
          });
          break;
        case 'done':
        case 'complete':
          fireDone();
          break;
        case 'error':
          throw new Error((event.data as { message?: string }).message || '流式输出错误');
      }
    }
    fireDone();
  } catch (error) {
    callbacks.onError?.(error instanceof Error ? error : new Error(String(error)));
  }
}
