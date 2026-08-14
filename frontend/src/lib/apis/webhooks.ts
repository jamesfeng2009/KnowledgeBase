/**
 * Webhook 订阅管理封装
 * 对接后端 openapi/webhooks 路由（事件列表 / 订阅 / 列表 / 取消订阅 / 测试）
 *
 * 这些端点使用 API Key（X-API-Key header）认证，需要 scope: webhook:manage。
 * 与常规业务接口不同：常规接口走 Bearer Token（Authorization），
 * 而 openapi 端点走 X-API-Key，因此这里复用统一请求层并显式：
 *   1. 通过 skipAuth 跳过 Authorization 注入
 *   2. 通过 headers 注入 X-API-Key
 *
 * 后端已提供 GET /subscriptions 列表端点，订阅记录由服务端维护，
 * 不再依赖前端 localStorage 持久化，避免换浏览器/清缓存后产生孤儿订阅。
 */
import { getData, postData, delData, ApiError } from '../api';

const BASE = '/api/v1/openapi/webhooks';

/** OpenAPI API Key 在 localStorage 中的存储键名 */
const OPENAPI_KEY_STORAGE = 'ekb_openapi_api_key';

// ===== 类型定义 =====

/** 可订阅的事件类型 */
export interface WebhookEvent {
  event: string;
  description: string;
}

/** Webhook 订阅记录 */
export interface WebhookSubscription {
  id: string;
  url: string;
  events: string[];
  secret: string | null;
  key_id: string;
  is_active: boolean;
  /** 前端补充字段：订阅创建时间（订阅成功时由页面层记录） */
  created_at?: string;
}

/** 测试事件发送结果 */
export interface TestEventResult {
  event: string;
  payload: Record<string, unknown>;
  targets: number;
  status: string;
  message: string;
}

// ===== API Key 管理（localStorage） =====

/** 从 localStorage 读取 OpenAPI API Key */
export function getOpenApiKey(): string | null {
  if (typeof window === 'undefined') return null;
  return localStorage.getItem(OPENAPI_KEY_STORAGE);
}

/** 保存 OpenAPI API Key 到 localStorage */
export function setOpenApiKey(key: string): void {
  if (typeof window === 'undefined') return;
  localStorage.setItem(OPENAPI_KEY_STORAGE, key);
}

/** 清除 OpenAPI API Key */
export function clearOpenApiKey(): void {
  if (typeof window === 'undefined') return;
  localStorage.removeItem(OPENAPI_KEY_STORAGE);
}

// ===== 内部：构建认证请求配置 =====

/** 请求配置（与 api.ts 的 RequestOptions 子集保持兼容） */
interface AuthOptions {
  skipAuth: true;
  headers: { 'X-API-Key': string };
}

/**
 * 构建带 X-API-Key 的请求配置。
 * 若未配置 API Key，抛出 ApiError（status=401），由调用方捕获后提示用户。
 */
function buildAuthOptions(): AuthOptions {
  const key = getOpenApiKey();
  if (!key) {
    throw new ApiError(
      '请在 API 密钥页面配置具有 webhook:manage 权限的密钥，并在本页填入 API Key',
      401
    );
  }
  return { skipAuth: true, headers: { 'X-API-Key': key } };
}

// ===== API 方法 =====

/**
 * 列出可订阅的事件类型
 * GET /api/v1/openapi/webhooks/events
 */
export async function getWebhookEvents(): Promise<WebhookEvent[]> {
  return getData<WebhookEvent[]>(`${BASE}/events`, undefined, buildAuthOptions());
}

/**
 * 订阅 Webhook 事件
 * POST /api/v1/openapi/webhooks/subscribe
 */
export async function subscribeWebhook(data: {
  url: string;
  events: string[];
  secret?: string;
}): Promise<WebhookSubscription> {
  return postData<WebhookSubscription>(`${BASE}/subscribe`, data, buildAuthOptions());
}

/**
 * 列出当前 API Key 的订阅
 * GET /api/v1/openapi/webhooks/subscriptions
 */
export async function getSubscriptions(): Promise<WebhookSubscription[]> {
  return getData<WebhookSubscription[]>(`${BASE}/subscriptions`, undefined, buildAuthOptions());
}

/**
 * 取消订阅
 * DELETE /api/v1/openapi/webhooks/subscribe/{sub_id}
 */
export async function unsubscribeWebhook(subId: string): Promise<void> {
  await delData<void>(`${BASE}/subscribe/${encodeURIComponent(subId)}`, buildAuthOptions());
}

/**
 * 发送测试事件
 * POST /api/v1/openapi/webhooks/test
 */
export async function sendTestEvent(data: {
  url?: string;
  event?: string;
}): Promise<TestEventResult> {
  return postData<TestEventResult>(`${BASE}/test`, data, buildAuthOptions());
}
