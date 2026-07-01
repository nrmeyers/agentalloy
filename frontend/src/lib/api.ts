import type {
  ConfigData,
  ConfigUpdate,
  ConfigUpdateResult,
  CorpusDiagnostics,
  CoverageResponse,
  HealthResponse,
  ReadinessResponse,
  ReloadResult,
  RuntimeDiagnostics,
  SavingsResponse,
  TracesParams,
  TracesResponse,
} from './types';

// Relative base — same origin in production, Vite proxy in dev.
const BASE = '';

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
    let bodyMessage: string | undefined;
    try {
      bodyMessage = extractErrorMessage(await res.json());
    } catch {
      // non-JSON error body — fall through to the status message
    }
    if (res.status === 403) {
      throw new Error(
        bodyMessage
          ? `Forbidden (403): ${bodyMessage}`
          : 'Forbidden (403): missing or rejected X-AgentAlloy-CSRF header',
      );
    }
    throw new Error(bodyMessage ?? `${method} ${url} failed (${res.status})`);
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
