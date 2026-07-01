import type { UseQueryResult } from '@tanstack/react-query';
import {
  Bar,
  BarChart,
  CartesianGrid,
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
import { fmt, fmtPct } from '../../lib/format';
import type { PhaseSavings, SavingsResponse } from '../../lib/types';

export function SavingsTab({ query }: { query: UseQueryResult<SavingsResponse, Error> }) {
  const { data, isLoading, error, refetch } = query;

  if (isLoading) return <PageSkeleton />;
  if (error) return <ErrorState message={error.message} onRetry={() => refetch()} />;
  if (!data || data.total_composes === 0) {
    return (
      <EmptyState
        title="No savings data available"
        hint="Savings will appear here once compositions have been recorded."
      />
    );
  }

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 lg:grid-cols-5 gap-4">
        <StatCard label="Total Composes" value={fmt(data.total_composes)} />
        <StatCard label="Tokens Returned" value={fmt(data.tokens_returned)} />
        <StatCard label="Flat Equivalent" value={fmt(data.tokens_flat_equivalent)} />
        <StatCard label="Tokens Saved" value={fmt(data.tokens_saved)} />
        <StatCard label="Savings" value={fmtPct(data.savings_pct)} />
      </div>

      {data.per_phase.length > 0 && (
        <Card>
          <h2 className="text-lg font-semibold mb-4">Tokens Saved by Phase</h2>
          <ResponsiveContainer width="100%" height={300}>
            <BarChart data={data.per_phase}>
              <CartesianGrid strokeDasharray="3 3" vertical={false} />
              <XAxis dataKey="phase" />
              <YAxis />
              <Tooltip />
              <Bar dataKey="tokens_saved" name="Tokens saved" fill="#3B82F6" />
            </BarChart>
          </ResponsiveContainer>
        </Card>
      )}

      <Card>
        <h2 className="text-lg font-semibold mb-4">Per-phase Breakdown</h2>
        <DataTable<PhaseSavings>
          data={data.per_phase}
          rowKey={(row, i) => row.phase ?? i}
          emptyLabel="No per-phase data"
          columns={[
            { key: 'phase', label: 'Phase', render: (r) => fmt(r.phase) },
            { key: 'composes', label: 'Composes', render: (r) => fmt(r.composes) },
            { key: 'tokens_returned', label: 'Tokens Returned', render: (r) => fmt(r.tokens_returned) },
            {
              key: 'tokens_flat_equivalent',
              label: 'Flat Equivalent',
              render: (r) => fmt(r.tokens_flat_equivalent),
            },
            { key: 'tokens_saved', label: 'Tokens Saved', render: (r) => fmt(r.tokens_saved) },
            { key: 'savings_pct', label: 'Savings %', render: (r) => fmtPct(r.savings_pct) },
          ]}
        />
      </Card>
    </div>
  );
}
