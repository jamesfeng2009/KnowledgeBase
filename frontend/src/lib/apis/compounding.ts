/**
 * 知识回流层 API 封装
 * 对接后端 knowledge_compounding.py 路由（prefix=/api/v1/compounding）
 */
import { getData, postData, putData, type PageResponse } from '../api';

const BASE = '/api/v1/compounding';

// ===== 类型定义 =====

export interface KnowledgeAsset {
  id: string;
  asset_type: string;
  source_type: string;
  source_id?: string;
  project_id?: string;
  title: string;
  content: string;
  summary?: string;
  tags?: string[];
  doc_id?: string;
  graph_nodes?: unknown[];
  graph_relationships?: unknown[];
  graphiti_entity_id?: string;
  confidence_score?: number;
  status: string;
  conflict_with?: string[];
  compounding_task_id?: string;
  created_at?: string;
  updated_at?: string;
}

export interface CompoundingTask {
  id: string;
  execution_id?: string;
  project_id?: string;
  task_type: string;
  status: string;
  trigger_source: string;
  extracted_asset_ids?: string[];
  conflicts_detected: number;
  assets_injected: number;
  error_message?: string;
  started_at?: string;
  completed_at?: string;
  created_at?: string;
}

export interface KnowledgeConflict {
  id: string;
  new_asset_id: string;
  existing_asset_id: string;
  conflict_type: string;
  description?: string;
  resolution: string;
  resolved_by?: string;
  resolved_at?: string;
  resolution_note?: string;
  created_at?: string;
}

export interface CompoundingStats {
  total_assets: number;
  assets_by_type: Record<string, number>;
  assets_by_status: Record<string, number>;
  total_tasks: number;
  tasks_by_status: Record<string, number>;
  total_conflicts: number;
  unresolved_conflicts: number;
  reuse_injection_count: number;
}

export interface ReuseInjectionResult {
  requirement_id: string;
  injected_assets: Record<string, unknown>[];
  injection_context?: string;
  asset_count: number;
}

export interface ExtractionResult {
  execution_id: string;
  task_id?: string;
  status: string;
  asset_count?: number;
  assets?: KnowledgeAsset[];
  conflicts?: number;
  reason?: string;
  error?: string;
}

// ===== 知识提取 API =====

export function extractKnowledge(data: {
  execution_id: string;
  trigger_source?: string;
}): Promise<ExtractionResult> {
  return postData<ExtractionResult>(`${BASE}/extract`, data);
}

// ===== 知识资产 API =====

export function getAssets(params: {
  project_id?: string;
  asset_type?: string;
  status?: string;
  page?: number;
  size?: number;
}): Promise<PageResponse<KnowledgeAsset>> {
  return getData<PageResponse<KnowledgeAsset>>(`${BASE}/assets`, params as Record<string, string | number>);
}

export function getAsset(id: string): Promise<KnowledgeAsset> {
  return getData<KnowledgeAsset>(`${BASE}/assets/${id}`);
}

// ===== 冲突检测 API =====

export function detectConflicts(assetId: string): Promise<{
  conflicts: KnowledgeConflict[];
  count: number;
}> {
  return postData(`${BASE}/conflicts/detect`, { asset_id: assetId });
}

export function getConflicts(params: {
  resolution?: string;
  page?: number;
  size?: number;
}): Promise<PageResponse<KnowledgeConflict>> {
  return getData<PageResponse<KnowledgeConflict>>(`${BASE}/conflicts`, params as Record<string, string | number>);
}

export function resolveConflict(
  id: string,
  data: { resolution: string; note?: string }
): Promise<KnowledgeConflict> {
  return putData<KnowledgeConflict>(`${BASE}/conflicts/${id}/resolve`, data);
}

// ===== 复用注入 API =====

export function injectForReuse(data: {
  requirement_id: string;
  max_assets?: number;
}): Promise<ReuseInjectionResult> {
  return postData<ReuseInjectionResult>(`${BASE}/reuse/inject`, data);
}

// ===== 回流任务 API =====

export function getTasks(params: {
  project_id?: string;
  task_type?: string;
  status?: string;
  page?: number;
  size?: number;
}): Promise<PageResponse<CompoundingTask>> {
  return getData<PageResponse<CompoundingTask>>(`${BASE}/tasks`, params as Record<string, string | number>);
}

// ===== 回流统计 API =====

export function getCompoundingStats(projectId?: string): Promise<CompoundingStats> {
  const params: Record<string, string> = {};
  if (projectId) params.project_id = projectId;
  return getData<CompoundingStats>(`${BASE}/stats`, params);
}
