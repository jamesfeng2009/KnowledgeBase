/**
 * 对话输入框组件
 * Enter 发送，Shift+Enter 换行；流式输出时禁用输入
 *
 * NOTE: 当前 dead code。chat/index.astro 使用内联 HTML 实现对话输入，
 * 未挂载本组件。本组件为 SKILL 规范要求的对话组件，保留供 P4-2
 * 重构 chat 页面使用组件化方案时挂载。
 */
import { useState, type KeyboardEvent } from 'react';

interface ChatInputProps {
  /** 发送消息回调，参数为用户输入文本 */
  onSend: (text: string) => void;
  /** 是否禁用（流式输出时为 true） */
  disabled?: boolean;
  placeholder?: string;
}

export function ChatInput({
  onSend,
  disabled = false,
  placeholder = '输入你的问题...',
}: ChatInputProps) {
  const [value, setValue] = useState('');

  const handleSend = () => {
    const text = value.trim();
    if (!text || disabled) return;
    onSend(text);
    setValue('');
  };

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    // Enter 发送，Shift+Enter 换行
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  return (
    <div className="chat-input-bar">
      <textarea
        className="chat-input-textarea"
        value={value}
        placeholder={placeholder}
        disabled={disabled}
        rows={1}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={handleKeyDown}
      />
      <button
        type="button"
        className="btn btn-primary chat-input-send"
        disabled={disabled || value.trim().length === 0}
        onClick={handleSend}
      >
        发送
      </button>
    </div>
  );
}

export default ChatInput;
