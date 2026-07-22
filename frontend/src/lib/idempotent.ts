/**
 * 幂等重试工具 — 前端请求重试 + 幂等键管理
 *
 * 三层幂等性保证（前端层）：
 *   L1  请求去重 — 同一幂等键的请求在 flight 中只发一次
 *   L2  自动重试 — 网络错误自动重试（指数退避）
 *   L3  用户重试 — 失败后提供手动重试按钮
 */

/** 正在进行中的请求缓存（幂等键 → Promise） */
const inflightRequests = new Map<string, Promise<unknown>>();

/** 幂等重试配置 */
export interface IdempotentRetryConfig {
  /** 最大重试次数（默认 3） */
  maxRetries?: number;
  /** 基础退避延迟（毫秒，默认 1000） */
  baseDelay?: number;
  /** 最大退避延迟（毫秒，默认 10000） */
  maxDelay?: number;
  /** 是否启用抖动（默认 true） */
  jitter?: boolean;
}

/** 默认配置 */
const DEFAULT_CONFIG: Required<IdempotentRetryConfig> = {
  maxRetries: 3,
  baseDelay: 1000,
  maxDelay: 10000,
  jitter: true,
};

/**
 * 生成幂等键 — 基于方法 + URL + body 摘要
 */
export function generateIdempotencyKey(
  method: string,
  url: string,
  body?: unknown,
): string {
  const bodyStr = body ? JSON.stringify(body) : '';
  const raw = `${method}:${url}:${bodyStr}`;
  // 简单哈希（前端不需要加密强度，只需去重）
  let hash = 0;
  for (let i = 0; i < raw.length; i++) {
    const char = raw.charCodeAt(i);
    hash = (hash << 5) - hash + char;
    hash |= 0; // 转为 32 位整数
  }
  return `idem-${Math.abs(hash).toString(36)}`;
}

/**
 * 计算指数退避延迟
 */
function computeBackoffDelay(
  attempt: number,
  baseDelay: number,
  maxDelay: number,
  jitter: boolean,
): number {
  const exponential = Math.min(baseDelay * Math.pow(2, attempt - 1), maxDelay);
  if (jitter) {
    // 全抖动：在 0 ~ exponential 之间随机
    return Math.random() * exponential;
  }
  return exponential;
}

/**
 * 判断错误是否可重试
 */
function isRetryableError(error: unknown): boolean {
  if (error instanceof TypeError) {
    // 网络错误（fetch 抛 TypeError）
    return true;
  }
  if (error instanceof Error) {
    const msg = error.message.toLowerCase();
    // 超时、连接重置等
    if (msg.includes('timeout') || msg.includes('network') || msg.includes('fetch')) {
      return true;
    }
  }
  return false;
}

/**
 * 幂等重试请求
 *
 * L1 幂等性：同一 idempotencyKey 的请求在 flight 中只发一次，
 *            其他调用者共享同一 Promise 结果。
 * L2 自动重试：网络错误自动按指数退避重试。
 *
 * @param fetchFn fetch 函数（返回 Promise<T>）
 * @param idempotencyKey 幂等键（相同键共享结果）
 * @param config 重试配置
 * @returns 请求结果
 */
export async function idempotentRequest<T>(
  fetchFn: () => Promise<T>,
  idempotencyKey: string,
  config?: IdempotentRetryConfig,
): Promise<T> {
  const cfg = { ...DEFAULT_CONFIG, ...config };

  // L1: 检查是否有相同请求正在进行
  const inflight = inflightRequests.get(idempotencyKey);
  if (inflight) {
    return inflight as Promise<T>;
  }

  // 创建新的请求 Promise
  const requestPromise = _executeWithRetry(fetchFn, cfg, 1);

  // 注册到 inflight 缓存
  inflightRequests.set(idempotencyKey, requestPromise);

  try {
    const result = await requestPromise;
    return result;
  } finally {
    // 请求完成后清除缓存（无论成功或失败）
    inflightRequests.delete(idempotencyKey);
  }
}

/**
 * 执行带重试的请求
 */
async function _executeWithRetry<T>(
  fetchFn: () => Promise<T>,
  cfg: Required<IdempotentRetryConfig>,
  attempt: number,
): Promise<T> {
  try {
    return await fetchFn();
  } catch (error) {
    // 不可重试的错误直接抛出
    if (!isRetryableError(error)) {
      throw error;
    }

    // 超过最大重试次数
    if (attempt > cfg.maxRetries) {
      throw error;
    }

    // 计算退避延迟
    const delay = computeBackoffDelay(
      attempt,
      cfg.baseDelay,
      cfg.maxDelay,
      cfg.jitter,
    );

    // 等待退避
    await new Promise(resolve => setTimeout(resolve, delay));

    // 递归重试
    return _executeWithRetry(fetchFn, cfg, attempt + 1);
  }
}

/**
 * 清除所有 inflight 请求缓存
 */
export function clearInflightRequests(): void {
  inflightRequests.clear();
}
