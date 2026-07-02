/** Render any value defensively; missing/empty values fall back. */
export function fmt(value: unknown, fallback = '—'): string {
  if (value === null || value === undefined || value === '') return fallback;
  if (typeof value === 'boolean') return value ? 'yes' : 'no';
  if (typeof value === 'number') return Number.isFinite(value) ? value.toLocaleString() : fallback;
  if (Array.isArray(value)) return value.length ? value.map((v) => String(v)).join(', ') : fallback;
  if (typeof value === 'object') {
    try {
      return JSON.stringify(value);
    } catch {
      return fallback;
    }
  }
  return String(value);
}

/** Diagnostics/health convention: missing keys render as "unknown". */
export function fmtUnknown(value: unknown): string {
  return fmt(value, 'unknown');
}

/** Epoch-ms timestamp to a local datetime string. */
export function fmtTs(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return '—';
  const d = new Date(ms);
  return Number.isNaN(d.getTime()) ? '—' : d.toLocaleString();
}

export function truncate(s: string | null | undefined, max = 60): string {
  if (!s) return '—';
  return s.length > max ? `${s.slice(0, max)}…` : s;
}

/** ISO datetime string to a local datetime; falls back to the raw string. */
export function fmtIsoTs(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

/** Last path segment (repo basename); tolerates trailing slashes. */
export function basename(path: string): string {
  const parts = path.replace(/\/+$/, '').split('/');
  return parts[parts.length - 1] || path;
}

/**
 * Render a rate as a percentage. Accepts either a 0..1 fraction or an
 * already-scaled 0..100 percentage (backend contract leaves this open).
 */
export function fmtRate(rate: number | null | undefined): string {
  if (rate === null || rate === undefined || !Number.isFinite(rate)) return 'unknown';
  const pct = rate <= 1 ? rate * 100 : rate;
  return `${pct.toFixed(1)}%`;
}

export function fmtPct(pct: number | null | undefined): string {
  if (pct === null || pct === undefined || !Number.isFinite(pct)) return 'unknown';
  return `${pct.toFixed(1)}%`;
}
