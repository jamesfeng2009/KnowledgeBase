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

/* === 页面内联事件桥接方法 ===
 *
 * 以下方法由各评测页的 <script> 挂到 window 上，供 HTML 里的 onclick="xxx(...)"
 * 调用。Astro 的 <script> 会编译成 ES module，作用域被隔离，所以这些方法
 * 只能通过 window 暴露——TypeScript 看不到动态赋值，必须在全局声明一次，
 * 否则 astro check 会对每个 window.xxx = ... 报 ts(2339)。
 */
interface Window {
  /** 打开数据集详情 */
  viewDetail: (datasetId: string) => Promise<void>;
  /** 打开「新增查询」弹窗 */
  showAddQuery: (datasetId: string) => void;
  /** 提交新增查询 */
  submitAddQuery: (datasetId: string) => Promise<void>;
  /** 标注查询的相关文档 */
  annotateQuery: (queryId: string, currentGt: string[]) => void;
  /** 删除后重新添加查询 */
  deleteAndReaddQuery: (queryId: string) => Promise<void>;
  /** 删除查询 */
  deleteQuery: (queryId: string) => Promise<void>;
  /** 导入预置查询库 */
  importPreset: (datasetId: string) => Promise<void>;
  /** 执行数据集评测 */
  runDataset: (datasetId: string) => Promise<void>;
  /** 查看评测结果 */
  viewResults: (datasetId: string) => Promise<void>;
  /** 删除数据集 */
  deleteDataset: (datasetId: string) => Promise<void>;
  /** 打开「新增用例」弹窗 */
  showAddCase: (datasetId: string) => void;
  /** 提交新增用例 */
  submitAddCase: (datasetId: string) => Promise<void>;
  /** 切换用例录入模式 */
  toggleCaseMode: () => void;
  /** 删除用例 */
  deleteCase: (caseId: string) => Promise<void>;
  /** 打开「批量新增」弹窗 */
  showBatchAdd: (datasetId: string) => void;
  /** 提交批量新增 */
  submitBatchAdd: (datasetId: string) => Promise<void>;
  /** 执行注入测试套件 */
  runSuite: (suiteId: string) => Promise<void>;
}
