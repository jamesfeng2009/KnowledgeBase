/**
 * 多模态处理 API 封装
 * 对接后端 multimodal.py 路由
 * 功能：图片 VLM 解析 / 表格结构化 / 扫描件 OCR / 白板拍照入库
 */
import { upload } from '../api';

const BASE = '/api/v1/multimodal';

// ===== 类型定义 =====

export interface ImageAnalysisResult {
  description: string;
  tags: string[];
  ocr_text: string | null;
}

export interface TableStructureResult {
  headers: string[];
  rows: string[][];
  raw_json: object;
}

export interface OcrResult {
  text: string;
  pages: number;
  confidence: number;
}

export interface WhiteboardMinutes {
  summary: string;
  key_points: string[];
  action_items: {
    content: string;
    assignee: string | null;
    deadline: string | null;
  }[];
  raw_text: string;
}

// ===== API 方法 =====

/** 图片智能解析（VLM 生成描述和标签） */
export function analyzeImage(file: File): Promise<ImageAnalysisResult> {
  const formData = new FormData();
  formData.append('file', file);
  return upload<ImageAnalysisResult>(`${BASE}/image`, formData);
}

/** 表格结构化（VLM 识别行列结构） */
export function analyzeTable(file: File): Promise<TableStructureResult> {
  const formData = new FormData();
  formData.append('file', file);
  return upload<TableStructureResult>(`${BASE}/table`, formData);
}

/** 扫描件 OCR（VLM 识别文字） */
export function ocrScannedPdf(file: File): Promise<OcrResult> {
  const formData = new FormData();
  formData.append('file', file);
  return upload<OcrResult>(`${BASE}/scanned-pdf`, formData);
}

/** 白板拍照入库（VLM 生成会议纪要） */
export function uploadWhiteboard(file: File): Promise<WhiteboardMinutes> {
  const formData = new FormData();
  formData.append('file', file);
  return upload<WhiteboardMinutes>(`${BASE}/whiteboard`, formData);
}
