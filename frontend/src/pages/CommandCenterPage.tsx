import { useNavigate } from 'react-router-dom';
import {
  PageHeader,
  StatCard,
  Card,
  StatusBadge,
  Button,
  PageSkeleton,
} from '../components';
import { useHealth, useReadiness } from '../hooks/useDiagnostics';
import { useRepos, useApprovals } from '../hooks/useRepos';
import { useCodeRepos, useCodeJobs } from '../hooks/useCodeIndex';
import { useTraces } from '../hooks/useTelemetry';
import {
  Activity,
  Server,
  GitBranch,
  CheckSquare,
  Database,
  ArrowRight,
  FlaskConical,
  RotateCw,
  Puzzle,
} from 'lucide-react';

function fmtTime(epoch: number | null): string {
  if (!epoch) return '—';
  const d = new Date(epoch * 1000);
  const now = Date.now();
  const diffMs = now - d.getTime();
  const diffMin = Math.floor(diffMs / 60_000);
  if (diffMin < 1) return 'just now';
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHr = Math.floor(diffMin / 60);
  if (diffHr < 24) return `${diffHr}h ago`;
  return `${Math.floor(diffHr / 24)}d ago`;
}

function fmtTs(epoch: number | null): string {
  if (!epoch) return '—';
  return new Date(epoch).toLocaleTimeString(undefined, {
    hour: '2-digit',
    minute: '2-digit',
  });
}

export function CommandCenterPage() {
  const navigate = useNavigate();
  const { data: health, isLoading: healthLoading } = useHealth();
  const { data: readiness } = useReadiness();
  const { data: repos, isLoading: reposLoading } = useRepos();
  const { data: approvals } = useApprovals();
  const { data: codeRepos } = useCodeRepos();
  const { data: codeJobs } = useCodeJobs(undefined, 5);
  const { data: traces } = useTraces({ limit: 5 });

  if (healthLoading || reposLoading) return <PageSkeleton />;

  const healthStatus = health?.status ?? 'unknown';
  const repoCount = repos?.total ?? 0;
  const approvalCount = approvals?.total ?? 0;
  const activeRepos = repos?.repos.filter((r) => r.exists && r.phase) ?? [];
  const activeJobs = codeJobs?.filter((j) => j.state === 'running' || j.state === 'queued') ?? [];

  return (
    <div>
      <PageHeader title="Dashboard" description="System overview and active work" />

      {/* Status row */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
        <StatCard
          label="Service"
          value={<StatusBadge status={healthStatus} pulse />}
          icon={<Server className="h-5 w-5" />}
        />
        <StatCard
          label="Repos"
          value={repoCount}
          description={`${activeRepos.length} active`}
          icon={<Database className="h-5 w-5" />}
        />
        <StatCard
          label="Approvals"
          value={approvalCount}
          description={approvalCount > 0 ? 'Pending review' : 'All clear'}
          icon={<CheckSquare className="h-5 w-5" />}
        />
        <StatCard
          label="Readiness"
          value={<StatusBadge status={readiness?.status ?? 'unknown'} />}
          icon={<Activity className="h-5 w-5" />}
        />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Active work */}
        <div className="lg:col-span-2 space-y-6">
          {/* Phase progress */}
          <section>
            <div className="flex items-center justify-between mb-3">
              <h2 className="text-sm font-medium text-[var(--text-secondary)] uppercase tracking-wider">
                Active Work
              </h2>
              <Button variant="ghost" size="xs" onClick={() => navigate('/lifecycle')}>
                View all <ArrowRight className="h-3 w-3 ml-1" />
              </Button>
            </div>
            {activeRepos.length === 0 ? (
              <Card>
                <p className="text-sm text-[var(--text-tertiary)] text-center py-4">
                  No repos with active phase state
                </p>
              </Card>
            ) : (
              <div className="space-y-2">
                {activeRepos.map((repo) => (
                  <Card key={repo.repo_root} hover onClick={() => navigate('/lifecycle')}>
                    <div className="flex items-center justify-between">
                      <div className="min-w-0">
                        <p className="text-sm font-medium text-[var(--text-primary)] truncate">
                          {repo.repo_root.split('/').pop()}
                        </p>
                        <p className="text-xs text-[var(--text-tertiary)] truncate">
                          {repo.repo_root}
                        </p>
                      </div>
                      <div className="flex items-center gap-3 shrink-0">
                        {repo.cursor && (
                          <span className="text-xs text-[var(--text-secondary)] flex items-center gap-1">
                            <GitBranch className="h-3 w-3" />
                            {repo.cursor}
                          </span>
                        )}
                        <StatusBadge status={repo.phase ?? 'unknown'} />
                        {repo.approval_pending && (
                          <StatusBadge status="pending" label="Approval" pulse />
                        )}
                      </div>
                    </div>
                  </Card>
                ))}
              </div>
            )}
          </section>

          {/* Recent traces */}
          <section>
            <div className="flex items-center justify-between mb-3">
              <h2 className="text-sm font-medium text-[var(--text-secondary)] uppercase tracking-wider">
                Recent Activity
              </h2>
              <Button variant="ghost" size="xs" onClick={() => navigate('/telemetry')}>
                View all <ArrowRight className="h-3 w-3 ml-1" />
              </Button>
            </div>
            {!traces || traces.traces.length === 0 ? (
              <Card>
                <p className="text-sm text-[var(--text-tertiary)] text-center py-4">
                  No recent composition traces
                </p>
              </Card>
            ) : (
              <Card padding={false}>
                <table className="w-full">
                  <thead>
                    <tr className="border-b border-[var(--border-primary)]">
                      <th className="px-4 py-2 text-left text-[10px] font-medium text-[var(--text-tertiary)] uppercase tracking-wider">
                        Time
                      </th>
                      <th className="px-4 py-2 text-left text-[10px] font-medium text-[var(--text-tertiary)] uppercase tracking-wider">
                        Phase
                      </th>
                      <th className="px-4 py-2 text-left text-[10px] font-medium text-[var(--text-tertiary)] uppercase tracking-wider">
                        Status
                      </th>
                      <th className="px-4 py-2 text-left text-[10px] font-medium text-[var(--text-tertiary)] uppercase tracking-wider">
                        Latency
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {traces.traces.map((t) => (
                      <tr
                        key={t.trace_id}
                        className="border-b border-[var(--border-subtle)] last:border-0"
                      >
                        <td className="px-4 py-2 text-sm text-[var(--text-secondary)] tabular-nums">
                          {fmtTs(t.request_ts)}
                        </td>
                        <td className="px-4 py-2 text-sm text-[var(--text-primary)]">
                          {t.phase ?? '—'}
                        </td>
                        <td className="px-4 py-2 text-sm">
                          <StatusBadge status={t.status ?? 'unknown'} />
                        </td>
                        <td className="px-4 py-2 text-sm text-[var(--text-secondary)] tabular-nums">
                          {t.total_latency_ms != null ? `${t.total_latency_ms}ms` : '—'}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </Card>
            )}
          </section>
        </div>

        {/* Right sidebar */}
        <div className="space-y-6">
          {/* Quick actions */}
          <section>
            <h2 className="text-sm font-medium text-[var(--text-secondary)] uppercase tracking-wider mb-3">
              Quick Actions
            </h2>
            <div className="grid grid-cols-2 gap-2">
              <Button
                variant="outline"
                size="sm"
                icon={FlaskConical}
                onClick={() => navigate('/playground')}
              >
                Playground
              </Button>
              <Button
                variant="outline"
                size="sm"
                icon={Puzzle}
                onClick={() => navigate('/skills')}
              >
                Skills
              </Button>
              <Button
                variant="outline"
                size="sm"
                icon={RotateCw}
                onClick={() => navigate('/code-index')}
              >
                Code Index
              </Button>
              <Button
                variant="outline"
                size="sm"
                icon={GitBranch}
                onClick={() => navigate('/lifecycle')}
              >
                Lifecycle
              </Button>
            </div>
          </section>

          {/* Index health */}
          <section>
            <div className="flex items-center justify-between mb-3">
              <h2 className="text-sm font-medium text-[var(--text-secondary)] uppercase tracking-wider">
                Index Health
              </h2>
              <Button variant="ghost" size="xs" onClick={() => navigate('/code-index')}>
                Details <ArrowRight className="h-3 w-3 ml-1" />
              </Button>
            </div>
            {!codeRepos || codeRepos.length === 0 ? (
              <Card>
                <p className="text-sm text-[var(--text-tertiary)] text-center py-4">
                  No indexed repos
                </p>
              </Card>
            ) : (
              <div className="space-y-2">
                {codeRepos.map((repo) => (
                  <Card key={repo.slug}>
                    <div className="flex items-center justify-between">
                      <div className="min-w-0">
                        <p className="text-sm font-medium text-[var(--text-primary)] truncate">
                          {repo.slug}
                        </p>
                        <p className="text-xs text-[var(--text-tertiary)]">
                          {repo.symbol_count.toLocaleString()} symbols ·{' '}
                          {fmtTime(repo.last_indexed_at)}
                        </p>
                      </div>
                      <div className="flex items-center gap-2 shrink-0">
                        {repo.watch_enabled && (
                          <span className="text-[10px] text-[var(--text-tertiary)] uppercase tracking-wider">
                            watching
                          </span>
                        )}
                        <StatusBadge
                          status={repo.is_stale ? 'stale' : 'ok'}
                          label={repo.is_stale ? 'Stale' : 'Current'}
                        />
                      </div>
                    </div>
                  </Card>
                ))}
              </div>
            )}

            {/* Active jobs */}
            {activeJobs.length > 0 && (
              <div className="mt-3 space-y-2">
                {activeJobs.map((job) => (
                  <Card key={job.id}>
                    <div className="flex items-center justify-between">
                      <div>
                        <p className="text-sm text-[var(--text-primary)]">{job.slug}</p>
                        <p className="text-xs text-[var(--text-tertiary)]">
                          {job.phase ?? 'starting'} · {job.progress.toFixed(0)}%
                        </p>
                      </div>
                      <StatusBadge status={job.state} pulse />
                    </div>
                    <div className="mt-2 h-1 rounded-full bg-[var(--bg-tertiary)] overflow-hidden">
                      <div
                        className="h-full rounded-full bg-brand-500 transition-all duration-500"
                        style={{ width: `${Math.min(job.progress, 100)}%` }}
                      />
                    </div>
                  </Card>
                ))}
              </div>
            )}
          </section>
        </div>
      </div>
    </div>
  );
}
