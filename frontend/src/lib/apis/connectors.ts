/**
 * 企业连接器 API 封装
 * 对接后端 connectors.py 路由
 *
 * NOTE: 当前文件整体为 dead code（getConnectors/testConnector/toggleConnector
 * 三个函数均未被任何页面或组件引用）。前端尚未提供连接器管理页面。
 * 保留本文件供 P5 连接器管理页面对接使用，不删除。
 */
import { getData, postData, putData } from '../api';

const BASE = '/api/v1/connectors';

// ===== 类型定义 =====

export interface Connector {
  id: string;
  name: string;
  type: string;
  status: 'active' | 'inactive' | 'error';
  last_sync: string | null;
  config: Record<string, unknown>;
}

export interface ConnectorTestResult {
  success: boolean;
  message: string;
}

// ===== API 方法 =====

/** 获取所有连接器列表及状态 */
export function getConnectors(): Promise<Connector[]> {
  return getData<Connector[]>(BASE);
}

/** 测试连接器连通性 */
export function testConnector(connectorId: string): Promise<ConnectorTestResult> {
  return postData<ConnectorTestResult>(`${BASE}/${connectorId}/test`);
}

/** 启用/停用连接器 */
export function toggleConnector(connectorId: string, active: boolean): Promise<void> {
  return putData<void>(`${BASE}/${connectorId}/toggle?active=${active}`);
}
