/**
 * 智能测试平台 API 封装
 * 对接后端 testing.py 路由（prefix=/api/v1/testing）
 */
import { getData, postData, putData, delData, type PageResponse } from '../api';

const BASE = '/api/v1/testing';

// ===== 类型定义 =====

export interface TestProject {
  id: string;
  name: string;
  description?: string;
  owner_id: string;
  prd_doc_ids?: string[];
  tech_doc_ids?: string[];
  api_doc_ids?: string[];
  status: string;
  tenant_id?: string;
  created_at?: string;
  updated_at?: string;
}

export interface TestRequirement {
  id: string;
  project_id: string;
  source_doc_id?: string;
  title: string;
  description?: string;
  category: string;
  priority: string;
  acceptance_criteria?: string[];
  source_text?: string;
  source: string;
  status: string;
  change_thread_id?: string;
  created_at?: string;
}

export interface TestCase {
  id: string;
  project_id: string;
  requirement_id?: string;
  title: string;
  description?: string;
  preconditions?: string;
  test_steps?: { step_no: number; action: string; expected: string }[];
  expected_result?: string;
  test_type: string;
  priority: string;
  status: string;
  tags?: string[];
  created_by: string;
  context_doc_ids?: string[];
  case_no?: string;
  verification_channels?: string[];
  created_at?: string;
}

export interface TestReview {
  id: string;
  case_id: string;
  submitter_id: string;
  reviewer_id?: string;
  status: string;
  comment?: string;
  suggestions?: { type: string; suggestion: string; severity: string }[];
  review_summary?: string;
  resolved_at?: string;
  created_at?: string;
}

export interface TestPlan {
  id: string;
  project_id: string;
  name: string;
  description?: string;
  case_ids?: string[];
  execution_strategy: string;
  ai_orchestration?: {
    execution_order?: string[];
    node_assignments?: Record<string, string[]>;
    dependencies?: Record<string, string[]>;
    rationale?: string;
  };
  status: string;
  created_by: string;
  created_at?: string;
}

export interface TestExecution {
  id: string;
  plan_id?: string;
  case_id: string;
  executor_id?: string;
  executor: string;
  status: string;
  result?: string;
  execution_log?: Record<string, unknown>;
  failure_reason?: string;
  duration_seconds: number;
  started_at?: string;
  completed_at?: string;
  evidence_ref?: Record<string, unknown>;
  compounding_status?: string;
}

export interface TestingStats {
  total_projects: number;
  total_cases: number;
  total_plans: number;
  total_executions: number;
  cases_by_status: Record<string, number>;
  cases_by_type: Record<string, number>;
  executions_by_status: Record<string, number>;
  pass_rate: number;
}

// ===== 项目 API =====

export function getProjects(page = 1, size = 20): Promise<PageResponse<TestProject>> {
  return getData<PageResponse<TestProject>>(`${BASE}/projects`, { page, size });
}

export function getProject(id: string): Promise<TestProject> {
  return getData<TestProject>(`${BASE}/projects/${id}`);
}

export function createProject(data: Partial<TestProject>): Promise<TestProject> {
  return postData<TestProject>(`${BASE}/projects`, data);
}

export function updateProject(id: string, data: Partial<TestProject>): Promise<TestProject> {
  return putData<TestProject>(`${BASE}/projects/${id}`, data);
}

export function deleteProject(id: string): Promise<void> {
  return delData<void>(`${BASE}/projects/${id}`);
}

// ===== 需求 API =====

export function extractRequirements(data: {
  project_id: string;
  doc_ids: string[];
}): Promise<{ requirements: TestRequirement[]; count: number }> {
  return postData(`${BASE}/requirements/extract`, data);
}

export function getRequirements(
  projectId: string,
  page = 1,
  size = 20
): Promise<PageResponse<TestRequirement>> {
  return getData<PageResponse<TestRequirement>>(`${BASE}/requirements`, {
    project_id: projectId,
    page,
    size,
  });
}

export function getRequirement(id: string): Promise<TestRequirement> {
  return getData<TestRequirement>(`${BASE}/requirements/${id}`);
}

export function updateRequirement(id: string, data: Partial<TestRequirement>): Promise<TestRequirement> {
  return putData<TestRequirement>(`${BASE}/requirements/${id}`, data);
}

// ===== 用例 API =====

export function generateCases(data: {
  requirement_id: string;
  context_doc_ids?: string[];
}): Promise<{ cases: TestCase[]; count: number }> {
  return postData(`${BASE}/cases/generate`, data);
}

export function getCases(
  projectId?: string,
  status?: string,
  page = 1,
  size = 20
): Promise<PageResponse<TestCase>> {
  const params: Record<string, string | number> = { page, size };
  if (projectId) params.project_id = projectId;
  if (status) params.status = status;
  return getData<PageResponse<TestCase>>(`${BASE}/cases`, params);
}

export function getCase(id: string): Promise<TestCase> {
  return getData<TestCase>(`${BASE}/cases/${id}`);
}

export function createCase(data: Partial<TestCase>): Promise<TestCase> {
  return postData<TestCase>(`${BASE}/cases`, data);
}

export function updateCase(id: string, data: Partial<TestCase>): Promise<TestCase> {
  return putData<TestCase>(`${BASE}/cases/${id}`, data);
}

export function deleteCase(id: string): Promise<void> {
  return delData<void>(`${BASE}/cases/${id}`);
}

export function batchUpdateStatus(data: {
  case_ids: string[];
  status: string;
}): Promise<{ updated: number }> {
  return postData(`${BASE}/cases/batch-status`, data);
}

// ===== 评审 API =====

export function submitReview(data: {
  case_id: string;
  comment?: string;
}): Promise<TestReview> {
  return postData<TestReview>(`${BASE}/reviews`, data);
}

export function getPendingReviews(page = 1, size = 20): Promise<PageResponse<TestReview>> {
  return getData<PageResponse<TestReview>>(`${BASE}/reviews/pending`, { page, size });
}

export function getReviewsByCase(caseId: string): Promise<TestReview[]> {
  return getData<TestReview[]>(`${BASE}/reviews/case/${caseId}`);
}

export function approveReview(id: string, data?: { comment?: string }): Promise<TestReview> {
  return putData<TestReview>(`${BASE}/reviews/${id}/approve`, data);
}

export function rejectReview(id: string, data: { comment: string; suggestions?: unknown[] }): Promise<TestReview> {
  return putData<TestReview>(`${BASE}/reviews/${id}/reject`, data);
}

// ===== 计划 API =====

export function createPlan(data: Partial<TestPlan>): Promise<TestPlan> {
  return postData<TestPlan>(`${BASE}/plans`, data);
}

export function getPlans(projectId?: string, page = 1, size = 20): Promise<PageResponse<TestPlan>> {
  const params: Record<string, string | number> = { page, size };
  if (projectId) params.project_id = projectId;
  return getData<PageResponse<TestPlan>>(`${BASE}/plans`, params);
}

export function getPlan(id: string): Promise<TestPlan> {
  return getData<TestPlan>(`${BASE}/plans/${id}`);
}

export function orchestratePlan(id: string): Promise<TestPlan> {
  return postData<TestPlan>(`${BASE}/plans/${id}/orchestrate`);
}

// ===== 执行 API =====

export function getExecutions(
  planId?: string,
  page = 1,
  size = 20
): Promise<PageResponse<TestExecution>> {
  const params: Record<string, string | number> = { page, size };
  if (planId) params.plan_id = planId;
  return getData<PageResponse<TestExecution>>(`${BASE}/executions`, params);
}

export function recordExecution(data: Partial<TestExecution>): Promise<TestExecution> {
  return postData<TestExecution>(`${BASE}/executions`, data);
}

// ===== 统计 API =====

export function getStats(projectId?: string): Promise<TestingStats> {
  const params: Record<string, string> = {};
  if (projectId) params.project_id = projectId;
  return getData<TestingStats>(`${BASE}/stats`, params);
}
