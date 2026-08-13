import type {
  CodeIndexJob,
  CodeIndexRepo,
  CodeIndexRepoStats,
} from './types';

const BASE = '';

async function request<T>(url: string, init: RequestInit = {}): Promise<T> {
  const method = (init.method ?? 'GET').toUpperCase();
  const headers = new Headers(init.headers);
  if (method !== 'GET' && method !== 'HEAD') {
    headers.set('X-AgentAlloy-CSRF', '1');
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

// --- Repos -------------------------------------------------------------------

export function getCodeRepos(): Promise<CodeIndexRepo[]> {
  return request<CodeIndexRepo[]>('/code/repos');
}

export function getCodeRepoStats(slug: string): Promise<CodeIndexRepoStats> {
  return request<CodeIndexRepoStats>(
    `/code/repos/${encodeURIComponent(slug)}/stats`,
  );
}

export function postCodeReindex(slug: string): Promise<CodeIndexJob> {
  return request<CodeIndexJob>(
    `/code/repos/${encodeURIComponent(slug)}/reindex`,
    { method: 'POST' },
  );
}

export function postCodeWatch(
  slug: string,
  enabled: boolean,
): Promise<{ slug: string; watch_enabled: boolean; watching: boolean; master_switch: boolean }> {
  return request(`/code/repos/${encodeURIComponent(slug)}/watch`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ enabled }),
  });
}

export function deleteCodeRepo(slug: string): Promise<void> {
  return request(`/code/repos/${encodeURIComponent(slug)}`, { method: 'DELETE' });
}

// --- Jobs --------------------------------------------------------------------

export function getCodeJobs(
  slug?: string,
  limit = 50,
): Promise<CodeIndexJob[]> {
  return request<CodeIndexJob[]>(
    `/code/index/jobs${query({ slug, limit })}`,
  );
}

export function postCodeIndex(
  repoPath: string,
  opts?: { force?: boolean; index_markdown?: boolean; prune_decisions?: boolean },
): Promise<CodeIndexJob> {
  return request<CodeIndexJob>('/code/index', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ repo_path: repoPath, ...opts }),
  });
}

export function postCodeJobCancel(jobId: string): Promise<CodeIndexJob> {
  return request<CodeIndexJob>(
    `/code/index/${encodeURIComponent(jobId)}/cancel`,
    { method: 'POST' },
  );
}
