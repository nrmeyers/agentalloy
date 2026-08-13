import { PageHeader, Card, StatusBadge, PageSkeleton } from '../components';
import { useDoctor } from '../hooks/useOps';
import { useHealth, useReadiness, useRuntimeDiagnostics, useCorpus } from '../hooks/useDiagnostics';
import { Server, Activity, Stethoscope, Database } from 'lucide-react';

export function SystemPage() {
  const { data: health, isLoading: healthLoading } = useHealth();
  const { data: readiness } = useReadiness();
  const { data: doctor, isLoading: doctorLoading } = useDoctor();
  const { data: runtime } = useRuntimeDiagnostics();
  const { data: corpus } = useCorpus();

  if (healthLoading || doctorLoading) return <PageSkeleton />;

  return (
    <div>
      <PageHeader
        title="System"
        description="Health, diagnostics, and runtime status"
      />

      {/* Health */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 mb-6">
        <Card>
          <div className="flex items-center gap-2 mb-3">
            <Server className="h-4 w-4 text-[var(--text-tertiary)]" />
            <h3 className="text-sm font-medium text-[var(--text-primary)]">Service Health</h3>
          </div>
          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <span className="text-sm text-[var(--text-secondary)]">Status</span>
              <StatusBadge status={health?.status ?? 'unknown'} pulse />
            </div>
            <div className="flex items-center justify-between">
              <span className="text-sm text-[var(--text-secondary)]">Readiness</span>
              <StatusBadge status={readiness?.status ?? 'unknown'} />
            </div>
          </div>
        </Card>

        <Card>
          <div className="flex items-center gap-2 mb-3">
            <Database className="h-4 w-4 text-[var(--text-tertiary)]" />
            <h3 className="text-sm font-medium text-[var(--text-primary)]">Corpus</h3>
          </div>
          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <span className="text-sm text-[var(--text-secondary)]">Skills</span>
              <span className="text-sm font-medium text-[var(--text-primary)] tabular-nums">
                {corpus?.skill_count ?? '—'}
              </span>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-sm text-[var(--text-secondary)]">Embedded vectors</span>
              <span className="text-sm font-medium text-[var(--text-primary)] tabular-nums">
                {corpus?.embedded_vector_count ?? '—'}
              </span>
            </div>
            <div className="flex items-center justify-between">
              <span className="text-sm text-[var(--text-secondary)]">Embedding dim</span>
              <span className="text-sm font-medium text-[var(--text-primary)] tabular-nums">
                {corpus?.embedding_dim ?? '—'}
              </span>
            </div>
          </div>
        </Card>
      </div>

      {/* Doctor checks */}
      <Card className="mb-6">
        <div className="flex items-center gap-2 mb-3">
          <Stethoscope className="h-4 w-4 text-[var(--text-tertiary)]" />
          <h3 className="text-sm font-medium text-[var(--text-primary)]">Doctor</h3>
        </div>
        {doctor?.checks ? (
          <div className="space-y-1.5">
            {doctor.checks.map((check, i) => (
              <div
                key={i}
                className="flex items-center justify-between py-1.5 border-b border-[var(--border-subtle)] last:border-0"
              >
                <span className="text-sm text-[var(--text-primary)]">{check.name}</span>
                <StatusBadge
                  status={check.passed ? 'ok' : 'error'}
                  label={check.passed ? 'Pass' : 'Fail'}
                />
              </div>
            ))}
          </div>
        ) : (
          <p className="text-sm text-[var(--text-tertiary)]">No checks available</p>
        )}
      </Card>

      {/* Runtime diagnostics */}
      <Card>
        <div className="flex items-center gap-2 mb-3">
          <Activity className="h-4 w-4 text-[var(--text-tertiary)]" />
          <h3 className="text-sm font-medium text-[var(--text-primary)]">Runtime</h3>
        </div>
        <div className="space-y-2">
          <div className="flex items-center justify-between">
            <span className="text-sm text-[var(--text-secondary)]">Cache loaded</span>
            <StatusBadge status={runtime?.cache_loaded ? 'ok' : 'warning'} />
          </div>
          {runtime?.consistency && (
            <>
              <div className="flex items-center justify-between">
                <span className="text-sm text-[var(--text-secondary)]">Matched</span>
                <span className="text-sm font-medium text-[var(--text-primary)] tabular-nums">
                  {runtime.consistency.matched ?? '—'}
                </span>
              </div>
              {runtime.consistency.missing_in_cache && runtime.consistency.missing_in_cache.length > 0 && (
                <div className="flex items-center justify-between">
                  <span className="text-sm text-[var(--text-secondary)]">Missing in cache</span>
                  <span className="text-sm font-medium text-warning tabular-nums">
                    {runtime.consistency.missing_in_cache.length}
                  </span>
                </div>
              )}
            </>
          )}
        </div>
      </Card>
    </div>
  );
}
