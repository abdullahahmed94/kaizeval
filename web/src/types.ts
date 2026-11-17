// Shapes returned by the Kaizeval API (/api/v1). They mirror the backend rows and service results.

export type Role = "owner" | "editor" | "reviewer" | "viewer";
export type JobStatus = "queued" | "running" | "review_required" | "completed" | "failed" | "cancelled";
export type Dict<T = unknown> = Record<string, T>;

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export interface Items<T> {
  items: T[];
}

// --- identity ---------------------------------------------------------------------------

export interface User {
  id: string;
  email: string;
  name: string;
  is_active: boolean;
  created_at: string;
}

export interface WorkspaceSettings {
  allow_autonomous_optimization?: boolean;
  retention_days?: number;
}

export interface Workspace {
  id: string;
  name: string;
  mode: "offline" | "connected";
  settings: WorkspaceSettings | null;
  created_at: string;
  role: Role | null;
  effective_offline: boolean;
  instance_offline: boolean;
}

export interface AdapterInfo {
  label: string;
  description: string;
  max_access_level: AccessLevel;
}

export interface SystemInfo {
  name: string;
  version: string;
  edition: string;
  instance_offline: boolean;
  signup_open: boolean;
  model_roles: string[];
  provider_kinds: string[];
  adapters: Dict<AdapterInfo>;
  roles: Role[];
}

export interface AuthResult {
  token: string;
  user: User;
}

export interface Me {
  user: User;
  workspaces: Workspace[];
}

export interface Member {
  user_id: string;
  email: string;
  name: string;
  role: Role;
}

// --- jobs -------------------------------------------------------------------------------

export interface Job {
  id: string;
  kind: string;
  status: JobStatus;
  progress: { total?: number; done?: number; run_id?: string } | null;
  result: Dict | null;
  error: string | null;
  attempts: number;
  cancel_requested: boolean;
  created_by: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

// --- models -----------------------------------------------------------------------------

export interface PreflightCheck {
  name: string;
  ok: boolean;
  detail: string;
}

export interface Preflight {
  ok: boolean;
  checks: PreflightCheck[];
  checked_at: string;
  offline: boolean;
  synthetic: boolean;
}

export interface Provider {
  id: string;
  name: string;
  kind: string;
  base_url: string | null;
  has_credential: boolean;
  default_model: string;
  allow_egress: boolean;
  params: Dict;
  last_preflight: Preflight | null;
  synthetic: boolean;
  created_at: string;
}

export interface RolePolicy {
  id: string;
  role: string;
  provider_id: string;
  model: string;
  temperature: number;
  max_tokens: number;
  timeout_s: number;
}

export interface RoleRouting {
  role: string;
  policy: RolePolicy | null;
  inherits_default: boolean;
  effective_provider_id: string | null;
  ready: boolean;
  synthetic: boolean;
}

export interface RolePolicies {
  default: RolePolicy | null;
  roles: RoleRouting[];
}

export interface UsageRow {
  calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  latency_ms: number;
  estimated_cost: number | null;
  provider_kind?: string;
}

export interface Usage extends UsageRow {
  by_role: Dict<UsageRow>;
  cost_note: string;
}

// --- agents -----------------------------------------------------------------------------

export type AccessLevel = "black_box" | "read_only" | "candidate_write";

export interface Agent {
  id: string;
  name: string;
  adapter: string;
  config: Dict;
  has_credential: boolean;
  access_level: AccessLevel;
  allow_promote: boolean;
  capabilities: string[];
  verified_at: string | null;
  verified: boolean;
  last_verification: {
    ok: boolean;
    at?: string;
    code?: string;
    message?: string;
    healthcheck?: Dict;
    adapter_capabilities?: string[];
    patchable_settings?: string[] | null;
  } | null;
  created_at: string;
}

export interface AdaptersResponse {
  adapters: Dict<AdapterInfo>;
  capabilities: string[];
  access_levels: Dict<string[]>;
}

export interface CapabilityRow {
  capability: string;
  supported_by_adapter: boolean;
  granted: boolean;
  available: boolean;
  reason: string | null;
}

export interface AgentCapabilities {
  verified: boolean;
  access_level: AccessLevel;
  capabilities: string[];
  matrix: CapabilityRow[];
  modes: {
    recommendations_only: boolean;
    human_guided_optimization: boolean;
    autonomous_optimization: boolean;
    promotion: boolean;
  };
  explanation: string | null;
}

export interface VerifyAgentResult {
  ok: boolean;
  healthcheck: Dict;
  capabilities: string[];
  matrix: CapabilityRow[];
}

export interface SettingSchema {
  type?: string;
  min?: number;
  max?: number;
  step?: number;
  suggested?: unknown;
  description?: string;
  addresses?: string[];
}

export interface AgentConfiguration {
  config: Dict;
  digest?: string;
  schema: Dict<SettingSchema>;
  description?: string;
}

// --- knowledge --------------------------------------------------------------------------

export interface KnowledgeDocument {
  id: string;
  filename: string;
  kind: "kb" | "requirements";
  content_type: string;
  sha256: string;
  version: number;
  status: "parsed" | "failed" | "superseded";
  error: string | null;
  size_bytes: number;
  chunk_count: number;
  supersedes_id: string | null;
  created_at: string;
  flagged_chunks?: number;
}

export interface Chunk {
  id: string;
  document_id: string;
  ordinal: number;
  text: string;
  page: number | null;
  section: string | null;
  char_start: number;
  char_end: number;
  flags: string[];
}

export interface ChunkPage extends Page<Chunk> {
  document: KnowledgeDocument;
}

export interface Evidence extends Chunk {
  filename: string | null;
  document_version: number | null;
  sha256: string | null;
}

// --- specifications ---------------------------------------------------------------------

export interface SpecObjective {
  id?: string;
  statement: string;
  source: string;
  confidence?: number;
  acceptance_criteria?: string[];
  evidence_refs?: string[];
}

export interface SpecContent {
  purpose?: string;
  objectives?: SpecObjective[];
  constraints?: string[];
  assumptions?: string[];
  open_questions?: string[];
  languages?: string[];
  provisional?: boolean;
  uncertainty?: string[];
  [key: string]: unknown;
}

export interface SpecReview {
  decision: "approve" | "skip";
  by: string;
  at: string;
  note: string | null;
}

export interface SpecVersion {
  id: string;
  version: number;
  status: string;
  source: string;
  created_at: string;
  created_by: string | null;
}

export interface Specification {
  id: string;
  agent_id: string | null;
  lineage_id: string;
  version: number;
  parent_id: string | null;
  source: string;
  status: "draft" | "reviewed" | "skipped";
  content: SpecContent;
  synthetic: boolean;
  created_by: string | null;
  review: SpecReview | null;
  created_at: string;
  versions?: SpecVersion[];
}

// --- datasets ---------------------------------------------------------------------------

export interface Dataset {
  id: string;
  name: string;
  agent_id: string | null;
  specification_id: string | null;
  status: "generating" | "ready" | "failed";
  version: number;
  seed: number;
  holdout_fraction: number;
  synthetic: boolean;
  holdout_exposed: boolean;
  holdout_generation: number;
  notes: string[];
  created_at: string;
  counts: Dict<number>;
}

export interface Coverage {
  dataset_id: string;
  version: number;
  synthetic: boolean;
  total: number;
  regression: {
    count: number;
    by_category: Dict<number>;
    by_severity: Dict<number>;
    by_language: Dict<number>;
    by_answer_status: Dict<number>;
    by_document: Dict<number>;
    multi_turn: number;
    stale: number;
  };
  holdout: {
    count: number;
    sealed: boolean;
    generation: number;
    exposed: boolean;
    by_category: Dict<number>;
  };
  uncovered_documents: string[];
  limits: string[];
  notes: string[];
}

export interface Turn {
  role: string;
  content: string;
}

export interface Expected {
  behavior?: string;
  answer?: string;
  key_facts?: string[];
  follow_up_answer?: string;
  follow_up_key_facts?: string[];
}

export interface EvidenceRef {
  provider?: string;
  chunk_id?: string;
  document_id?: string;
  filename?: string;
  document_version?: number;
  sha256?: string;
  page?: number | null;
  section?: string | null;
  quote?: string;
}

export interface TestCase {
  id: string;
  dataset_id: string;
  split: string;
  holdout_generation: number;
  category: string;
  severity: string;
  language: string;
  turns: Turn[];
  expected: Expected;
  answer_status: string;
  evidence_refs: EvidenceRef[];
  provenance: Dict;
  weight: number;
  stale: boolean;
  created_at: string;
}

export interface GenerateDatasetResult {
  job: Job;
  dataset: Dataset;
}

export interface ImportDatasetResult {
  dataset: Dataset;
  imported: number;
  skipped: number;
  regression: number;
  holdout: number;
}

// --- evaluation -------------------------------------------------------------------------

export interface DimensionSummary {
  mean: number;
  n: number;
  methods: Dict<number>;
}

export interface CategorySummary {
  n: number;
  scored: number;
  mean: number | null;
  pass_rate: number;
}

export interface AgentUsage {
  calls: number;
  estimated_cost: number | null;
  cost_note: string;
}

export interface RunSummary {
  cases: number;
  scored: number;
  not_scored: number;
  overall: number | null;
  pass_rate: number | null;
  by_dimension: Dict<DimensionSummary>;
  by_category: Dict<CategorySummary>;
  critical_failures: number;
  needs_human_review: number;
  agent_errors: number;
  evaluator_agreement: { compared: number; agreed: number; rate: number | null; arbitrated: number } | null;
  latency_ms: { p50: number; p95: number; mean: number };
  agent: AgentUsage;
  model_usage: Usage;
  reference_basis: Dict<number>;
  caveats: string[];
  partial?: boolean;
}

export interface EvaluationRun {
  id: string;
  agent_id: string;
  dataset_id: string;
  dataset_version: number;
  split: "regression" | "holdout";
  kind: string;
  agent_version: string;
  agent_config_digest: string | null;
  mode: "deterministic" | "single" | "dual";
  status: JobStatus;
  config: Dict;
  summary: RunSummary | null;
  error: string | null;
  synthetic: boolean;
  optimization_run_id: string | null;
  job_id: string | null;
  created_by: string | null;
  created_at: string;
  finished_at: string | null;
  job?: Job | null;
}

export interface StartRunResult {
  job: Job;
  run: EvaluationRun;
}

export interface ScoreDetail {
  score: number;
  method: string;
  detail?: string;
}

export interface Judgment {
  task: string;
  role: string;
  model: string;
  prompt_version: string;
  synthetic: boolean;
  scores: Dict<number>;
  verdict: string;
  confidence: number;
  rationale: string;
  needs_human_review?: boolean;
}

export interface CaseFinal {
  scores?: Dict<ScoreDetail>;
  overall: number | null;
  verdict: string;
  critical_failure?: boolean;
  needs_human_review?: boolean;
  conflicts?: string[];
  model_estimated?: boolean;
  reason?: string;
  judge?: { agreement?: boolean | null; arbitrated?: boolean; confidence?: number; verdict?: string };
  judge_error?: string;
  attempt_overalls?: number[];
}

export interface ReplyTurn {
  text: string;
  citations: string[];
  latency_ms: number;
}

export interface CaseResult {
  id: string;
  run_id: string;
  case_id: string;
  split: string;
  response: { turns?: ReplyTurn[]; text?: string; citations?: string[] };
  latency_ms: number;
  agent_cost: number | null;
  error: string | null;
  deterministic: Dict<{ score: number; decisive: boolean; detail?: string; missing?: string[] }>;
  judgments: Judgment[];
  final: CaseFinal;
  overall: number | null;
  verdict: string;
  created_at: string;
  case?: {
    category: string;
    severity: string;
    language: string;
    turns: Turn[];
    expected: Expected;
    answer_status: string;
    evidence_refs: EvidenceRef[];
    stale: boolean;
  };
}

export interface Finding {
  id: string;
  run_id: string;
  case_result_id: string;
  category: string;
  severity: string;
  observation: string;
  evidence: Dict;
  hypothesis: string;
  confidence: number;
  source: string;
  created_at: string;
}

export interface Recommendation {
  id: string;
  run_id: string;
  title: string;
  detail: string;
  category: string;
  target_component: string;
  impact: { cases_affected?: number; share_of_cases_with_findings?: number; critical_cases?: number; note?: string };
  risk: string;
  required_capability: string;
  auto_apply_available: boolean;
  auto_apply_blocker: string | null;
  suggested_patch: Dict | null;
  priority: number;
  state: "proposed" | "approved" | "deferred" | "rejected" | "applied";
  state_reason: string | null;
  state_by: string | null;
  synthetic: boolean;
  created_at: string;
}

// --- comparison -------------------------------------------------------------------------

export interface PairedDelta {
  n: number;
  verdict: string;
  baseline_mean?: number;
  candidate_mean?: number;
  delta?: number;
  delta_percentage_points?: number;
  relative_change_pct?: number | null;
  ci95?: [number, number] | null;
  ci95_percentage_points?: [number, number] | null;
  wins?: number;
  losses?: number;
  ties?: number;
  sign_test_p?: number | null;
  interpretation?: string;
}

export interface GroupDelta {
  n: number;
  baseline_mean: number;
  candidate_mean: number;
  delta_percentage_points: number;
}

export interface CriticalFailures {
  baseline: number;
  candidate: number;
  new: number;
  fixed: number;
}

export interface LatencyComparison {
  baseline_p50: number | null;
  candidate_p50: number | null;
  baseline_p95: number | null;
  candidate_p95: number | null;
}

export interface Comparison {
  baseline_run_id: string;
  candidate_run_id: string;
  baseline_version: string;
  candidate_version: string;
  split: string;
  paired_cases: number;
  unpaired_cases: number;
  overall: PairedDelta;
  by_category: Dict<GroupDelta>;
  by_dimension: Dict<GroupDelta>;
  critical_failures: CriticalFailures;
  pass_rate: { baseline: number | null; candidate: number | null };
  latency_ms: LatencyComparison;
  agent_cost: { baseline: number | null; candidate: number | null };
  synthetic: boolean;
  conditions: { same_dataset_version: boolean; same_mode: boolean; dataset_version: number };
}

export interface HoldoutResult {
  status: "completed" | "skipped" | "failed";
  reason?: string;
  experiment_id?: string;
  generation?: number;
  paired_cases?: number;
  overall?: PairedDelta;
  by_category?: Dict<GroupDelta>;
  critical_failures?: CriticalFailures;
  latency_ms?: LatencyComparison;
  synthetic?: boolean;
  note?: string;
}

// --- optimization -----------------------------------------------------------------------

export interface OptimizationPolicy {
  max_rounds: number;
  target_score: number | null;
  min_improvement: number;
  stagnation_rounds: number;
  max_category_regression: number;
  allow_new_critical_failures: boolean;
  category_minimums: Dict<number>;
  max_latency_increase_pct: number | null;
  eval_mode: string;
  eval_config: Dict;
  run_holdout: boolean;
  limits: Dict<number>;
}

export interface HistoryEntry {
  round: number;
  patch: Dict;
  outcome: string;
  overall?: number;
  delta_vs_baseline_pp?: number;
  reasons?: string[];
  evaluation_run_id?: string;
}

export interface OptimizationState {
  phase?: string;
  round?: number;
  history?: HistoryEntry[];
  stagnation?: number;
  best_overall?: number;
  baseline_overall?: number;
  pending_patch_id?: string | null;
}

export interface Patch {
  id: string;
  candidate_id: string;
  round: number;
  patch: Dict;
  diff: string;
  rationale: string;
  risk: string;
  state: string;
  decided_by: string | null;
  decision_note: string | null;
  outcome: {
    overall?: number;
    delta_vs_baseline_pp?: number;
    verdict?: string;
    reasons?: string[];
    evaluation_run_id?: string;
  } | null;
  synthetic: boolean;
  created_at: string;
}

export interface ExperimentBrief {
  id: string;
  round: number;
  kind: string;
  decision: string;
  overall: PairedDelta | null;
  critical_failures: CriticalFailures | null;
}

export interface OptimizationRun {
  id: string;
  agent_id: string;
  dataset_id: string;
  mode: "human_guided" | "autonomous";
  status: JobStatus;
  policy: OptimizationPolicy;
  state: OptimizationState;
  candidate_id: string | null;
  baseline_run_id: string | null;
  best_run_id: string | null;
  holdout: HoldoutResult | null;
  stop_reason: string | null;
  error: string | null;
  synthetic: boolean;
  job_id: string | null;
  created_by: string | null;
  created_at: string;
  finished_at: string | null;
  patches?: Patch[];
  experiments?: ExperimentBrief[];
  job?: Job | null;
}

export interface StartOptimizationResult {
  job: Job;
  optimization_run: OptimizationRun;
}

export interface Candidate {
  id: string;
  agent_id: string;
  external_id: string;
  status: "active" | "rejected" | "awaiting_decision" | "promoted" | "rolled_back";
  baseline_snapshot: Dict;
  baseline_digest: string;
  config_snapshot: Dict;
  optimization_run_id: string | null;
  diff_from_baseline: string;
  created_at: string;
  patches?: Patch[];
}

export interface ReleaseApproval {
  decision: "approved" | "rejected";
  by: string;
  at: string;
  note: string | null;
  consumed: boolean;
}

export interface OptimizationReport {
  type: "optimization";
  id: string;
  generated_at: string;
  synthetic: boolean;
  banner: string | null;
  regression_comparison: Comparison | null;
  holdout: HoldoutResult | null;
  usage: { platform_models: Usage; target_agent: AgentUsage };
  release_decision: { candidate_status: string | null; approvals: ReleaseApproval[]; note: string };
  caveats: string[];
}

// --- audit ------------------------------------------------------------------------------

export interface AuditEvent {
  id: string;
  user_id: string | null;
  action: string;
  subject_type: string;
  subject_id: string;
  detail: Dict;
  created_at: string;
}
