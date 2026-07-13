/**
 * 代码块语法高亮配置
 * 基于 lowlight 注册常用编程语言，供 Tiptap CodeBlockLowLight 扩展使用
 */
import { createLowlight, common } from 'lowlight';
import rust from 'highlight.js/lib/languages/rust';
import dockerfile from 'highlight.js/lib/languages/dockerfile';

// common 包含常用语言：JS/TS/Python/Go/SQL/JSON/YAML/Bash/HTML/CSS 等
const lowlight = createLowlight(common);

// 额外注册不常用语言
lowlight.register('rust', rust);
lowlight.register('dockerfile', dockerfile);

export { lowlight };
