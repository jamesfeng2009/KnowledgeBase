/**
 * Yjs 协同 Provider 管理
 * 负责协作者光标颜色分配与协作者颜色池
 *
 * ## 鉴权契约（唯一事实来源：HttpOnly Cookie）
 *
 * 浏览器 WebSocket API **无法**自定义请求头，所以协同连接的身份只能靠
 * 同源 Cookie 携带：``access_token`` 由后端登录时以 HttpOnly 下发，
 * 建立 WebSocket 时浏览器自动附带，collab-service 在握手阶段验签。
 *
 * 本文件曾提供 ``createCollabProvider(docId, wsUrl, token)``，把 JWT 拼进
 * ``?token=`` —— 而 collab-service 早已改成只读 Cookie，两边的注释互相打脸
 * （前端说"后端从 ?token= 读"，后端说"前端走 Cookie"），且该函数无人调用。
 * 已删除，避免后来者照着错误注释实现出"token 进访问日志"的版本。
 *
 * 实际使用的 Provider 在 ``components/management/CollabEditor.tsx``（Cookie 模式）。
 */

/** 协作者光标颜色池（12 种稳定颜色） */
export const CURSOR_COLORS = [
  '#FF6B6B', '#4ECDC4', '#45B7D1', '#FFA07A',
  '#98D8C8', '#F7DC6F', '#BB8FCE', '#85C1E2',
  '#F1948A', '#82E0AA', '#F8B739', '#AED6F1',
];

/**
 * 基于用户 ID 哈希分配稳定的光标颜色
 * 同一用户在任何设备上都会得到相同颜色
 *
 * @param userId - 用户唯一标识
 * @returns 16 进制颜色字符串
 */
export function getUserColor(userId: string): string {
  let hash = 0;
  for (let i = 0; i < userId.length; i++) {
    hash = ((hash << 5) - hash) + userId.charCodeAt(i);
    hash |= 0;
  }
  return CURSOR_COLORS[Math.abs(hash) % CURSOR_COLORS.length];
}

export default {
  CURSOR_COLORS,
  getUserColor,
};
