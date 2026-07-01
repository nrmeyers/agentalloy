import type { ReactNode } from 'react';
import { Card } from '../../components';
import { fmt } from '../../lib/format';
import type { SkillDetail } from '../../lib/types';
import { Chip, ChipRow, ClassBadge } from './shared';

function MetaItem({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div>
      <dt className="text-xs font-medium text-gray-500 uppercase">{label}</dt>
      <dd className="text-sm text-gray-900 break-all">{value}</dd>
    </div>
  );
}

export function ContentTab({ detail }: { detail: SkillDetail }) {
  const version = detail.active_version;
  return (
    <div className="space-y-6">
      <Card>
        <h2 className="text-lg font-semibold mb-4">{detail.canonical_name}</h2>
        <dl className="grid grid-cols-2 lg:grid-cols-4 gap-x-4 gap-y-2">
          <MetaItem label="Class" value={<ClassBadge skillClass={detail.skill_class} />} />
          <MetaItem label="Category" value={fmt(detail.category)} />
          <MetaItem label="Tier" value={fmt(detail.tier)} />
          <MetaItem label="Tags" value={<ChipRow items={detail.domain_tags} max={0} />} />
        </dl>
      </Card>

      <Card>
        <h2 className="text-lg font-semibold mb-4">Active Version</h2>
        {version ? (
          <div className="space-y-4">
            <dl className="grid grid-cols-2 lg:grid-cols-4 gap-x-4 gap-y-2">
              <MetaItem label="Version" value={`v${version.version_number}`} />
              <MetaItem
                label="Version ID"
                value={<span className="font-mono text-xs">{version.version_id}</span>}
              />
              <MetaItem label="Author" value={fmt(version.author)} />
              <MetaItem label="Authored At" value={fmt(version.authored_at)} />
              <MetaItem
                label="Change Summary"
                value={fmt(version.change_summary)}
              />
            </dl>
            <div>
              <h3 className="text-sm font-semibold text-gray-700 mb-2">Raw Prose</h3>
              <pre className="text-xs text-gray-800 bg-gray-50 border border-gray-200 rounded p-3 whitespace-pre-wrap break-words max-h-96 overflow-y-auto">
                {version.raw_prose}
              </pre>
            </div>
          </div>
        ) : (
          <p className="text-sm text-gray-500">No active version.</p>
        )}
      </Card>

      <Card>
        <h2 className="text-lg font-semibold mb-4">
          Fragments{' '}
          <span className="text-sm font-normal text-gray-500">({detail.fragments.length})</span>
        </h2>
        {detail.fragments.length === 0 ? (
          <p className="text-sm text-gray-500">No fragments.</p>
        ) : (
          <div className="space-y-3">
            {[...detail.fragments]
              .sort((a, b) => a.sequence - b.sequence)
              .map((fragment) => (
                <div
                  key={fragment.fragment_id}
                  className="border border-gray-200 rounded-md p-3"
                >
                  <div className="flex items-center gap-2 mb-2">
                    <Chip tone="blue">{fragment.fragment_type}</Chip>
                    <span className="text-xs text-gray-500">seq {fragment.sequence}</span>
                    <span className="font-mono text-xs text-gray-400">
                      {fragment.fragment_id}
                    </span>
                  </div>
                  <pre className="text-xs text-gray-800 whitespace-pre-wrap break-words max-h-48 overflow-y-auto">
                    {fragment.content}
                  </pre>
                </div>
              ))}
          </div>
        )}
      </Card>
    </div>
  );
}
