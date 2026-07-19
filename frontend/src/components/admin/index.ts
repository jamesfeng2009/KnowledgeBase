/**
 * admin 组件目录导出
 *
 * 治理后台专用组件集合：
 * - HealthRing    健康度环形指示器
 * - AuditStep     审核流程步骤指示器
 * - UserTableRow  用户表格行
 *
 * 注：StatCard 已统一收敛至 @/components/common，admin 页面请从 common 导入。
 *
 * @example
 * ```typescript
 * import { HealthRing, AuditStep, UserTableRow } from '@/components/admin';
 * import { StatCard } from '@/components/common';
 * ```
 */
export { default as HealthRing } from './HealthRing.astro';
export { default as AuditStep } from './AuditStep.astro';
export { default as UserTableRow } from './UserTableRow.astro';
