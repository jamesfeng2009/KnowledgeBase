/**
 * 文档智能处理 API 封装
 * 对接后端 intelligence.py 路由
 * 功能：自动摘要 / 标签 / 分类 / 行动项
 */
import { getData, postData, putData } from '../api';

const BASE = '/api/v1/intelligence';

// ===== 类型定义 =====

export interface IntelligenceStatus {
  has_summary: boolean;
  has_category: boolean;
  has_tags: boolean;
  has_actions: boolean;
  processing: boolean;
}

export interface ActionItem {
  id: string;
  doc_id: string;
  content: string;
  assignee: string | null;
  deadline: string | null;
  priority: string;
  status: string;
  created_at: string;
}

// ===== API 方法 =====

/** 触发文档智能处理（自动摘要/标签/分类/行动项） */
export function processIntelligence(docId: string): Promise<void> {
  return postData<void>(`${BASE}/${docId}/process`);
}

/** 查询智能处理状态 */
export function getIntelligenceStatus(docId: string): Promise<IntelligenceStatus> {
  return getData<IntelligenceStatus>(`${BASE}/${docId}/status`);
}

/** 手动修改文档摘要 */
export function updateSummary(docId: string, summary: string): Promise<void> {
  return putData<void>(`${BASE}/${docId}/summary`, { summary });
}

/** 手动修改文档标签 */
export function updateTags(docId: string, tags: string[]): Promise<void> {
  return putData<void>(`${BASE}/${docId}/tags`, { tags });
}

/** 获取文档行动项列表 */
export function getActionItems(docId: string): Promise<ActionItem[]> {
  return getData<ActionItem[]>(`${BASE}/${docId}/actions`);
}

/** 更新行动项状态（pending/completed） */
export function updateActionItem(actionId: string, status: string): Promise<ActionItem> {
  return putData<ActionItem>(`${BASE}/actions/${actionId}?status=${status}`);
}

/**
 * 轮询智能处理状态直到完成
 * @param docId - 文档 ID
 * @param interval - 轮询间隔（毫秒），默认 3 秒
 * @param maxAttempts - 最大尝试次数，默认 60 次（3 分钟）
 */
export async function pollIntelligenceStatus(
  docId: string,
  interval = 3000,
  maxAttempts = 60
): Promise<IntelligenceStatus> {
  for (let i = 0; i < maxAttempts; i++) {
    const status = await getIntelligenceStatus(docId);
    if (!status.processing) return status;
    await new Promise((resolve) => setTimeout(resolve, interval));
  }
  throw new Error('智能处理超时，请稍后查看');
}
