/**
 * 通用组件导出
 * 汇总管理类与对话类 React 组件，提供统一导入入口
 *
 * @example
 * ```typescript
 * import { CollabEditor, ChatMessage } from '@/components/common';
 * ```
 */
// 协同编辑类
export { CollabEditor } from '@/components/management/CollabEditor';
export { EditorToolbar } from '@/components/management/EditorToolbar';
export { ConnectionStatus } from '@/components/management/ConnectionStatus';

// AI 对话类
export { ChatMessage } from '@/components/chat/ChatMessage';
export type { ChatMessageData } from '@/components/chat/ChatMessage';
export { ChatInput } from '@/components/chat/ChatInput';
export { CitationPanel } from '@/components/chat/CitationPanel';
export type { Source } from '@/components/chat/CitationPanel';
