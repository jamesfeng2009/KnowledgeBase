/**
 * AI 对话消息组件
 * 渲染用户与 AI 消息的不同气泡样式，AI 消息中的 [1] [2] 引用标注渲染为可点击元素
 *
 * NOTE: 当前 dead code。chat/index.astro 使用内联 HTML 实现消息渲染，
 * 未挂载本组件。本组件为 SKILL 规范要求的对话组件，保留供 P4-2
 * 重构 chat 页面使用组件化方案时挂载。
 */
import { Fragment } from 'react';
import type { Source } from './CitationPanel';

/** 对话消息数据结构 */
export interface ChatMessageData {
  role: 'user' | 'assistant';
  content: string;
  sources?: Source[];
}

interface ChatMessageProps {
  message: ChatMessageData;
  /** 是否正在流式输出 */
  isStreaming?: boolean;
  /** 点击引用标注时的回调，index 从 1 开始 */
  onCitationClick?: (index: number) => void;
}

/** 匹配 [1] [12] 形式的引用标注 */
const CITATION_RE = /(\[\d+\])/g;

/**
 * 将文本按引用标注切分，引用部分渲染为可点击元素
 */
function renderContent(
  content: string,
  onCitationClick?: (index: number) => void
) {
  const parts = content.split(CITATION_RE);
  return parts.map((part, i) => {
    const match = part.match(/^\[(\d+)\]$/);
    if (match) {
      const idx = parseInt(match[1], 10);
      return (
        <span
          key={i}
          className="chat-msg-citation"
          role="button"
          tabIndex={0}
          onClick={() => onCitationClick?.(idx)}
          onKeyDown={(e) => {
            if (e.key === 'Enter' || e.key === ' ') {
              e.preventDefault();
              onCitationClick?.(idx);
            }
          }}
        >
          [{idx}]
        </span>
      );
    }
    return <Fragment key={i}>{part}</Fragment>;
  });
}

export function ChatMessage({
  message,
  isStreaming = false,
  onCitationClick,
}: ChatMessageProps) {
  const isUser = message.role === 'user';
  // 流式输出且暂无内容时显示打字动画
  const showTyping = isStreaming && message.content.length === 0;

  return (
    <div className={`chat-msg${isUser ? ' chat-msg-user' : ' chat-msg-ai'}`}>
      <div className="chat-msg-bubble">
        {showTyping ? (
          <div className="typing-indicator">
            <span />
            <span />
            <span />
          </div>
        ) : (
          <div className="chat-msg-content">
            {renderContent(message.content, onCitationClick)}
          </div>
        )}
      </div>
    </div>
  );
}

export default ChatMessage;
