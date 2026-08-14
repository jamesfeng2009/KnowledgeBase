/**
 * 系统健康 API 封装
 * 对接后端 /health/* 路由，包括熔断器状态查询
 */

import { API_BASE } from '../api';

// ===== 类型定义 =====

/** 熔断器状态 */
export type CircuitState = 'closed' | 'open' | 'half_open';

/** 单个熔断器状态快照 */
export interface CircuitBreakerStatus {
  name: string;
  state: CircuitState;
  failure_count: number;
  failure_threshold: number;
  recovery_timeout: number;
  half_open_max_calls: number;
  last_failure_time: number;
  half_open_calls: number;
}

/** 熔断器状态查询响应 */
export interface CircuitBreakerResponse {
  breakers: Record<string, CircuitBreakerStatus>;
  any_open: boolean;
}

// ===== API 方法 =====

/**
 * 获取所有熔断器状态
 * 对接 GET /health/circuit-breakers
 */
export async function getCircuitBreakerStatus(): Promise<CircuitBreakerResponse> {
  // P0 安全修复：认证通过 HttpOnly Cookie 自动携带
  const resp = await fetch(`${API_BASE}/health/circuit-breakers`, {
    credentials: 'include',
  });
  if (!resp.ok) {
    throw new Error(`熔断器状态查询失败: ${resp.status}`);
  }

  const json = await resp.json();
  return json.data as CircuitBreakerResponse;
}

/**
 * 获取熔断器状态文本（用于 UI 展示）
 */
export function getCircuitStateLabel(state: CircuitState): string {
  switch (state) {
    case 'closed': return '正常';
    case 'open': return '熔断中';
    case 'half_open': return '探测中';
    default: return '未知';
  }
}

/**
 * 获取熔断器状态颜色（用于 UI 标签）
 */
export function getCircuitStateColor(state: CircuitState): string {
  switch (state) {
    case 'closed': return 'green';
    case 'open': return 'red';
    case 'half_open': return 'orange';
    default: return 'gray';
  }
}

// ===== Provider 健康检查 =====

/** Provider 类型 */
export type ProviderType = 'embedder' | 'reranker' | 'vectorstore' | 'llm';

/** 单个 Provider 的健康状态 */
export interface ProviderHealth {
  name: string;
  type: ProviderType;
  healthy: boolean;
  latency_ms: number | null;
  error: string | null;
  circuit_state: CircuitState;
  last_check: string;
}

/** Provider 健康检查响应 */
export interface ProviderHealthResponse {
  providers: Record<string, ProviderHealth>;
  source: 'cache' | 'fresh';
  healthy_count: number;
  total: number;
}

/**
 * 获取所有 AI 服务 Provider 健康状态
 * 对接 GET /health/providers（幂等，Redis 缓存优先）
 */
export async function getProviderHealth(): Promise<ProviderHealthResponse> {
  // P0 安全修复：认证通过 HttpOnly Cookie 自动携带
  const resp = await fetch(`${API_BASE}/health/providers`, {
    credentials: 'include',
  });
  if (!resp.ok) {
    throw new Error(`Provider 健康状态查询失败: ${resp.status}`);
  }

  const json = await resp.json();
  return json.data as ProviderHealthResponse;
}

/** Provider 类型中文标签 */
export function getProviderTypeLabel(type: ProviderType): string {
  switch (type) {
    case 'embedder': return '向量嵌入';
    case 'reranker': return '重排器';
    case 'vectorstore': return '向量存储';
    case 'llm': return '大语言模型';
    default: return type;
  }
}

/** Provider 健康状态标签 */
export function getProviderHealthLabel(healthy: boolean): string {
  return healthy ? '健康' : '异常';
}

/** Provider 健康状态颜色 */
export function getProviderHealthColor(healthy: boolean): string {
  return healthy ? 'green' : 'red';
}
