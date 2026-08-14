/**
 * 知识库与文档 API 封装
 * 对接后端 knowledge.py + documents.py 路由
 */
import { getData, postData, putData, delData, API_BASE, ApiError, type PageResponse } from '../api';

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

/** 文档解析进度（P1 增强：真实阶段反馈，替代前端模拟进度） */
export interface DocParseProgress {
  stage: 'queued' | 'parsing' | 'chunking' | 'embedding' | 'indexing' | 'publishing' | 'done' | 'failed' | 'unknown';
  current: number;
  total: number;
  message: string;
  /** P2-E: 子阶段标识（如 "asr_segment_3" / "keyframe_extract"） */
  sub_stage?: string;
  /** P2-E: 子阶段当前进度（如已完成的 ASR 段数） */
  sub_current?: number;
  /** P2-E: 子阶段总进度（如总 ASR 段数） */
  sub_total?: number;
}

/** 查询文档解析进度（从 Redis 读取 Celery 任务实时写入的进度） */
export function getDocumentProgress(docId: string): Promise<DocParseProgress> {
  return getData<DocParseProgress>(`${BASE}/documents/${docId}/progress`);
}

// ===== 分片上传（GB 级视频）=====

/** 分片上传初始化返回结构 */
export interface MultipartUploadInit {
  upload_id: string;
  object_name: string;
}

/** 单个分片上传返回结构 */
export interface MultipartPartResult {
  etag: string;
}

/** 分片信息（用于 complete 请求体） */
export interface MultipartPart {
  part_number: number;
  etag: string;
}

/** 服务端已上传分片信息（listUploadedParts 返回的单个分片） */
export interface MultipartUploadedPart {
  part_number: number;
  etag: string;
  size: number;
}

/** listUploadedParts 返回结构（complete 前服务端对账用） */
export interface MultipartUploadedPartsList {
  parts: MultipartUploadedPart[];
  count: number;
}

/**
 * 初始化分片上传
 * 后端在对象存储（MinIO/Ceph S3 协议）创建多段上传会话，返回 upload_id 和 object_name
 */
export function initMultipartUpload(
  kbId: string,
  title: string,
  filename: string
): Promise<MultipartUploadInit> {
  const params = new URLSearchParams({ kb_id: kbId, title, filename });
  return postData<MultipartUploadInit>(`${BASE}/documents/multipart/init?${params.toString()}`);
}

/**
 * 上传单个分片（二进制 body，手动拼接 URL 与 Authorization header）
 * 不能用 putData 封装，因为 putData 会 JSON.stringify body
 * @param uploadId - 分片上传会话 ID
 * @param partNumber - 分片序号（从 1 开始）
 * @param data - 分片二进制数据
 * @param signal - AbortSignal，用于取消上传
 */
export async function uploadPart(
  uploadId: string,
  partNumber: number,
  data: Blob,
  signal?: AbortSignal
) {
  const url = `${API_BASE}${BASE}/documents/multipart/${uploadId}/parts/${partNumber}`;
  const response = await fetch(url, {
    method: 'PUT',
    credentials: 'include', // 必须：携带 HttpOnly Cookie
    body: data,
    signal,
  });
  if (!response.ok) {
    let message = `分片上传失败 (${response.status})`;
    try {
      const errBody = await response.json();
      const errData = errBody?.data ?? errBody;
      if (errData?.message) message = errData.message;
      else if (errBody?.message) message = errBody.message;
    } catch {
      // 响应非 JSON，使用默认错误信息
    }
    throw new ApiError(message, response.status);
  }
  // S3 空响应体时 .json() 会抛 SyntaxError，兜底返回空对象
  const result = await response.json().catch(() => ({}));
  // 兼容后端统一响应格式 { code, data, message }
  if (result && typeof result === 'object' && 'data' in result && 'code' in result) {
    return result.data as MultipartPartResult;
  }
  return result as MultipartPartResult;
}

/**
 * 完成分片上传
 * 后端合并对象存储分片 → 创建 Document 记录（status='draft'）→ 触发解析流程
 */
export function completeMultipartUpload(
  uploadId: string,
  parts: MultipartPart[],
  objectName: string,
  kbId: string,
  title: string,
  docType: string
): Promise<Document> {
  return postData<Document>(`${BASE}/documents/multipart/${uploadId}/complete`, {
    parts,
    object_name: objectName,
    kb_id: kbId,
    title,
    doc_type: docType,
  });
}

/**
 * 取消分片上传
 * 清理对象存储中的临时分片，释放存储空间
 */
export function abortMultipartUpload(
  uploadId: string,
  objectName: string
): Promise<void> {
  const params = new URLSearchParams({ object_name: objectName });
  return delData<void>(`${BASE}/documents/multipart/${uploadId}?${params.toString()}`);
}

/**
 * 列出已上传的分片（P0: complete 前服务端对账用）
 * 查询对象存储中该 upload_id 已接收的分片列表，用于前端与服务端状态对账
 * @param uploadId - 分片上传会话 ID
 * @param objectName - 对象存储对象名（init 时返回）
 */
export function listUploadedParts(
  uploadId: string,
  objectName: string
): Promise<MultipartUploadedPartsList> {
  const params = new URLSearchParams({ object_name: objectName });
  return getData<MultipartUploadedPartsList>(
    `${BASE}/documents/multipart/${uploadId}/parts?${params.toString()}`
  );
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
