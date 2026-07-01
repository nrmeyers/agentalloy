const statusStyles: Record<string, string> = {
  ok: 'bg-green-100 text-green-800',
  healthy: 'bg-green-100 text-green-800',
  ready: 'bg-green-100 text-green-800',
  degraded: 'bg-amber-100 text-amber-800',
  warning: 'bg-amber-100 text-amber-800',
  warming_up: 'bg-amber-100 text-amber-800',
  unavailable: 'bg-red-100 text-red-800',
  error: 'bg-red-100 text-red-800',
};

export function StatusBadge({ status, label }: { status: unknown; label?: string }) {
  const value = typeof status === 'string' && status.trim() !== '' ? status : 'unknown';
  const style = statusStyles[value.toLowerCase()] ?? 'bg-gray-100 text-gray-800';
  return (
    <span className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-medium ${style}`}>
      {label ?? value}
    </span>
  );
}
