type BadgeStatus =
  | 'ok'
  | 'healthy'
  | 'active'
  | 'success'
  | 'done'
  | 'merged'
  | 'running'
  | 'queued'
  | 'pending'
  | 'warning'
  | 'degraded'
  | 'stale'
  | 'error'
  | 'failed'
  | 'archived'
  | 'superseded'
  | 'cancelled'
  | 'interrupted'
  | string;

const statusColors: Record<string, string> = {
  ok: 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-400',
  healthy: 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-400',
  active: 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-400',
  success: 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-400',
  done: 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-400',
  merged: 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-400',
  running: 'bg-blue-500/10 text-blue-600 dark:text-blue-400',
  queued: 'bg-blue-500/10 text-blue-600 dark:text-blue-400',
  pending: 'bg-amber-500/10 text-amber-600 dark:text-amber-400',
  warning: 'bg-amber-500/10 text-amber-600 dark:text-amber-400',
  degraded: 'bg-amber-500/10 text-amber-600 dark:text-amber-400',
  stale: 'bg-amber-500/10 text-amber-600 dark:text-amber-400',
  error: 'bg-red-500/10 text-red-600 dark:text-red-400',
  failed: 'bg-red-500/10 text-red-600 dark:text-red-400',
  archived: 'bg-zinc-500/10 text-zinc-500 dark:text-zinc-400',
  superseded: 'bg-zinc-500/10 text-zinc-500 dark:text-zinc-400',
  cancelled: 'bg-zinc-500/10 text-zinc-500 dark:text-zinc-400',
  interrupted: 'bg-zinc-500/10 text-zinc-500 dark:text-zinc-400',
};

const defaultColor = 'bg-zinc-500/10 text-zinc-500 dark:text-zinc-400';

interface StatusBadgeProps {
  status: BadgeStatus | unknown;
  label?: string;
  pulse?: boolean;
}

export function StatusBadge({ status, label, pulse }: StatusBadgeProps) {
  const value = typeof status === 'string' && status.trim() !== '' ? status : 'unknown';
  const colors = statusColors[value.toLowerCase()] ?? defaultColor;
  const display = label ?? value;
  const isLive = value.toLowerCase() === 'running' || value.toLowerCase() === 'queued';

  return (
    <span
      className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-xs font-medium ${colors}`}
    >
      {pulse && isLive && (
        <span className="relative flex h-1.5 w-1.5">
          <span
            className={`animate-ping absolute inline-flex h-full w-full rounded-full opacity-75 ${
              value.toLowerCase() === 'running' ? 'bg-blue-400' : 'bg-amber-400'
            }`}
          />
          <span
            className={`relative inline-flex rounded-full h-1.5 w-1.5 ${
              value.toLowerCase() === 'running' ? 'bg-blue-500' : 'bg-amber-500'
            }`}
          />
        </span>
      )}
      {display}
    </span>
  );
}
