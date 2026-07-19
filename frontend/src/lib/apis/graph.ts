/**
 * 知识图谱 API 封装
 * 对接后端 graph.py 路由
 */
import { getData, postData, delData } from '../api';

const BASE = '/api/v1/graph';

// ===== 类型定义 =====

export interface GraphNode {
  id: string;
  label: string;
  type: string;
  properties: Record<string, unknown>;
}

export interface GraphEdge {
  source: string;
  target: string;
  type: string;
  properties: Record<string, unknown>;
}

export interface GraphData {
  nodes: GraphNode[];
  edges: GraphEdge[];
}

export interface GraphStats {
  total_nodes: number;
  total_edges: number;
  node_types: Record<string, number>;
  edge_types: Record<string, number>;
}

export interface Recommendation {
  doc_id: string;
  title: string;
  score: number;
  reason: string;
}

// ===== API 方法 =====

/** 获取图谱可视化数据（节点+边） */
export function getGraphData(params: { node_label?: string; node_id?: string; limit?: number } = {}): Promise<GraphData> {
  return getData<GraphData>(`${BASE}/data`, params as Record<string, string | number>);
}

/** 获取节点详情 */
export function getNode(nodeId: string, label = 'Document'): Promise<GraphNode> {
  return getData<GraphNode>(`${BASE}/nodes/${nodeId}`, { label });
}

/** 多跳图遍历查找相关节点 */
export function getRelatedNodes(nodeId: string, params: { label?: string; max_depth?: number } = {}): Promise<GraphNode[]> {
  return getData<GraphNode[]>(`${BASE}/nodes/${nodeId}/related`, params as Record<string, string | number>);
}

/** 获取图谱统计信息 */
export function getGraphStats(): Promise<GraphStats> {
  return getData<GraphStats>(`${BASE}/stats`);
}

/** 文档关联推荐 */
export function getRecommendations(docId: string, topK = 5): Promise<Recommendation[]> {
  return getData<Recommendation[]>(`${BASE}/recommendations/${docId}`, { top_k: topK });
}

/** 从文档构建知识图谱 */
export function buildGraphFromDocument(docId: string, useRules = true, useLlm = true): Promise<{ nodes_created: number; edges_created: number }> {
  return postData<{ nodes_created: number; edges_created: number }>(`${BASE}/documents/${docId}/build-graph?use_rules=${useRules}&use_llm=${useLlm}`);
}

/** 失效文档推荐缓存 */
export function invalidateRecommendationCache(docId: string): Promise<void> {
  return delData<void>(`${BASE}/recommendations/${docId}/cache`);
}
