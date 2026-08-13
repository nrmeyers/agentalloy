import type { ReactNode } from 'react';

export interface Column<T> {
  key: string;
  label: string;
  render: (row: T) => ReactNode;
  className?: string;
}

export function DataTable<T>({
  data,
  columns,
  rowKey,
  emptyLabel = 'No data',
  onRowClick,
  selectedRowKey,
}: {
  data: T[];
  columns: Column<T>[];
  rowKey: (row: T, index: number) => string | number;
  emptyLabel?: string;
  onRowClick?: (row: T) => void;
  selectedRowKey?: string | number | null;
}) {
  if (data.length === 0) {
    return (
      <div className="py-8 text-center text-sm text-[var(--text-tertiary)]">
        {emptyLabel}
      </div>
    );
  }
  return (
    <div className="overflow-x-auto">
      <table className="min-w-full">
        <thead>
          <tr className="border-b border-[var(--border-primary)]">
            {columns.map((col) => (
              <th
                key={col.key}
                className="px-4 py-2 text-left text-[10px] font-medium text-[var(--text-tertiary)] uppercase tracking-wider"
              >
                {col.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {data.map((row, i) => {
            const key = rowKey(row, i);
            const isSelected =
              selectedRowKey !== undefined && String(key) === String(selectedRowKey);
            return (
              <tr
                key={key}
                onClick={onRowClick ? () => onRowClick(row) : undefined}
                className={`border-b border-[var(--border-subtle)] transition-colors
                  ${onRowClick ? 'cursor-pointer hover:bg-[var(--bg-tertiary)]' : ''}
                  ${isSelected ? 'bg-brand-500/5' : ''}`}
              >
                {columns.map((col) => (
                  <td
                    key={col.key}
                    className={`px-4 py-2.5 text-sm text-[var(--text-primary)] ${col.className ?? ''}`}
                  >
                    {col.render(row)}
                  </td>
                ))}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
