import { useMemo, useState } from 'react';
import { FilterBar, RANGE_MS, StatCard } from '../components';
import type { FilterValues } from '../components';
import { useCoverage, useSavings, useTraces } from '../hooks/useTelemetry';
import { fmt, fmtPct, fmtRate } from '../lib/format';
import { CoverageTab } from './telemetry/CoverageTab';
import { SavingsTab } from './telemetry/SavingsTab';
import { TracesTab } from './telemetry/TracesTab';

const PAGE_SIZE = 50;

type Tab = 'traces' | 'savings' | 'coverage';

const TABS: { id: Tab; label: string }[] = [
  { id: 'traces', label: 'Traces' },
  { id: 'savings', label: 'Savings' },
  { id: 'coverage', label: 'Coverage' },
];

export function TelemetryPage() {
  const [tab, setTab] = useState<Tab>('traces');
  const [filters, setFilters] = useState<FilterValues>({
    phase: '',
    status: '',
    repo: '',
    range: '',
  });
  const [offset, setOffset] = useState(0);

  // Anchored when the preset changes; polling reuses the same window start.
  const since = useMemo(
    () => (filters.range && RANGE_MS[filters.range] ? Date.now() - RANGE_MS[filters.range] : undefined),
    [filters.range],
  );

  const tracesQuery = useTraces({
    limit: PAGE_SIZE,
    offset,
    phase: filters.phase || undefined,
    status: filters.status || undefined,
    repo: filters.repo || undefined,
    since,
  });
  const savingsQuery = useSavings(filters.repo || undefined);
  const coverageQuery = useCoverage(filters.repo || undefined);

  const savings = savingsQuery.data;
  const coverage = coverageQuery.data;
  const traces = tracesQuery.data;

  // Phase options are derived from returned data — never hardcoded.
  const phases = useMemo(() => {
    const set = new Set<string>();
    savings?.per_phase?.forEach((p) => p.phase && set.add(p.phase));
    coverage?.per_phase?.forEach((p) => p.phase && set.add(p.phase));
    traces?.traces?.forEach((t) => {
      if (t.phase) set.add(t.phase);
    });
    if (filters.phase) set.add(filters.phase);
    return [...set].sort();
  }, [savings, coverage, traces, filters.phase]);

  const statuses = useMemo(() => {
    const set = new Set<string>();
    traces?.traces?.forEach((t) => {
      if (t.status) set.add(t.status);
    });
    if (filters.status) set.add(filters.status);
    return [...set].sort();
  }, [traces, filters.status]);

  const repos = useMemo(() => {
    const set = new Set<string>();
    coverage?.per_repo?.forEach((r) => {
      if (r.repo) set.add(r.repo);
    });
    traces?.traces?.forEach((t) => {
      if (t.repo) set.add(t.repo);
    });
    if (filters.repo) set.add(filters.repo);
    return [...set].sort();
  }, [coverage, traces, filters.repo]);

  const handleFilters = (patch: Partial<FilterValues>) => {
    setFilters((prev) => ({ ...prev, ...patch }));
    setOffset(0);
  };

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold">Telemetry</h1>

      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <StatCard label="Total Composes" value={savings ? fmt(savings.total_composes) : '—'} />
        <StatCard label="Tokens Saved" value={savings ? fmt(savings.tokens_saved) : '—'} />
        <StatCard label="Savings %" value={savings ? fmtPct(savings.savings_pct) : '—'} />
        <StatCard label="Compose Rate" value={coverage ? fmtRate(coverage.compose_rate) : '—'} />
      </div>

      <FilterBar
        phases={phases}
        statuses={statuses}
        repos={repos}
        values={filters}
        onChange={handleFilters}
      />

      <div className="border-b border-gray-200">
        <nav className="flex gap-4">
          {TABS.map((t) => (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              className={`px-1 py-2 text-sm font-medium border-b-2 -mb-px ${
                tab === t.id
                  ? 'border-brand text-brand'
                  : 'border-transparent text-gray-500 hover:text-gray-700'
              }`}
            >
              {t.label}
            </button>
          ))}
        </nav>
      </div>

      {tab === 'traces' && (
        <TracesTab query={tracesQuery} offset={offset} limit={PAGE_SIZE} onPage={setOffset} />
      )}
      {tab === 'savings' && <SavingsTab query={savingsQuery} />}
      {tab === 'coverage' && <CoverageTab query={coverageQuery} />}
    </div>
  );
}
