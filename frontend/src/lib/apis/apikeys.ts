/**
 * API 密钥管理封装
 * 对接后端 apikeys 路由（密钥列表 / 创建 / 删除 / 使用记录）
 */
import { getData, postData, delData } from '../api';

const BASE = '/api/v1/apikeys';

// ===== 类型定义 =====

export interface ApiKey {
  id: string;
  name: string;
  scopes: string[];
  expires_at?: string;
  created_at: string;
  last_used_at?: string;
  /** 仅返回前几位（脱敏） */
  key_prefix?: string;
  /** 仅创建时返回完整密钥 */
  full_key?: string;
}

export interface CreateApiKeyRequest {
  name: string;
  scopes: string[];
  expires_at?: string;
}

// ===== API 方法 =====

/** 获取 API 密钥列表 */
export function getApiKeys(): Promise<ApiKey[]> {
  return getData<ApiKey[]>(BASE);
}

/** 创建 API 密钥（返回值包含 full_key，仅本次可见） */
export function createApiKey(data: CreateApiKeyRequest): Promise<ApiKey> {
  return postData<ApiKey>(BASE, data);
}

/** 删除 API 密钥 */
export function deleteApiKey(keyId: string): Promise<void> {
  return delData<void>(`${BASE}/${keyId}`);
}

/** 获取指定密钥的使用记录（后端可选支持） */
export function getApiKeyUsage(keyId: string): Promise<unknown[]> {
  return getData<unknown[]>(`${BASE}/${keyId}/usage`);
}
