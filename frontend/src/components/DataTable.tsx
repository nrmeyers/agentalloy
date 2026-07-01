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
}: {
  data: T[];
  columns: Column<T>[];
  rowKey: (row: T, index: number) => string | number;
  emptyLabel?: string;
  onRowClick?: (row: T) => void;
}) {
  if (data.length === 0) {
    return <div className="py-6 text-center text-sm text-gray-500">{emptyLabel}</div>;
  }
  return (
    <div className="overflow-x-auto">
      <table className="min-w-full divide-y divide-gray-200">
        <thead>
          <tr>
            {columns.map((col) => (
              <th
                key={col.key}
                className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase tracking-wide"
              >
                {col.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-200">
          {data.map((row, i) => (
            <tr
              key={rowKey(row, i)}
              onClick={onRowClick ? () => onRowClick(row) : undefined}
              className={onRowClick ? 'cursor-pointer hover:bg-gray-50' : undefined}
            >
              {columns.map((col) => (
                <td key={col.key} className={`px-4 py-2 text-sm text-gray-900 ${col.className ?? ''}`}>
                  {col.render(row)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
