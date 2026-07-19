/**
 * IT 工单 API 封装
 * 对接后端 tickets 路由（工单列表 / 创建 / 更新）
 */
import { getData, postData, putData } from '../api';

const BASE = '/api/v1/tickets';

// ===== 类型定义 =====

export interface Ticket {
  id: string;
  title: string;
  description: string;
  category: string;
  priority: string;
  status: 'open' | 'progress' | 'resolved';
  created_at: string;
  updated_at?: string;
  user_name?: string;
  ai_answer?: string;
  ai_solved?: boolean;
  ai_solution?: string;
}

export interface TicketPage {
  items: Ticket[];
  total: number;
}

// ===== API 方法 =====

/** 获取工单列表（支持状态 / 分类过滤） */
export function getTickets(params?: {
  status?: string;
  category?: string;
  page?: number;
  size?: number;
}): Promise<TicketPage> {
  return getData<TicketPage>(BASE, params as Record<string, string | number> | undefined);
}

/** 创建工单 */
export function createTicket(data: {
  title: string;
  description: string;
  category: string;
  priority: string;
}): Promise<Ticket> {
  return postData<Ticket>(BASE, data);
}

/** 更新工单（状态 / AI 解答等） */
export function updateTicket(ticketId: string, data: Partial<Ticket>): Promise<void> {
  return putData<void>(`${BASE}/${ticketId}`, data);
}
