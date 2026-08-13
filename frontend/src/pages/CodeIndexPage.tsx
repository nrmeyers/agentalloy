import { useState } from 'react';
import {
  PageHeader,
  Card,
  StatusBadge,
  Button,
  PageSkeleton,
  EmptyState,
} from '../components';
import { useCodeRepos, useCodeJobs, useCodeRepoStats, useCodeReindex, useCodeWatchToggle, useCodeRepoRemove, useCodeJobCancel } from '../hooks/useCodeIndex';
import {
  Database,
  RefreshCw,
  Eye,
  EyeOff,
  Trash2,
  XCircle,
  BarChart3,
  List,
  Clock,
} from 'lucide-react';
import type { CodeIndexRepo, CodeIndexJob } from '../lib/types';

type Tab = 'repos' | 'jobs' | 'stats';

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

function fmtDuration(startEpoch: number, endEpoch: number | null): string {
  const end = endEpoch ?? Date.now() / 1000;
  const secs = Math.round(end - startEpoch);
  if (secs < 60) return `${secs}s`;
  const mins = Math.floor(secs / 60);
  const remSecs = secs % 60;
  if (mins < 60) return `${mins}m ${remSecs}s`;
  const hrs = Math.floor(mins / 60);
  return `${hrs}h ${mins % 60}m`;
}

// --- Repos Tab ---------------------------------------------------------------

function ReposTab() {
  const { data: repos, isLoading } = useCodeRepos();
  const reindex = useCodeReindex();
  const watchToggle = useCodeWatchToggle();
  const remove = useCodeRepoRemove();

  if (isLoading) return <PageSkeleton />;
  if (!repos || repos.length === 0) {
    return (
      <EmptyState
        title="No indexed repositories"
        hint="Use the CLI: agentalloy code-index index <path>"
        icon={<Database />}
      />
    );
  }

  return (
    <div className="space-y-3">
      {repos.map((repo) => (
        <RepoRow
          key={`${repo.slug}-${repo.repo_path}`}
          repo={repo}
          onReindex={() => reindex.mutate(repo.slug)}
          onToggleWatch={() =>
            watchToggle.mutate({ slug: repo.slug, enabled: !repo.watch_enabled })
          }
          onRemove={() => remove.mutate(repo.slug)}
          reindexing={reindex.isPending}
        />
      ))}
    </div>
  );
}

function RepoRow({
  repo,
  onReindex,
  onToggleWatch,
  onRemove,
  reindexing,
}: {
  repo: CodeIndexRepo;
  onReindex: () => void;
  onToggleWatch: () => void;
  onRemove: () => void;
  reindexing: boolean;
}) {
  return (
    <Card>
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 mb-1">
            <p className="text-sm font-medium text-[var(--text-primary)] truncate">
              {repo.slug}
            </p>
            <StatusBadge
              status={repo.is_stale ? 'stale' : 'ok'}
              label={repo.is_stale ? 'Stale' : 'Current'}
            />
            {repo.watch_enabled && (
              <span className="text-[10px] text-brand-500 uppercase tracking-wider font-medium">
                Watching
              </span>
            )}
          </div>
          <p className="text-xs text-[var(--text-tertiary)] truncate mb-2">
            {repo.repo_path}
          </p>
          <div className="flex items-center gap-4 text-xs text-[var(--text-secondary)]">
            <span>{repo.symbol_count.toLocaleString()} symbols</span>
            <span>{repo.edge_count.toLocaleString()} edges</span>
            <span className="flex items-center gap-1">
              <Clock className="h-3 w-3" />
              {fmtTime(repo.last_indexed_at)}
            </span>
            {repo.indexed_head && (
              <span className="font-mono text-[var(--text-tertiary)]">
                {repo.indexed_head.slice(0, 7)}
                {repo.is_stale && repo.current_head && (
                  <span className="text-warning"> → {repo.current_head.slice(0, 7)}</span>
                )}
              </span>
            )}
          </div>
        </div>
        <div className="flex items-center gap-1.5 shrink-0">
          <Button
            variant="ghost"
            size="xs"
            icon={RefreshCw}
            onClick={onReindex}
            loading={reindexing}
          >
            Reindex
          </Button>
          <Button
            variant="ghost"
            size="xs"
            icon={repo.watch_enabled ? EyeOff : Eye}
            onClick={onToggleWatch}
          >
            {repo.watch_enabled ? 'Unwatch' : 'Watch'}
          </Button>
          <Button
            variant="ghost"
            size="xs"
            icon={Trash2}
            onClick={onRemove}
            className="text-error hover:text-error"
          />
        </div>
      </div>
    </Card>
  );
}

// --- Jobs Tab ----------------------------------------------------------------

function JobsTab() {
  const { data: jobs, isLoading } = useCodeJobs();
  const cancel = useCodeJobCancel();

  if (isLoading) return <PageSkeleton />;
  if (!jobs || jobs.length === 0) {
    return (
      <EmptyState
        title="No index jobs"
        hint="Jobs appear when you trigger a reindex"
        icon={<Clock />}
      />
    );
  }

  return (
    <Card padding={false}>
      <table className="w-full">
        <thead>
          <tr className="border-b border-[var(--border-primary)]">
            <th className="px-4 py-2 text-left text-[10px] font-medium text-[var(--text-tertiary)] uppercase tracking-wider">
              Repo
            </th>
            <th className="px-4 py-2 text-left text-[10px] font-medium text-[var(--text-tertiary)] uppercase tracking-wider">
              State
            </th>
            <th className="px-4 py-2 text-left text-[10px] font-medium text-[var(--text-tertiary)] uppercase tracking-wider">
              Progress
            </th>
            <th className="px-4 py-2 text-left text-[10px] font-medium text-[var(--text-tertiary)] uppercase tracking-wider">
              Symbols
            </th>
            <th className="px-4 py-2 text-left text-[10px] font-medium text-[var(--text-tertiary)] uppercase tracking-wider">
              Edges
            </th>
            <th className="px-4 py-2 text-left text-[10px] font-medium text-[var(--text-tertiary)] uppercase tracking-wider">
              Embeddings
            </th>
            <th className="px-4 py-2 text-left text-[10px] font-medium text-[var(--text-tertiary)] uppercase tracking-wider">
              Duration
            </th>
            <th className="px-4 py-2 text-left text-[10px] font-medium text-[var(--text-tertiary)] uppercase tracking-wider">
              GOVERNS
            </th>
            <th className="px-4 py-2" />
          </tr>
        </thead>
        <tbody>
          {jobs.map((job) => (
            <JobRow
              key={job.id}
              job={job}
              onCancel={() => cancel.mutate(job.id)}
              cancelling={cancel.isPending}
            />
          ))}
        </tbody>
      </table>
    </Card>
  );
}

function JobRow({
  job,
  onCancel,
  cancelling,
}: {
  job: CodeIndexJob;
  onCancel: () => void;
  cancelling: boolean;
}) {
  const isActive = job.state === 'running' || job.state === 'queued';
  return (
    <tr className="border-b border-[var(--border-subtle)] last:border-0">
      <td className="px-4 py-2.5 text-sm text-[var(--text-primary)] font-medium">
        {job.slug}
      </td>
      <td className="px-4 py-2.5 text-sm">
        <StatusBadge status={job.state} pulse />
      </td>
      <td className="px-4 py-2.5 text-sm">
        <div className="flex items-center gap-2 min-w-[120px]">
          <div className="flex-1 h-1.5 rounded-full bg-[var(--bg-tertiary)] overflow-hidden">
            <div
              className={`h-full rounded-full transition-all duration-500 ${
                job.state === 'failed' ? 'bg-error' : 'bg-brand-500'
              }`}
              style={{ width: `${Math.min(job.progress, 100)}%` }}
            />
          </div>
          <span className="text-xs text-[var(--text-tertiary)] tabular-nums w-10 text-right">
            {job.progress.toFixed(0)}%
          </span>
        </div>
        {job.phase && (
          <span className="text-[10px] text-[var(--text-tertiary)]">{job.phase}</span>
        )}
      </td>
      <td className="px-4 py-2.5 text-sm text-[var(--text-secondary)] tabular-nums">
        {job.symbol_count.toLocaleString()}
      </td>
      <td className="px-4 py-2.5 text-sm text-[var(--text-secondary)] tabular-nums">
        {job.edge_count.toLocaleString()}
      </td>
      <td className="px-4 py-2.5 text-sm text-[var(--text-secondary)] tabular-nums">
        {job.embedding_count.toLocaleString()}
      </td>
      <td className="px-4 py-2.5 text-sm text-[var(--text-secondary)] tabular-nums">
        {fmtDuration(job.started_at, job.finished_at)}
      </td>
      <td className="px-4 py-2.5 text-sm text-[var(--text-secondary)] tabular-nums">
        <span className="text-success">+{job.governs_written}</span>
        {job.governs_dropped > 0 && (
          <span className="text-error ml-1">-{job.governs_dropped}</span>
        )}
      </td>
      <td className="px-4 py-2.5 text-right">
        {isActive && (
          <Button
            variant="ghost"
            size="xs"
            icon={XCircle}
            onClick={onCancel}
            loading={cancelling}
          >
            Cancel
          </Button>
        )}
        {job.error && (
          <span className="text-xs text-error" title={job.error}>
            Error
          </span>
        )}
      </td>
    </tr>
  );
}

// --- Stats Tab ---------------------------------------------------------------

function StatsTab() {
  const { data: repos } = useCodeRepos();
  const [selectedSlug, setSelectedSlug] = useState<string | null>(null);
  const { data: stats, isLoading } = useCodeRepoStats(selectedSlug);

  if (!repos || repos.length === 0) {
    return (
      <EmptyState
        title="No indexed repos"
        hint="Index a repo to see stats"
        icon={<BarChart3 />}
      />
    );
  }

  return (
    <div className="grid grid-cols-1 lg:grid-cols-4 gap-4">
      {/* Repo selector */}
      <div className="space-y-1">
        {repos.map((repo) => (
          <button
            key={`${repo.slug}-${repo.repo_path}`}
            onClick={() => setSelectedSlug(repo.slug)}
            className={`w-full text-left px-3 py-2 rounded-md text-sm transition-colors ${
              selectedSlug === repo.slug
                ? 'bg-brand-500/10 text-brand-600 dark:text-brand-400 font-medium'
                : 'text-[var(--text-secondary)] hover:bg-[var(--bg-tertiary)]'
            }`}
          >
            <span className="truncate">{repo.slug}</span>
          </button>
        ))}
      </div>

      {/* Stats detail */}
      <div className="lg:col-span-3">
        {!selectedSlug ? (
          <Card>
            <p className="text-sm text-[var(--text-tertiary)] text-center py-8">
              Select a repo to view stats
            </p>
          </Card>
        ) : isLoading ? (
          <PageSkeleton />
        ) : stats ? (
          <div className="space-y-4">
            {/* Summary cards */}
            <div className="grid grid-cols-3 gap-3">
              <Card>
                <p className="text-xs text-[var(--text-tertiary)] uppercase tracking-wider">
                  Symbols
                </p>
                <p className="text-xl font-semibold text-[var(--text-primary)] tabular-nums mt-1">
                  {Object.values(stats.counts_by_kind).reduce((a, b) => a + b, 0).toLocaleString()}
                </p>
              </Card>
              <Card>
                <p className="text-xs text-[var(--text-tertiary)] uppercase tracking-wider">
                  Vectors
                </p>
                <p className="text-xl font-semibold text-[var(--text-primary)] tabular-nums mt-1">
                  {stats.vector_count.toLocaleString()}
                </p>
              </Card>
              <Card>
                <p className="text-xs text-[var(--text-tertiary)] uppercase tracking-wider">
                  Kinds
                </p>
                <p className="text-xl font-semibold text-[var(--text-primary)] tabular-nums mt-1">
                  {Object.keys(stats.counts_by_kind).length}
                </p>
              </Card>
            </div>

            {/* Counts by kind */}
            <Card>
              <h3 className="text-sm font-medium text-[var(--text-primary)] mb-3">
                Symbols by Kind
              </h3>
              <div className="space-y-2">
                {Object.entries(stats.counts_by_kind)
                  .sort(([, a], [, b]) => b - a)
                  .map(([kind, count]) => {
                    const total = Object.values(stats.counts_by_kind).reduce(
                      (a, b) => a + b,
                      0,
                    );
                    const pct = total > 0 ? (count / total) * 100 : 0;
                    return (
                      <div key={kind} className="flex items-center gap-3">
                        <span className="text-xs text-[var(--text-secondary)] w-24 shrink-0">
                          {kind}
                        </span>
                        <div className="flex-1 h-2 rounded-full bg-[var(--bg-tertiary)] overflow-hidden">
                          <div
                            className="h-full rounded-full bg-brand-500/60"
                            style={{ width: `${pct}%` }}
                          />
                        </div>
                        <span className="text-xs text-[var(--text-tertiary)] tabular-nums w-12 text-right">
                          {count.toLocaleString()}
                        </span>
                      </div>
                    );
                  })}
              </div>
            </Card>

            {/* Top centrality */}
            {stats.top_centrality.length > 0 && (
              <Card>
                <h3 className="text-sm font-medium text-[var(--text-primary)] mb-3">
                  Top Centrality (PageRank)
                </h3>
                <div className="space-y-1">
                  {stats.top_centrality.slice(0, 15).map((entry, i) => (
                    <div
                      key={entry.qualified_name}
                      className="flex items-center justify-between py-1 border-b border-[var(--border-subtle)] last:border-0"
                    >
                      <div className="flex items-center gap-2 min-w-0">
                        <span className="text-[10px] text-[var(--text-tertiary)] tabular-nums w-4">
                          {i + 1}
                        </span>
                        <span className="text-sm text-[var(--text-primary)] font-mono truncate">
                          {entry.qualified_name}
                        </span>
                      </div>
                      <span className="text-xs text-[var(--text-tertiary)] tabular-nums shrink-0 ml-2">
                        {entry.pagerank.toFixed(4)}
                      </span>
                    </div>
                  ))}
                </div>
              </Card>
            )}
          </div>
        ) : null}
      </div>
    </div>
  );
}

// --- Main Page ---------------------------------------------------------------

export function CodeIndexPage() {
  const [tab, setTab] = useState<Tab>('repos');

  const tabs: { id: Tab; label: string; icon: typeof List }[] = [
    { id: 'repos', label: 'Repos', icon: Database },
    { id: 'jobs', label: 'Jobs', icon: Clock },
    { id: 'stats', label: 'Stats', icon: BarChart3 },
  ];

  return (
    <div>
      <PageHeader
        title="Code Index"
        description="Repository indexing status, jobs, and graph health"
      />

      {/* Tab bar */}
      <div className="flex items-center gap-1 mb-6 border-b border-[var(--border-primary)]">
        {tabs.map((t) => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            className={`flex items-center gap-1.5 px-3 py-2 text-sm font-medium border-b-2 -mb-px transition-colors ${
              tab === t.id
                ? 'border-brand-500 text-brand-600 dark:text-brand-400'
                : 'border-transparent text-[var(--text-tertiary)] hover:text-[var(--text-primary)]'
            }`}
          >
            <t.icon className="h-3.5 w-3.5" />
            {t.label}
          </button>
        ))}
      </div>

      {tab === 'repos' && <ReposTab />}
      {tab === 'jobs' && <JobsTab />}
      {tab === 'stats' && <StatsTab />}
    </div>
  );
}
