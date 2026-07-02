import type {
  ApprovalsResponse,
  ApproveRequest,
  ApproveResult,
  ComposeResponse,
  ConfigData,
  ConfigUpdate,
  ConfigUpdateResult,
  CorpusDiagnostics,
  CoverageResponse,
  DoctorResponse,
  HealthResponse,
  OverrideUpdate,
  OverrideWriteResult,
  PacksResponse,
  ProfileResolveResult,
  ProfilesResponse,
  ReadinessResponse,
  ReembedResult,
  ReembedStatus,
  ReloadResult,
  RepoGates,
  ReposResponse,
  RetrieveRequest,
  RetrieveResponse,
  RuntimeDiagnostics,
  SavingsResponse,
  SignalEvaluateRequest,
  SignalVerdict,
  SkillDetail,
  SkillOverride,
  SkillsListParams,
  SkillsListResponse,
  SkillVersionsResponse,
  TracesParams,
  TracesResponse,
  WizardFileWriteRequest,
  WizardFileWriteResult,
  WizardInstallRequest,
  WizardInstallResult,
  WizardPackContents,
  WizardScaffoldRequest,
  WizardScaffoldResult,
  WizardValidateResult,
} from './types';

// Relative base — same origin in production, Vite proxy in dev.
const BASE = '';

/**
 * HTTP failure carrying the status code and parsed JSON body so callers can
 * branch on status (404 = not overridable) or read structured validation
 * errors ({"detail": {"error": "validation_failed", "errors": [...]}}).
 */
export class ApiError extends Error {
  readonly status: number;
  readonly body: unknown;

  constructor(message: string, status: number, body: unknown) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.body = body;
  }
}

/** Extract the errors[] list from a validation_failed 400 body, if present. */
export function extractValidationErrors(err: unknown): string[] | null {
  if (!(err instanceof ApiError) || err.status !== 400) return null;
  if (!err.body || typeof err.body !== 'object') return null;
  const detail = (err.body as Record<string, unknown>).detail;
  if (!detail || typeof detail !== 'object') return null;
  const errors = (detail as Record<string, unknown>).errors;
  if (!Array.isArray(errors)) return null;
  return errors.map((e) => String(e));
}

/**
 * Extract a human-readable message from an error body. FastAPI nests custom
 * payloads under "detail": {"detail": {"error": "invalid_field", "detail": "<msg>"}}.
 */
function extractErrorMessage(body: unknown): string | undefined {
  if (!body || typeof body !== 'object') return undefined;
  const b = body as Record<string, unknown>;
  const payload = 'detail' in b ? b.detail : b;
  if (typeof payload === 'string') return payload;
  if (payload && typeof payload === 'object') {
    const p = payload as Record<string, unknown>;
    const error = typeof p.error === 'string' ? p.error : undefined;
    const detail = typeof p.detail === 'string' ? p.detail : undefined;
    const message = [error, detail].filter(Boolean).join(': ');
    if (message) return message;
  }
  if (typeof b.error === 'string') return b.error;
  return undefined;
}

async function request<T>(url: string, init: RequestInit = {}): Promise<T> {
  const method = (init.method ?? 'GET').toUpperCase();
  const headers = new Headers(init.headers);
  // Mutating endpoints require the CSRF marker header; the server 403s without it.
  if (method !== 'GET' && method !== 'HEAD') {
    headers.set('X-AgentAlloy-CSRF', '1');
  }
  let res: Response;
  try {
    res = await fetch(`${BASE}${url}`, { ...init, headers });
  } catch {
    throw new Error(`Network error reaching ${url}`);
  }
  if (!res.ok) {
    let body: unknown;
    let bodyMessage: string | undefined;
    try {
      body = await res.json();
      bodyMessage = extractErrorMessage(body);
    } catch {
      // non-JSON error body — fall through to the status message
    }
    if (res.status === 403) {
      throw new ApiError(
        bodyMessage
          ? `Forbidden (403): ${bodyMessage}`
          : 'Forbidden (403): missing or rejected X-AgentAlloy-CSRF header',
        res.status,
        body,
      );
    }
    throw new ApiError(bodyMessage ?? `${method} ${url} failed (${res.status})`, res.status, body);
  }
  return res.json() as Promise<T>;
}

function query(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== '') search.set(key, String(value));
  }
  const qs = search.toString();
  return qs ? `?${qs}` : '';
}

// --- Config ----------------------------------------------------------------

export function getConfig(): Promise<ConfigData> {
  return request<ConfigData>('/api/config');
}

export function updateConfig(partial: ConfigUpdate): Promise<ConfigUpdateResult> {
  return request<ConfigUpdateResult>('/api/config', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(partial),
  });
}

export function reloadConfig(): Promise<ReloadResult> {
  return request<ReloadResult>('/api/config/reload', { method: 'POST' });
}

// --- Telemetry ---------------------------------------------------------------

export function getTraces(params: TracesParams): Promise<TracesResponse> {
  return request<TracesResponse>(`/telemetry/traces${query({ ...params })}`);
}

export function getSavings(repo?: string): Promise<SavingsResponse> {
  return request<SavingsResponse>(`/telemetry/savings${query({ repo })}`);
}

export function getCoverage(repo?: string): Promise<CoverageResponse> {
  return request<CoverageResponse>(`/telemetry/coverage${query({ repo })}`);
}

// --- Diagnostics -------------------------------------------------------------

export function getRuntimeDiagnostics(): Promise<RuntimeDiagnostics> {
  return request<RuntimeDiagnostics>('/diagnostics/runtime');
}

export function getCorpusDiagnostics(): Promise<CorpusDiagnostics> {
  return request<CorpusDiagnostics>('/diagnostics/corpus');
}

// --- Health ------------------------------------------------------------------

export function getHealth(): Promise<HealthResponse> {
  return request<HealthResponse>('/health');
}

export function getReadiness(): Promise<ReadinessResponse> {
  return request<ReadinessResponse>('/readiness');
}

// --- Skills --------------------------------------------------------------------

export function getSkills(params: SkillsListParams): Promise<SkillsListResponse> {
  return request<SkillsListResponse>(`/api/skills${query({ ...params })}`);
}

export function getSkillDetail(skillId: string): Promise<SkillDetail> {
  return request<SkillDetail>(`/skills/${encodeURIComponent(skillId)}`);
}

export function getSkillVersions(skillId: string): Promise<SkillVersionsResponse> {
  return request<SkillVersionsResponse>(`/api/skills/${encodeURIComponent(skillId)}/versions`);
}

/** Throws ApiError with status 404 when the skill is not overridable. */
export function getSkillOverride(skillId: string): Promise<SkillOverride> {
  return request<SkillOverride>(`/api/skills/${encodeURIComponent(skillId)}/override`);
}

export function putSkillOverride(
  skillId: string,
  body: OverrideUpdate,
): Promise<OverrideWriteResult> {
  return request<OverrideWriteResult>(`/api/skills/${encodeURIComponent(skillId)}/override`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

export function deleteSkillOverride(
  skillId: string,
  layer = 'profile',
): Promise<OverrideWriteResult> {
  return request<OverrideWriteResult>(
    `/api/skills/${encodeURIComponent(skillId)}/override${query({ layer })}`,
    { method: 'DELETE' },
  );
}

// --- Repos -----------------------------------------------------------------------

export function getRepos(): Promise<ReposResponse> {
  return request<ReposResponse>('/api/repos');
}

export function getRepoGates(repo: string): Promise<RepoGates> {
  return request<RepoGates>(`/api/repos/gates${query({ repo })}`);
}

// --- Approvals ---------------------------------------------------------------------

export function getApprovals(): Promise<ApprovalsResponse> {
  return request<ApprovalsResponse>('/api/approvals');
}

/**
 * Sign off a pending gate. IMPORTANT: approval auto-advances the repo phase.
 * 409 = {"detail": {"error": "approve_refused", "detail": "<why>"}}.
 */
export function postApprove(body: ApproveRequest): Promise<ApproveResult> {
  return request<ApproveResult>('/api/repos/approve', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

// --- Ops (doctor / packs / reembed / profiles) --------------------------------------

export function getDoctor(): Promise<DoctorResponse> {
  return request<DoctorResponse>('/api/doctor');
}

export function getPacks(): Promise<PacksResponse> {
  return request<PacksResponse>('/api/packs');
}

export function getReembedStatus(): Promise<ReembedStatus> {
  return request<ReembedStatus>('/api/reembed/status');
}

export function postReembed(dryRun: boolean): Promise<ReembedResult> {
  return request<ReembedResult>('/api/reembed', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ dry_run: dryRun }),
  });
}

export function getProfiles(): Promise<ProfilesResponse> {
  return request<ProfilesResponse>('/api/profiles');
}

export function resolveProfile(repo: string): Promise<ProfileResolveResult> {
  return request<ProfileResolveResult>('/api/profiles/resolve', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ repo }),
  });
}

// --- Wizard (custom-skill creation) --------------------------------------------------

/** 400 = {"detail": {"error": "<action>", "detail": "<why>"}} (invalid names, already-exists). */
export function postWizardScaffold(body: WizardScaffoldRequest): Promise<WizardScaffoldResult> {
  return request<WizardScaffoldResult>('/api/wizard/scaffold', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

export function getWizardPack(repo: string, pack: string): Promise<WizardPackContents> {
  return request<WizardPackContents>(`/api/wizard/pack${query({ repo, pack })}`);
}

/** 400 = {"detail": {"error": "invalid_yaml", "detail": "<parser message>"}}. */
export function putWizardFile(body: WizardFileWriteRequest): Promise<WizardFileWriteResult> {
  return request<WizardFileWriteResult>('/api/wizard/file', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

export function postWizardValidate(repo: string, pack: string): Promise<WizardValidateResult> {
  return request<WizardValidateResult>('/api/wizard/validate', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ repo, pack }),
  });
}

/** 409 = {"detail": {"error": "approve_refused", "detail": "<why>"}}. */
export function postWizardInstall(body: WizardInstallRequest): Promise<WizardInstallResult> {
  return request<WizardInstallResult>('/api/wizard/install', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

// --- Playground ------------------------------------------------------------------

export function postRetrieve(body: RetrieveRequest): Promise<RetrieveResponse> {
  return request<RetrieveResponse>('/retrieve', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

export function postCompose(body: RetrieveRequest): Promise<ComposeResponse> {
  return request<ComposeResponse>('/compose', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

export function evaluateSignal(body: SignalEvaluateRequest): Promise<SignalVerdict> {
  return request<SignalVerdict>('/api/signal/evaluate', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}
