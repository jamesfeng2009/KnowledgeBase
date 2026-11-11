/**
 * @file index.ts
 * @description Yjs 协作服务入口。
 *
 * 职责：
 *  - 在端口 8001 启动 HTTP + WebSocket 服务
 *  - 路由：``/ws/collab/<docId>`` → 协同编辑；``/ws/comments`` → 评论通知；``/health`` → 健康检查
 *  - 认证：**本地 HS256 验签**（``auth.ts``），JWT 取自 HttpOnly Cookie ``access_token``
 *  - 授权：连上之后还要查库校验「这个用户能不能写/读这个文档」（防 IDOR）
 *  - 初始化 PostgreSQL 持久化（不可用时降级内存模式）
 *  - 优雅关闭处理
 *
 * ## 本轮修复的两个 P1 缺陷
 *
 * 1. **鉴权可绕过**：原 ``verifyToken`` 只 base64 解 payload、不验签，
 *    注释称"签名由 APISIX 验证"，但网关既没有到本服务的路由也没有 JWT 插件，
 *    且 compose 把 8001 直接发布到宿主机 —— 伪造 JWT 即可进任意文档。
 *    现在改为本地验签 + fail-closed（SECRET_KEY 缺失则拒绝所有连接）。
 * 2. **房间名路由错位**：y-websocket 客户端把 room 拼在路径里
 *    （``/ws/collab/<docId>``），原实现对 ``path === '/ws/collab'`` 做全等匹配，
 *    带房间名的连接一律 ``socket.destroy()`` —— 协同编辑根本连不上。
 *    现在按前缀匹配并解析出路径里的 room。
 */

import { createServer, type IncomingMessage, type ServerResponse } from 'http';
import { WebSocketServer, WebSocket } from 'ws';
import { PersistenceManager } from './persistence.js';
import { AwarenessManager } from './awareness.js';
import { CommentsService } from './comments.js';
import { handleConnection, docs } from './connection.js';
import { authorizeDocument, getCookieToken, verifyJwt } from './auth.js';
import type { JwtPayload } from './types.js';

/** 服务监听端口 */
const PORT = Number(process.env.YJS_PORT ?? process.env.PORT ?? 8001);

/**
 * 与后端共享的 JWT 签名密钥。
 *
 * 缺失时服务照常启动，但**所有连接都会被拒绝**（fail-closed）：
 * 让"忘记配密钥"表现为"协作不可用"，而不是"任何人都能连"。
 */
const JWT_SECRET = process.env.SECRET_KEY;

/** 关闭码：鉴权失败 */
const CLOSE_UNAUTHORIZED = 4001;
/** 关闭码：无权访问该文档 */
const CLOSE_FORBIDDEN = 4003;
/** 关闭码：服务端配置/依赖异常 */
const CLOSE_UNAVAILABLE = 1013;

/** 全局 awareness 管理器 */
const awarenessManager = new AwarenessManager();
/** 评论通知服务 */
const commentsService = new CommentsService();

/**
 * 服务启动主流程。
 */
async function main(): Promise<void> {
  if (!JWT_SECRET) {
    console.error(
      '[yjs-server] 未设置 SECRET_KEY —— 协作服务将拒绝所有连接（fail-closed）。' +
        '请在环境变量中配置与后端一致的 SECRET_KEY。'
    );
  }

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
    const path = parseUrl(req).pathname;
    // y-websocket 客户端把 room 名拼在路径末尾 → 必须前缀匹配，不能全等
    if (path === '/ws/collab' || path.startsWith('/ws/collab/')) {
      collabWss.handleUpgrade(req, socket, head, (ws) => {
        collabWss.emit('connection', ws, req);
      });
    } else if (path === '/ws/comments' || path.startsWith('/ws/comments/')) {
      commentsWss.handleUpgrade(req, socket, head, (ws) => {
        commentsWss.emit('connection', ws, req);
      });
    } else {
      socket.destroy();
    }
  });

  // 4. 协同编辑连接处理：验签 → 文档授权 → 建连
  collabWss.on('connection', (conn: WebSocket, req: IncomingMessage) => {
    void handleCollabConnection(conn, req, persistence);
  });

  // 4b. 评论通知连接处理：验签（订阅具体文档时再做读权限校验）
  commentsWss.on('connection', (conn: WebSocket, req: IncomingMessage) => {
    const user = authenticate(req);
    if (!user) {
      conn.close(CLOSE_UNAUTHORIZED, 'unauthorized: invalid or missing token');
      return;
    }
    commentsService.handleConnection(conn, req, user, async (docId, mode) => {
      const result = await authorizeDocument(
        persistence.getPool(),
        docId,
        user.sub,
        mode
      );
      return result.allowed;
    });
  });

  // 5. 启动监听
  server.listen(PORT, () => {
    console.log(`[yjs-server] 协作服务已启动，端口 ${PORT}`);
    console.log(`[yjs-server]   /ws/collab/<docId>   协同编辑`);
    console.log(`[yjs-server]   /ws/comments        评论通知`);
    console.log(`[yjs-server]   /health             健康检查`);
    console.log(
      `[yjs-server] 持久化: ${persistence.available ? 'PostgreSQL' : '内存模式（降级）'}`
    );
    console.log(
      `[yjs-server] 鉴权: ${JWT_SECRET ? 'HS256 本地验签 + 文档归属校验' : '未配置 SECRET_KEY（全拒绝）'}`
    );
  });

  // 6. 优雅关闭
  setupGracefulShutdown(server, collabWss, commentsWss, persistence);
}

/**
 * 协同编辑连接：先验签，再校验用户对该文档的写权限，全部通过才交给 connection.ts。
 */
async function handleCollabConnection(
  conn: WebSocket,
  req: IncomingMessage,
  persistence: PersistenceManager
): Promise<void> {
  const user = authenticate(req);
  if (!user) {
    conn.close(CLOSE_UNAUTHORIZED, 'unauthorized: invalid or missing token');
    return;
  }

  const docId = resolveDocId(req);
  if (!docId) {
    conn.close(CLOSE_FORBIDDEN, 'bad request: missing document id');
    return;
  }

  let auth;
  try {
    auth = await authorizeDocument(persistence.getPool(), docId, user.sub, 'write');
  } catch (err) {
    console.error('[yjs-server] 文档授权查询失败:', err);
    conn.close(CLOSE_UNAVAILABLE, 'authorization unavailable');
    return;
  }

  if (!auth.allowed) {
    // 只记 doc/user 与原因，不记 token
    console.warn(
      `[yjs-server] 拒绝协同连接 doc=${docId} user=${user.sub ?? 'unknown'} reason=${auth.reason}`
    );
    conn.close(CLOSE_FORBIDDEN, `forbidden: ${auth.reason ?? 'no access'}`);
    return;
  }

  await handleConnection(conn, docId, persistence, awarenessManager);
}

/**
 * 解析文档 ID。
 *
 * 优先取路径里的 room（y-websocket 标准行为 ``/ws/collab/<room>``），
 * 兼容旧客户端的 ``?doc=`` 参数；两者都没有时返回 null（**不再兜底成 "default"**，
 * 那个房间不对应任何真实文档，等于给未授权方一个公共草稿板）。
 */
function resolveDocId(req: IncomingMessage): string | null {
  const url = parseUrl(req);
  const fromPath = url.pathname.replace(/^\/ws\/collab\/?/, '');
  if (fromPath) return decodeURIComponent(fromPath);
  const fromQuery = url.searchParams.get('doc');
  return fromQuery ? fromQuery : null;
}

/**
 * 认证：从 Cookie 取 JWT 并本地验签。
 *
 * 只认 Cookie，不认 ``?token=``：query 里的 token 会写进 access_log。
 * 前端对应的 ``?token=`` 代码路径已删除，两端契约以 Cookie 为唯一事实来源。
 *
 * @returns 校验通过的用户信息；失败返回 null
 */
function authenticate(req: IncomingMessage): JwtPayload | null {
  const token = getCookieToken(req.headers.cookie);
  const result = verifyJwt(token, JWT_SECRET);
  if (!result.ok) {
    console.warn(`[auth] 拒绝连接: ${result.reason}`);
    return null;
  }
  return result.payload;
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
