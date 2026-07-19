/**
 * 专家发现 API 封装
 * 对接后端 experts.py 路由
 */
import { getData } from '../api';

const BASE = '/api/v1/experts';

// ===== 类型定义 =====

export interface Expert {
  user_id: string;
  name: string;
  avatar: string | null;
  department: string | null;
  title: string | null;
  expertise_score: number;
  expertise_areas: string[];
  document_count: number;
  answer_count: number;
}

export interface Expertise {
  area: string;
  score: number;
  document_count: number;
}

export interface Contributor {
  user_id: string;
  name: string;
  avatar: string | null;
  department: string | null;
  contribution_score: number;
  document_count: number;
  answer_count: number;
  comment_count: number;
}

// ===== API 方法 =====

/** 按关键词查找相关专家 */
export function findExperts(keyword: string, topK = 5): Promise<Expert[]> {
  return getData<Expert[]>(BASE, { q: keyword, top_k: topK });
}

/** 获取全站贡献排行榜 */
export function getTopContributors(days = 30, topK = 10): Promise<Contributor[]> {
  return getData<Contributor[]>(`${BASE}/top`, { days, top_k: topK });
}

/** 获取用户的专业领域 */
export function getUserExpertise(userId: string): Promise<Expertise[]> {
  return getData<Expertise[]>(`${BASE}/${userId}/expertise`);
}
