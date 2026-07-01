export interface FilterValues {
  phase: string;
  status: string;
  repo: string;
  range: string;
}

export const RANGE_OPTIONS: { value: string; label: string }[] = [
  { value: '', label: 'All time' },
  { value: '1h', label: 'Last hour' },
  { value: '24h', label: 'Last 24 hours' },
  { value: '7d', label: 'Last 7 days' },
];

export const RANGE_MS: Record<string, number> = {
  '1h': 3_600_000,
  '24h': 86_400_000,
  '7d': 604_800_000,
};

export function FilterSelect({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: string;
  options: { value: string; label: string }[];
  onChange: (value: string) => void;
}) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-xs font-medium text-gray-500 uppercase">{label}</span>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="px-2 py-1.5 border border-gray-300 rounded-md text-sm bg-white min-w-[9rem]"
      >
        {options.map((opt) => (
          <option key={opt.value} value={opt.value}>
            {opt.label}
          </option>
        ))}
      </select>
    </label>
  );
}

function withAll(values: string[], allLabel: string): { value: string; label: string }[] {
  return [{ value: '', label: allLabel }, ...values.map((v) => ({ value: v, label: v }))];
}

/**
 * Phase / status / repo options are derived from live data by the caller —
 * never hardcoded (phase vocabulary is owned by the backend).
 */
export function FilterBar({
  phases,
  statuses,
  repos,
  values,
  onChange,
}: {
  phases: string[];
  statuses: string[];
  repos: string[];
  values: FilterValues;
  onChange: (patch: Partial<FilterValues>) => void;
}) {
  return (
    <div className="flex flex-wrap items-end gap-3">
      <FilterSelect
        label="Phase"
        value={values.phase}
        options={withAll(phases, 'All phases')}
        onChange={(phase) => onChange({ phase })}
      />
      <FilterSelect
        label="Status"
        value={values.status}
        options={withAll(statuses, 'All statuses')}
        onChange={(status) => onChange({ status })}
      />
      <FilterSelect
        label="Repo"
        value={values.repo}
        options={withAll(repos, 'All repos')}
        onChange={(repo) => onChange({ repo })}
      />
      <FilterSelect
        label="Range"
        value={values.range}
        options={RANGE_OPTIONS}
        onChange={(range) => onChange({ range })}
      />
    </div>
  );
}
