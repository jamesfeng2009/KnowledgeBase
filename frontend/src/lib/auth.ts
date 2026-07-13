/**
 * 认证工具
 * 管理 JWT Token 存储和用户认证状态
 */

import { get } from './api';

/** Token 在 localStorage 中的存储键名 */
const TOKEN_KEY = 'ekb_access_token';

/** 用户信息接口 */
export interface User {
  id: string;
  email: string;
  name: string;
  avatar?: string;
  role?: string;
  tenant_id?: string;
}

/** 登录响应 */
export interface LoginResponse {
  access_token: string;
  token_type: string;
  user: User;
}

/**
 * 获取当前 Token
 * @returns 存储在 localStorage 中的 JWT Token，不存在则返回 null
 */
export function getToken(): string | null {
  if (typeof window === 'undefined') return null;
  return localStorage.getItem(TOKEN_KEY);
}

/**
 * 设置 Token
 * @param token - JWT Token 字符串
 */
export function setToken(token: string): void {
  if (typeof window === 'undefined') return;
  localStorage.setItem(TOKEN_KEY, token);
}

/**
 * 移除 Token（登出时调用）
 */
export function removeToken(): void {
  if (typeof window === 'undefined') return;
  localStorage.removeItem(TOKEN_KEY);
}

/**
 * 检查是否已认证（Token 是否存在）
 * 注意：此方法仅检查 Token 是否存在，不验证 Token 是否有效
 * @returns Token 是否存在
 */
export function isAuthenticated(): boolean {
  return !!getToken();
}

/**
 * 获取当前登录用户信息
 * 调用 /api/v1/auth/me 接口获取
 * @returns 用户信息，未登录或请求失败时返回 null
 */
export async function getCurrentUser(): Promise<User | null> {
  if (!isAuthenticated()) {
    return null;
  }

  try {
    const response = await get<{ data: User }>('/api/v1/auth/me');
    return response.data;
  } catch {
    // Token 无效或请求失败，清除 Token
    removeToken();
    return null;
  }
}

/**
 * 登出
 * 清除 Token 并跳转登录页
 */
export function logout(): void {
  removeToken();
  if (typeof window !== 'undefined') {
    window.location.href = '/auth/login';
  }
}

/**
 * 路由守卫：检查认证状态，未登录则跳转登录页
 * 在需要认证的页面客户端脚本中调用
 *
 * @example
 * ```typescript
 * import { requireAuth } from '@/lib/auth';
 * // 在页面 <script> 中
 * requireAuth();
 * ```
 */
export function requireAuth(): void {
  if (!isAuthenticated()) {
    if (typeof window !== 'undefined') {
      window.location.href = '/auth/login';
    }
    return;
  }
}

export default {
  getToken,
  setToken,
  removeToken,
  isAuthenticated,
  getCurrentUser,
  logout,
  requireAuth,
};
