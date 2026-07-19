/**
 * 问答社区 API 封装
 * 对接后端 qa 路由（问题列表 / 详情 / 提问 / 回答 / 采纳）
 */
import { getData, postData, putData } from '../api';

const BASE = '/api/v1/qa';

// ===== 类型定义 =====

export interface Question {
  id: string;
  title: string;
  content: string;
  tags?: string[];
  kb_id?: string;
  author_name: string;
  status: 'open' | 'resolved' | 'closed';
  views: number;
  answers_count: number;
  created_at: string;
}

export interface Answer {
  id: string;
  question_id: string;
  content: string;
  author_name: string;
  is_accepted: boolean;
  is_ai_generated: boolean;
  created_at: string;
}

export interface QuestionPage {
  items: Question[];
  total: number;
}

// ===== API 方法 =====

/** 获取问题列表（支持状态过滤） */
export function getQuestions(params?: {
  status?: string;
  page?: number;
  size?: number;
}): Promise<QuestionPage> {
  return getData<QuestionPage>(`${BASE}/questions`, params as Record<string, string | number> | undefined);
}

/** 获取问题详情（含回答列表） */
export function getQuestion(questionId: string): Promise<Question & { answers?: Answer[] }> {
  return getData<Question & { answers?: Answer[] }>(`${BASE}/questions/${questionId}`);
}

/** 创建问题 */
export function createQuestion(data: {
  kb_id?: string;
  title: string;
  content: string;
  tags?: string[];
}): Promise<Question> {
  return postData<Question>(`${BASE}/questions`, data);
}

/** 创建回答 */
export function createAnswer(
  questionId: string,
  data: { content: string; is_ai_generated?: boolean }
): Promise<Answer> {
  return postData<Answer>(`${BASE}/questions/${questionId}/answers`, data);
}

/** 采纳回答 */
export function acceptAnswer(answerId: string): Promise<void> {
  return putData<void>(`${BASE}/answers/${answerId}/accept`);
}
