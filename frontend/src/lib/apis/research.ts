/**
 * 深度调研 API 封装 — 幂等提交 + 结果查询
 *
 * 幂等设计（关键约束）：
 *   幂等键在"提交发起时"生成一次，所有重试（网络层自动重试 + 用户手动重试）
 *   一律沿用同一键 — 键在 attempt 闭包内固化，重试路径不会换键。
 *   服务端同用户同键只建一条任务：超时重试返回原 task_id（reused=true），
 *   同键不同输入返回 409 IDEMPOTENCY_CONFLICT。
 */
import { get, post } from '../api';
import { generateIdempotencyKey, idempotentRequest } from '../idempotent';

const BASE = '/api/v1/research';

// ===== 类型定义 =====

/** 后端统一响应包络（原始结构，便于识别业务码 409） */
interface RawEnvelope<T> {
  code: number;
  data: T;
  message: string;
}

export interface ResearchStartResult {
  status: string;
  task_id: string;
  /** true = 重复提交，服务端返回已创建的原任务 */
  reused: boolean;
}

export type ResearchResultState =
  | { status: 'running' }
  | { status: 'success'; report: Record<string, unknown> }
  | { status: 'failed'; error: string };

// ===== 幂等键 =====

/**
 * 生成一次提交的幂等键 — 每次新提交调用一次；
 * 该次提交的所有重试必须复用返回值，禁止在重试路径重新生成。
 */
export function newResearchIdempotencyKey(goal: string): string {
  const contentHash = generateIdempotencyKey('POST', BASE, { goal });
  // 时间戳 + 随机盐：同一目标的两次独立提交是两个意图，须有不同的键
  return `research-${Date.now().toString(36)}-${contentHash}-${Math.random()
    .toString(36)
    .slice(2, 8)}`;
}

// ===== API 方法 =====

/**
 * 提交深度调研（单次尝试）— 自动重试与 inflight 去重由 idempotentRequest 承担。
 *
 * @param idempotencyKey 调用方持有的幂等键（提交发起时生成一次）
 */
export function startResearch(
  goal: string,
  kbIds: string[] | null,
  idempotencyKey: string,
): Promise<ResearchStartResult> {
  return idempotentRequest(
    () =>
      post<RawEnvelope<ResearchStartResult>>(
        BASE,
        { goal, kb_ids: kbIds },
        { headers: { 'Idempotency-Key': idempotencyKey } },
      ).then((raw) => {
        // HTTP 200 但业务码非 0（409 冲突 / 500 提交失败）→ 转异常
        if (raw.code !== 0) {
          throw new Error(raw.message || '调研任务提交失败');
        }
        return raw.data;
      }),
    idempotencyKey,
  );
}

/**
 * 提交深度调研 — 完整编排：键生成一次，失败后手动重试沿用同一键。
 *
 * @param onSuccess 提交成功（含 reused=true 的重复提交合并结果）
 * @param onError 提交失败；retry() 以同一幂等键重新提交
 */
export function submitResearch(
  goal: string,
  kbIds: string[] | null,
  onSuccess: (result: ResearchStartResult) => void,
  onError: (message: string, retry: () => void) => void,
): void {
  const key = newResearchIdempotencyKey(goal);

  const attempt = (): void => {
    startResearch(goal, kbIds, key)
      .then(onSuccess)
      .catch((err: unknown) => {
        onError(err instanceof Error ? err.message : '网络请求异常', attempt);
      });
  };

  attempt();
}

/** 查询调研任务结果（running / success / failed） */
export async function getResearchResult(
  taskId: string,
): Promise<ResearchResultState> {
  const raw = await get<RawEnvelope<ResearchResultState>>(
    `${BASE}/${taskId}/result`,
  );
  if (raw.code !== 0) {
    throw new Error(raw.message || '调研结果查询失败');
  }
  return raw.data;
}
