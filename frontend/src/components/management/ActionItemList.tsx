/**
 * 行动项列表组件
 *
 * 页面加载时调用 getActionItems(docId) 获取行动项。
 * 每个行动项展示：复选框、内容、负责人、截止日期（已过期标红）、
 * 优先级标签（low=灰/normal=蓝/high=橙/urgent=红）、状态标签（待办/已完成）。
 * 勾选 / 取消勾选时调用 updateActionItem(actionId, status) 并做乐观更新。
 * 无行动项时显示"暂无行动项"。
 *
 * 作为 React Island 在文档详情页通过 IntelligencePanel 内嵌使用，
 * 也可独立以 client:load / client:only="react" 挂载。
 */
import { useEffect, useState } from 'react';
import { getActionItems, updateActionItem } from '@/lib/apis/intelligence';
import type { ActionItem } from '@/lib/apis/intelligence';
import styles from './intelligence.module.css';

interface ActionItemListProps {
  docId: string;
}

/** 优先级文案映射 */
const PRIORITY_LABEL: Record<string, string> = {
  low: '低',
  normal: '中',
  high: '高',
  urgent: '紧急',
};

/** 优先级对应的样式类名（需在构建期静态引用以被 CSS Module 识别） */
function getPriorityClass(priority: string): string {
  switch (priority) {
    case 'low':
      return styles.priorityLow;
    case 'normal':
      return styles.priorityNormal;
    case 'high':
      return styles.priorityHigh;
    case 'urgent':
      return styles.priorityUrgent;
    default:
      return styles.priorityNormal;
  }
}

/** 格式化截止日期为 YYYY-MM-DD */
function formatDate(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

/** 判断是否已过期（未完成且截止时间早于当前） */
function isOverdue(deadline: string | null, status: string): boolean {
  if (!deadline || status === 'completed') return false;
  const d = new Date(deadline);
  if (Number.isNaN(d.getTime())) return false;
  return d.getTime() < Date.now();
}

export function ActionItemList({ docId }: ActionItemListProps) {
  const [items, setItems] = useState<ActionItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [togglingId, setTogglingId] = useState<string | null>(null);

  // 加载行动项
  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      setLoading(true);
      try {
        const data = await getActionItems(docId);
        if (!cancelled) setItems(data);
      } catch (err) {
        if (!cancelled) {
          alert(err instanceof Error ? err.message : '加载行动项失败');
          setItems([]);
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
  }, [docId]);

  /** 勾选 / 取消勾选，乐观更新，失败回滚 */
  const handleToggle = async (item: ActionItem) => {
    if (togglingId) return;
    const nextStatus = item.status === 'completed' ? 'pending' : 'completed';
    const prevStatus = item.status;
    setTogglingId(item.id);
    setItems((prev) =>
      prev.map((it) => (it.id === item.id ? { ...it, status: nextStatus } : it))
    );
    try {
      await updateActionItem(item.id, nextStatus);
    } catch (err) {
      setItems((prev) =>
        prev.map((it) => (it.id === item.id ? { ...it, status: prevStatus } : it))
      );
      alert(err instanceof Error ? err.message : '更新行动项失败');
    } finally {
      setTogglingId(null);
    }
  };

  if (loading) {
    return <div className={styles.loadingState}>加载中...</div>;
  }

  if (items.length === 0) {
    return <div className={styles.actionEmpty}>暂无行动项</div>;
  }

  return (
    <div className={styles.actionList}>
      {items.map((item) => {
        const completed = item.status === 'completed';
        const overdue = isOverdue(item.deadline, item.status);
        return (
          <div
            key={item.id}
            className={`${styles.actionItem}${completed ? ' ' + styles.actionItemCompleted : ''}`}
          >
            <input
              type="checkbox"
              className={styles.actionCheckbox}
              checked={completed}
              onChange={() => handleToggle(item)}
              disabled={togglingId === item.id}
              aria-label={completed ? '标记为待办' : '标记为完成'}
            />
            <div className={styles.actionBody}>
              <div className={styles.actionContent}>{item.content}</div>
              <div className={styles.actionMeta}>
                {item.assignee && (
                  <span className={styles.actionAssignee}>
                    <span aria-hidden="true">👤</span>
                    <span>{item.assignee}</span>
                  </span>
                )}
                {item.deadline && (
                  <span
                    className={`${styles.actionDeadline}${overdue ? ' ' + styles.actionDeadlineOverdue : ''}`}
                  >
                    <span aria-hidden="true">📅</span>
                    <span>{formatDate(item.deadline)}</span>
                    {overdue && <span>（已过期）</span>}
                  </span>
                )}
                <span className={`${styles.priorityTag} ${getPriorityClass(item.priority)}`}>
                  {PRIORITY_LABEL[item.priority] ?? item.priority}
                </span>
                <span
                  className={`${styles.statusTag} ${completed ? styles.statusCompleted : styles.statusPending}`}
                >
                  {completed ? '已完成' : '待办'}
                </span>
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}

export default ActionItemList;
