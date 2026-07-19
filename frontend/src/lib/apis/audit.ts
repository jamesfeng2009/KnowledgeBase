/**
 * 审核工作流 API 封装
 * 对接后端审核路由（待审核列表 / 批准 / 拒绝）
 */
import { getData, putData } from '../api';

const BASE = '/api/v1/audit';

// ===== 类型定义 =====

export interface AuditItem {
  id: string;
  resource_type: string;
  resource_id: string;
  status: 'pending' | 'approved' | 'rejected';
  priority: string;
  submitted_by: string;
  submitted_at: string;
  comment?: string;
}

export interface AuditPage {
  items: AuditItem[];
  total: number;
}

// ===== API 方法 =====

/** 获取待审核列表（可按状态过滤） */
export function getPendingAudits(
  page = 1,
  size = 20,
  status?: string
): Promise<AuditPage> {
  const params: Record<string, string | number> = { page, size };
  if (status) params.status = status;
  return getData<AuditPage>(`${BASE}/pending`, params);
}

/** 批准审核项 */
export function approveAudit(auditId: string, comment?: string): Promise<void> {
  return putData<void>(`${BASE}/${auditId}/approve`, { comment });
}

/** 拒绝审核项 */
export function rejectAudit(auditId: string, comment?: string): Promise<void> {
  return putData<void>(`${BASE}/${auditId}/reject`, { comment });
}
