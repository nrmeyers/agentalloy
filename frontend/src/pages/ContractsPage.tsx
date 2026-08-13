import { useEffect, useMemo, useState } from 'react';
import { ClipboardList } from 'lucide-react';
import {
  Card,
  ChipInput,
  DataTable,
  EmptyState,
  ErrorState,
  FilterSelect,
  TableSkeleton,
} from '../components';
import type { Column } from '../components';
import { useArchiveContract, usePatchContract, useContractsList } from '../hooks/useContracts';
import type { Contract } from '../lib/types';
import { StatusBadge } from '../components/StatusBadge';

const PHASE_OPTIONS = [
  { value: '', label: 'All phases' },
  { value: 'intake', label: 'intake' },
  { value: 'spec', label: 'spec' },
  { value: 'design', label: 'design' },
  { value: 'build', label: 'build' },
  { value: 'qa', label: 'qa' },
  { value: 'ship', label: 'ship' },
];

const STATUS_OPTIONS = [
  { value: '', label: 'All statuses' },
  { value: 'active', label: 'active' },
  { value: 'archived', label: 'archived' },
  { value: 'superseded', label: 'superseded' },
];

const Q_DEBOUNCE_MS = 300;

function TagsCell({ tags }: { tags: string[] | null }) {
  if (!tags || tags.length === 0) return <span className="text-[var(--text-tertiary)] text-sm">—</span>;
  return (
    <span className="flex flex-wrap gap-1">
      {tags.map((t) => (
        <span
          key={t}
          className="inline-block px-1.5 py-0.5 rounded bg-blue-100 text-blue-800 text-xs"
        >
          {t}
        </span>
      ))}
    </span>
  );
}

function ContractDetailPanel({
  contract,
  onClose,
}: {
  contract: Contract;
  onClose: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [body, setBody] = useState(contract.body ?? '');
  const [tags, setTags] = useState<string[]>(contract.domain_tags ?? []);

  const patchMut = usePatchContract(contract.contract_id);
  const archiveMut = useArchiveContract(contract.contract_id);

  const isEditable = contract.status === 'active';

  useEffect(() => {
    setBody(contract.body ?? '');
    setTags(contract.domain_tags ?? []);
  }, [contract]);

  const handleSave = () => {
    patchMut.mutate({
      body: body || undefined,
      domain_tags: tags.length ? tags : undefined,
    });
    setEditing(false);
  };

  const handleArchive = () => {
    if (confirm(`Archive contract ${contract.contract_id}?`)) {
      archiveMut.mutate();
    }
  };

  return (
    <Card className="mt-4">
      <div className="flex justify-between items-start mb-4">
        <div>
          <h3 className="text-lg font-bold font-mono">{contract.contract_id}</h3>
          <div className="flex items-center gap-2 mt-1">
            <StatusBadge status={contract.status} />
            <span className="text-sm text-[var(--text-tertiary)]">{contract.phase}</span>
            <span className="text-sm text-[var(--text-tertiary)]">{contract.slug}</span>
            {contract.work_item && (
              <span className="text-sm text-[var(--text-tertiary)]">· {contract.work_item}</span>
            )}
          </div>
        </div>
        <button
          onClick={onClose}
          className="text-[var(--text-tertiary)] hover:text-[var(--text-secondary)] text-xl leading-none"
        >
          ×
        </button>
      </div>

      {contract.supersedes && (
        <p className="text-xs text-[var(--text-tertiary)] mb-2">Supersedes: {contract.supersedes}</p>
      )}

      <div className="flex gap-2 mb-3 text-xs text-[var(--text-tertiary)]">
        <span>Created: {new Date(contract.created_at).toLocaleString()}</span>
        <span>Updated: {new Date(contract.updated_at).toLocaleString()}</span>
      </div>

      {(contract.scope_touches?.length || contract.scope_avoids?.length ||
        contract.success_criteria?.length) && (
        <div className="mb-3 space-y-1 text-sm">
          {contract.scope_touches?.length && (
            <p>
              <span className="text-[var(--text-tertiary)]">Touches:</span>{' '}
              {contract.scope_touches.join(', ')}
            </p>
          )}
          {contract.scope_avoids?.length && (
            <p>
              <span className="text-[var(--text-tertiary)]">Avoids:</span> {contract.scope_avoids.join(', ')}
            </p>
          )}
          {contract.success_criteria?.length && (
            <p>
              <span className="text-[var(--text-tertiary)]">Criteria:</span>{' '}
              {contract.success_criteria.join('; ')}
            </p>
          )}
        </div>
      )}

      <div className="mb-3">
        <span className="text-xs font-medium text-[var(--text-tertiary)] uppercase">Tags</span>
        {editing && isEditable ? (
          <div className="mt-1">
            <ChipInput values={tags} onChange={setTags} placeholder="domain tag" />
          </div>
        ) : (
          <div className="mt-1">
            <TagsCell tags={contract.domain_tags} />
          </div>
        )}
      </div>

      <div>
        <span className="text-xs font-medium text-[var(--text-tertiary)] uppercase">Body</span>
        {editing && isEditable ? (
          <textarea
            value={body}
            onChange={(e) => setBody(e.target.value)}
            rows={10}
            className="mt-1 w-full px-2 py-1.5 border border-[var(--border-primary)] rounded-md text-sm font-mono bg-[var(--bg-elevated)]"
          />
        ) : (
          <pre className="mt-1 whitespace-pre-wrap text-sm font-mono bg-[var(--bg-secondary)] rounded-md p-3 max-h-96 overflow-auto">
            {contract.body || '(empty)'}
          </pre>
        )}
      </div>

      {isEditable && (
        <div className="flex gap-2 mt-4 pt-3 border-t border-[var(--border-primary)]">
          {editing ? (
            <>
              <button
                onClick={handleSave}
                disabled={patchMut.isPending}
                className="px-3 py-1.5 bg-blue-600 text-white rounded-md text-sm hover:bg-blue-700 disabled:opacity-50"
              >
                {patchMut.isPending ? 'Saving…' : 'Save'}
              </button>
              <button
                onClick={() => setEditing(false)}
                className="px-3 py-1.5 bg-[var(--border-primary)] text-[var(--text-secondary)] rounded-md text-sm hover:bg-[var(--border-primary)]"
              >
                Cancel
              </button>
            </>
          ) : (
            <>
              <button
                onClick={() => setEditing(true)}
                className="px-3 py-1.5 bg-blue-600 text-white rounded-md text-sm hover:bg-blue-700"
              >
                Edit
              </button>
              <button
                onClick={handleArchive}
                disabled={archiveMut.isPending}
                className="px-3 py-1.5 bg-red-600 text-white rounded-md text-sm hover:bg-red-700 disabled:opacity-50"
              >
                {archiveMut.isPending ? 'Archiving…' : 'Archive'}
              </button>
            </>
          )}
        </div>
      )}

      {!isEditable && contract.status !== 'active' && (
        <p className="mt-3 text-xs text-[var(--text-tertiary)] italic">
          Cannot edit — contract is {contract.status}.
        </p>
      )}
    </Card>
  );
}

export function ContractsPage() {
  const [phase, setPhase] = useState('');
  const [status, setStatus] = useState('');
  const [qInput, setQInput] = useState('');
  const [q, setQ] = useState('');
  const [selectedId, setSelectedId] = useState<string | null>(null);

  // Debounce free-text search (client-side filter on slug/contract_id/work_item).
  useEffect(() => {
    const handle = setTimeout(() => setQ(qInput.trim()), Q_DEBOUNCE_MS);
    return () => clearTimeout(handle);
  }, [qInput]);

  const { data, isLoading, error, refetch } = useContractsList({
    phase: phase || undefined,
    status: status || undefined,
  });

  // Client-side text filter
  const filtered = useMemo(() => {
    if (!data || !q) return data?.contracts ?? [];
    const lower = q.toLowerCase();
    return data.contracts.filter((c) => {
      const haystack = [c.contract_id, c.slug, c.work_item, c.body]
        .filter(Boolean)
        .join(' ')
        .toLowerCase();
      return haystack.includes(lower);
    });
  }, [data, q]);

  const selected = filtered.find((c) => c.contract_id === selectedId) ?? null;

  const columns: Column<Contract>[] = [
    {
      key: 'contract_id',
      label: 'Contract ID',
      render: (c) => <span className="font-mono text-xs">{c.contract_id}</span>,
    },
    {
      key: 'phase',
      label: 'Phase',
      render: (c) => (
        <span className="text-xs px-1.5 py-0.5 rounded bg-[var(--bg-tertiary)] text-[var(--text-secondary)]">
          {c.phase}
        </span>
      ),
    },
    {
      key: 'slug',
      label: 'Slug',
      render: (c) => <span className="font-medium text-sm">{c.slug}</span>,
    },
    {
      key: 'work_item',
      label: 'Work Item',
      render: (c) => c.work_item || '—',
    },
    {
      key: 'domain_tags',
      label: 'Tags',
      render: (c) => <TagsCell tags={c.domain_tags} />,
    },
    {
      key: 'status',
      label: 'Status',
      render: (c) => <StatusBadge status={c.status} />,
    },
  ];

  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <h1 className="text-2xl font-bold">Contracts</h1>
        {data && (
          <span className="text-sm text-[var(--text-tertiary)]">
            {data.contracts.length} contract{data.contracts.length === 1 ? '' : 's'}
          </span>
        )}
      </div>

      <div className="flex flex-wrap items-end gap-3">
        <FilterSelect
          label="Phase"
          value={phase}
          options={PHASE_OPTIONS}
          onChange={setPhase}
        />
        <FilterSelect
          label="Status"
          value={status}
          options={STATUS_OPTIONS}
          onChange={setStatus}
        />
        <label className="flex flex-col gap-1 flex-1 min-w-[14rem] max-w-md">
          <span className="text-xs font-medium text-[var(--text-tertiary)] uppercase">Search</span>
          <input
            value={qInput}
            onChange={(e) => setQInput(e.target.value)}
            placeholder="contract id, slug, work item…"
            className="px-2 py-1.5 border border-[var(--border-primary)] rounded-md text-sm bg-[var(--bg-elevated)]"
          />
        </label>
      </div>

      {isLoading ? (
        <TableSkeleton />
      ) : error ? (
        <ErrorState message={error.message} onRetry={() => refetch()} />
      ) : filtered.length === 0 ? (
        <EmptyState
          title="No contracts match"
          hint="Try clearing the phase/status filters or the search text."
          icon={<ClipboardList className="h-6 w-6" />}
        />
      ) : (
        <Card>
          <DataTable
            data={filtered}
            columns={columns}
            rowKey={(c) => c.contract_id}
            onRowClick={(c) => setSelectedId(c.contract_id)}
            selectedRowKey={selectedId}
          />
        </Card>
      )}

      {selected && (
        <ContractDetailPanel
          contract={selected}
          onClose={() => setSelectedId(null)}
        />
      )}
    </div>
  );
}
