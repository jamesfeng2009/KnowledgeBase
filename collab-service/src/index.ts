/**
 * @file index.ts
 * @description Yjs 协作服务入口。
 *
 * 职责：
 *  - 在端口 8001 启动 HTTP + WebSocket 服务
 *  - 路由：/ws/collab → 协同编辑；/ws/comments → 评论通知；/health → 健康检查
 *  - 从 URL query 参数解析 JWT Token（仅解码，签名由 APISIX 验证）
 *  - 初始化 PostgreSQL 持久化（不可用时降级内存模式）
 *  - 优雅关闭处理
 */

import { createServer, type IncomingMessage, type ServerResponse } from 'http';
import { WebSocketServer, WebSocket } from 'ws';
import { PersistenceManager } from './persistence.js';
import { AwarenessManager } from './awareness.js';
import { CommentsService } from './comments.js';
import { handleConnection, docs } from './connection.js';
import type { JwtPayload } from './types.js';

/** 服务监听端口 */
const PORT = Number(process.env.YJS_PORT ?? process.env.PORT ?? 8001);

/** 全局 awareness 管理器 */
const awarenessManager = new AwarenessManager();
/** 评论通知服务 */
const commentsService = new CommentsService();

/**
 * 服务启动主流程。
 */
async function main(): Promise<void> {
  // 1. 初始化持久化（失败时降级内存模式）
  const databaseUrl = process.env.DATABASE_URL ?? 'postgresql://ekb:ekb@localhost:5432/ekb';
  const persistence = new PersistenceManager(databaseUrl);
  await persistence.initPersistence();

  // 2. 创建 HTTP 服务器（处理健康检查）
  const server = createServer((req: IncomingMessage, res: ServerResponse) => {
    handleHealth(req, res);
  });

  // 3. 两个 WebSocket 服务共享同一 HTTP 服务器（noServer + 手动 upgrade 路由）
  const collabWss = new WebSocketServer({ noServer: true });
  const commentsWss = new WebSocketServer({ noServer: true });

  server.on('upgrade', (req, socket, head) => {
    const url = parseUrl(req);
    const path = url.pathname;
    if (path === '/ws/collab') {
      collabWss.handleUpgrade(req, socket, head, (ws) => {
        collabWss.emit('connection', ws, req);
      });
    } else if (path === '/ws/comments') {
      commentsWss.handleUpgrade(req, socket, head, (ws) => {
        commentsWss.emit('connection', ws, req);
      });
    } else {
      socket.destroy();
    }
  });

  // 4. 协同编辑连接处理
  collabWss.on('connection', (conn: WebSocket, req: IncomingMessage) => {
    const url = parseUrl(req);
    const token = url.searchParams.get('token');
    const docId = url.searchParams.get('doc') || 'default';

    const user = verifyToken(token);
    if (!user) {
      conn.close(4001, 'unauthorized: invalid or missing token');
      return;
    }
    handleConnection(conn, docId, persistence, awarenessManager);
  });

  // 5. 评论通知连接处理
  commentsWss.on('connection', (conn: WebSocket, req: IncomingMessage) => {
    const url = parseUrl(req);
    const token = url.searchParams.get('token');
    const user = verifyToken(token);
    if (!user) {
      conn.close(4001, 'unauthorized: invalid or missing token');
      return;
    }
    commentsService.handleConnection(conn, req, user);
  });

  // 6. 启动监听
  server.listen(PORT, () => {
    console.log(`[yjs-server] 协作服务已启动，端口 ${PORT}`);
    console.log(`[yjs-server]   /ws/collab   协同编辑`);
    console.log(`[yjs-server]   /ws/comments 评论通知`);
    console.log(`[yjs-server]   /health      健康检查`);
    console.log(
      `[yjs-server] 持久化: ${persistence.available ? 'PostgreSQL' : '内存模式（降级）'}`
    );
  });

  // 7. 优雅关闭
  setupGracefulShutdown(server, collabWss, commentsWss, persistence);
}

/**
 * 处理 HTTP 健康检查 GET /health。
 */
function handleHealth(req: IncomingMessage, res: ServerResponse): void {
  if (req.method === 'GET' && (req.url?.split('?')[0] === '/health')) {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(
      JSON.stringify({
        status: 'ok',
        service: 'ekb-yjs-server',
        port: PORT,
        activeDocs: docs.size,
        timestamp: new Date().toISOString(),
      })
    );
    return;
  }
  res.writeHead(404, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify({ error: 'not found' }));
}

/**
 * 解析请求 URL（安全处理可能的相对 URL）。
 */
function parseUrl(req: IncomingMessage): URL {
  const host = req.headers.host ?? 'localhost';
  return new URL(req.url ?? '/', `http://${host}`);
}

/**
 * 解析 JWT Token。仅解码 payload，不验证签名（签名由 APISIX 网关完成）。
 * @param token JWT 字符串（来自 URL query 参数）
 * @returns 解码后的用户信息；token 无效或过期返回 null
 */
function verifyToken(token: string | null): JwtPayload | null {
  if (!token) return null;
  try {
    const parts = token.split('.');
    if (parts.length !== 3) return null;
    // base64url → JSON
    const json = Buffer.from(parts[1], 'base64url').toString('utf8');
    const payload = JSON.parse(json) as JwtPayload;
    // 可选：校验过期时间
    if (payload.exp && Date.now() / 1000 > payload.exp) {
      console.warn('[auth] Token 已过期');
      return null;
    }
    return payload;
  } catch (err) {
    console.error('[auth] Token 解析失败:', err);
    return null;
  }
}

/**
 * 设置优雅关闭：收到 SIGINT/SIGTERM 时关闭连接与持久化。
 */
function setupGracefulShutdown(
  server: ReturnType<typeof createServer>,
  collabWss: WebSocketServer,
  commentsWss: WebSocketServer,
  persistence: PersistenceManager
): void {
  let shuttingDown = false;
  const shutdown = (signal: string) => {
    if (shuttingDown) return;
    shuttingDown = true;
    console.log(`\n[yjs-server] 收到 ${signal}，开始优雅关闭...`);

    // 关闭所有 WebSocket 连接
    collabWss.clients.forEach((ws) => ws.close(1001, 'server shutting down'));
    commentsWss.clients.forEach((ws) => ws.close(1001, 'server shutting down'));

    server.close(() => {
      console.log('[yjs-server] HTTP 服务已关闭');
    });

    persistence
      .close()
      .then(() => {
        console.log('[yjs-server] 优雅关闭完成');
        process.exit(0);
      })
      .catch((err) => {
        console.error('[yjs-server] 关闭异常:', err);
        process.exit(1);
      });
  };

  process.on('SIGINT', () => shutdown('SIGINT'));
  process.on('SIGTERM', () => shutdown('SIGTERM'));
}

// 启动服务
main().catch((err) => {
  console.error('[yjs-server] 启动失败:', err);
  process.exit(1);
});
