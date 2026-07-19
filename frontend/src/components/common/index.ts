/**
 * 通用组件导出
 * 汇总基础 Astro 组件、管理类与对话类 React 组件，提供统一导入入口
 *
 * @example
 * ```typescript
 * import { Button, Badge, CollabEditor, ChatMessage } from '@/components/common';
 * ```
 */

// === 基础 Astro 组件（P4-1 common 组件库） ===
export { default as Button } from './Button.astro';
export { default as Badge } from './Badge.astro';
export { default as Tag } from './Tag.astro';
export { default as Avatar } from './Avatar.astro';
export { default as Modal } from './Modal.astro';
export { default as Tabs } from './Tabs.astro';
export { default as Toast } from './Toast.astro';
export { default as StatCard } from './StatCard.astro';
export { default as PageHeader } from './PageHeader.astro';
export { default as EmptyState } from './EmptyState.astro';

// === 协同编辑类 ===
export { CollabEditor } from '@/components/management/CollabEditor';
export { EditorToolbar } from '@/components/management/EditorToolbar';
export { ConnectionStatus } from '@/components/management/ConnectionStatus';

// === AI 对话类 ===
export { ChatMessage } from '@/components/chat/ChatMessage';
export type { ChatMessageData } from '@/components/chat/ChatMessage';
export { ChatInput } from '@/components/chat/ChatInput';
export { CitationPanel } from '@/components/chat/CitationPanel';
export type { Source } from '@/components/chat/CitationPanel';
