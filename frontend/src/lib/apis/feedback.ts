/**
 * 反馈管理 API 封装
 * 对接后端反馈路由（反馈列表 / 回复 / 状态更新）
 */
import { getData, putData } from '../api';

const BASE = '/api/v1/feedback';

// ===== 类型定义 =====

export interface Feedback {
  id: string;
  type: 'bug' | 'suggestion' | 'question' | 'praise';
  content: string;
  status: 'pending' | 'processing' | 'resolved';
  response?: string;
  created_at: string;
  user_name: string;
}

export interface FeedbackPage {
  items: Feedback[];
  total: number;
}

// ===== API 方法 =====

/** 获取反馈列表（支持状态过滤） */
export function getFeedbacks(params?: {
  status?: string;
  page?: number;
  size?: number;
}): Promise<FeedbackPage> {
  return getData<FeedbackPage>(BASE, params as Record<string, string | number> | undefined);
}

/** 回复反馈 */
export function respondFeedback(feedbackId: string, response: string): Promise<void> {
  return putData<void>(`${BASE}/${feedbackId}/respond`, { response });
}

/** 更新反馈状态 */
export function updateFeedbackStatus(feedbackId: string, status: string): Promise<void> {
  return putData<void>(`${BASE}/${feedbackId}/status`, { status });
}
