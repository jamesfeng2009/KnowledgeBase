/**
 * 用户与部门 API 封装
 * 对接后端用户管理路由（用户列表 / 角色更新 / 部门树 / LDAP 同步）
 */
import { getData, putData, postData } from '../api';

const BASE = '/api/v1/users';
const DEPT_BASE = '/api/v1/departments';

// ===== 类型定义 =====

export interface User {
  id: string;
  email: string;
  name: string;
  role: string;
  department?: string;
  avatar?: string;
  status: 'active' | 'disabled';
  created_at?: string;
}

export interface Department {
  id: string;
  name: string;
  parent_id?: string;
  children?: Department[];
}

export interface UserPage {
  items: User[];
  total: number;
}

export interface LdapSyncResult {
  success: boolean;
  message: string;
}

// ===== API 方法 =====

/** 获取用户列表（支持关键词 / 角色 / 部门过滤） */
export function getUsers(params?: {
  keyword?: string;
  role?: string;
  dept_id?: string;
  page?: number;
  size?: number;
}): Promise<UserPage> {
  return getData<UserPage>(BASE, params as Record<string, string | number> | undefined);
}

/** 获取单个用户详情 */
export function getUser(userId: string): Promise<User> {
  return getData<User>(`${BASE}/${userId}`);
}

/** 更新用户角色 */
export function updateUserRole(userId: string, role: string): Promise<void> {
  return putData<void>(`${BASE}/${userId}/role`, { role });
}

/** 获取部门组织树 */
export function getDepartments(): Promise<Department[]> {
  return getData<Department[]>(DEPT_BASE);
}

/** 触发 LDAP 同步 */
export function syncLdap(): Promise<LdapSyncResult> {
  return postData<LdapSyncResult>(`${BASE}/sync-ldap`);
}
