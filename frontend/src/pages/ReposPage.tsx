import { useState } from 'react';
import { Card, EmptyState, ErrorState, PageSkeleton, Skeleton } from '../components';
import { useRepoGates, useRepos } from '../hooks/useRepos';
import { basename, fmt, fmtIsoTs } from '../lib/format';
import type { RepoEntry } from '../lib/types';
import { Chip } from './skills/shared';

/** Lazy-mounted gate detail — the query only fires once the section is expanded. */
function GateStatus({ repo }: { repo: string }) {
  const gates = useRepoGates(repo);

  if (gates.isLoading) return <Skeleton className="h-16" />;
  if (gates.error) {
    return <ErrorState message={gates.error.message} onRetry={() => gates.refetch()} />;
  }
  const g = gates.data;
  if (!g) return null;

  const approvalSatisfied = g.approval_required && !g.approval_pending;

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <span
          className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-bold ${
            g.blocked ? 'bg-red-100 text-red-800' : 'bg-green-100 text-green-800'
          }`}
        >
          {g.blocked ? 'BLOCKED' : 'NOT BLOCKED'}
        </span>
        <span className="text-sm text-gray-700">
          phase <span className="font-medium">{fmt(g.phase)}</span>
          {' → next '}
          <span className="font-medium">{fmt(g.next_phase)}</span>
        </span>
        {g.approval_pending && <Chip tone="amber">approval pending</Chip>}
        {!g.approval_required && <Chip tone="gray">no approval required</Chip>}
      </div>

      {g.advisories.length > 0 ? (
        <div>
          <div className="text-xs font-medium text-gray-500 uppercase mb-1">Advisories</div>
          <ul className="list-disc list-inside space-y-0.5">
            {g.advisories.map((a) => (
              <li key={a} className="text-sm text-gray-700">
                {a}
              </li>
            ))}
          </ul>
        </div>
      ) : (
        <p className="text-sm text-gray-500">No advisories.</p>
      )}

      {approvalSatisfied && (
        <p className="text-sm text-green-700">
          Approved by <span className="font-medium">{fmt(g.approver, 'unknown')}</span> at{' '}
          {fmtIsoTs(g.approved_at)}
        </p>
      )}
    </div>
  );
}

function RepoCard({ repo }: { repo: RepoEntry }) {
  const [gatesOpen, setGatesOpen] = useState(false);
  const upstream =
    repo.upstream_model || repo.upstream_url
      ? [repo.upstream_model, repo.upstream_url].filter(Boolean).join(' @ ')
      : null;
  const contractChips = Object.entries(repo.contracts_by_phase);

  return (
    <Card>
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-base font-bold text-gray-900">{basename(repo.repo_root)}</span>
            {!repo.exists && <Chip tone="red">missing</Chip>}
            {repo.approval_pending && <Chip tone="amber">approval pending</Chip>}
          </div>
          <div className="text-xs text-gray-500 break-all">{repo.repo_root}</div>
        </div>
        <div className="flex flex-wrap items-center gap-1 justify-end">
          {repo.harnesses.map((h) => (
            <Chip key={h} tone="blue">
              {h}
            </Chip>
          ))}
        </div>
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-2">
        <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-purple-100 text-purple-800">
          phase: {fmt(repo.phase, 'none')}
        </span>
        {repo.lifecycle_mode && <Chip tone="gray">lifecycle: {repo.lifecycle_mode}</Chip>}
        {repo.profile && <Chip tone="green">profile: {repo.profile}</Chip>}
        {contractChips.map(([phase, count]) => (
          <Chip key={phase} tone="gray">
            {phase} ×{count}
          </Chip>
        ))}
      </div>

      {(upstream || repo.cursor) && (
        <div className="mt-2 space-y-0.5">
          {upstream && <div className="text-xs text-gray-500 break-all">upstream: {upstream}</div>}
          {repo.cursor && (
            <div className="text-xs text-gray-500 break-all font-mono">cursor: {repo.cursor}</div>
          )}
        </div>
      )}

      <div className="mt-3 border-t border-gray-100 pt-2">
        <button
          type="button"
          onClick={() => setGatesOpen((open) => !open)}
          className="text-sm text-gray-600 hover:text-gray-900"
        >
          {gatesOpen ? '▾' : '▸'} Gate status
        </button>
        {gatesOpen && (
          <div className="mt-2">
            <GateStatus repo={repo.repo_root} />
          </div>
        )}
      </div>
    </Card>
  );
}

export function ReposPage() {
  const repos = useRepos();

  if (repos.isLoading) return <PageSkeleton />;
  if (repos.error) {
    return <ErrorState message={repos.error.message} onRetry={() => repos.refetch()} />;
  }

  const entries = repos.data?.repos ?? [];

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">
          Repos{' '}
          <span className="text-base font-normal text-gray-500">({repos.data?.total ?? 0})</span>
        </h1>
        <button
          onClick={() => repos.refetch()}
          disabled={repos.isFetching}
          className="px-4 py-2 bg-gray-100 text-gray-700 rounded-md text-sm hover:bg-gray-200 disabled:opacity-50"
        >
          {repos.isFetching ? 'Refreshing…' : 'Refresh'}
        </button>
      </div>

      {entries.length === 0 ? (
        <EmptyState title="No repos registered" hint="Onboard a repo to see it here." icon="📁" />
      ) : (
        <div className="space-y-4">
          {entries.map((repo) => (
            <RepoCard key={repo.repo_root} repo={repo} />
          ))}
        </div>
      )}
    </div>
  );
}
