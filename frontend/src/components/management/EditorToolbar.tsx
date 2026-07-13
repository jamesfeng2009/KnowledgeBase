/**
 * 格式化工具栏组件
 * 监听编辑器 transaction 事件更新激活状态，通过 chain 命令执行格式化
 */
import { useEffect, useState } from 'react';
import type { Editor } from '@tiptap/react';

interface EditorToolbarProps {
  editor: Editor;
}

export function EditorToolbar({ editor }: EditorToolbarProps) {
  const [, forceUpdate] = useState({});

  useEffect(() => {
    const trigger = () => forceUpdate({});
    editor.on('transaction', trigger);
    return () => {
      editor.off('transaction', trigger);
    };
  }, [editor]);

  const active = (name: string, attrs?: Record<string, unknown>): boolean =>
    editor.isActive(name, attrs);

  return (
    <div className="collab-toolbar">
      {/* 文本格式 */}
      <button
        type="button"
        className={`tool-btn${active('bold') ? ' active' : ''}`}
        onClick={() => editor.chain().focus().toggleBold().run()}
      >
        B
      </button>
      <button
        type="button"
        className={`tool-btn${active('italic') ? ' active' : ''}`}
        onClick={() => editor.chain().focus().toggleItalic().run()}
      >
        I
      </button>
      <button
        type="button"
        className={`tool-btn${active('strike') ? ' active' : ''}`}
        onClick={() => editor.chain().focus().toggleStrike().run()}
      >
        S
      </button>
      <button
        type="button"
        className={`tool-btn${active('code') ? ' active' : ''}`}
        onClick={() => editor.chain().focus().toggleCode().run()}
      >
        {'</>'}
      </button>
      <button
        type="button"
        className={`tool-btn${active('highlight') ? ' active' : ''}`}
        onClick={() => editor.chain().focus().toggleHighlight().run()}
      >
        Mark
      </button>

      <span className="tool-divider" />

      {/* 标题 */}
      <button
        type="button"
        className={`tool-btn${active('heading', { level: 1 }) ? ' active' : ''}`}
        onClick={() => editor.chain().focus().toggleHeading({ level: 1 }).run()}
      >
        H1
      </button>
      <button
        type="button"
        className={`tool-btn${active('heading', { level: 2 }) ? ' active' : ''}`}
        onClick={() => editor.chain().focus().toggleHeading({ level: 2 }).run()}
      >
        H2
      </button>
      <button
        type="button"
        className={`tool-btn${active('heading', { level: 3 }) ? ' active' : ''}`}
        onClick={() => editor.chain().focus().toggleHeading({ level: 3 }).run()}
      >
        H3
      </button>

      <span className="tool-divider" />

      {/* 列表与引用 */}
      <button
        type="button"
        className={`tool-btn${active('bulletList') ? ' active' : ''}`}
        onClick={() => editor.chain().focus().toggleBulletList().run()}
      >
        • List
      </button>
      <button
        type="button"
        className={`tool-btn${active('orderedList') ? ' active' : ''}`}
        onClick={() => editor.chain().focus().toggleOrderedList().run()}
      >
        1. List
      </button>
      <button
        type="button"
        className={`tool-btn${active('blockquote') ? ' active' : ''}`}
        onClick={() => editor.chain().focus().toggleBlockquote().run()}
      >
        Quote
      </button>

      <span className="tool-divider" />

      {/* 插入元素 */}
      <button
        type="button"
        className="tool-btn"
        onClick={() => {
          const url = window.prompt('输入图片 URL');
          if (url) editor.chain().focus().setImage({ src: url }).run();
        }}
      >
        图片
      </button>
      <button
        type="button"
        className="tool-btn"
        onClick={() => {
          const url = window.prompt('输入链接 URL');
          if (url) editor.chain().focus().setLink({ href: url }).run();
        }}
      >
        链接
      </button>
      <button
        type="button"
        className="tool-btn"
        onClick={() =>
          editor
            .chain()
            .focus()
            .insertTable({ rows: 3, cols: 3, withHeaderRow: true })
            .run()
        }
      >
        表格
      </button>
      <button
        type="button"
        className="tool-btn"
        onClick={() => {
          const lang = window.prompt('编程语言', 'typescript') || 'plaintext';
          editor.chain().focus().toggleCodeBlock({ language: lang }).run();
        }}
      >
        代码块
      </button>

      <span className="tool-divider" />

      {/* 撤销/重做（由 Yjs 管理） */}
      <button
        type="button"
        className="tool-btn"
        onClick={() => editor.chain().focus().undo().run()}
      >
        ↶
      </button>
      <button
        type="button"
        className="tool-btn"
        onClick={() => editor.chain().focus().redo().run()}
      >
        ↷
      </button>
    </div>
  );
}

export default EditorToolbar;
