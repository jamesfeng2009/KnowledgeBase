/**
 * 系统设置 API 封装
 * 对接后端 settings 路由（LLM 配置 / 系统配置）
 */
import { getData, putData } from '../api';

const BASE = '/api/v1/settings';

// ===== 类型定义 =====

export interface LlmConfig {
  provider: string;
  model: string;
  api_key_masked?: string;
  api_base?: string;
  temperature?: number;
  max_tokens?: number;
  [key: string]: unknown;
}

export interface SystemConfig {
  site_name?: string;
  site_url?: string;
  [key: string]: unknown;
}

// ===== API 方法 =====

/** 获取 LLM 配置 */
export function getLlmConfig(): Promise<LlmConfig> {
  return getData<LlmConfig>(`${BASE}/llm`);
}

/** 更新 LLM 配置 */
export function updateLlmConfig(data: Partial<LlmConfig>): Promise<void> {
  return putData<void>(`${BASE}/llm`, data);
}

/** 获取系统配置 */
export function getSystemConfig(): Promise<SystemConfig> {
  return getData<SystemConfig>(`${BASE}/system`);
}

/** 更新系统配置 */
export function updateSystemConfig(data: Partial<SystemConfig>): Promise<void> {
  return putData<void>(`${BASE}/system`, data);
}
