/**
 * CollabEditor 挂载桥接组件
 *
 * 由于 client:only="react" Island 的 props 在服务端（构建期）序列化，
 * 无法读取浏览器端信息，故通过此包装组件在客户端读取 user 后再渲染
 * CollabEditor。认证改为 HttpOnly Cookie 经 WebSocket 握手自动携带，
 * 不再从 localStorage 读取 Token 拼接到 URL。
 *
 * 作为 React Island 在 /manage/editor 路由以 client:only="react" 挂载。
 */
import { useEffect, useState } from 'react';
import { CollabEditor } from './CollabEditor';
import { getUserColor } from '@/lib/collab';

interface CollabEditorMountProps {
  /** 文档 ID（从 URL 参数获取，服务端可计算） */
  docId: string;
  /** WebSocket 服务基础地址（环境变量，构建期内联） */
  wsUrl: string;
}

/** CollabEditor 所需的 user 结构 */
interface CollabUser {
  name: string;
  color: string;
}

export function CollabEditorMount({ docId, wsUrl }: CollabEditorMountProps) {
  const [user, setUser] = useState<CollabUser | null>(null);

  // 客户端读取本地 user 信息（仅用于光标颜色/名称展示，不再读取 Token）
  useEffect(() => {
    let name = '当前用户';
    let id = 'anonymous';
    const userRaw = localStorage.getItem('ekb_user');
    if (userRaw) {
      try {
        const parsed = JSON.parse(userRaw) as { name?: string; id?: string };
        if (parsed.name) name = parsed.name;
        if (parsed.id) id = parsed.id;
      } catch {
        // 解析失败，使用默认用户信息
      }
    }
    setUser({ name, color: getUserColor(id) });
  }, []);

  // Island 渲染完成后通知页面隐藏加载占位
  useEffect(() => {
    if (!user) return;
    window.dispatchEvent(new CustomEvent('ekb:collab-ready'));
  }, [user]);

  if (!user) {
    return null;
  }

  return (
    <CollabEditor
      docId={docId}
      user={user}
      wsUrl={wsUrl}
    />
  );
}

export default CollabEditorMount;
