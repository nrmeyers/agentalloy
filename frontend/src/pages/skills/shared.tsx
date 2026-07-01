import type { ReactNode } from 'react';

const classStyles: Record<string, string> = {
  domain: 'bg-blue-100 text-blue-800',
  system: 'bg-purple-100 text-purple-800',
  workflow: 'bg-teal-100 text-teal-800',
};

export function ClassBadge({ skillClass }: { skillClass: string }) {
  const style = classStyles[skillClass] ?? 'bg-gray-100 text-gray-800';
  return (
    <span className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-medium ${style}`}>
      {skillClass}
    </span>
  );
}

export type ChipTone = 'gray' | 'amber' | 'green' | 'red' | 'blue';

const chipStyles: Record<ChipTone, string> = {
  gray: 'bg-gray-100 text-gray-700',
  amber: 'bg-amber-100 text-amber-800',
  green: 'bg-green-100 text-green-800',
  red: 'bg-red-100 text-red-800',
  blue: 'bg-blue-100 text-blue-800',
};

export function Chip({ children, tone = 'gray' }: { children: ReactNode; tone?: ChipTone }) {
  return (
    <span className={`inline-flex items-center px-1.5 py-0.5 rounded text-xs ${chipStyles[tone]}`}>
      {children}
    </span>
  );
}

/** Chip row with truncation: shows the first `max` items plus a "+N" chip. */
export function ChipRow({
  items,
  tone = 'gray',
  max = 3,
}: {
  items: string[] | null | undefined;
  tone?: ChipTone;
  max?: number;
}) {
  if (!items || items.length === 0) return <span className="text-sm text-gray-400">—</span>;
  const shown = max > 0 ? items.slice(0, max) : items;
  const hidden = items.length - shown.length;
  return (
    <span className="flex flex-wrap gap-1">
      {shown.map((item) => (
        <Chip key={item} tone={tone}>
          {item}
        </Chip>
      ))}
      {hidden > 0 && (
        <span title={items.slice(shown.length).join(', ')}>
          <Chip tone="gray">+{hidden}</Chip>
        </span>
      )}
    </span>
  );
}
