import type { UseQueryResult } from '@tanstack/react-query';
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import {
  Card,
  DataTable,
  EmptyState,
  ErrorState,
  PageSkeleton,
  StatCard,
} from '../../components';
import { fmt, fmtRate } from '../../lib/format';
import type { CoverageResponse, PhaseCoverage, RepoCoverage } from '../../lib/types';

// Coverage v2: composed vs passthrough rates per phase / repo.
export function CoverageTab({ query }: { query: UseQueryResult<CoverageResponse, Error> }) {
  const { data, isLoading, error, refetch } = query;

  if (isLoading) return <PageSkeleton />;
  if (error) return <ErrorState message={error.message} onRetry={() => refetch()} />;
  if (!data || data.total === 0) {
    return (
      <EmptyState
        title="No coverage data available"
        hint="Coverage will appear here once proxy events have been recorded."
      />
    );
  }

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <StatCard label="Total Events" value={fmt(data.total)} />
        <StatCard label="Composed" value={fmt(data.composed)} />
        <StatCard label="Passthrough" value={fmt(data.passthrough)} />
        <StatCard label="Compose Rate" value={fmtRate(data.compose_rate)} />
      </div>

      {data.per_phase.length > 0 && (
        <Card>
          <h2 className="text-lg font-semibold mb-4">Composed vs Passthrough by Phase</h2>
          <ResponsiveContainer width="100%" height={300}>
            <BarChart data={data.per_phase}>
              <CartesianGrid strokeDasharray="3 3" vertical={false} />
              <XAxis dataKey="phase" />
              <YAxis allowDecimals={false} />
              <Tooltip />
              <Legend />
              <Bar dataKey="composed" name="Composed" stackId="a" fill="#3B82F6" />
              <Bar dataKey="passthrough" name="Passthrough" stackId="a" fill="#D1D5DB" />
            </BarChart>
          </ResponsiveContainer>
        </Card>
      )}

      <Card>
        <h2 className="text-lg font-semibold mb-4">Per-phase</h2>
        <DataTable<PhaseCoverage>
          data={data.per_phase}
          rowKey={(row, i) => row.phase ?? i}
          emptyLabel="No per-phase data"
          columns={[
            { key: 'phase', label: 'Phase', render: (r) => fmt(r.phase) },
            { key: 'composed', label: 'Composed', render: (r) => fmt(r.composed) },
            { key: 'passthrough', label: 'Passthrough', render: (r) => fmt(r.passthrough) },
            {
              key: 'rate',
              label: 'Compose Rate',
              render: (r) => {
                const total = (r.composed ?? 0) + (r.passthrough ?? 0);
                return total > 0 ? fmtRate((r.composed ?? 0) / total) : '—';
              },
            },
          ]}
        />
      </Card>

      <Card>
        <h2 className="text-lg font-semibold mb-4">Per-repo</h2>
        <DataTable<RepoCoverage>
          data={data.per_repo}
          rowKey={(row, i) => row.repo ?? `none-${i}`}
          emptyLabel="No per-repo data"
          columns={[
            { key: 'repo', label: 'Repo', render: (r) => r.repo ?? '(no repo)' },
            { key: 'composed', label: 'Composed', render: (r) => fmt(r.composed) },
            { key: 'passthrough', label: 'Passthrough', render: (r) => fmt(r.passthrough) },
            {
              key: 'rate',
              label: 'Compose Rate',
              render: (r) => {
                const total = (r.composed ?? 0) + (r.passthrough ?? 0);
                return total > 0 ? fmtRate((r.composed ?? 0) / total) : '—';
              },
            },
          ]}
        />
      </Card>
    </div>
  );
}
