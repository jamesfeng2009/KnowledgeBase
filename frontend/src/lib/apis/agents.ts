/**
 * Agent 管理 API 封装
 * 对接后端 agents.py 路由
 */
import { getData, postData, putData } from '../api';

const BASE = '/api/v1/agents';

// ===== 类型定义 =====

export interface Agent {
  id: string;
  name: string;
  type: string;
  description: string;
  config: Record<string, unknown>;
  enabled: boolean;
  created_at: string;
  updated_at: string;
}

export interface AgentInvokeRequest {
  query: string;
  session_id?: string;
  context?: Record<string, unknown>;
}

// ===== API 方法 =====

/** 获取 Agent 列表（仅已启用） */
export function getAgents(): Promise<Agent[]> {
  return getData<Agent[]>(BASE);
}

/** 获取 Agent 详情 */
export function getAgent(agentId: string): Promise<Agent> {
  return getData<Agent>(`${BASE}/${agentId}`);
}

/** 创建自定义 Agent */
export function createAgent(data: { name: string; type: string; description?: string; config?: Record<string, unknown> }): Promise<Agent> {
  return postData<Agent>(BASE, data);
}

/**
 * 更新 Agent 配置
 *
 * NOTE: 当前 dead code。chat/agent.astro 已实现创建/查看/调用 Agent，
 * 但尚未提供编辑入口。保留供 P4/P5 Agent 管理页编辑功能对接使用。
 */
export function updateAgent(agentId: string, data: Partial<Agent>): Promise<Agent> {
  return putData<Agent>(`${BASE}/${agentId}`, data);
}

/**
 * 调用 Agent — SSE 流式响应
 * 返回 AsyncGenerator，yield 每个 content chunk
 */
export async function* invokeAgent(
  agentId: string,
  request: AgentInvokeRequest
): AsyncGenerator<string> {
  const apiBase = import.meta.env.PUBLIC_API_BASE || 'http://localhost:8000';

  const response = await fetch(`${apiBase}${BASE}/${agentId}/invoke`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    credentials: 'include', // 必须：携带 HttpOnly Cookie
    body: JSON.stringify(request),
  });

  if (response.status === 401) {
    window.location.href = '/auth/login';
    throw new Error('登录已过期');
  }

  if (!response.ok) {
    throw new Error(`Agent 调用失败 (${response.status})`);
  }

  const reader = response.body!.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    const lines = buffer.split('\n');
    buffer = lines.pop() || '';

    for (const line of lines) {
      if (line.startsWith('data: ')) {
        try {
          const data = JSON.parse(line.slice(6));
          if (data.content) yield data.content;
          if (data.done) return;
        } catch {
          // 忽略非 JSON 行
        }
      }
    }
  }
}
