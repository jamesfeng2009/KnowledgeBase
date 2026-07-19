/**
 * MCP（Model Context Protocol）工具协议封装
 * 对接后端 openapi/mcp 路由（工具列表 / 工具 Schema / 工具调用）
 *
 * 这些端点属于 OpenAPI 公开访问层，使用 API Key 认证（X-API-Key header），
 * 不走 api.ts 中默认的 Bearer Token 认证，因此这里使用独立 fetch 封装。
 *
 * API Key 与 webhooks.ts 共享同一个 localStorage 键（ekb_openapi_api_key），
 * 用户只需配置一个同时具备 `mcp:use` 和 `webhook:manage` 权限的密钥即可。
 */
import { API_BASE, ApiError } from '../api';
import { getOpenApiKey, setOpenApiKey, clearOpenApiKey } from './webhooks';

// 重新导出，方便页面层从 mcp 模块导入
export { getOpenApiKey as getMcpApiKey, setOpenApiKey as setMcpApiKey, clearOpenApiKey as clearMcpApiKey };

const BASE = '/api/v1/openapi/mcp';

// ===== 类型定义 =====

export interface McpTool {
  /** 工具名称（调用时使用） */
  name: string;
  /** 工具描述 */
  description: string;
  /** JSON Schema 格式的入参定义 */
  parameters: Record<string, unknown>;
}

export interface McpToolResult {
  /** 被调用的工具名称 */
  tool: string;
  /** 工具执行返回值，可能是对象或字符串 */
  result: unknown;
}

/** 后端统一响应结构 */
interface ApiResponse<T> {
  code: number;
  data: T;
  message: string;
}

// ===== 内部工具方法 =====

/** 构建完整 URL（兼容已包含 host 的绝对地址） */
function buildUrl(path: string): string {
  return path.startsWith('http') ? path : `${API_BASE}${path}`;
}

/**
 * 统一请求方法：自动注入 X-API-Key header
 * 兼容后端 { code, data, message } 响应格式，自动提取 data 字段。
 */
async function request<T>(
  path: string,
  options: { method?: 'GET' | 'POST'; body?: unknown } = {}
): Promise<T> {
  const { method = 'GET', body } = options;

  const apiKey = getOpenApiKey();
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
  };
  if (apiKey) {
    headers['X-API-Key'] = apiKey;
  }

  const config: RequestInit = {
    method,
    headers,
    body: body ? JSON.stringify(body) : undefined,
  };

  try {
    const response = await fetch(buildUrl(path), config);

    // 解析响应体（允许空 body）
    const raw = (await response.json().catch(() => null)) as ApiResponse<T> | T | null;

    if (!response.ok) {
      const message =
        (raw && typeof raw === 'object' && 'message' in raw
          ? String((raw as ApiResponse<T>).message)
          : '') || `请求失败 (${response.status})`;
      throw new ApiError(message, response.status, raw);
    }

    // 兼容 { code, data, message } 与直接返回数据两种格式
    if (raw && typeof raw === 'object' && 'code' in raw && 'data' in raw) {
      return (raw as ApiResponse<T>).data;
    }
    return raw as T;
  } catch (error) {
    if (error instanceof ApiError) {
      throw error;
    }
    throw new ApiError(
      error instanceof Error ? error.message : '网络请求异常，请检查网络连接',
      0
    );
  }
}

// ===== API 方法 =====

/** 列出所有可用的 MCP 工具 */
export function listMcpTools(): Promise<McpTool[]> {
  return request<McpTool[]>(`${BASE}/tools`);
}

/** 调用指定的 MCP 工具 */
export function invokeMcpTool(
  toolName: string,
  args: Record<string, unknown>
): Promise<McpToolResult> {
  return request<McpToolResult>(`${BASE}/tools/${encodeURIComponent(toolName)}/invoke`, {
    method: 'POST',
    body: { arguments: args },
  });
}

/** 获取指定工具的 JSON Schema */
export function getMcpToolSchema(toolName: string): Promise<McpTool> {
  return request<McpTool>(`${BASE}/tools/${encodeURIComponent(toolName)}/schema`);
}
