/**
 * IntelligencePanel 挂载桥接组件
 *
 * IntelligencePanel 需要 summary / tags / category 作为初始 props，
 * 而这些数据需在客户端调用 getDocument 获取（受认证保护）。
 * client:only="react" Island 的 props 在服务端（构建期）序列化，
 * 无法读取 API 数据，故通过此包装组件在客户端获取文档后再渲染 IntelligencePanel，
 * 从而保持 IntelligencePanel 的 props 契约不变。
 *
 * 作为 React Island 在 /knowledge/detail 路由以 client:only="react" 挂载。
 */
import { useEffect, useState } from 'react';
import { getDocument } from '@/lib/apis/knowledge';
import type { Document } from '@/lib/apis/knowledge';
import { IntelligencePanel } from './IntelligencePanel';
import styles from './intelligence.module.css';

interface IntelligencePanelMountProps {
  /** 文档 ID（从 URL 参数获取，服务端可计算） */
  docId: string;
}

/** 客户端读取完成后的面板初始数据 */
interface PanelData {
  summary: string;
  tags: string[];
  category: string;
}

export function IntelligencePanelMount({ docId }: IntelligencePanelMountProps) {
  const [data, setData] = useState<PanelData | null>(null);

  useEffect(() => {
    let cancelled = false;

    const token = localStorage.getItem('ekb_access_token');
    if (!token) {
      window.location.href = '/auth/login';
      return;
    }

    // 新文档无智能处理数据，直接以空值渲染面板
    if (!docId || docId === 'new') {
      setData({ summary: '', tags: [], category: '' });
      return;
    }

    getDocument(docId)
      .then((doc: Document) => {
        if (cancelled) return;
        setData({
          summary: doc.summary || '',
          tags: doc.tags || [],
          category: doc.category || '',
        });
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        // 拉取失败时仍渲染面板（空数据），避免阻塞页面，错误以 alert 提示
        alert(err instanceof Error ? err.message : '加载智能处理数据失败');
        setData({ summary: '', tags: [], category: '' });
      });

    return () => {
      cancelled = true;
    };
  }, [docId]);

  if (!data) {
    return <div className={styles.panelLoading}>加载智能处理面板...</div>;
  }

  return (
    <IntelligencePanel
      docId={docId}
      summary={data.summary}
      tags={data.tags}
      category={data.category}
    />
  );
}

export default IntelligencePanelMount;
