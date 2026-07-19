/**
 * 智能处理面板（聚合组件）
 *
 * 顶部"AI 智能处理"按钮：点击调用 processIntelligence(docId)，
 * 处理中显示进度条 + "AI 处理中..." 文字，并通过 pollIntelligenceStatus 轮询状态，
 * 处理完成后刷新数据（重新加载页面）。
 * 分类以 badge 形式展示，内嵌 SummaryCard / AutoTagEditor / ActionItemList。
 * 整体放在 .card 容器中。
 *
 * 作为 React Island 在文档详情页以 client:load / client:only="react" 挂载
 * （实际通过 IntelligencePanelMount 包装读取文档数据后渲染）。
 */
import { useState } from 'react';
import {
  processIntelligence,
  pollIntelligenceStatus,
} from '@/lib/apis/intelligence';
import { SummaryCard } from './SummaryCard';
import { AutoTagEditor } from './AutoTagEditor';
import { ActionItemList } from './ActionItemList';
import styles from './intelligence.module.css';

interface IntelligencePanelProps {
  docId: string;
  summary: string;
  tags: string[];
  category: string;
}

export function IntelligencePanel({
  docId,
  summary,
  tags,
  category,
}: IntelligencePanelProps) {
  const [processing, setProcessing] = useState(false);

  const handleProcess = async () => {
    if (processing) return;
    if (!docId || docId === 'new') {
      alert('文档 ID 无效，无法触发智能处理');
      return;
    }
    setProcessing(true);
    try {
      await processIntelligence(docId);
      await pollIntelligenceStatus(docId, 3000, 60);
      alert('AI 智能处理完成');
      // 处理完成后重新加载页面以获取最新摘要 / 标签 / 分类 / 行动项
      window.location.reload();
    } catch (err) {
      alert(err instanceof Error ? err.message : '智能处理失败');
    } finally {
      setProcessing(false);
    }
  };

  return (
    <div className="card">
      <div className={styles.panelHeader}>
        <span className={styles.panelTitle}>
          <span aria-hidden="true">🤖</span>
          <span>AI 智能处理</span>
        </span>
        <button
          type="button"
          className="btn btn-primary btn-sm"
          onClick={handleProcess}
          disabled={processing}
        >
          {processing ? 'AI 处理中...' : '🤖 AI 智能处理'}
        </button>
      </div>

      {processing && (
        <div className={styles.progressWrap}>
          <div className={styles.progressText}>
            <span className="spinner" aria-hidden="true" />
            <span>AI 处理中...</span>
          </div>
          <div className={styles.progressBar}>
            <div className={styles.progressFill} />
          </div>
        </div>
      )}

      <div className={styles.panelCategory}>
        <span>分类：</span>
        {category ? (
          <span className="badge badge-primary">📁 {category}</span>
        ) : (
          <span className={styles.tagEmpty}>暂无分类</span>
        )}
      </div>

      <div className={styles.section}>
        <SummaryCard docId={docId} summary={summary} editable />
      </div>

      <div className={styles.section}>
        <div className={styles.sectionTitle}>自动标签</div>
        <AutoTagEditor docId={docId} tags={tags} />
      </div>

      <div className={styles.section}>
        <div className={styles.sectionTitle}>行动项</div>
        <ActionItemList docId={docId} />
      </div>
    </div>
  );
}

export default IntelligencePanel;
