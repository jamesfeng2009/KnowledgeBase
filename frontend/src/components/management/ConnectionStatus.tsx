/**
 * 连接状态指示器
 * 监听 WebsocketProvider 的 status 事件，实时显示协同连接状态
 */
import { useEffect, useState } from 'react';
import type { WebsocketProvider } from 'y-websocket';

interface ConnectionStatusProps {
  provider: WebsocketProvider;
}

type ConnStatus = 'connecting' | 'connected' | 'disconnected';

const STATUS_MAP: Record<ConnStatus, { label: string; color: string }> = {
  connecting: { label: '连接中', color: 'var(--warning)' },
  connected: { label: '已连接', color: 'var(--success)' },
  disconnected: { label: '已断开', color: 'var(--danger)' },
};

export function ConnectionStatus({ provider }: ConnectionStatusProps) {
  const [status, setStatus] = useState<ConnStatus>('connecting');

  useEffect(() => {
    const handleStatus = (e: { status: string }) => {
      setStatus(e.status as ConnStatus);
    };
    provider.on('status', handleStatus);
    return () => {
      provider.off('status', handleStatus);
    };
  }, [provider]);

  const cfg = STATUS_MAP[status] ?? STATUS_MAP.connecting;

  return (
    <span className="collab-status">
      <span className="collab-status-dot" style={{ background: cfg.color }} />
      {cfg.label}
    </span>
  );
}

export default ConnectionStatus;
