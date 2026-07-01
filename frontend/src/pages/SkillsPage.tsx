import { useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Card,
  DataTable,
  EmptyState,
  ErrorState,
  FilterSelect,
  TableSkeleton,
} from '../components';
import type { Column } from '../components';
import { useSkillsList } from '../hooks/useSkills';
import type { SkillSummary } from '../lib/types';
import { Chip, ChipRow, ClassBadge } from './skills/shared';

// skill_class is a closed enum in the API contract.
const CLASS_OPTIONS = [
  { value: '', label: 'All classes' },
  { value: 'domain', label: 'domain' },
  { value: 'system', label: 'system' },
  { value: 'workflow', label: 'workflow' },
];

const Q_DEBOUNCE_MS = 300;

export function SkillsPage() {
  const navigate = useNavigate();
  const [skillClass, setSkillClass] = useState('');
  const [category, setCategory] = useState('');
  const [qInput, setQInput] = useState('');
  const [q, setQ] = useState('');

  // Debounce free-text search — the filter is server-driven.
  useEffect(() => {
    const handle = setTimeout(() => setQ(qInput.trim()), Q_DEBOUNCE_MS);
    return () => clearTimeout(handle);
  }, [qInput]);

  const { data, isLoading, error, refetch } = useSkillsList({
    class: skillClass || undefined,
    category: category || undefined,
    q: q || undefined,
  });

  // Category options are derived from returned data — never hardcoded.
  const categories = useMemo(() => {
    const set = new Set<string>();
    data?.skills?.forEach((s) => {
      if (s.category) set.add(s.category);
    });
    if (category) set.add(category);
    return [...set].sort();
  }, [data, category]);

  const columns: Column<SkillSummary>[] = [
    {
      key: 'skill_id',
      label: 'Skill ID',
      render: (s) => <span className="font-mono text-xs">{s.skill_id}</span>,
    },
    {
      key: 'canonical_name',
      label: 'Canonical Name',
      render: (s) => <span className="font-medium">{s.canonical_name}</span>,
    },
    {
      key: 'skill_class',
      label: 'Class',
      render: (s) => <ClassBadge skillClass={s.skill_class} />,
    },
    {
      key: 'category',
      label: 'Category',
      render: (s) => s.category || '—',
    },
    {
      key: 'domain_tags',
      label: 'Tags',
      render: (s) => <ChipRow items={s.domain_tags} max={3} />,
    },
    {
      key: 'provenance',
      label: 'Provenance',
      render: (s) => (
        <span className="flex flex-wrap gap-1">
          {s.pack && <Chip tone="gray">pack: {s.pack}</Chip>}
          {s.override_layer && <Chip tone="amber">overridden: {s.override_layer}</Chip>}
          {!s.pack && !s.override_layer && <span className="text-sm text-gray-400">—</span>}
        </span>
      ),
    },
  ];

  return (
    <div className="space-y-6">
      <div className="flex justify-between items-center">
        <h1 className="text-2xl font-bold">Skills</h1>
        {data && (
          <span className="text-sm text-gray-500">
            {data.total} skill{data.total === 1 ? '' : 's'}
          </span>
        )}
      </div>

      <div className="flex flex-wrap items-end gap-3">
        <FilterSelect
          label="Class"
          value={skillClass}
          options={CLASS_OPTIONS}
          onChange={setSkillClass}
        />
        <FilterSelect
          label="Category"
          value={category}
          options={[
            { value: '', label: 'All categories' },
            ...categories.map((c) => ({ value: c, label: c })),
          ]}
          onChange={setCategory}
        />
        <label className="flex flex-col gap-1 flex-1 min-w-[14rem] max-w-md">
          <span className="text-xs font-medium text-gray-500 uppercase">Search</span>
          <input
            value={qInput}
            onChange={(e) => setQInput(e.target.value)}
            placeholder="name, id, description…"
            className="px-2 py-1.5 border border-gray-300 rounded-md text-sm bg-white"
          />
        </label>
      </div>

      {isLoading ? (
        <TableSkeleton />
      ) : error ? (
        <ErrorState message={error.message} onRetry={() => refetch()} />
      ) : !data || data.skills.length === 0 ? (
        <EmptyState
          title="No skills match"
          hint="Try clearing the class/category filters or the search text."
          icon="🧩"
        />
      ) : (
        <Card>
          <DataTable
            data={data.skills}
            columns={columns}
            rowKey={(s) => s.skill_id}
            onRowClick={(s) => navigate(`/skills/${encodeURIComponent(s.skill_id)}`)}
          />
        </Card>
      )}
    </div>
  );
}
