/**
 * AI 摘要卡片组件
 *
 * 展示 AI 生成的文档摘要，支持内联编辑（textarea）并保存到后端。
 * 顶部标题"AI 智能摘要" + 闪烁的"AI 生成"渐变徽章。
 * 摘要为空时显示空状态提示，引导用户触发 AI 智能处理。
 *
 * 作为 React Island 在文档详情页通过 IntelligencePanel 内嵌使用，
 * 也可独立以 client:load / client:only="react" 挂载。
 */
import { useEffect, useRef, useState } from 'react';
import { updateSummary } from '@/lib/apis/intelligence';
import styles from './intelligence.module.css';

interface SummaryCardProps {
  docId: string;
  summary: string;
  editable?: boolean;
}

export function SummaryCard({ docId, summary, editable = true }: SummaryCardProps) {
  const [isEditing, setIsEditing] = useState(false);
  const [draft, setDraft] = useState(summary);
  const [saving, setSaving] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // 外部 summary 刷新后同步草稿（如 AI 处理完成后重新拉取数据）
  useEffect(() => {
    setDraft(summary);
  }, [summary]);

  // 进入编辑模式时自动聚焦
  useEffect(() => {
    if (isEditing && textareaRef.current) {
      textareaRef.current.focus();
    }
  }, [isEditing]);

  const handleStartEdit = () => {
    setDraft(summary);
    setIsEditing(true);
  };

  const handleCancel = () => {
    setDraft(summary);
    setIsEditing(false);
  };

  const handleSave = async () => {
    const trimmed = draft.trim();
    if (trimmed === summary) {
      setIsEditing(false);
      return;
    }
    setSaving(true);
    try {
      await updateSummary(docId, trimmed);
      setIsEditing(false);
    } catch (err) {
      alert(err instanceof Error ? err.message : '摘要保存失败');
      setDraft(summary);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className={styles.summaryCard}>
      <div className={styles.summaryHeader}>
        <span className={styles.summaryTitle}>
          <span aria-hidden="true">📋</span>
          <span>AI 智能摘要</span>
        </span>
        <span className={styles.aiBadge}>AI 生成</span>
      </div>

      {isEditing ? (
        <div>
          <textarea
            ref={textareaRef}
            className={styles.summaryTextarea}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder="请输入摘要内容..."
            disabled={saving}
          />
          <div className={styles.summaryActions}>
            <button
              type="button"
              className="btn btn-ghost btn-sm"
              onClick={handleCancel}
              disabled={saving}
            >
              取消
            </button>
            <button
              type="button"
              className="btn btn-primary btn-sm"
              onClick={handleSave}
              disabled={saving || draft.trim() === ''}
            >
              {saving ? '保存中...' : '保存'}
            </button>
          </div>
        </div>
      ) : (
        <>
          {summary ? (
            <div className={styles.summaryBody}>{summary}</div>
          ) : (
            <div className={styles.summaryEmpty}>
              暂无摘要，点击 AI 智能处理生成
            </div>
          )}
          {editable && (
            <div className={styles.summaryActions}>
              <button
                type="button"
                className="btn btn-outline btn-sm"
                onClick={handleStartEdit}
              >
                {summary ? '编辑' : '手动添加'}
              </button>
            </div>
          )}
        </>
      )}
    </div>
  );
}

export default SummaryCard;
