/**
 * 知识库与文档 API 封装
 * 对接后端 knowledge.py + documents.py 路由
 */
import { getData, postData, putData, delData, type PageResponse } from '../api';

const BASE = '/api/v1';

// ===== 类型定义 =====

export interface KnowledgeBase {
  id: string;
  name: string;
  description: string;
  visibility: string;
  document_count?: number;
  created_at: string;
  updated_at: string;
}

export interface Document {
  id: string;
  kb_id: string;
  title: string;
  content?: string;
  content_html?: string;
  content_text?: string;
  doc_type: string;
  status: string;
  file_size?: number;
  file_path?: string;
  summary?: string;
  category?: string;
  tags?: string[];
  parse_status?: string;
  parse_warnings?: string[];
  page_count?: number;
  char_count?: number;
  tenant_id?: string;
  created_at: string;
  updated_at: string;
}

export interface DocSummary {
  preview: string;
  structure: string[];
  warnings: string[];
  pages: number;
  char_count: number;
  parse_status: string;
}

export interface DocVersion {
  id: string;
  version: number;
  created_at: string;
  author: string;
  preview: string;
}

// ===== 知识库 CRUD =====

export function getKnowledgeBases(page = 1, size = 20): Promise<PageResponse<KnowledgeBase>> {
  return getData<PageResponse<KnowledgeBase>>(`${BASE}/knowledge`, { page, size });
}

export function createKnowledgeBase(data: { name: string; description?: string; visibility?: string }): Promise<KnowledgeBase> {
  return postData<KnowledgeBase>(`${BASE}/knowledge`, data);
}

export function updateKnowledgeBase(kbId: string, data: Partial<KnowledgeBase>): Promise<KnowledgeBase> {
  return putData<KnowledgeBase>(`${BASE}/knowledge/${kbId}`, data);
}

export function deleteKnowledgeBase(kbId: string): Promise<void> {
  return delData<void>(`${BASE}/knowledge/${kbId}`);
}

// ===== 文档管理 =====

export function getDocuments(params: { kb_id?: string; status?: string; keyword?: string; page?: number; size?: number } = {}): Promise<PageResponse<Document>> {
  return getData<PageResponse<Document>>(`${BASE}/documents`, params as Record<string, string | number>);
}

export function getDocument(docId: string): Promise<Document> {
  return getData<Document>(`${BASE}/documents/${docId}`);
}

export function updateDocument(docId: string, data: Partial<Document>): Promise<Document> {
  return putData<Document>(`${BASE}/documents/${docId}`, data);
}

/** 获取文档解析摘要（preview/structure/warnings/pages/char_count/parse_status） */
export function getDocumentSummary(docId: string): Promise<DocSummary> {
  return getData<DocSummary>(`${BASE}/documents/${docId}/summary`);
}

// ===== 文档版本历史 =====

export function getDocumentVersions(docId: string): Promise<DocVersion[]> {
  return getData<DocVersion[]>(`${BASE}/documents/${docId}/versions`);
}

export function restoreDocumentVersion(docId: string, versionId: string): Promise<void> {
  return postData<void>(`${BASE}/documents/${docId}/versions/${versionId}/restore`);
}

// ===== 搜索 =====

export interface SearchResult {
  id: string;
  title: string;
  content: string;
  highlight: string;
  score: number;
  kb_id: string;
  kb_name: string;
  doc_type: string;
}

export function searchKnowledge(params: { q: string; kb_ids?: string; search_type?: string; page?: number; page_size?: number }): Promise<PageResponse<SearchResult>> {
  return getData<PageResponse<SearchResult>>(`${BASE}/search`, params as Record<string, string | number>);
}

// ===== 统一搜索 =====

export interface ExternalSearchResult {
  id: string;
  title: string;
  content: string;
  highlight: string;
  source: string; // knowledge/oa/erp/crm/mail
  source_url: string;
  source_icon: string;
  score: number;
}

/** 跨系统统一搜索（知识库 + OA + ERP + CRM + Mail 并行） */
export function unifiedSearch(params: {
  q: string;
  sources?: string;
  top_k?: number;
}): Promise<Record<string, ExternalSearchResult[]>> {
  return getData<Record<string, ExternalSearchResult[]>>(
    `${BASE}/search/unified`,
    params as Record<string, string | number>
  );
}
