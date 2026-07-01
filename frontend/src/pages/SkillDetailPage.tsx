import { useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { ErrorState, PageSkeleton, StatusBadge } from '../components';
import { useSkillDetail } from '../hooks/useSkills';
import { ContentTab } from './skills/ContentTab';
import { CustomizeTab } from './skills/CustomizeTab';
import { VersionsTab } from './skills/VersionsTab';

type Tab = 'content' | 'versions' | 'customize';

const TABS: { id: Tab; label: string }[] = [
  { id: 'content', label: 'Content' },
  { id: 'versions', label: 'Versions' },
  { id: 'customize', label: 'Customize' },
];

export function SkillDetailPage() {
  const { skillId = '' } = useParams<{ skillId: string }>();
  const [tab, setTab] = useState<Tab>('content');
  const { data: detail, isLoading, error, refetch } = useSkillDetail(skillId);

  if (isLoading) return <PageSkeleton />;
  if (error) return <ErrorState message={error.message} onRetry={() => refetch()} />;
  if (!detail) return null;

  // Domain skills carry no customization surface.
  const customizable = detail.skill_class !== 'domain';
  const visibleTabs = TABS.filter((t) => t.id !== 'customize' || customizable);
  const activeTab: Tab = tab === 'customize' && !customizable ? 'content' : tab;

  return (
    <div className="space-y-6">
      <div>
        <Link to="/skills" className="text-sm text-brand hover:underline">
          ← Skills
        </Link>
        <div className="flex items-center gap-3 mt-1">
          <h1 className="text-2xl font-bold">{detail.canonical_name}</h1>
          <StatusBadge
            status={detail.is_active ? 'ok' : 'unavailable'}
            label={detail.is_active ? 'active' : 'inactive'}
          />
        </div>
        <p className="font-mono text-xs text-gray-500 mt-1">{detail.skill_id}</p>
      </div>

      <div className="border-b border-gray-200">
        <nav className="flex items-center gap-4">
          {visibleTabs.map((t) => (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              className={`px-1 py-2 text-sm font-medium border-b-2 -mb-px ${
                activeTab === t.id
                  ? 'border-brand text-brand'
                  : 'border-transparent text-gray-500 hover:text-gray-700'
              }`}
            >
              {t.label}
            </button>
          ))}
          {!customizable && (
            <span className="text-xs text-gray-400 ml-auto">
              domain skills are not customizable
            </span>
          )}
        </nav>
      </div>

      {activeTab === 'content' && <ContentTab detail={detail} />}
      {activeTab === 'versions' && <VersionsTab skillId={skillId} />}
      {activeTab === 'customize' && customizable && <CustomizeTab skillId={skillId} />}
    </div>
  );
}
