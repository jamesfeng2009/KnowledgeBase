/**
 * CollabEditor 挂载桥接组件
 *
 * 由于 CollabEditor 依赖浏览器 localStorage 中的 wsToken / user，
 * 而 client:only="react" Island 的 props 在服务端（构建期）序列化，
 * 无法读取 localStorage，故通过此包装组件在客户端读取 localStorage 后
 * 再渲染 CollabEditor，从而保持 CollabEditor 的 props 契约不变。
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

/** 客户端读取完成后的就绪状态 */
interface ReadyState {
  wsToken: string;
  user: CollabUser;
}

export function CollabEditorMount({ docId, wsUrl }: CollabEditorMountProps) {
  const [ready, setReady] = useState<ReadyState | null>(null);

  // 客户端读取 localStorage，组装 wsToken 与 user（含稳定光标颜色）
  useEffect(() => {
    const wsToken = localStorage.getItem('ekb_access_token');
    if (!wsToken) {
      // 未登录，跳转登录页
      window.location.href = '/auth/login';
      return;
    }

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

    setReady({
      wsToken,
      user: { name, color: getUserColor(id) },
    });
  }, []);

  // Island 渲染完成后通知页面隐藏加载占位
  useEffect(() => {
    if (!ready) return;
    window.dispatchEvent(new CustomEvent('ekb:collab-ready'));
  }, [ready]);

  if (!ready) {
    return null;
  }

  return (
    <CollabEditor
      docId={docId}
      user={ready.user}
      wsToken={ready.wsToken}
      wsUrl={wsUrl}
    />
  );
}

export default CollabEditorMount;
