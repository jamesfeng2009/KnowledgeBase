/**
 * 评论 API 封装
 * 对接后端 comments.py 路由
 */
import { getData, postData, putData } from '../api';

const BASE = '/api/v1';

// ===== 类型定义 =====

export interface Comment {
  id: string;
  doc_id: string;
  user_id: string;
  user_name: string;
  content: string;
  parent_id: string | null;
  resolved: boolean;
  created_at: string;
  updated_at: string;
  replies?: Comment[];
}

// ===== API 方法 =====

/** 获取文档顶层评论列表 */
export function getComments(docId: string): Promise<Comment[]> {
  return getData<Comment[]>(`${BASE}/documents/${docId}/comments`);
}

/** 在文档下发表评论或回复 */
export function createComment(docId: string, content: string, parentId?: string): Promise<Comment> {
  return postData<Comment>(`${BASE}/documents/${docId}/comments`, { content, parent_id: parentId });
}

/** 标记评论为已解决 */
export function resolveComment(commentId: string): Promise<void> {
  return putData<void>(`${BASE}/comments/${commentId}/resolve`);
}
