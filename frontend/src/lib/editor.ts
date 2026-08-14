/**
 * Tiptap 编辑器配置工具
 * 提供内容序列化、Yjs 编解码、文档保存与图片上传等能力
 */
import type { Editor } from '@tiptap/react';
import * as Y from 'yjs';
import { API_BASE } from './api';

/** 编辑器内容（三种格式） */
export interface EditorContent {
  /** HTML，用于前端展示 */
  html: string;
  /** JSON，用于编辑器状态恢复 */
  json: object;
  /** 纯文本，用于全文检索 */
  text: string;
}

/** 图片上传响应 */
interface UploadImageResponse {
  data: { url: string };
}

/**
 * 获取编辑器内容（HTML / JSON / 纯文本）
 */
export function getEditorContent(editor: Editor): EditorContent {
  return {
    html: editor.getHTML(),
    json: editor.getJSON(),
    text: editor.getText(),
  };
}

/**
 * 将 Yjs 文档编码为二进制 Update（用于后端持久化）
 */
export function encodeYDoc(ydoc: Y.Doc): Uint8Array {
  return Y.encodeStateAsUpdate(ydoc);
}

/**
 * 从二进制 Update 解码出 Yjs 文档
 */
export function decodeYDoc(data: Uint8Array): Y.Doc {
  const ydoc = new Y.Doc();
  Y.applyUpdate(ydoc, data);
  return ydoc;
}

/**
 * 保存文档
 * 将编辑器内容与 Yjs 状态提交到后端
 *
 * @param editor - Tiptap 编辑器实例
 * @param ydoc - Yjs 文档
 * @param docId - 文档 ID
 */
export async function saveDocument(
  editor: Editor,
  ydoc: Y.Doc,
  docId: string
): Promise<void> {
  const content = getEditorContent(editor);
  const update = encodeYDoc(ydoc);

  // P0 安全修复：认证通过 HttpOnly Cookie 自动携带
  const response = await fetch(`${API_BASE}/api/v1/documents/${docId}`, {
    method: 'PUT',
    headers: {
      'Content-Type': 'application/json',
    },
    credentials: 'include',
    body: JSON.stringify({
      content_html: content.html,
      content_json: content.json,
      content_text: content.text,
      // Uint8Array → number[] 以便 JSON 传输
      yjs_update: Array.from(update),
    }),
  });

  if (!response.ok) {
    const errorData = await response.json().catch(() => null);
    const message =
      (errorData as { message?: string })?.message ||
      `文档保存失败 (${response.status})`;
    throw new Error(message);
  }
}

/**
 * 上传图片到 R2 存储
 *
 * @param file - 图片文件
 * @returns R2 公开访问 URL
 */
export async function uploadImage(file: File): Promise<string> {
  const formData = new FormData();
  formData.append('file', file);
  formData.append('type', 'doc-image');

  // P0 安全修复：认证通过 HttpOnly Cookie 自动携带
  const response = await fetch(`${API_BASE}/api/v1/documents/upload-image`, {
    method: 'POST',
    credentials: 'include',
    body: formData,
  });

  if (!response.ok) {
    throw new Error(`图片上传失败 (${response.status})`);
  }

  const result = (await response.json()) as UploadImageResponse;
  return result.data.url;
}

/**
 * 注册图片拖拽与粘贴处理
 * 拖拽或粘贴图片时自动上传到 R2，禁止 base64 内嵌
 *
 * 返回清理函数：调用方（如 useEffect cleanup）在重跑或卸载时调用，
 * 移除已注册的监听器，防止重复注册导致同一图片被重复上传。
 *
 * @param editor - Tiptap 编辑器实例
 * @returns 清理函数，调用后移除本函数注册的所有事件监听
 */
export function setupImageHandlers(editor: Editor): () => void {
  const editorDom = editor.view.dom;

  // 拖拽图片自动上传
  const handleDrop = async (event: DragEvent) => {
    const files = event.dataTransfer?.files;
    if (!files || files.length === 0) return;

    const imageFiles = Array.from(files).filter((f) => f.type.startsWith('image/'));
    if (imageFiles.length === 0) return;

    event.preventDefault();
    for (const file of imageFiles) {
      try {
        const url = await uploadImage(file);
        editor.chain().focus().setImage({ src: url, alt: file.name }).run();
      } catch (error) {
        alert(error instanceof Error ? error.message : '图片上传失败');
      }
    }
  };

  // 粘贴图片（如截图）自动上传
  const handlePaste = async (event: ClipboardEvent) => {
    const items = event.clipboardData?.items;
    if (!items) return;

    const imageItems = Array.from(items).filter((item) => item.type.startsWith('image/'));
    if (imageItems.length === 0) return;

    event.preventDefault();
    for (const item of imageItems) {
      const file = item.getAsFile();
      if (!file) continue;
      try {
        const url = await uploadImage(file);
        editor.chain().focus().setImage({ src: url }).run();
      } catch (error) {
        alert(error instanceof Error ? error.message : '图片上传失败');
      }
    }
  };

  editorDom.addEventListener('drop', handleDrop);
  editorDom.addEventListener('paste', handlePaste);

  // 清理函数：移除本函数注册的监听器（幂等，可重复调用）
  return () => {
    editorDom.removeEventListener('drop', handleDrop);
    editorDom.removeEventListener('paste', handlePaste);
  };
}

/**
 * 导入 Markdown 到编辑器
 * 将 Markdown 文本作为内容设置到编辑器
 */
export function importMarkdown(editor: Editor, md: string): void {
  editor.commands.setContent(md);
}

/**
 * 导出编辑器内容为 HTML（后续可由 turndown 转为 Markdown）
 */
export function exportMarkdown(editor: Editor): string {
  return editor.getHTML();
}

export default {
  getEditorContent,
  encodeYDoc,
  decodeYDoc,
  saveDocument,
  uploadImage,
  setupImageHandlers,
  importMarkdown,
  exportMarkdown,
};
