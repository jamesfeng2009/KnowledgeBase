/**
 * API 请求封装
 * 基于 fetch 的统一请求层，自动管理认证 Token 和错误处理
 */

// API 基础地址，从环境变量读取
export const API_BASE = import.meta.env.PUBLIC_API_BASE || 'http://localhost:8000';

/** API 错误类型 */
export class ApiError extends Error {
  status: number;
  data: unknown;

  constructor(message: string, status: number, data?: unknown) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.data = data;
  }
}

/** 请求配置 */
interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'DELETE' | 'PATCH';
  body?: unknown;
  headers?: Record<string, string>;
  /** 是否跳过认证头（如登录/注册接口） */
  skipAuth?: boolean;
  /** 自定义 Content-Type */
  contentType?: string;
}

/** 构建请求头（认证改为 HttpOnly Cookie，不再从 localStorage 读取 Token）。 */
function buildHeaders(options: RequestOptions): Record<string, string> {
  const headers: Record<string, string> = {
    'Content-Type': options.contentType || 'application/json',
    ...options.headers,
  };

  return headers;
}

/** 构建完整 URL，拼接 query 参数 */
function buildUrl(path: string, params?: Record<string, string | number | boolean>): string {
  const url = new URL(path.startsWith('http') ? path : `${API_BASE}${path}`);

  if (params) {
    Object.entries(params).forEach(([key, value]) => {
      url.searchParams.append(key, String(value));
    });
  }

  return url.toString();
}

/** 统一请求方法（认证通过 HttpOnly Cookie 自动携带）。 */
async function request<T = unknown>(
  path: string,
  options: RequestOptions = {}
): Promise<T> {
  const { method = 'GET', body, skipAuth, contentType } = options;

  const headers = buildHeaders({ ...options, skipAuth, contentType });

  // FormData 不需要手动设置 Content-Type，浏览器会自动添加 boundary
  if (body instanceof FormData) {
    delete headers['Content-Type'];
  }

  const config: RequestInit = {
    method,
    headers,
    credentials: 'include', // 必须：携带 HttpOnly Cookie
    body: body instanceof FormData ? body : body ? JSON.stringify(body) : undefined,
  };

  try {
    console.log('[API Request]', { method, path });

    const response = await fetch(buildUrl(path), config);

    // 401 未授权：跳转登录页（Token 在 HttpOnly Cookie 中，由服务端清除）
    if (response.status === 401 && !skipAuth) {
      if (typeof window !== 'undefined' && window.location.pathname !== '/auth/login') {
        window.location.href = '/auth/login';
      }
      throw new ApiError('登录已过期，请重新登录', 401);
    }

    // 解析响应体
    const data = await response.json().catch(() => null);

    if (!response.ok) {
      console.warn('[API Error]', { method, path, status: response.status });
      const message = (data as { message?: string })?.message || `请求失败 (${response.status})`;
      throw new ApiError(message, response.status, data);
    }

    return data as T;
  } catch (error) {
    if (error instanceof ApiError) {
      throw error;
    }
    // 网络错误
    throw new ApiError(
      error instanceof Error ? error.message : '网络请求异常，请检查网络连接',
      0
    );
  }
}

/** GET 请求 */
export function get<T = unknown>(
  path: string,
  params?: Record<string, string | number | boolean>,
  options?: Omit<RequestOptions, 'method' | 'body'>
): Promise<T> {
  return request<T>(params ? `${path}?` + new URLSearchParams(
    Object.entries(params).map(([k, v]) => [k, String(v)])
  ).toString() : path, { ...options, method: 'GET' });
}

/** POST 请求 */
export function post<T = unknown>(
  path: string,
  body?: unknown,
  options?: Omit<RequestOptions, 'method' | 'body'>
): Promise<T> {
  return request<T>(path, { ...options, method: 'POST', body });
}

/** PUT 请求 */
export function put<T = unknown>(
  path: string,
  body?: unknown,
  options?: Omit<RequestOptions, 'method' | 'body'>
): Promise<T> {
  return request<T>(path, { ...options, method: 'PUT', body });
}

/** DELETE 请求 */
export function del<T = unknown>(
  path: string,
  options?: Omit<RequestOptions, 'method' | 'body'>
): Promise<T> {
  return request<T>(path, { ...options, method: 'DELETE' });
}

/** 上传文件（FormData） */
export function upload<T = unknown>(
  path: string,
  formData: FormData,
  options?: Omit<RequestOptions, 'method' | 'body' | 'contentType'>
): Promise<T> {
  return request<T>(path, {
    ...options,
    method: 'POST',
    body: formData,
    contentType: '', // 让浏览器自动设置 multipart boundary
  });
}

// ===== 后端统一响应格式 { code, data, message } 自动提取 data =====

/** 后端统一响应结构 */
interface ApiResponse<T = unknown> {
  code: number;
  data: T;
  message: string;
}

/** 分页响应结构 */
interface PageResponse<T = unknown> {
  items: T[];
  total: number;
  page: number;
  size: number;
  pages: number;
}

/** 请求并自动提取 .data 字段（后端统一响应格式） */
async function requestData<T = unknown>(
  path: string,
  options: RequestOptions = {}
): Promise<T> {
  const raw = await request<ApiResponse<T>>(path, options);
  // 兼容两种返回格式：{code, data, message} 或直接返回数据
  if (raw && typeof raw === 'object' && 'data' in raw && 'code' in raw) {
    return raw.data;
  }
  return raw as T;
}

/** GET 请求并提取 data 字段 */
export function getData<T = unknown>(
  path: string,
  params?: Record<string, string | number | boolean>,
  options?: Omit<RequestOptions, 'method' | 'body'>
): Promise<T> {
  return requestData<T>(params ? `${path}?` + new URLSearchParams(
    Object.entries(params).map(([k, v]) => [k, String(v)])
  ).toString() : path, { ...options, method: 'GET' });
}

/** POST 请求并提取 data 字段 */
export function postData<T = unknown>(
  path: string,
  body?: unknown,
  options?: Omit<RequestOptions, 'method' | 'body'>
): Promise<T> {
  return requestData<T>(path, { ...options, method: 'POST', body });
}

/** PUT 请求并提取 data 字段 */
export function putData<T = unknown>(
  path: string,
  body?: unknown,
  options?: Omit<RequestOptions, 'method' | 'body'>
): Promise<T> {
  return requestData<T>(path, { ...options, method: 'PUT', body });
}

/** DELETE 请求并提取 data 字段 */
export function delData<T = unknown>(
  path: string,
  options?: Omit<RequestOptions, 'method' | 'body'>
): Promise<T> {
  return requestData<T>(path, { ...options, method: 'DELETE' });
}

/** PATCH 请求并提取 data 字段 */
export function patchData<T = unknown>(
  path: string,
  body?: unknown,
  options?: Omit<RequestOptions, 'method' | 'body'>
): Promise<T> {
  return requestData<T>(path, { ...options, method: 'PATCH', body });
}

export type { ApiResponse, PageResponse };

export default { get, post, put, del, upload, getData, postData, putData, delData, patchData, ApiError };
