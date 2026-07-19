/**
 * 协同编辑器组件
 * 基于 Tiptap + Yjs 的实时协同富文本编辑器
 * 作为 React Island 在 /manage/editor 路由按需加载
 */
import { useEffect, useMemo, useRef } from 'react';
import { useEditor, EditorContent } from '@tiptap/react';
import StarterKit from '@tiptap/starter-kit';
import Placeholder from '@tiptap/extension-placeholder';
import { Collaboration } from '@tiptap/extension-collaboration';
import { CollaborationCursor } from '@tiptap/extension-collaboration-cursor';
import Image from '@tiptap/extension-image';
import Table from '@tiptap/extension-table';
import TableRow from '@tiptap/extension-table-row';
import TableCell from '@tiptap/extension-table-cell';
import TableHeader from '@tiptap/extension-table-header';
import Link from '@tiptap/extension-link';
import Highlight from '@tiptap/extension-highlight';
import CodeBlockLowLight from '@tiptap/extension-code-block-lowlight';
import * as Y from 'yjs';
import { WebsocketProvider } from 'y-websocket';
import { IndexeddbPersistence } from 'y-indexeddb';
import { lowlight } from '@/lib/lowlight-config';
import { saveDocument, setupImageHandlers } from '@/lib/editor';
import { EditorToolbar } from './EditorToolbar';
import { ConnectionStatus } from './ConnectionStatus';

interface CollabEditorProps {
  docId: string;
  user: { name: string; color: string };
  wsToken: string;
  wsUrl: string;
}

/** 自动保存防抖时间（毫秒） */
const AUTOSAVE_DEBOUNCE = 5000;

/** 保存状态，用于向页面工具栏广播 */
type SaveStatus = 'editing' | 'saving' | 'saved' | 'error';

/**
 * 向外广播保存状态（自定义事件通信）
 * editor.astro 顶部工具栏监听 `ekb:save-status` 事件更新保存状态指示器
 */
function dispatchSaveStatus(status: SaveStatus): void {
  if (typeof window === 'undefined') return;
  window.dispatchEvent(
    new CustomEvent('ekb:save-status', { detail: { status } })
  );
}

export function CollabEditor({ docId, user, wsToken, wsUrl }: CollabEditorProps) {
  const saveTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // 1. 创建 Yjs 文档 + WebSocket Provider + 离线缓存
  const { ydoc, provider } = useMemo(() => {
    const doc = new Y.Doc();
    const wsProvider = new WebsocketProvider(
      `${wsUrl}/ws/collab`,
      docId,
      doc,
      { params: { token: wsToken } }
    );
    new IndexeddbPersistence(docId, doc);
    return { ydoc: doc, provider: wsProvider };
  }, [docId, wsToken, wsUrl]);

  // 2. 初始化 Tiptap 编辑器
  const editor = useEditor({
    extensions: [
      // 协同模式禁用内置 history，由 Yjs 管理 undo/redo
      // codeBlock 由 CodeBlockLowLight 替代以支持语法高亮
      StarterKit.configure({
        history: false,
        codeBlock: false,
      }),
      Placeholder.configure({ placeholder: '开始输入内容...' }),
      Collaboration.configure({ document: ydoc }),
      CollaborationCursor.configure({
        provider,
        user: { name: user.name, color: user.color },
      }),
      Image.configure({ inline: false, allowBase64: false }),
      Table.configure({ resizable: true }),
      TableRow,
      TableCell,
      TableHeader,
      Link.configure({ openOnClick: false, autolink: true }),
      Highlight,
      CodeBlockLowLight.configure({ lowlight }),
    ],
    editorProps: {
      attributes: { class: 'collab-content' },
    },
  });

  // 3. 图片拖拽 / 粘贴自动上传
  useEffect(() => {
    if (!editor) return;
    setupImageHandlers(editor, () => wsToken);
  }, [editor, wsToken]);

  // 4. 自动保存（编辑器变化后 debounce 5 秒）
  useEffect(() => {
    if (!editor) return;
    const handleUpdate = () => {
      dispatchSaveStatus('editing');
      if (saveTimerRef.current) {
        clearTimeout(saveTimerRef.current);
      }
      saveTimerRef.current = setTimeout(() => {
        dispatchSaveStatus('saving');
        saveDocument(editor, ydoc, docId, () => wsToken)
          .then(() => dispatchSaveStatus('saved'))
          .catch((error) => {
            dispatchSaveStatus('error');
            alert(error instanceof Error ? error.message : '文档保存失败');
          });
      }, AUTOSAVE_DEBOUNCE);
    };
    editor.on('update', handleUpdate);
    return () => {
      editor.off('update', handleUpdate);
      if (saveTimerRef.current) {
        clearTimeout(saveTimerRef.current);
      }
    };
  }, [editor, ydoc, docId, wsToken]);

  // 5. 卸载时销毁 Provider 与文档，释放连接
  useEffect(() => {
    return () => {
      provider.destroy();
      ydoc.destroy();
    };
  }, [provider, ydoc]);

  if (!editor) {
    return null;
  }

  return (
    <div className="collab-editor">
      <EditorToolbar editor={editor} />
      <ConnectionStatus provider={provider} />
      <EditorContent editor={editor} />
    </div>
  );
}

export default CollabEditor;
