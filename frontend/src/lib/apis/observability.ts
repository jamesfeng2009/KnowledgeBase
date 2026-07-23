/**
 * 可观测性 API 客户端 — 供 admin/observability 页面调用。
 *
 * P0-Stage4: 前端可观测面板数据源。
 */

import { getData } from '../api';

const BASE = '/api/v1/observability';

// ------------------------------------------------------------------
// 类型定义
// ------------------------------------------------------------------

export interface UsageRecordItem {
  id: string;
  created_at: string;
  model: string;
  input_tokens: number;
  output_tokens: number;
  cost_cents: number;
  request_type: string;
  duration_ms: number;
  success: boolean;
  request_id: string | null;
  user_id: string | null;
}

export interface RecentUsageResponse {
  items: UsageRecordItem[];
  total: number;
  page: number;
  page_size: number;
}

export interface ModelStat {
  model: string;
  count: number;
  tokens: number;
  cost_cents: number;
  avg_duration_ms: number;
}

export interface DateStat {
  date: string;
  count: number;
  tokens: number;
  cost_cents: number;
}

export interface ObservabilityStats {
  total_requests: number;
  total_input_tokens: number;
  total_output_tokens: number;
  total_cost_cents: number;
  avg_duration_ms: number;
  success_rate: number;
  by_model: ModelStat[];
  by_date: DateStat[];
}

// ------------------------------------------------------------------
// API 函数
// ------------------------------------------------------------------

/**
 * 获取最近的 LLM 调用记录（分页）。
 */
export function getRecentUsage(
  page = 1,
  pageSize = 20,
  model?: string,
  success?: boolean,
): Promise<RecentUsageResponse> {
  const params: Record<string, string | number | boolean> = {
    page,
    page_size: pageSize,
  };
  if (model) params.model = model;
  if (success !== undefined) params.success = success;
  return getData<RecentUsageResponse>(`${BASE}/recent`, params);
}

/**
 * 获取可观测性聚合统计。
 */
export function getObservabilityStats(days = 7): Promise<ObservabilityStats> {
  return getData<ObservabilityStats>(`${BASE}/stats`, { days });
}
