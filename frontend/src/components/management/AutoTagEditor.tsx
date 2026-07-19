/**
 * 自动标签编辑器组件
 *
 * 展示文档标签列表（可删除 ×），底部输入框可添加新标签。
 * 添加 / 删除时调用 updateTags(docId, newTags) 同步到后端。
 * 无标签时显示"暂无标签"。
 *
 * 作为 React Island 在文档详情页通过 IntelligencePanel 内嵌使用，
 * 也可独立以 client:load / client:only="react" 挂载。
 */
import { useEffect, useRef, useState } from 'react';
import { updateTags } from '@/lib/apis/intelligence';
import styles from './intelligence.module.css';

interface AutoTagEditorProps {
  docId: string;
  tags: string[];
}

export function AutoTagEditor({ docId, tags }: AutoTagEditorProps) {
  const [localTags, setLocalTags] = useState<string[]>(tags);
  const [input, setInput] = useState('');
  const [saving, setSaving] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  // 外部 tags 刷新后同步本地状态
  useEffect(() => {
    setLocalTags(tags);
  }, [tags]);

  /** 同步标签到后端，失败时回滚 */
  const syncTags = async (next: string[]) => {
    const prev = localTags;
    setLocalTags(next);
    setSaving(true);
    try {
      await updateTags(docId, next);
    } catch (err) {
      alert(err instanceof Error ? err.message : '标签更新失败');
      setLocalTags(prev);
    } finally {
      setSaving(false);
    }
  };

  const handleRemove = (tag: string) => {
    if (saving) return;
    void syncTags(localTags.filter((t) => t !== tag));
  };

  const handleAdd = () => {
    const value = input.trim();
    if (!value || saving) return;
    if (localTags.includes(value)) {
      alert('该标签已存在');
      return;
    }
    setInput('');
    void syncTags([...localTags, value]);
    inputRef.current?.focus();
  };

  return (
    <div>
      {localTags.length === 0 ? (
        <div className={styles.tagEmpty}>暂无标签</div>
      ) : (
        <div className={styles.tagList}>
          {localTags.map((tag) => (
            <span key={tag} className={styles.tagItem}>
              <span aria-hidden="true">🏷️ {tag}</span>
              <button
                type="button"
                className={styles.tagRemove}
                onClick={() => handleRemove(tag)}
                disabled={saving}
                aria-label={`删除标签 ${tag}`}
              >
                ×
              </button>
            </span>
          ))}
        </div>
      )}

      <div className={styles.tagInputRow}>
        <input
          ref={inputRef}
          type="text"
          className={styles.tagInput}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault();
              handleAdd();
            }
          }}
          placeholder="输入标签后回车或点击添加"
          disabled={saving}
        />
        <button
          type="button"
          className="btn btn-outline btn-sm"
          onClick={handleAdd}
          disabled={saving || input.trim() === ''}
        >
          添加
        </button>
      </div>
    </div>
  );
}

export default AutoTagEditor;
