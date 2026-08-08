/**
 * 微调数据集管理 API 封装
 * 对接后端微调数据集路由（导出构建 / 版本列表 / 详情 / 样本预览）
 * 下载接口返回文件流，需带 Authorization 头单独 fetch，故不在此封装
 */
import { getData, postData } from '../api';

const BASE = '/api/v1/finetune/datasets';

// ===== 类型定义 =====

export type DatasetType = 'sft' | 'dpo' | 'embedding' | 'golden';

export type DatasetStatus = 'pending' | 'building' | 'completed' | 'failed';

export interface DatasetVersion {
  id: string;
  dataset_type: string;
  version: string;
  status: DatasetStatus;
  sample_count: number;
  filtered_stats: Record<string, unknown> | null;
  file_size_bytes: number | null;
  created_at: string;
  completed_at: string | null;
}

/** 详情额外携带构建参数 / 统计 / Celery 任务 ID */
export interface DatasetDetail extends DatasetVersion {
  params?: Record<string, unknown>;
  stats?: Record<string, unknown>;
  celery_task_id?: string;
}

export interface DatasetPage {
  items: DatasetVersion[];
  total: number;
}

export interface ExportParams {
  dataset_type: DatasetType;
  max_classification: string;
  days: number;
  min_rating: number;
  limit: number;
}

export interface ExportResult {
  export_id: string;
  task_id: string;
  status: string;
}

export interface PreviewResult {
  items: Record<string, unknown>[];
}

// ===== API 方法 =====

/** 提交数据集导出构建任务 */
export function exportDataset(params: ExportParams): Promise<ExportResult> {
  return postData<ExportResult>(`${BASE}/export`, params);
}

/** 获取数据集版本列表（支持类型过滤与分页） */
export function getDatasets(params?: {
  dataset_type?: string;
  page?: number;
  size?: number;
}): Promise<DatasetPage> {
  return getData<DatasetPage>(BASE, params as Record<string, string | number> | undefined);
}

/** 获取单个数据集版本详情 */
export function getDataset(id: string): Promise<DatasetDetail> {
  return getData<DatasetDetail>(`${BASE}/${id}`);
}

/** 预览数据集前 N 条样本 */
export function previewDataset(id: string, limit = 5): Promise<PreviewResult> {
  return getData<PreviewResult>(`${BASE}/${id}/preview`, { limit });
}
