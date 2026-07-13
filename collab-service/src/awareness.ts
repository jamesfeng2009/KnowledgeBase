/**
 * @file awareness.ts
 * @description 协作者状态管理器。基于 y-protocols/awareness 的 Awareness 类，
 *              在其之上提供在线协作者查询、光标更新、加入/离开通知等高层 API。
 *
 * 每个文档维护一个 Awareness 实例（与标准 Yjs 客户端协议兼容），
 * 同时维护一个 Map<clientId, AwarenessState> 供前端查询当前在线协作者。
 */

import * as Y from 'yjs';
import * as awarenessProtocol from 'y-protocols/awareness';
import { Awareness } from 'y-protocols/awareness';
import type { AwarenessState } from './types.js';

/**
 * 协作者状态管理器。维护每个文档的 Awareness 实例与可查询的协作者列表。
 */
export class AwarenessManager {
  /** 每个文档的 Awareness 实例（docId → Awareness） */
  private docsAwareness = new Map<string, Awareness>();
  /** 每个文档的协作者状态（docId → clientId → AwarenessState） */
  private collaborators = new Map<string, Map<number, AwarenessState>>();

  /**
   * 获取（或创建）文档的 Awareness 实例。
   * @param docId 文档 ID
   * @param ydoc 文档的 Yjs Doc，首次创建时关联
   */
  getOrCreate(docId: string, ydoc: Y.Doc): Awareness {
    let awareness = this.docsAwareness.get(docId);
    if (!awareness) {
      awareness = new Awareness(ydoc);
      this.docsAwareness.set(docId, awareness);
      this.collaborators.set(docId, new Map());
    }
    return awareness;
  }

  /**
   * 获取文档的 Awareness 实例（不存在返回 undefined）。
   */
  get(docId: string): Awareness | undefined {
    return this.docsAwareness.get(docId);
  }

  /**
   * 应用 awareness 二进制更新（来自远端客户端）。
   * 同步更新本地可查询的协作者列表。
   * @param docId 文档 ID
   * @param update awareness 编码更新（Uint8Array）
   * @param origin 消息来源（通常为 WebSocket 连接）
   */
  applyUpdate(docId: string, update: Uint8Array, origin?: unknown): void {
    const awareness = this.docsAwareness.get(docId);
    if (!awareness) return;
    awarenessProtocol.applyAwarenessUpdate(awareness, update, origin);
    this.syncCollaborators(docId, awareness);
  }

  /**
   * 从 Awareness 实例同步协作者列表到可查询的 Map。
   * @param docId 文档 ID
   * @param awareness Awareness 实例
   */
  private syncCollaborators(docId: string, awareness: Awareness): void {
    const map = this.collaborators.get(docId);
    if (!map) return;
    const states = awareness.getStates();
    // 清理已不存在的客户端
    for (const clientId of map.keys()) {
      if (!states.has(clientId)) {
        map.delete(clientId);
      }
    }
    // 写入当前状态
    for (const [clientId, state] of states) {
      const parsed = this.parseAwarenessState(clientId, state);
      if (parsed) {
        map.set(clientId, parsed);
      }
    }
  }

  /**
   * 解析 awareness state 为 AwarenessState。
   * awareness state 为任意 JSON 对象（前端写入）。
   * @param clientId Yjs 客户端 ID
   * @param state 原始 awareness state 对象
   */
  private parseAwarenessState(
    clientId: number,
    state: Record<string, unknown> | null
  ): AwarenessState | null {
    if (!state) return null;
    return {
      clientId,
      name: (state.name as string) ?? `用户${clientId}`,
      color: (state.color as string) ?? '#6b7280',
      cursor: (state.cursor as { x: number; y: number } | null) ?? null,
      selection:
        (state.selection as { start: number; end: number } | null) ?? null,
    };
  }

  /**
   * 处理协作者加入。若其已携带初始 awareness state 则记入列表。
   * @param docId 文档 ID
   * @param clientId 客户端 ID
   */
  handleJoin(docId: string, clientId: number): void {
    const map = this.collaborators.get(docId);
    const awareness = this.docsAwareness.get(docId);
    if (!map || !awareness) return;
    const state = awareness.getStates().get(clientId);
    if (state) {
      const parsed = this.parseAwarenessState(clientId, state);
      if (parsed) map.set(clientId, parsed);
    }
  }

  /**
   * 处理协作者离开。从 Awareness 与可查询列表中移除。
   * @param docId 文档 ID
   * @param clientId 客户端 ID
   * @param origin 移除来源（WebSocket 连接）
   */
  handleLeave(docId: string, clientId: number, origin?: unknown): void {
    const awareness = this.docsAwareness.get(docId);
    if (awareness) {
      // 通知其他协作者该客户端已离线
      awarenessProtocol.removeAwarenessStates(awareness, [clientId], origin);
    }
    this.collaborators.get(docId)?.delete(clientId);
  }

  /**
   * 查询文档当前在线协作者列表（供前端调用）。
   * @param docId 文档 ID
   * @returns 协作者状态数组
   */
  getCollaborators(docId: string): AwarenessState[] {
    const map = this.collaborators.get(docId);
    if (!map) return [];
    return Array.from(map.values());
  }

  /**
   * 清理文档的 awareness 资源（最后一个连接断开时调用）。
   * @param docId 文档 ID
   */
  cleanup(docId: string): void {
    this.docsAwareness.delete(docId);
    this.collaborators.delete(docId);
  }
}
