// TypeScript mirrors of the v5 API contract.

// ---------------------------------------------------------------------------
// Config (/api/config — flat v5 Settings contract)
// ---------------------------------------------------------------------------

export interface ConfigData {
  upstream_url: string | null;
  upstream_model: string | null;
  /** "***" when a key is set, null otherwise. Never the real value. */
  upstream_api_key: string | null;
  anthropic_upstream_url: string;
  runtime_embed_base_url: string;
  runtime_embedding_model: string;
  embedding_provider: string;
  log_level: string;
  dedup_hard_threshold: number;
  dedup_soft_threshold: number;
  bounce_budget: number;
  sdd_fast_require_approval: boolean;
  profile_root: string;
  forced_profile: string | null;
  code_indexer_url: string | null;
  authoring_model: string;
  authoring_critic_model: string;
  authoring_lm_base_url: string;
  // Read-only display fields
  duckdb_path: string;
  fragments_lance_path: string;
  telemetry_db_path: string;
  env_file_path: string;
}

/** Read-only fields that must never be sent in a PUT. */
export type ReadOnlyConfigKey =
  | 'duckdb_path'
  | 'fragments_lance_path'
  | 'telemetry_db_path'
  | 'env_file_path';

export type ConfigUpdate = Partial<Omit<ConfigData, ReadOnlyConfigKey>>;

export interface ConfigUpdateResult {
  status: string;
  message: string;
  env_file_path: string;
}

export interface ReloadResult {
  status: string;
  message: string;
}

// ---------------------------------------------------------------------------
// Telemetry (/telemetry/*)
// ---------------------------------------------------------------------------

export interface TraceRecord {
  trace_id: string;
  correlation_id: string | null;
  /** Epoch milliseconds. */
  request_ts: number | null;
  phase: string | null;
  category: string | null;
  task_prompt: string | null;
  selected_fragment_ids: string[] | null;
  source_skill_ids: string[] | null;
  system_skill_ids: string[] | null;
  workflow_skill_ids: string[] | null;
  assembly_tier: string | null;
  assembly_model: string | null;
  retrieval_latency_ms: number | null;
  assembly_latency_ms: number | null;
  total_latency_ms: number | null;
  status: string | null;
  error_code: string | null;
  response_size_chars: number | null;
  prompt_version: string | null;
  event_type: string | null;
  pre_filter_matched: boolean | number | null;
  gates_met: string[] | null;
  gates_unmet: string[] | null;
  qwen_calls: number | null;
  contract_path: string | null;
  contract_tags: string[] | null;
  bm25_source: string | null;
  reranked: boolean | null;
  tokens_returned: number | null;
  tokens_flat_equivalent: number | null;
  lm_assist_outcome: string | null;
  lm_assist_model: string | null;
  lm_assist_kept_ids: string[] | null;
  lm_assist_dropped_ids: string[] | null;
  lm_assist_scores: string | null;
  dense_leg_degraded: boolean | null;
  phase_gate_embed_failed: boolean | null;
  repo: string | null;
  session_key: string | null;
  session_source: string | null;
}

export interface TracesParams {
  limit?: number;
  offset?: number;
  phase?: string;
  status?: string;
  /** Epoch milliseconds. */
  since?: number;
  /** Epoch milliseconds. */
  until?: number;
  repo?: string;
}

export interface TracesResponse {
  total: number;
  offset: number;
  limit: number;
  traces: TraceRecord[];
}

export interface PhaseSavings {
  phase: string;
  composes: number;
  tokens_returned: number;
  tokens_flat_equivalent: number;
  tokens_saved: number;
  savings_pct: number;
}

export interface SavingsResponse {
  total_composes: number;
  tokens_returned: number;
  tokens_flat_equivalent: number;
  tokens_saved: number;
  savings_pct: number;
  per_phase: PhaseSavings[];
}

// Coverage v2: composed vs passthrough rates.
export interface PhaseCoverage {
  phase: string;
  composed: number;
  passthrough: number;
}

export interface RepoCoverage {
  repo: string | null;
  composed: number;
  passthrough: number;
}

export interface CoverageResponse {
  total: number;
  composed: number;
  passthrough: number;
  compose_rate: number;
  per_phase: PhaseCoverage[];
  per_repo: RepoCoverage[];
}

// ---------------------------------------------------------------------------
// Diagnostics (/diagnostics/*) — defensive: everything optional, statuses are
// open strings ("ok" | "degraded" | "unavailable" expected, render unknowns).
// ---------------------------------------------------------------------------

export interface SkillVersionEntry {
  skill_id?: string;
  version_id?: string;
  version_number?: number;
}

export interface VersionMismatch {
  skill_id?: string;
  store_version?: string;
  cache_version?: string;
}

export interface ConsistencyReport {
  matched?: number;
  missing_in_cache?: string[];
  missing_in_store?: string[];
  version_mismatches?: VersionMismatch[];
}

export interface DependencyReadiness {
  runtime_store?: string;
  telemetry_store?: string;
  embedding_runtime?: string;
  runtime_cache?: string;
  per_path?: Record<string, string>;
}

export interface RuntimeDiagnostics {
  cache_loaded?: boolean;
  store_state?: SkillVersionEntry[];
  runtime_state?: SkillVersionEntry[];
  consistency?: ConsistencyReport;
  dependency_readiness?: DependencyReadiness;
}

export interface CorpusDiagnostics {
  skill_count?: number;
  embedded_vector_count?: number;
  embedding_dim?: number | null;
}

// ---------------------------------------------------------------------------
// Skills (/api/skills, /skills/{id})
// ---------------------------------------------------------------------------

export type SkillClass = 'domain' | 'system' | 'workflow';
export type OverrideLayer = 'project' | 'profile';

export interface SkillSummary {
  skill_id: string;
  canonical_name: string;
  category: string;
  skill_class: SkillClass;
  domain_tags: string[];
  phase_scope: string[] | null;
  tier: string | null;
  description: string | null;
  always_apply: boolean;
  pack: string | null;
  override_layer: OverrideLayer | null;
}

export interface SkillsListResponse {
  total: number;
  skills: SkillSummary[];
}

export interface SkillsListParams {
  class?: string;
  category?: string;
  phase?: string;
  q?: string;
}

export interface SkillActiveVersion {
  version_id: string;
  version_number: number;
  authored_at: string | null;
  author: string | null;
  change_summary: string | null;
  raw_prose: string;
}

export interface SkillFragment {
  fragment_id: string;
  fragment_type: string;
  sequence: number;
  content: string;
}

export interface SkillDetail {
  skill_id: string;
  canonical_name: string;
  category: string;
  skill_class: SkillClass;
  is_active: boolean;
  active_version: SkillActiveVersion | null;
  fragments: SkillFragment[];
  // Detail endpoint may carry more metadata than the summary; tolerate drift.
  tier?: string | null;
  domain_tags?: string[];
  [key: string]: unknown;
}

export interface SkillVersion {
  version_id: string;
  version_number: number;
  authored_at: string | null;
  author: string | null;
  change_summary: string | null;
  status: string | null;
  raw_prose: string;
  is_active: boolean;
}

export interface SkillVersionsResponse {
  skill_id: string;
  versions: SkillVersion[];
}

export interface SkillOverride {
  skill_id: string;
  skill_class: string | null;
  active_layer: 'project' | 'profile' | 'default';
  active_profile: string;
  paths: {
    project: string | null;
    profile: string | null;
    default: string | null;
  };
  raw_prose: string | null;
  domain_tags: string[];
  shipped_raw_prose: string | null;
  locked_fields: Record<string, unknown>;
  prose_invariants: string[];
}

export interface OverrideUpdate {
  layer: OverrideLayer;
  raw_prose: string;
  domain_tags?: string[];
}

export interface OverrideWriteResult {
  status: string;
  layer?: string;
  path?: string;
  message?: string;
  [key: string]: unknown;
}

// ---------------------------------------------------------------------------
// Playground (/retrieve, /compose, /api/signal/evaluate)
// ---------------------------------------------------------------------------

export interface RetrieveRequest {
  task: string;
  phase?: string;
  domain_tags?: string[];
  k?: number;
}

export interface RetrieveResult {
  skill_id: string;
  version_id: string;
  canonical_name: string;
  raw_prose: string;
  /** 0..1 */
  score: number;
}

export interface RetrieveResponse {
  status: string;
  results: RetrieveResult[];
}

/** Compose is rendered defensively — every key optional, unknowns surfaced raw. */
export interface ComposeResponse {
  status?: string;
  result_type?: string;
  output?: string;
  source_skills?: string[];
  latency_ms?: {
    retrieval_ms?: number;
    assembly_ms?: number;
    total_ms?: number;
    [key: string]: unknown;
  };
  dense_leg_degraded?: boolean;
  [key: string]: unknown;
}

export interface SignalEvaluateRequest {
  repo: string;
  prompt: string;
}

export interface SignalVerdict {
  should_compose: boolean;
  phase: string | null;
  task: string | null;
  domain_tags: string[];
  announce: boolean;
  workflow_skill_id: string | null;
  current_contract: string | null;
  pre_filter_matched: string | null;
  gates_met: string[];
  gates_unmet: string[];
  qwen_calls: number;
  phase_gate_embed_failed: boolean;
  advisories: string[];
  banner: string | null;
  would_announce: boolean;
}

// ---------------------------------------------------------------------------
// Repos (/api/repos, /api/repos/gates)
// ---------------------------------------------------------------------------

export interface RepoEntry {
  repo_root: string;
  harnesses: string[];
  exists: boolean;
  phase: string | null;
  lifecycle_mode: string | null;
  profile: string | null;
  upstream_url: string | null;
  upstream_model: string | null;
  cursor: string | null;
  contracts_by_phase: Record<string, number>;
  approval_required: boolean;
  approval_pending: boolean;
}

export interface ReposResponse {
  total: number;
  repos: RepoEntry[];
}

export interface RepoGates {
  repo: string;
  phase: string | null;
  next_phase: string | null;
  blocked: boolean;
  advisories: string[];
  approval_required: boolean;
  approval_pending: boolean;
  approver: string | null;
  /** ISO datetime string. */
  approved_at: string | null;
}

// ---------------------------------------------------------------------------
// Approvals (/api/approvals, /api/repos/approve)
// ---------------------------------------------------------------------------

export interface PendingApproval {
  repo: string;
  phase: string;
  next_phase: string | null;
  /** true = approved once, but the artifact changed after sign-off. */
  stale: boolean;
  artifacts: string[];
}

export interface ApprovalsResponse {
  total: number;
  pending: PendingApproval[];
}

export interface ApproveRequest {
  repo: string;
  phase: string;
  approver?: string;
}

/** run_approve result — approval AUTO-ADVANCES the repo phase. */
export interface ApproveResult {
  ok: boolean;
  phase: string;
  approver: string;
  marker: string;
  advanced?: { phase?: string; [key: string]: unknown } | null;
  [key: string]: unknown;
}

// ---------------------------------------------------------------------------
// Doctor (/api/doctor) — render defensively, every key optional.
// ---------------------------------------------------------------------------

export interface DoctorCheck {
  name?: string;
  passed?: boolean;
  duration_ms?: number;
  detail?: string;
  error?: string;
  remediation?: string;
  severity?: string;
  [key: string]: unknown;
}

export interface DoctorResponse {
  schema_version?: number | string;
  all_checks_passed?: boolean;
  checks?: DoctorCheck[];
  [key: string]: unknown;
}

// ---------------------------------------------------------------------------
// Packs (/api/packs)
// ---------------------------------------------------------------------------

export interface PackEntry {
  name: string;
  version: string | null;
  tier: string | null;
  description: string | null;
  skill_count: number;
  installed_count: number;
}

export interface PacksResponse {
  total: number;
  packs: PackEntry[];
}

// ---------------------------------------------------------------------------
// Reembed (/api/reembed, /api/reembed/status)
// ---------------------------------------------------------------------------

export interface ReembedStatus {
  embedded_total: number;
  unembedded: number;
}

/** dry_run:true → would_embed; dry_run:false → exit_code (+ optional dedup lists). */
export interface ReembedResult {
  dry_run: boolean;
  would_embed?: number;
  exit_code?: number;
  dedup_hard?: Record<string, unknown>[];
  dedup_soft?: Record<string, unknown>[];
  [key: string]: unknown;
}

// ---------------------------------------------------------------------------
// Profiles (/api/profiles, /api/profiles/resolve)
// ---------------------------------------------------------------------------

export interface ProfileEntry {
  name: string;
  is_default: boolean;
  active_for_cwd: boolean;
  match_remote: string[];
  match_path: string[];
  has_overrides: boolean;
}

export interface ProfilesResponse {
  total: number;
  profiles: ProfileEntry[];
}

export interface ProfileResolveResult {
  repo: string;
  profile: string;
  is_default: boolean;
}

// ---------------------------------------------------------------------------
// Wizard (/api/wizard/*) — custom-skill creation, the human-driven twin of the
// add-skill lane: scaffold → draft → validate → approve + install.
// ---------------------------------------------------------------------------

export interface WizardScaffoldRequest {
  repo: string;
  pack: string;
  skill_id: string;
  skill_class?: string;
  canonical_name?: string;
}

/** new-skill-pack result — open shape; skill_file/skill_yaml are appended by the API. */
export interface WizardScaffoldResult {
  skill_file?: string;
  skill_yaml?: string;
  action?: string;
  [key: string]: unknown;
}

export interface WizardPackFile {
  name: string;
  content: string;
}

export interface WizardPackContents {
  pack: string;
  pack_dir: string;
  exists: boolean;
  files: WizardPackFile[];
}

export interface WizardFileWriteRequest {
  repo: string;
  pack: string;
  file: string;
  content: string;
}

export interface WizardFileWriteResult {
  status: string;
  path: string;
}

/**
 * validate-pack result — rendered defensively: the contract is open, so every
 * key is optional and error-ish arrays are discovered at runtime.
 */
export interface WizardValidateResult {
  ok?: boolean;
  valid?: boolean;
  errors?: unknown[];
  warnings?: unknown[];
  skills?: unknown;
  [key: string]: unknown;
}

export interface WizardInstallRequest {
  repo: string;
  pack: string;
  approver?: string;
  allow_duplicates?: boolean;
}

/** run_approve outcome when the repo sat in the add-skill lane; null otherwise. */
export interface WizardApprovalOutcome {
  ok?: boolean;
  phase?: string;
  approver?: string;
  marker?: string;
  advanced?: { phase?: string; [key: string]: unknown } | null;
  [key: string]: unknown;
}

export interface WizardInstallOutcome {
  action?: string;
  skills_ingested?: number;
  dedup_hard?: Record<string, unknown>[];
  dedup_soft?: Record<string, unknown>[];
  [key: string]: unknown;
}

export interface WizardInstallResult {
  approval: WizardApprovalOutcome | null;
  install: WizardInstallOutcome;
}

// ---------------------------------------------------------------------------
// Health (/health, /readiness) — tolerate shape drift.
// ---------------------------------------------------------------------------

export interface HealthResponse {
  status?: string;
  dependencies?: Record<string, unknown> | null;
  [key: string]: unknown;
}

export interface ReadinessResponse {
  status?: string;
  progress?: Record<string, unknown> | null;
  [key: string]: unknown;
}
