import { Card, ErrorState, PageSkeleton, StatusBadge } from '../components';
import { useHealth, useReadiness } from '../hooks/useDiagnostics';
import { fmtUnknown } from '../lib/format';

function DependencyRow({ name, value }: { name: string; value: unknown }) {
  if (value && typeof value === 'object' && !Array.isArray(value)) {
    const v = value as Record<string, unknown>;
    return (
      <div className="flex items-start justify-between gap-4 py-2">
        <div>
          <div className="text-sm font-medium text-gray-900">{name}</div>
          {(v.impact !== undefined || v.detail !== undefined) && (
            <div className="text-xs text-gray-500">
              {[v.impact, v.detail]
                .filter((x) => x !== undefined && x !== null && x !== '')
                .map((x) => String(x))
                .join(' — ') || null}
            </div>
          )}
        </div>
        <StatusBadge status={v.status} />
      </div>
    );
  }
  return (
    <div className="flex items-center justify-between gap-4 py-2">
      <div className="text-sm font-medium text-gray-900">{name}</div>
      <span className="text-sm text-gray-700">{fmtUnknown(value)}</span>
    </div>
  );
}

function KeyValueList({ record }: { record: Record<string, unknown> }) {
  const entries = Object.entries(record);
  if (entries.length === 0) return <p className="text-sm text-gray-500">unknown</p>;
  return (
    <dl className="grid grid-cols-2 gap-x-4 gap-y-2">
      {entries.map(([key, value]) => (
        <div key={key}>
          <dt className="text-xs font-medium text-gray-500 uppercase">{key}</dt>
          <dd className="text-sm text-gray-900 break-all">{fmtUnknown(value)}</dd>
        </div>
      ))}
    </dl>
  );
}

export function HealthPage() {
  const health = useHealth();
  const readiness = useReadiness();

  if (health.isLoading && readiness.isLoading) return <PageSkeleton />;

  const dependencies =
    health.data?.dependencies && typeof health.data.dependencies === 'object'
      ? health.data.dependencies
      : null;

  // Render any extra top-level health keys defensively (shape drift tolerated).
  const extraHealth = Object.fromEntries(
    Object.entries(health.data ?? {}).filter(
      ([key]) => key !== 'status' && key !== 'dependencies',
    ),
  );

  return (
    <div className="space-y-6 max-w-3xl">
      <h1 className="text-2xl font-bold">Health</h1>

      {health.error ? (
        <ErrorState message={health.error.message} onRetry={() => health.refetch()} />
      ) : (
        <Card>
          <div className="flex items-center justify-between">
            <h2 className="text-lg font-semibold">Service Status</h2>
            <StatusBadge status={health.data?.status} />
          </div>
          {dependencies && (
            <div className="mt-4 divide-y divide-gray-100">
              <h3 className="text-sm font-semibold text-gray-700 pb-2">Dependencies</h3>
              {Object.entries(dependencies).map(([name, value]) => (
                <DependencyRow key={name} name={name} value={value} />
              ))}
            </div>
          )}
          {Object.keys(extraHealth).length > 0 && (
            <div className="mt-4">
              <h3 className="text-sm font-semibold text-gray-700 mb-2">Details</h3>
              <KeyValueList record={extraHealth} />
            </div>
          )}
        </Card>
      )}

      {readiness.error ? (
        <ErrorState message={readiness.error.message} onRetry={() => readiness.refetch()} />
      ) : (
        <Card>
          <div className="flex items-center justify-between">
            <h2 className="text-lg font-semibold">Readiness</h2>
            <StatusBadge status={readiness.data?.status} />
          </div>
          <div className="mt-4">
            <h3 className="text-sm font-semibold text-gray-700 mb-2">Warm-up Progress</h3>
            <KeyValueList
              record={
                readiness.data?.progress && typeof readiness.data.progress === 'object'
                  ? readiness.data.progress
                  : {}
              }
            />
          </div>
        </Card>
      )}
    </div>
  );
}
