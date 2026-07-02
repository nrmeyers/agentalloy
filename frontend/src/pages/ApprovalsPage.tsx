import { useState } from 'react';
import { Card, EmptyState, ErrorState, FormField, PageSkeleton, inputClass } from '../components';
import { useApprovals, useApprove } from '../hooks/useRepos';
import { basename, fmt } from '../lib/format';
import type { PendingApproval } from '../lib/types';

function ApprovalCard({
  entry,
  approver,
  onApprove,
  approving,
}: {
  entry: PendingApproval;
  approver: string;
  onApprove: (entry: PendingApproval) => void;
  approving: boolean;
}) {
  return (
    <Card>
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-base font-bold text-gray-900">{basename(entry.repo)}</span>
            <span className="text-sm text-gray-700">
              <span className="font-medium">{entry.phase}</span>
              {' → '}
              <span className="font-medium">{fmt(entry.next_phase)}</span>
            </span>
          </div>
          <div className="text-xs text-gray-500 break-all">{entry.repo}</div>
        </div>
        <button
          onClick={() => onApprove(entry)}
          disabled={approving}
          className="shrink-0 px-4 py-2 bg-brand text-white rounded-md text-sm hover:bg-brand-dark disabled:opacity-50"
          title={approver.trim() ? `Approve as ${approver.trim()}` : 'Approve'}
        >
          {approving ? 'Approving…' : 'Approve'}
        </button>
      </div>

      {entry.stale && (
        <div className="mt-3 bg-red-50 border border-red-200 text-red-800 px-3 py-2 rounded-md text-sm font-medium">
          STALE — artifact changed after sign-off
        </div>
      )}

      {entry.artifacts.length > 0 && (
        <div className="mt-3">
          <div className="text-xs font-medium text-gray-500 uppercase mb-1">Artifacts</div>
          <ul className="space-y-0.5">
            {entry.artifacts.map((path) => (
              <li key={path} className="text-xs font-mono text-gray-700 break-all">
                {path}
              </li>
            ))}
          </ul>
        </div>
      )}
    </Card>
  );
}

export function ApprovalsPage() {
  const approvals = useApprovals();
  const approve = useApprove();
  const [approver, setApprover] = useState('');
  // Track which entry is in flight so only its button shows the spinner state.
  const [inFlight, setInFlight] = useState<string | null>(null);

  const handleApprove = (entry: PendingApproval) => {
    const key = `${entry.repo}::${entry.phase}`;
    setInFlight(key);
    approve.mutate(
      {
        repo: entry.repo,
        phase: entry.phase,
        approver: approver.trim() || undefined,
      },
      { onSettled: () => setInFlight(null) },
    );
  };

  if (approvals.isLoading) return <PageSkeleton />;
  if (approvals.error) {
    return <ErrorState message={approvals.error.message} onRetry={() => approvals.refetch()} />;
  }

  const pending = approvals.data?.pending ?? [];

  return (
    <div className="space-y-6 max-w-3xl">
      <h1 className="text-2xl font-bold">
        Approvals{' '}
        <span className="text-base font-normal text-gray-500">({approvals.data?.total ?? 0} pending)</span>
      </h1>

      <p className="text-sm text-gray-600">
        Approving a gate signs off the artifact and{' '}
        <span className="font-medium">auto-advances the repo to the next phase</span>.
      </p>

      <div className="max-w-sm">
        <FormField label="Approver" hint="Optional — recorded on the sign-off marker; defaults to the service user.">
          <input
            value={approver}
            onChange={(e) => setApprover(e.target.value)}
            placeholder="$USER"
            className={inputClass}
          />
        </FormField>
      </div>

      {pending.length === 0 ? (
        <EmptyState title="No approvals waiting." icon="✅" />
      ) : (
        <div className="space-y-4">
          {pending.map((entry) => (
            <ApprovalCard
              key={`${entry.repo}::${entry.phase}`}
              entry={entry}
              approver={approver}
              onApprove={handleApprove}
              approving={approve.isPending && inFlight === `${entry.repo}::${entry.phase}`}
            />
          ))}
        </div>
      )}
    </div>
  );
}
