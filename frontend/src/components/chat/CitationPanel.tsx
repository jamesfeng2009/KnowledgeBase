/**
 * 引用面板组件
 * 展示 AI 回答所引用的文档来源卡片列表
 */

/** 引用来源数据结构 */
export interface Source {
  id: string;
  title: string;
  /** 摘要片段 */
  snippet: string;
  /** 来源类型（如 文档 / 问答 / 网页） */
  source_type: string;
  url?: string;
}

interface CitationPanelProps {
  sources: Source[];
  /** 点击来源卡片时的回调 */
  onSourceClick?: (source: Source, index: number) => void;
}

export function CitationPanel({ sources, onSourceClick }: CitationPanelProps) {
  if (sources.length === 0) {
    return null;
  }

  const handleActivate = (source: Source, index: number) => {
    onSourceClick?.(source, index);
  };

  return (
    <aside className="chat-right">
      <div className="chat-right-header">
        <h3>引用来源</h3>
        <span className="badge badge-primary">{sources.length}</span>
      </div>
      <div className="chat-right-list">
        {sources.map((source, index) => (
          <div
            key={source.id ?? index}
            className="search-result-item"
            role="button"
            tabIndex={0}
            onClick={() => handleActivate(source, index)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault();
                handleActivate(source, index);
              }
            }}
          >
            <div className="search-result-head">
              <span className="search-result-index">[{index + 1}]</span>
              <strong className="search-result-title">{source.title}</strong>
            </div>
            <p className="search-result-snippet text-muted">{source.snippet}</p>
            <span className="badge badge-primary">{source.source_type}</span>
          </div>
        ))}
      </div>
    </aside>
  );
}

export default CitationPanel;
