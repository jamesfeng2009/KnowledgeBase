/**
 * 认证工具
 * 管理用户认证状态（JWT 已迁移到 HttpOnly Cookie，本模块不再直接操作 Token）。
 */

import { get, post, ApiError } from './api';

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
 * 检查是否已认证（通过调用 /me 校验 Cookie）。
 * 注意：SSR 阶段无法直接读取 HttpOnly Cookie，需由 Astro middleware 处理。
 * @returns 服务端 /me 返回的用户信息，未登录时返回 null
 */
export async function getCurrentUser(): Promise<User | null> {
  try {
    const response = await get<{ data: User }>('/api/v1/auth/me');
    return response.data;
  } catch (err) {
    if (err instanceof ApiError && err.status === 401) {
      console.warn('[Auth] Cookie 认证已失效');
    } else {
      console.warn('[Auth] 获取用户信息失败:', err instanceof Error ? err.message : String(err));
    }
    return null;
  }
}

/**
 * 登出
 * 调用后端 /logout 清除 HttpOnly Cookie，并跳转登录页。
 */
export async function logout(): Promise<void> {
  try {
    await post('/api/v1/auth/logout');
  } catch (err) {
    console.warn('[Auth] 后端登出调用失败，继续跳转登录页:', err instanceof Error ? err.message : String(err));
  }
  if (typeof window !== 'undefined') {
    window.location.href = '/auth/login';
  }
}

/**
 * 路由守卫：检查认证状态，未登录则跳转登录页
 * 在需要认证的页面客户端脚本中调用。
 *
 * @example
 * ```typescript
 * import { requireAuth } from '@/lib/auth';
 * // 在页面 <script> 中
 * requireAuth();
 * ```
 */
export async function requireAuth(): Promise<void> {
  const user = await getCurrentUser();
  if (!user && typeof window !== 'undefined') {
    window.location.href = '/auth/login';
  }
}

export default {
  getCurrentUser,
  logout,
  requireAuth,
};
