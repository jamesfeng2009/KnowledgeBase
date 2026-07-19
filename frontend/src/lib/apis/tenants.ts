/**
 * 租户管理与模块门控 API 封装
 * 对接后端 tenants.py 路由
 */
import { getData, putData, patchData } from '../api';

const BASE = '/api/v1/tenants';

// ===== 类型定义 =====

export interface Tenant {
  id: string;
  name: string;
  domain: string | null;
  plan: string;
  max_users: number;
  max_storage: number;
  settings: {
    enabled_modules?: string[];
    [key: string]: unknown;
  };
  expired_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface TenantUsage {
  user_count: number;
  storage_used: number;
  storage_limit: number;
  document_count: number;
  kb_count: number;
}

export interface ModuleInfo {
  id: string;
  name: string;
  description: string;
  category: 'basic' | 'intelligence' | 'integration';
  is_basic: boolean;
  enabled: boolean;
}

// ===== API 方法 =====

/** 获取当前租户信息 */
export function getCurrentTenant(): Promise<Tenant> {
  return getData<Tenant>(`${BASE}/current`);
}

/** 更新租户配置 */
export function updateTenant(data: Partial<Tenant>): Promise<Tenant> {
  return putData<Tenant>(`${BASE}/current`, data);
}

/** 获取用量统计 */
export function getTenantUsage(): Promise<TenantUsage> {
  return getData<TenantUsage>(`${BASE}/usage`);
}

/** 获取所有模块及启用状态 */
export function getTenantModules(): Promise<ModuleInfo[]> {
  return getData<ModuleInfo[]>(`${BASE}/modules`);
}

/** 批量更新启用模块列表 */
export function updateTenantModules(moduleIds: string[]): Promise<void> {
  return putData<void>(`${BASE}/modules`, { module_ids: moduleIds });
}

/** 开关单个模块 */
export function toggleTenantModule(moduleId: string, enabled: boolean): Promise<void> {
  return patchData<void>(`${BASE}/modules/${moduleId}`, { enabled });
}
