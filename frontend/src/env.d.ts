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
