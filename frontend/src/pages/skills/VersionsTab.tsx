import { useState } from 'react';
import { Card, EmptyState, ErrorState, StatusBadge, TableSkeleton } from '../../components';
import { useSkillVersions } from '../../hooks/useSkills';
import { fmt } from '../../lib/format';

export function VersionsTab({ skillId }: { skillId: string }) {
  const { data, isLoading, error, refetch } = useSkillVersions(skillId);
  const [expanded, setExpanded] = useState<string | null>(null);

  if (isLoading) return <TableSkeleton />;
  if (error) return <ErrorState message={error.message} onRetry={() => refetch()} />;
  if (!data || data.versions.length === 0) {
    return <EmptyState title="No versions" hint="This skill has no recorded versions." icon="🗂️" />;
  }

  const versions = [...data.versions].sort((a, b) => b.version_number - a.version_number);

  return (
    <Card>
      <div className="divide-y divide-gray-200">
        {versions.map((version) => {
          const isOpen = expanded === version.version_id;
          return (
            <div key={version.version_id} className="py-3">
              <button
                type="button"
                onClick={() => setExpanded(isOpen ? null : version.version_id)}
                className="w-full flex flex-wrap items-center gap-3 text-left hover:bg-gray-50 rounded px-2 py-1"
              >
                <span className="text-gray-400 text-xs w-4">{isOpen ? '▾' : '▸'}</span>
                <span className="font-semibold text-sm text-gray-900 w-12">
                  v{version.version_number}
                </span>
                <span className="text-sm text-gray-600 w-44">{fmt(version.authored_at)}</span>
                <span className="text-sm text-gray-600 w-32 truncate">{fmt(version.author)}</span>
                <span className="text-sm text-gray-700 flex-1 min-w-[10rem] truncate">
                  {fmt(version.change_summary)}
                </span>
                {version.status && <StatusBadge status={version.status} />}
                {version.is_active && (
                  <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-green-100 text-green-800">
                    active
                  </span>
                )}
              </button>
              {isOpen && (
                <pre className="mt-2 ml-8 text-xs text-gray-800 bg-gray-50 border border-gray-200 rounded p-3 whitespace-pre-wrap break-words max-h-96 overflow-y-auto">
                  {version.raw_prose}
                </pre>
              )}
            </div>
          );
        })}
      </div>
    </Card>
  );
}
