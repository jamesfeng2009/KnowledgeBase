/**
 * @file connection.ts
 * @description WebSocket 连接管理器。处理协同编辑的 Yjs sync 协议与
 *              awareness 协议，负责文档同步、更新广播与持久化、连接生命周期管理。
 *
 * 消息协议（与 y-websocket 兼容，二进制传输）：
 *  外层 message type（varuint）：
 *   - 0 sync          → readSyncMessage 处理 step1/step2/update
 *   - 1 queryAwareness → 响应当前 awareness 全量状态
 *   - 2 awareness     → applyAwarenessUpdate + 广播
 */

import { WebSocket } from 'ws';
import * as Y from 'yjs';
import * as encoding from 'lib0/encoding';
import * as decoding from 'lib0/decoding';
import * as syncProtocol from 'y-protocols/sync';
import * as awarenessProtocol from 'y-protocols/awareness';
import type { Awareness } from 'y-protocols/awareness';
import type { DocState } from './types.js';
import { PersistenceManager } from './persistence.js';
import { AwarenessManager } from './awareness.js';

/** 外层消息类型常量 */
const messageSync = 0;
const messageQueryAwareness = 1;
const messageAwareness = 2;

/** 活跃文档缓存：docId → DocState */
export const docs = new Map<string, DocState>();

/** 持久化去抖：docId → 待执行的保存定时器（避免每次按键都写 PG） */
const saveTimers = new Map<string, NodeJS.Timeout>();
/** 持久化去抖间隔（ms） */
const SAVE_DEBOUNCE_MS = 500;

/**
 * 处理新的 WebSocket 连接。
 * 1. 加载或创建 DocState（从 PostgreSQL 恢复历史内容）
 * 2. 发送初始同步（sync step 1）与当前 awareness 状态
 * 3. 注册 message / close / error 事件监听
 *
 * @param conn WebSocket 连接
 * @param docId 文档 ID
 * @param persistence PostgreSQL 持久化管理器
 * @param awarenessManager awareness 管理器
 */
export async function handleConnection(
  conn: WebSocket,
  docId: string,
  persistence: PersistenceManager,
  awarenessManager: AwarenessManager
): Promise<void> {
  try {
    let state = docs.get(docId);

    if (!state) {
      state = {
        ydoc: new Y.Doc(),
        connections: new Set(),
        lastModified: Date.now(),
      };
      docs.set(docId, state);

      // 从 PostgreSQL 恢复历史内容
      const saved = await persistence.loadDoc(docId);
      if (saved && saved.byteLength > 0) {
        Y.applyUpdate(state.ydoc, saved, 'server');
        console.log(`[connection] 文档 ${docId} 已从 PostgreSQL 恢复`);
      }
    }

    // 初始化该文档的 awareness
    awarenessManager.getOrCreate(docId, state.ydoc);

    state.connections.add(conn);

    // 1. 发送初始同步：sync step 1（请求客户端的状态向量）
    const syncEncoder = encoding.createEncoder();
    encoding.writeVarUint(syncEncoder, messageSync);
    syncProtocol.writeSyncStep1(syncEncoder, state.ydoc);
    send(conn, encoding.toUint8Array(syncEncoder));

    // 2. 发送当前 awareness 全量状态
    const awareness = awarenessManager.get(docId);
    if (awareness) {
      const awEncoder = encoding.createEncoder();
      encoding.writeVarUint(awEncoder, messageAwareness);
      const clientIds = Array.from(awareness.getStates().keys());
      const awUpdate = awarenessProtocol.encodeAwarenessUpdate(awareness, clientIds);
      encoding.writeVarUint8Array(awEncoder, awUpdate);
      send(conn, encoding.toUint8Array(awEncoder));
    }

    conn.on('message', (data: ArrayBuffer | Buffer | Uint8Array) =>
      handleMessage(conn, state!, new Uint8Array(toUint8(data)), docId, persistence, awarenessManager)
    );
    conn.on('close', () =>
      handleClose(conn, state!, docId, persistence, awarenessManager)
    );
    conn.on('error', (err: Error) =>
      console.error(`[connection] WebSocket 错误 (doc=${docId}):`, err.message)
    );
  } catch (err) {
    console.error(`[connection] handleConnection(${docId}) 异常:`, err);
    conn.close();
  }
}

/**
 * 处理来自客户端的消息。
 * - sync：解析 sync step1/step2/update
 * - awareness：应用 awareness 更新并广播
 * - queryAwareness：返回当前 awareness 全量状态
 *
 * @param conn WebSocket 连接
 * @param state 文档状态
 * @param data 原始二进制消息
 * @param docId 文档 ID
 * @param persistence 持久化管理器
 * @param awarenessManager awareness 管理器
 */
export function handleMessage(
  conn: WebSocket,
  state: DocState,
  data: Uint8Array,
  docId: string,
  persistence: PersistenceManager,
  awarenessManager: AwarenessManager
): void {
  try {
    if (data.length === 0) return;

    const decoder = decoding.createDecoder(data);
    const messageType = decoding.readVarUint(decoder);
    const awareness = awarenessManager.get(docId);

    switch (messageType) {
      case messageSync: {
        const encoder = encoding.createEncoder();
        // readSyncMessage 内部读取子类型（step1/step2/update）并处理
        const syncType = syncProtocol.readSyncMessage(
          decoder,
          encoder,
          state.ydoc,
          conn
        );

        // 若有响应（如 step1 的 step2 响应），回传给发送方
        const response = encoding.toUint8Array(encoder);
        if (response.length > 1) {
          send(conn, response);
        }

        // step2 / update 表示文档被修改：广播 + 持久化
        if (
          syncType === syncProtocol.messageYjsSyncStep2 ||
          syncType === syncProtocol.messageYjsUpdate
        ) {
          broadcastUpdate(conn, state, data);
          schedulePersist(docId, state, persistence);
          state.lastModified = Date.now();
        }
        break;
      }
      case messageAwareness: {
        const update = decoding.readVarUint8Array(decoder);
        if (awareness) {
          awarenessManager.applyUpdate(docId, update, conn);
        }
        broadcastUpdate(conn, state, data);
        break;
      }
      case messageQueryAwareness: {
        if (awareness) {
          const encoder = encoding.createEncoder();
          encoding.writeVarUint(encoder, messageAwareness);
          const clientIds = Array.from(awareness.getStates().keys());
          const awUpdate = awarenessProtocol.encodeAwarenessUpdate(awareness, clientIds);
          encoding.writeVarUint8Array(encoder, awUpdate);
          send(conn, encoding.toUint8Array(encoder));
        }
        break;
      }
      default:
        console.warn(`[connection] 未知消息类型 ${messageType} (doc=${docId})`);
    }
  } catch (err) {
    console.error(`[connection] handleMessage 异常 (doc=${docId}):`, err);
  }
}

/**
 * 广播更新给除发送方外的所有连接。
 * @param sender 发起更新的连接（不回传给自身）
 * @param state 文档状态
 * @param data 原始二进制消息
 */
export function broadcastUpdate(
  sender: WebSocket,
  state: DocState,
  data: Uint8Array
): void {
  for (const conn of state.connections) {
    if (conn !== sender && conn.readyState === WebSocket.OPEN) {
      send(conn, data);
    }
  }
}

/**
 * 处理连接关闭。
 * - 移除连接
 * - 清理该客户端的 awareness 状态并广播其离线
 * - 最后一个连接断开时保存并清理文档
 *
 * @param conn 关闭的连接
 * @param state 文档状态
 * @param docId 文档 ID
 * @param persistence 持久化管理器
 * @param awarenessManager awareness 管理器
 */
export function handleClose(
  conn: WebSocket,
  state: DocState,
  docId: string,
  persistence: PersistenceManager,
  awarenessManager: AwarenessManager
): void {
  try {
    state.connections.delete(conn);

    // 清理该连接的 awareness 状态并通知其他协作者
    const awareness = awarenessManager.get(docId) as (Awareness & {
      clientID: number;
    }) | undefined;
    if (awareness) {
      // 移除该客户端的 awareness 状态（awarenessProtocol 内部会广播 remove）
      awarenessManager.handleLeave(docId, awareness.clientID, conn);
      // 将 awareness 更新广播给剩余连接
      const encoder = encoding.createEncoder();
      encoding.writeVarUint(encoder, messageAwareness);
      const awUpdate = awarenessProtocol.encodeAwarenessUpdate(
        awareness,
        Array.from(awareness.getStates().keys())
      );
      encoding.writeVarUint8Array(encoder, awUpdate);
      const update = encoding.toUint8Array(encoder);
      for (const c of state.connections) {
        if (c.readyState === WebSocket.OPEN) send(c, update);
      }
    }

    // 最后一个连接断开：保存并清理文档缓存
    if (state.connections.size === 0) {
      flushPersist(docId, state, persistence).catch((err) =>
        console.error(`[connection] 最终持久化失败 (doc=${docId}):`, err)
      );
      docs.delete(docId);
      awarenessManager.cleanup(docId);
      console.log(`[connection] 文档 ${docId} 无活跃连接，已清理缓存`);
    }
  } catch (err) {
    console.error(`[connection] handleClose 异常 (doc=${docId}):`, err);
  }
}

/**
 * 去抖调度文档持久化。合并短时间内的多次更新为一次写库。
 */
function schedulePersist(
  docId: string,
  state: DocState,
  persistence: PersistenceManager
): void {
  const existing = saveTimers.get(docId);
  if (existing) clearTimeout(existing);
  const timer = setTimeout(() => {
    saveTimers.delete(docId);
    flushPersist(docId, state, persistence).catch((err) =>
      console.error(`[connection] 去抖持久化失败 (doc=${docId}):`, err)
    );
  }, SAVE_DEBOUNCE_MS);
  saveTimers.set(docId, timer);
}

/**
 * 立即持久化文档当前全量状态。
 */
async function flushPersist(
  docId: string,
  state: DocState,
  persistence: PersistenceManager
): Promise<void> {
  try {
    const fullState = Y.encodeStateAsUpdate(state.ydoc);
    await persistence.saveDoc(docId, fullState);
  } catch (err) {
    console.error(`[connection] 持久化异常 (doc=${docId}):`, err);
  }
}

/**
 * 向连接发送二进制数据。仅当连接处于 OPEN 状态时发送。
 */
function send(conn: WebSocket, data: Uint8Array): void {
  if (conn.readyState === WebSocket.OPEN) {
    conn.send(data);
  }
}

/**
 * 将多种二进制类型归一化为 Uint8Array。
 */
function toUint8(data: ArrayBuffer | Buffer | Uint8Array): Uint8Array {
  if (data instanceof Uint8Array) return data;
  if (data instanceof ArrayBuffer) return new Uint8Array(data);
  // Buffer (Node.js)
  const buf = data as unknown as { buffer: ArrayBuffer; byteOffset: number; byteLength: number };
  return new Uint8Array(buf.buffer, buf.byteOffset, buf.byteLength);
}
