/**
 * middleware.ts - Astro 服务端路由守卫
 *
 * P0 安全修复：Token 已迁移到 HttpOnly Cookie，中间件可在服务端读取 Cookie
 * 并调用后端 /me 接口做服务端鉴权，未登录请求直接重定向到登录页。
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
  '/_astro',
  '/favicon.svg',
];

/** 判断路径是否在白名单中 */
export function isPublicPath(pathname: string): boolean {
  return PUBLIC_PATHS.some((p) => pathname === p || pathname.startsWith(p));
}

/** 从请求头中解析指定 Cookie */
function getCookie(headers: Headers, name: string): string | undefined {
  const cookieHeader = headers.get('cookie') || '';
  const match = cookieHeader.match(new RegExp(`(?:^|\\s)${name}=([^;]+)`));
  return match?.[1];
}

export const onRequest = defineMiddleware(async (context, next) => {
  const { pathname } = context.url;

  // 静态资源与白名单路径放行
  if (isPublicPath(pathname) || pathname.match(/\.(js|css|svg|png|jpg|jpeg|gif|webp|ico|woff|woff2|ttf|otf)$/)) {
    return next();
  }

  const apiBase = import.meta.env.PUBLIC_API_BASE || 'http://localhost:8000';
  const token = getCookie(context.request.headers, 'access_token');

  // 无 Token 直接重定向
  if (!token) {
    console.warn('[Middleware] 未提供 Cookie Token，重定向登录页:', pathname);
    return context.redirect('/auth/login');
  }

  // 服务端校验 Token 有效性
  try {
    const response = await fetch(`${apiBase}/api/v1/auth/me`, {
      method: 'GET',
      headers: {
        Authorization: `Bearer ${token}`,
      },
    });

    if (!response.ok) {
      console.warn('[Middleware] Token 校验失败:', pathname, response.status);
      return context.redirect('/auth/login');
    }
  } catch (err) {
    console.warn('[Middleware] 鉴权服务不可用，放行请求:', pathname, err instanceof Error ? err.message : String(err));
    // 后端不可达时降级放行，由页面客户端 / API 调用再次校验，避免完全不可用
    return next();
  }

  return next();
});
