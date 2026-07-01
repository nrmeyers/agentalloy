import {
  Card,
  DataTable,
  ErrorState,
  PageSkeleton,
  StatCard,
  StatusBadge,
} from '../components';
import { useCorpus, useRuntimeDiagnostics } from '../hooks/useDiagnostics';
import { fmt, fmtUnknown } from '../lib/format';
import type { SkillVersionEntry, VersionMismatch } from '../lib/types';

const DEPENDENCY_LABELS: Record<string, string> = {
  runtime_store: 'Runtime Store',
  telemetry_store: 'Telemetry Store',
  embedding_runtime: 'Embedding Runtime',
  runtime_cache: 'Runtime Cache',
};

function SkillStateTable({ title, entries }: { title: string; entries: SkillVersionEntry[] }) {
  return (
    <details className="mt-2">
      <summary className="cursor-pointer text-sm text-gray-600 hover:text-gray-900">
        {title} ({entries.length} skills)
      </summary>
      <div className="mt-2">
        <DataTable<SkillVersionEntry>
          data={entries}
          rowKey={(row, i) => row.skill_id ?? i}
          emptyLabel="No entries"
          columns={[
            { key: 'skill_id', label: 'Skill', render: (r) => fmtUnknown(r.skill_id) },
            { key: 'version_id', label: 'Version ID', render: (r) => fmtUnknown(r.version_id) },
            { key: 'version_number', label: 'Version #', render: (r) => fmtUnknown(r.version_number) },
          ]}
        />
      </div>
    </details>
  );
}

export function DiagnosticsPage() {
  const runtime = useRuntimeDiagnostics();
  const corpus = useCorpus();

  if (runtime.isLoading) return <PageSkeleton />;
  if (runtime.error) {
    return <ErrorState message={runtime.error.message} onRetry={() => runtime.refetch()} />;
  }

  const diag = runtime.data ?? {};
  const readiness = diag.dependency_readiness ?? {};
  const perPath = readiness.per_path ?? {};
  const consistency = diag.consistency ?? {};
  const missingInCache = consistency.missing_in_cache ?? [];
  const missingInStore = consistency.missing_in_store ?? [];
  const mismatches = consistency.version_mismatches ?? [];
  const consistent = missingInCache.length === 0 && missingInStore.length === 0 && mismatches.length === 0;

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold">Diagnostics</h1>

      <Card>
        <h2 className="text-lg font-semibold mb-4">Dependency Readiness</h2>
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
          {Object.entries(DEPENDENCY_LABELS).map(([key, label]) => (
            <div key={key} className="flex flex-col gap-1">
              <span className="text-sm text-gray-500">{label}</span>
              <StatusBadge status={(readiness as Record<string, unknown>)[key]} />
            </div>
          ))}
        </div>
        <h3 className="text-sm font-semibold text-gray-700 mt-6 mb-2">Per-path Readiness</h3>
        {Object.keys(perPath).length === 0 ? (
          <p className="text-sm text-gray-500">unknown</p>
        ) : (
          <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
            {Object.entries(perPath).map(([path, status]) => (
              <div key={path} className="flex flex-col gap-1">
                <span className="text-sm text-gray-500">{path}</span>
                <StatusBadge status={status} />
              </div>
            ))}
          </div>
        )}
      </Card>

      <Card>
        <h2 className="text-lg font-semibold mb-4">Store / Cache Consistency</h2>
        <div
          className={`px-4 py-3 rounded-md text-sm mb-4 ${
            consistent
              ? 'bg-green-50 border border-green-200 text-green-800'
              : 'bg-amber-50 border border-amber-200 text-amber-800'
          }`}
        >
          {consistent
            ? `Store and cache are consistent — ${fmtUnknown(consistency.matched)} skills matched.`
            : 'Store and cache are inconsistent — see details below.'}
        </div>
        <dl className="grid grid-cols-2 lg:grid-cols-4 gap-4 text-sm">
          <div>
            <dt className="text-gray-500">Cache Loaded</dt>
            <dd className="font-medium">{fmtUnknown(diag.cache_loaded)}</dd>
          </div>
          <div>
            <dt className="text-gray-500">Matched</dt>
            <dd className="font-medium">{fmtUnknown(consistency.matched)}</dd>
          </div>
          <div>
            <dt className="text-gray-500">Missing in Cache</dt>
            <dd className="font-medium">{missingInCache.length}</dd>
          </div>
          <div>
            <dt className="text-gray-500">Missing in Store</dt>
            <dd className="font-medium">{missingInStore.length}</dd>
          </div>
        </dl>

        {missingInCache.length > 0 && (
          <div className="mt-4">
            <h3 className="text-sm font-semibold text-gray-700 mb-1">Missing in Cache</h3>
            <p className="text-sm text-gray-700 break-all">{missingInCache.join(', ')}</p>
          </div>
        )}
        {missingInStore.length > 0 && (
          <div className="mt-4">
            <h3 className="text-sm font-semibold text-gray-700 mb-1">Missing in Store</h3>
            <p className="text-sm text-gray-700 break-all">{missingInStore.join(', ')}</p>
          </div>
        )}
        {mismatches.length > 0 && (
          <div className="mt-4">
            <h3 className="text-sm font-semibold text-gray-700 mb-2">Version Mismatches</h3>
            <DataTable<VersionMismatch>
              data={mismatches}
              rowKey={(row, i) => row.skill_id ?? i}
              emptyLabel="No mismatches"
              columns={[
                { key: 'skill_id', label: 'Skill', render: (r) => fmtUnknown(r.skill_id) },
                { key: 'store_version', label: 'Store Version', render: (r) => fmtUnknown(r.store_version) },
                { key: 'cache_version', label: 'Cache Version', render: (r) => fmtUnknown(r.cache_version) },
              ]}
            />
          </div>
        )}

        <SkillStateTable title="Store state" entries={diag.store_state ?? []} />
        <SkillStateTable title="Runtime state" entries={diag.runtime_state ?? []} />
      </Card>

      <div>
        <h2 className="text-lg font-semibold mb-4">Corpus</h2>
        {corpus.error ? (
          <ErrorState message={corpus.error.message} onRetry={() => corpus.refetch()} />
        ) : (
          <div className="grid grid-cols-2 lg:grid-cols-3 gap-4">
            <StatCard label="Skills" value={fmt(corpus.data?.skill_count, 'unknown')} />
            <StatCard
              label="Embedded Vectors"
              value={fmt(corpus.data?.embedded_vector_count, 'unknown')}
            />
            <StatCard label="Embedding Dim" value={fmt(corpus.data?.embedding_dim, 'unknown')} />
          </div>
        )}
      </div>
    </div>
  );
}
