/**
 * @file persistence.ts
 * @description PostgreSQL 持久化管理器。负责加载 / 保存 Yjs 文档二进制状态，
 *              并维护文档版本历史。PostgreSQL 不可用时自动降级为内存模式，
 *              保证协作服务不崩溃（数据仅在内存中，重启丢失）。
 */

import { Pool, type PoolClient } from 'pg';
import * as Y from 'yjs';

/**
 * Yjs 文档持久化管理器。
 *
 * 表结构：
 *  - yjs_documents(doc_id PK, content BYTEA, created_at, updated_at)  — 最新文档状态
 *  - yjs_doc_versions(doc_id, version_id PK, content BYTEA, author, created_at) — 版本历史
 */
export class PersistenceManager {
  /** pg 连接池 */
  private pool: Pool;
  /** PostgreSQL 是否可用（false 时进入内存降级模式） */
  public available = false;

  /**
   * @param databaseUrl PostgreSQL 连接字符串，如 postgresql://ekb:ekb@postgres:5432/ekb
   */
  constructor(databaseUrl: string) {
    this.pool = new Pool({ connectionString: databaseUrl });
  }

  /**
   * 初始化持久化：建表 + 探活。
   * 失败时将 available 置为 false，协作服务继续以内存模式运行。
   */
  async initPersistence(): Promise<void> {
    try {
      // 探活连接
      const client = await this.pool.connect();
      try {
        await client.query(`
          CREATE TABLE IF NOT EXISTS yjs_documents (
            doc_id VARCHAR(64) PRIMARY KEY,
            content BYTEA NOT NULL,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
          )
        `);
        await client.query(`
          CREATE TABLE IF NOT EXISTS yjs_doc_versions (
            id SERIAL PRIMARY KEY,
            doc_id VARCHAR(64) NOT NULL,
            version_id VARCHAR(64) NOT NULL,
            content BYTEA NOT NULL,
            author VARCHAR(255),
            created_at TIMESTAMPTZ DEFAULT NOW()
          )
        `);
        await client.query(
          `CREATE INDEX IF NOT EXISTS idx_yjs_doc_versions_doc_id ON yjs_doc_versions(doc_id)`
        );
        this.available = true;
        console.log('[persistence] PostgreSQL 就绪，文档持久化已启用');
      } finally {
        client.release();
      }
    } catch (err) {
      this.available = false;
      console.error('[persistence] PostgreSQL 不可用，降级为内存模式（数据不持久化）:', err);
    }
  }

  /**
   * 加载文档二进制状态。
   * @returns 文档的 Yjs state update；不存在或 PG 不可用时返回 null
   */
  async loadDoc(docId: string): Promise<Uint8Array | null> {
    if (!this.available) return null;
    let client: PoolClient | null = null;
    try {
      client = await this.pool.connect();
      const result = await client.query(
        'SELECT content FROM yjs_documents WHERE doc_id = $1',
        [docId]
      );
      if (result.rows.length === 0) return null;
      // pg 返回 BYTEA 为 Buffer，转为 Uint8Array
      const buf: Buffer = result.rows[0].content;
      return new Uint8Array(buf.buffer, buf.byteOffset, buf.byteLength);
    } catch (err) {
      console.error(`[persistence] loadDoc(${docId}) 失败:`, err);
      return null;
    } finally {
      if (client) client.release();
    }
  }

  /**
   * 保存文档（合并更新）。将传入的 update 合并到已有内容后写回全量状态。
   * @param docId 文档 ID
   * @param update Yjs 增量更新或全量 state update
   */
  async saveDoc(docId: string, update: Uint8Array): Promise<void> {
    if (!this.available) return;
    let client: PoolClient | null = null;
    try {
      client = await this.pool.connect();
      await client.query('BEGIN');

      // 读取已有内容
      const existing = await client.query(
        'SELECT content FROM yjs_documents WHERE doc_id = $1 FOR UPDATE',
        [docId]
      );

      // 合并：加载已有 → 应用 update → 编码全量状态
      const merged = new Y.Doc();
      if (existing.rows.length > 0) {
        const buf: Buffer = existing.rows[0].content;
        const prev = new Uint8Array(buf.buffer, buf.byteOffset, buf.byteLength);
        Y.applyUpdate(merged, prev);
      }
      Y.applyUpdate(merged, update);
      const fullState = Y.encodeStateAsUpdate(merged);

      if (existing.rows.length === 0) {
        await client.query(
          `INSERT INTO yjs_documents (doc_id, content, created_at, updated_at)
           VALUES ($1, $2, NOW(), NOW())`,
          [docId, Buffer.from(fullState)]
        );
      } else {
        await client.query(
          `UPDATE yjs_documents SET content = $2, updated_at = NOW() WHERE doc_id = $1`,
          [docId, Buffer.from(fullState)]
        );
      }

      await client.query('COMMIT');
    } catch (err) {
      console.error(`[persistence] saveDoc(${docId}) 失败:`, err);
      try {
        if (client) await client.query('ROLLBACK');
      } catch {
        /* ignore rollback error */
      }
    } finally {
      if (client) client.release();
    }
  }

  /**
   * 保存一个文档版本快照（用于版本历史 / 回滚）。
   * @param docId 文档 ID
   * @param versionId 版本 ID（前端生成，如 UUID 或时间戳）
   * @param author 操作作者
   */
  async saveVersion(
    docId: string,
    versionId: string,
    author?: string
  ): Promise<void> {
    if (!this.available) return;
    let client: PoolClient | null = null;
    try {
      const fullState = await this.loadDoc(docId);
      if (!fullState) return; // 文档不存在则跳过
      client = await this.pool.connect();
      await client.query(
        `INSERT INTO yjs_doc_versions (doc_id, version_id, content, author, created_at)
         VALUES ($1, $2, $3, $4, NOW())`,
        [docId, versionId, Buffer.from(fullState), author ?? null]
      );
    } catch (err) {
      console.error(`[persistence] saveVersion(${docId}) 失败:`, err);
    } finally {
      if (client) client.release();
    }
  }

  /**
   * 关闭连接池。用于优雅关闭。
   */
  async close(): Promise<void> {
    try {
      await this.pool.end();
      console.log('[persistence] 连接池已关闭');
    } catch (err) {
      console.error('[persistence] 关闭连接池失败:', err);
    }
  }
}
