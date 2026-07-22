/**
 * Markdown 渲染工具
 * 基于 marked + DOMPurify + highlight.js，支持代码块语法高亮、表格、列表、引用等
 * 用于 AI 对话回复的富文本渲染
 */
import { marked } from 'marked';
import DOMPurify from 'dompurify';
import hljs from 'highlight.js';

// 注册常用语言（避免全量加载，减少打包体积）
import javascript from 'highlight.js/lib/languages/javascript';
import typescript from 'highlight.js/lib/languages/typescript';
import python from 'highlight.js/lib/languages/python';
import go from 'highlight.js/lib/languages/go';
import bash from 'highlight.js/lib/languages/bash';
import json from 'highlight.js/lib/languages/json';
import sql from 'highlight.js/lib/languages/sql';
import yaml from 'highlight.js/lib/languages/yaml';
import markdownLang from 'highlight.js/lib/languages/markdown';
import xml from 'highlight.js/lib/languages/xml';

hljs.registerLanguage('javascript', javascript);
hljs.registerLanguage('typescript', typescript);
hljs.registerLanguage('python', python);
hljs.registerLanguage('go', go);
hljs.registerLanguage('bash', bash);
hljs.registerLanguage('json', json);
hljs.registerLanguage('sql', sql);
hljs.registerLanguage('yaml', yaml);
hljs.registerLanguage('markdown', markdownLang);
// HTML 复用 xml 语法定义
hljs.registerLanguage('html', xml);
hljs.registerLanguage('xml', xml);

/**
 * 配置 marked：启用 GFM（表格、删除线等）+ 自动换行
 * 自定义 renderer：代码块使用 highlight.js 进行语法高亮，并附带复制按钮
 */
marked.use({
  gfm: true,
  breaks: true,
  renderer: {
    code({ text, lang }): string {
      const language = lang && hljs.getLanguage(lang) ? lang : 'plaintext';
      try {
        const highlighted = hljs.highlight(text, { language }).value;
        return `<pre class="code-block"><code class="hljs language-${language}">${highlighted}</code><button class="code-copy-btn" type="button" data-code="${encodeURIComponent(text)}">复制</button></pre>`;
      } catch {
        return `<pre class="code-block"><code class="hljs">${text}</code></pre>`;
      }
    },
  },
});

/** DOMPurify 白名单：允许常见 Markdown 标签和自定义的 citation/code-copy-btn */
const SANITIZE_CONFIG = {
  ALLOWED_TAGS: [
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
    'p', 'br', 'hr',
    'ul', 'ol', 'li',
    'blockquote', 'code', 'pre',
    'em', 'strong', 'del',
    'a', 'img',
    'table', 'thead', 'tbody', 'tr', 'th', 'td',
    'div', 'span', 'sup', 'sub',
    'button',
  ],
  ALLOWED_ATTR: [
    'href', 'src', 'alt', 'title',
    'class', 'data-code', 'data-idx',
    'target', 'rel',
  ],
};

/**
 * 将 Markdown 文本渲染为经过 XSS 过滤的 HTML
 * @param text - Markdown 原始文本
 * @returns 安全的 HTML 字符串
 */
export function renderMarkdown(text: string): string {
  if (!text) return '';
  // marked v18：传入 { async: false } 确保同步返回 string
  const raw = marked.parse(text, { async: false }) as string;
  return DOMPurify.sanitize(raw, SANITIZE_CONFIG);
}

/**
 * 对已有 HTML（如协同编辑器产出的富文本）做 XSS 白名单过滤
 * 用于文档详情等直接渲染服务端 HTML 的场景，防止存储型 XSS
 * @param html - 未受信任的 HTML 字符串
 * @returns 安全的 HTML 字符串
 */
export function sanitizeHtml(html: string): string {
  if (!html) return '';
  return DOMPurify.sanitize(html, SANITIZE_CONFIG);
}

/**
 * HTML 转义（含双引号/单引号），可安全用于标签内容与属性上下文（如 title="..."）
 * 用于把未受信任的文本插入 innerHTML 模板字符串前做转义，防止 XSS 注入
 * @param s - 未受信任的原始文本
 * @returns 转义后的安全字符串
 */
export function escapeHtml(s: string): string {
  return String(s ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/**
 * 将引用标注 [1] [2] ... 替换为可点击的徽章 span
 * 仅替换正文中的引用，跳过 <pre>/<code> 代码块内的内容
 */
function applyCitations(html: string): string {
  // 用捕获组分割，保留 <pre>...</pre> 和 <code>...</code> 块
  const parts = html.split(/(<pre[\s\S]*?<\/pre>|<code[\s\S]*?<\/code>)/g);
  return parts
    .map((part, i) => {
      // 奇数索引为代码块/行内代码，保持原样
      if (i % 2 === 1) return part;
      // 正文中的 [N] 替换为可点击引用徽章
      return part.replace(
        /\[(\d+)\]/g,
        '<span class="chat-msg-citation" data-idx="$1">[$1]</span>'
      );
    })
    .join('');
}

/**
 * 流式增量渲染：用于 SSE 流式输出时，每次新 token 到达后重新渲染整个累积文本
 * 在 Markdown 渲染基础上额外处理引用标注 [1] [2]
 * @param accumulatedText - 累积的完整文本
 * @returns 安全的 HTML 字符串（含引用徽章）
 */
export function renderMarkdownStream(accumulatedText: string): string {
  return applyCitations(renderMarkdown(accumulatedText));
}
