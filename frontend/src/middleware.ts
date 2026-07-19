/**
 * middleware.ts - Astro 路由守卫
 * 检查 JWT Token，未登录重定向到 /auth/login
 *
 * 注意：Astro 中间件在服务端运行，无法直接读取 localStorage。
 * 本项目 Token 存储在客户端 localStorage，因此采用「客户端守卫」策略：
 * 中间件仅放行所有请求，实际的 Token 校验在页面客户端脚本中完成
 * （DefaultLayout 的 loadUser 与各页面的初始化逻辑已实现 401 跳转）。
 *
 * 这里保留中间件用于：
 * 1. 定义白名单路径常量，供客户端脚本共享判断逻辑
 * 2. 为后续 SSR 改造预留扩展点
 */
import { defineMiddleware } from 'astro:middleware';

// 白名单路径：无需登录即可访问
export const PUBLIC_PATHS = [
  '/auth/login',
  '/auth/register',
  '/auth/forgot-password',
  '/auth/sso',
  '/health',
  '/api/health',
];

/** 判断路径是否在白名单中 */
export function isPublicPath(pathname: string): boolean {
  return PUBLIC_PATHS.some((p) => pathname === p || pathname.startsWith(p));
}

export const onRequest = defineMiddleware((_context, next) => {
  // 当前采用客户端 Token 守卫，中间件放行所有请求
  // 如需 SSR 模式下的服务端鉴权，可在此读取 Cookie 中的 Token 并校验
  return next();
});
