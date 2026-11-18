// Typed client for the Kaizeval API. Every request goes to the same origin under /api/v1.

import type {
  AdaptersResponse,
  Agent,
  AgentCapabilities,
  AgentConfiguration,
  AuditEvent,
  AuthResult,
  Candidate,
  CaseResult,
  ChunkPage,
  Coverage,
  Dataset,
  Dict,
  EvaluationRun,
  Evidence,
  Finding,
  GenerateDatasetResult,
  ImportDatasetResult,
  Items,
  Job,
  KnowledgeDocument,
  Me,
  Member,
  OptimizationReport,
  OptimizationRun,
  Page,
  Patch,
  Preflight,
  Provider,
  Recommendation,
  Role,
  RolePolicies,
  RolePolicy,
  Specification,
  StartOptimizationResult,
  StartRunResult,
  SystemInfo,
  TestCase,
  Usage,
  VerifyAgentResult,
  Workspace,
} from "./types";

const BASE = "/api/v1";
const TOKEN_KEY = "kaizeval.token";

export interface Issue {
  field: string;
  message: string;
}

export class ApiError extends Error {
  status: number;
  code: string;
  issues: Issue[];

  constructor(status: number, code: string, message: string, issues: Issue[] = []) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.issues = issues;
  }
}

export function getToken(): string | null {
  try {
    return window.localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setToken(token: string | null): void {
  try {
    if (token) window.localStorage.setItem(TOKEN_KEY, token);
    else window.localStorage.removeItem(TOKEN_KEY);
  } catch {
    // Storage can be unavailable (private mode); the session then lasts until the page is closed.
  }
}

let onUnauthorized: (() => void) | null = null;

/** Called by the session provider so that any 401 signs the user out and returns to the login page. */
export function setUnauthorizedHandler(handler: (() => void) | null): void {
  onUnauthorized = handler;
}

type QueryValue = string | number | boolean | null | undefined;
type Query = Record<string, QueryValue>;

interface RequestOptions {
  query?: Query;
  body?: unknown;
  form?: FormData;
  /** Return the response body as text instead of parsed JSON. */
  text?: boolean;
  /** Login and sign-up: a 401 is an answer to show, not an expired session. */
  anonymous?: boolean;
}

function buildUrl(path: string, query?: Query): string {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value !== undefined && value !== null && value !== "") params.set(key, String(value));
  }
  const qs = params.toString();
  return `${BASE}${path}${qs ? `?${qs}` : ""}`;
}

async function request<T>(method: string, path: string, options: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = {};
  const token = getToken();
  if (token && !options.anonymous) headers.Authorization = `Bearer ${token}`;
  let body: BodyInit | undefined;
  if (options.form) {
    body = options.form;
  } else if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(options.body);
  }

  let response: Response;
  try {
    response = await fetch(buildUrl(path, options.query), { method, headers, body });
  } catch {
    throw new ApiError(0, "NETWORK_ERROR", "cannot reach the Kaizeval server; check that it is running");
  }

  if (!response.ok) {
    let code = `HTTP_${response.status}`;
    let message = response.statusText || "request failed";
    let issues: Issue[] = [];
    try {
      const payload = (await response.json()) as { error?: { code?: string; message?: string; issues?: Issue[] } };
      if (payload.error) {
        code = payload.error.code ?? code;
        message = payload.error.message ?? message;
        issues = payload.error.issues ?? [];
      }
    } catch {
      // Not a JSON error body; keep the HTTP status text.
    }
    if (response.status === 401 && !options.anonymous) {
      setToken(null);
      onUnauthorized?.();
    }
    throw new ApiError(response.status, code, message, issues);
  }

  if (response.status === 204) return undefined as T;
  if (options.text) return (await response.text()) as T;
  const raw = await response.text();
  return (raw ? JSON.parse(raw) : undefined) as T;
}

// --- instance-level calls -----------------------------------------------------------------

export const system = {
  info: () => request<SystemInfo>("GET", "/system", { anonymous: true }),
  login: (email: string, password: string) =>
    request<AuthResult>("POST", "/auth/login", { body: { email, password }, anonymous: true }),
  signup: (email: string, password: string, name: string) =>
    request<AuthResult>("POST", "/auth/signup", { body: { email, password, name }, anonymous: true }),
  me: () => request<Me>("GET", "/auth/me"),
  createWorkspace: (name: string, mode: "offline" | "connected") =>
    request<Workspace>("POST", "/workspaces", { body: { name, mode } }),
};

// --- workspace-scoped calls ---------------------------------------------------------------

// A type alias (not an interface) so that it is assignable to the generic query record.
export type PageQuery = {
  limit?: number;
  offset?: number;
};

export interface ProviderInput {
  name: string;
  kind: string;
  base_url?: string | null;
  api_key?: string | null;
  default_model?: string;
  allow_egress?: boolean;
  params?: Dict;
}

export interface RolePolicyInput {
  provider_id: string;
  model: string;
  temperature: number;
  max_tokens: number;
  timeout_s: number;
}

export interface AgentInput {
  name: string;
  adapter: string;
  config: Dict;
  credential?: string | null;
  access_level: string;
  allow_promote: boolean;
}

export interface SpecBuildInput {
  agent_id: string | null;
  requirements_text: string | null;
  requirement_document_ids: string[];
  infer: boolean;
}

export interface DatasetGenerateInput {
  name: string;
  agent_id: string | null;
  specification_id: string | null;
  n_cases: number;
  testers: number;
  holdout_fraction: number;
  seed: number;
  categories: string[];
  include_multi_turn: boolean;
  unanswerable_share: number;
  languages: string[];
}

export interface RunInput {
  agent_id: string;
  dataset_id: string;
  mode: string;
  split: "regression";
  agent_version: "baseline";
  config: Dict;
}

export interface OptimizationInput {
  agent_id: string;
  dataset_id: string;
  mode: string;
  policy: Dict;
}

export function workspaceApi(ws: string) {
  const w = `/workspaces/${ws}`;
  return {
    workspaceId: ws,

    // workspace and members
    getWorkspace: () => request<Workspace>("GET", w),
    updateWorkspace: (body: { name?: string; mode?: string; settings?: Dict }) =>
      request<Workspace>("PATCH", w, { body }),
    members: () => request<Items<Member>>("GET", `${w}/members`),
    setMember: (email: string, role: Role) =>
      request<{ user_id: string; email: string; role: Role }>("PUT", `${w}/members`, { body: { email, role } }),
    removeMember: (userId: string) => request<void>("DELETE", `${w}/members/${userId}`),

    // jobs
    job: (id: string) => request<Job>("GET", `${w}/jobs/${id}`),
    cancelRun: (runId: string) => request<EvaluationRun | OptimizationRun>("POST", `${w}/runs/${runId}/cancel`),

    // models
    providers: () => request<Items<Provider>>("GET", `${w}/model-providers`),
    createProvider: (body: ProviderInput) => request<Provider>("POST", `${w}/model-providers`, { body }),
    verifyProvider: (id: string) => request<Preflight>("POST", `${w}/model-providers/${id}/verify`),
    deleteProvider: (id: string) => request<void>("DELETE", `${w}/model-providers/${id}`),
    rolePolicies: () => request<RolePolicies>("GET", `${w}/model-role-policies`),
    setRolePolicy: (role: string, body: RolePolicyInput) =>
      request<RolePolicy>("PUT", `${w}/model-role-policies/${role}`, { body }),
    clearRolePolicy: (role: string) => request<void>("DELETE", `${w}/model-role-policies/${role}`),
    modelUsage: () => request<Usage>("GET", `${w}/model-usage`),

    // agents
    adapters: () => request<AdaptersResponse>("GET", `${w}/agent-adapters`),
    agents: () => request<Items<Agent>>("GET", `${w}/agents`),
    agent: (id: string) => request<Agent>("GET", `${w}/agents/${id}`),
    createAgent: (body: AgentInput) => request<Agent>("POST", `${w}/agents`, { body }),
    verifyAgent: (id: string) => request<VerifyAgentResult>("POST", `${w}/agents/${id}/verify`),
    deleteAgent: (id: string) => request<void>("DELETE", `${w}/agents/${id}`),
    agentCapabilities: (id: string) => request<AgentCapabilities>("GET", `${w}/agents/${id}/capabilities`),
    agentConfiguration: (id: string) => request<AgentConfiguration>("GET", `${w}/agents/${id}/configuration`),

    // knowledge
    documents: (query: PageQuery & { kind?: string; include_superseded?: boolean } = {}) =>
      request<Page<KnowledgeDocument>>("GET", `${w}/documents`, { query }),
    uploadDocument: (file: File, kind: string) => {
      const form = new FormData();
      form.append("file", file);
      form.append("kind", kind);
      return request<KnowledgeDocument>("POST", `${w}/documents`, { form });
    },
    chunks: (documentId: string, query: PageQuery = {}) =>
      request<ChunkPage>("GET", `${w}/documents/${documentId}/chunks`, { query }),
    evidence: (chunkId: string) => request<Evidence>("GET", `${w}/evidence/${chunkId}`),

    // specifications
    buildSpecification: (body: SpecBuildInput) => request<Job>("POST", `${w}/specifications:build`, { body }),
    specifications: (query: PageQuery = {}) => request<Page<Specification>>("GET", `${w}/specifications`, { query }),
    specification: (id: string) => request<Specification>("GET", `${w}/specifications/${id}`),
    editSpecification: (id: string, content: Dict, approve: boolean) =>
      request<Specification>("PATCH", `${w}/specifications/${id}`, { body: { content, approve } }),
    reviewSpecification: (id: string, decision: "approve" | "skip", note: string | null) =>
      request<Specification>("POST", `${w}/specifications/${id}/review`, { body: { decision, note } }),

    // datasets
    generateDataset: (body: DatasetGenerateInput) =>
      request<GenerateDatasetResult>("POST", `${w}/datasets:generate`, { body }),
    importDataset: (input: { file: File; name?: string; datasetId?: string; approved: boolean; holdoutFraction: number }) => {
      const form = new FormData();
      form.append("file", input.file);
      if (input.name) form.append("name", input.name);
      if (input.datasetId) form.append("dataset_id", input.datasetId);
      form.append("approved", input.approved ? "true" : "false");
      form.append("holdout_fraction", String(input.holdoutFraction));
      return request<ImportDatasetResult>("POST", `${w}/datasets:import`, { form });
    },
    datasets: (query: PageQuery = {}) => request<Page<Dataset>>("GET", `${w}/datasets`, { query }),
    dataset: (id: string) => request<Dataset>("GET", `${w}/datasets/${id}`),
    coverage: (id: string) => request<Coverage>("GET", `${w}/datasets/${id}/coverage`),
    cases: (id: string, query: PageQuery = {}) =>
      request<Page<TestCase>>("GET", `${w}/datasets/${id}/cases`, { query: { ...query, split: "regression" } }),
    /** Opens the sealed holdout. Audited by the server, and the holdout must be rotated afterwards. */
    openHoldoutCases: (id: string, query: PageQuery = {}) =>
      request<Page<TestCase>>("GET", `${w}/datasets/${id}/cases`, {
        query: { ...query, split: "holdout", acknowledge: true },
      }),
    reviewCase: (datasetId: string, caseId: string, answerStatus: string) =>
      request<TestCase>("PATCH", `${w}/datasets/${datasetId}/cases/${caseId}`, { body: { answer_status: answerStatus } }),
    rotateHoldout: (id: string, nCases: number | null) =>
      request<Job>("POST", `${w}/datasets/${id}/holdout:rotate`, { body: nCases ? { n_cases: nCases } : {} }),
    retireStale: (id: string) =>
      request<{ retired: number; dataset: Dataset }>("POST", `${w}/datasets/${id}/stale:retire`),

    // evaluation
    startRun: (body: RunInput) => request<StartRunResult>("POST", `${w}/evaluation-runs`, { body }),
    runs: (query: PageQuery & { standalone?: boolean; agent_id?: string; dataset_id?: string } = {}) =>
      request<Page<EvaluationRun>>("GET", `${w}/evaluation-runs`, { query }),
    run: (id: string) => request<EvaluationRun>("GET", `${w}/evaluation-runs/${id}`),
    runResults: (id: string, query: PageQuery & { verdict?: string } = {}) =>
      request<Page<CaseResult>>("GET", `${w}/evaluation-runs/${id}/results`, { query: { ...query, acknowledge: true } }),
    runFindings: (id: string, query: PageQuery = {}) =>
      request<Page<Finding>>("GET", `${w}/evaluation-runs/${id}/findings`, { query }),
    reportText: (objectId: string, format: "json" | "md") =>
      request<string>("GET", `${w}/reports/${objectId}`, { query: { format }, text: true }),
    optimizationReport: (id: string) => request<OptimizationReport>("GET", `${w}/reports/${id}`),

    // recommendations
    recommendations: (runId?: string) =>
      request<Items<Recommendation>>("GET", `${w}/recommendations`, { query: { run_id: runId } }),
    buildRecommendations: (runId: string) =>
      request<Items<Recommendation>>("POST", `${w}/recommendations`, { body: { run_id: runId } }),
    setRecommendationState: (id: string, state: string, reason: string | null) =>
      request<Recommendation>("PATCH", `${w}/recommendations/${id}`, { body: { state, reason } }),

    // optimization
    startOptimization: (body: OptimizationInput) =>
      request<StartOptimizationResult>("POST", `${w}/optimization-runs`, { body }),
    optimizations: (query: PageQuery = {}) =>
      request<Page<OptimizationRun>>("GET", `${w}/optimization-runs`, { query }),
    optimization: (id: string) => request<OptimizationRun>("GET", `${w}/optimization-runs/${id}`),
    stopOptimization: (id: string) => request<OptimizationRun>("POST", `${w}/optimization-runs/${id}/stop`),
    continueOptimization: (id: string, policy: Record<string, unknown> = {}) =>
      request<StartOptimizationResult>("POST", `${w}/optimization-runs/${id}/continue`, { body: { policy } }),
    decidePatch: (patchId: string, decision: "approved" | "rejected", note: string | null) =>
      request<{ patch: Patch; optimization_run: OptimizationRun | null }>(
        "POST", `${w}/candidate-patches/${patchId}/decision`, { body: { decision, note } }),

    // candidates
    candidates: () => request<Items<Candidate>>("GET", `${w}/candidates`),
    candidate: (id: string) => request<Candidate>("GET", `${w}/candidates/${id}`),
    decideRelease: (id: string, decision: "approved" | "rejected", note: string | null) =>
      request<{ approval: Dict; candidate: Candidate }>("POST", `${w}/candidates/${id}/approve`, {
        body: { decision, note },
      }),
    promoteCandidate: (id: string) =>
      request<{ result: Dict; candidate: Candidate }>("POST", `${w}/candidates/${id}/promote`),
    rollbackCandidate: (id: string, note: string | null) =>
      request<{ result: Dict; candidate: Candidate }>("POST", `${w}/candidates/${id}/rollback`, { body: { note } }),

    // audit
    audit: (query: PageQuery & { action?: string } = {}) => request<Page<AuditEvent>>("GET", `${w}/audit`, { query }),
  };
}

export type WorkspaceApi = ReturnType<typeof workspaceApi>;
