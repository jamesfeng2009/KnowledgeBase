/// <reference path="../.astro/types.d.ts" />

interface ImportMetaEnv {
  readonly PUBLIC_API_BASE: string;
  readonly PUBLIC_WS_URL: string;
  readonly PUBLIC_APP_NAME: string;
  readonly PUBLIC_APP_ENV: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

/* CSS Module 类型声明（供 React 组件 import 样式表使用） */
declare module '*.module.css' {
  const classes: { readonly [key: string]: string };
  export default classes;
}

/* === 全局 Toast API 类型声明 === */
/* 由 Toast.astro 组件注入到 window.ekbToast，供所有客户端脚本调用 */
interface EkbToastAPI {
  show(message: string, type?: 'info' | 'success' | 'warning' | 'error', duration?: number): void;
  success(message: string, duration?: number): void;
  error(message: string, duration?: number): void;
  warning(message: string, duration?: number): void;
  info(message: string, duration?: number): void;
}

interface Window {
  ekbToast: EkbToastAPI;
}
