/**
 * 分析仪表盘 API 封装
 * 对接后端 analytics.py 路由
 */
import { getData, postData } from '../api';

const BASE = '/api/v1/analytics';

// ===== 类型定义 =====

export interface DashboardData {
  total_searches: number;
  total_documents: number;
  total_users: number;
  zero_click_rate: number;
  coverage_rate: number;
  freshness_rate: number;
  recent_searches: { query: string; count: number; result_count: number }[];
}

export interface KeywordCount {
  keyword: string;
  count: number;
}

export interface PopularDoc {
  doc_id: string;
  title: string;
  view_count: number;
  kb_name: string;
}

export interface CoverageData {
  covered: number;
  total: number;
  rate: number;
  uncovered_topics: string[];
}

export interface FreshnessData {
  fresh: number;
  stale: number;
  expired: number;
  total: number;
  fresh_rate: number;
}

export interface ContributorData {
  user_id: string;
  name: string;
  avatar: string | null;
  department: string | null;
  contribution_score: number;
  document_count: number;
  answer_count: number;
}

// ===== API 方法 =====

/** 仪表盘汇总（一次返回六项指标） */
export function getDashboard(days = 30): Promise<DashboardData> {
  return getData<DashboardData>(`${BASE}/dashboard`, { days });
}

/** 搜索热词 Top N */
export function getSearchHotwords(days = 30, topK = 20): Promise<KeywordCount[]> {
  return getData<KeywordCount[]>(`${BASE}/search-hotwords`, { days, top_k: topK });
}

/** 零点击搜索词 */
export function getZeroClickQueries(days = 30, topK = 20): Promise<KeywordCount[]> {
  return getData<KeywordCount[]>(`${BASE}/zero-click`, { days, top_k: topK });
}

/** 文档热度排行 */
export function getPopularDocs(days = 30, topK = 10): Promise<PopularDoc[]> {
  return getData<PopularDoc[]>(`${BASE}/popular-docs`, { days, top_k: topK });
}

/** 知识覆盖率 */
export function getCoverage(): Promise<CoverageData> {
  return getData<CoverageData>(`${BASE}/coverage`);
}

/** 知识新鲜度 */
export function getFreshness(): Promise<FreshnessData> {
  return getData<FreshnessData>(`${BASE}/freshness`);
}

/** 专家贡献排行 */
export function getContributors(days = 30, topK = 10): Promise<ContributorData[]> {
  return getData<ContributorData[]>(`${BASE}/contributors`, { days, top_k: topK });
}

/** 记录搜索行为（内部调用） */
export function logSearch(query: string, resultCount: number, source = 'knowledge'): Promise<void> {
  return postData<void>(`${BASE}/log-search`, { query, result_count: resultCount, source });
}
