const BASE = '';

async function request<T>(url: string, init: RequestInit = {}): Promise<T> {
  const method = (init.method ?? 'GET').toUpperCase();
  const headers = new Headers(init.headers);
  if (method !== 'GET' && method !== 'HEAD') {
    headers.set('X-AgentAlloy-CSRF', '1');
  }
  if (init.body && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json');
  }
  const res = await fetch(`${BASE}${url}`, { ...init, headers });
  if (!res.ok) {
    let body: unknown;
    try {
      body = await res.json();
    } catch { /* ignore */ }
    const detail =
      body && typeof body === 'object' && 'detail' in body
        ? String((body as Record<string, unknown>).detail)
        : undefined;
    throw new Error(detail ?? `${method} ${url} failed (${res.status})`);
  }
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

function query(params: Record<string, string | number | boolean | undefined>): string {
  const search = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== '') search.set(k, String(v));
  }
  const qs = search.toString();
  return qs ? `?${qs}` : '';
}

// --- Phase -------------------------------------------------------------------

export interface PhaseReadResponse {
  kind: string;
  value: string | null;
  mode: string | null;
  paused_since: string | null;
  transitioned_by: string | null;
  started_at: string | null;
  last_updated: string | null;
  workflow: string | null;
  phase_start_ref: string | null;
}

export interface PhaseAdvanceRequest {
  value: string;
  owner?: string;
  actor?: string;
  override?: boolean;
}

export interface StateWriteResponse {
  success: boolean;
  kind: string;
  value: string;
  owner: string | null;
}

export function getPhase(repo: string): Promise<PhaseReadResponse> {
  return request<PhaseReadResponse>(`/state/phase${query({ repo })}`);
}

export function postPhaseAdvance(
  repo: string,
  data: PhaseAdvanceRequest,
): Promise<StateWriteResponse> {
  return request<StateWriteResponse>(`/state/phase${query({ repo })}`, {
    method: 'POST',
    body: JSON.stringify(data),
  });
}

export function deletePhase(repo: string): Promise<void> {
  return request(`/state/phase${query({ repo })}`, { method: 'DELETE' });
}

// --- Cursor ------------------------------------------------------------------

export interface CursorReadResponse {
  kind: string;
  value: string | null;
}

export function getCursor(repo: string, sessionKey?: string): Promise<CursorReadResponse> {
  const path = sessionKey
    ? `/state/cursors/${encodeURIComponent(sessionKey)}`
    : '/state/cursor';
  return request<CursorReadResponse>(`${path}${query({ repo })}`);
}

export function postCursor(
  repo: string,
  value: string,
  sessionKey?: string,
): Promise<StateWriteResponse> {
  const path = sessionKey
    ? `/state/cursors/${encodeURIComponent(sessionKey)}`
    : '/state/cursor';
  return request<StateWriteResponse>(`${path}${query({ repo })}`, {
    method: 'POST',
    body: JSON.stringify({ value }),
  });
}

// --- Artifacts ---------------------------------------------------------------

export interface Artifact {
  repo: string;
  phase: string;
  slug: string;
  name: string;
  body: string;
  updated_at: string;
}

export function getArtifacts(repo: string): Promise<Artifact[]> {
  return request<Artifact[]>(`/state/artifact${query({ repo })}`);
}

export function putArtifact(
  repo: string,
  phase: string,
  slug: string,
  name: string,
  body: string,
): Promise<void> {
  return request(`/state/artifact${query({ repo })}`, {
    method: 'PUT',
    body: JSON.stringify({ phase, slug, name, body }),
  });
}

// --- Resume ------------------------------------------------------------------

export interface ResumeData {
  phase: string | null;
  cursor: string | null;
  owed_artifacts: { phase: string; slug: string; name: string }[];
  governing_decisions: string[];
  [key: string]: unknown;
}

export function getResume(repo: string): Promise<ResumeData> {
  return request<ResumeData>(`/state/resume${query({ repo })}`);
}

// --- Repo state reset --------------------------------------------------------

export function deleteRepoState(repo: string): Promise<void> {
  return request(`/state/repo${query({ repo })}`, { method: 'DELETE' });
}
