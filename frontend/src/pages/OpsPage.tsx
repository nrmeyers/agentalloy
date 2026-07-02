import { useState } from 'react';
import {
  Card,
  DataTable,
  ErrorState,
  FormField,
  Skeleton,
  StatCard,
  TableSkeleton,
  inputClass,
} from '../components';
import {
  useDoctor,
  usePacks,
  useProfiles,
  useReembed,
  useReembedStatus,
  useResolveProfile,
} from '../hooks/useOps';
import { fmt, fmtUnknown, truncate } from '../lib/format';
import type { DoctorCheck, PackEntry, ProfileEntry } from '../lib/types';
import { Chip, ChipRow } from './skills/shared';

// --- Doctor ----------------------------------------------------------------------

function PassFailBadge({ passed }: { passed: boolean | undefined }) {
  if (passed === undefined) {
    return (
      <span className="inline-flex items-center px-2 py-0.5 rounded text-xs font-medium bg-gray-100 text-gray-800">
        unknown
      </span>
    );
  }
  return (
    <span
      className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-medium ${
        passed ? 'bg-green-100 text-green-800' : 'bg-red-100 text-red-800'
      }`}
    >
      {passed ? 'pass' : 'fail'}
    </span>
  );
}

function DoctorSection() {
  const doctor = useDoctor();

  const checks = Array.isArray(doctor.data?.checks) ? doctor.data.checks : [];
  const anyFailed = checks.some((c) => c.passed === false) || doctor.data?.all_checks_passed === false;

  return (
    <Card>
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-lg font-semibold">Doctor</h2>
        <button
          onClick={() => doctor.refetch()}
          disabled={doctor.isFetching}
          className="px-4 py-2 bg-gray-100 text-gray-700 rounded-md text-sm hover:bg-gray-200 disabled:opacity-50"
        >
          {doctor.isFetching ? 'Running…' : 'Re-run checks'}
        </button>
      </div>

      {doctor.isLoading ? (
        <TableSkeleton rows={4} />
      ) : doctor.error ? (
        <ErrorState message={doctor.error.message} onRetry={() => doctor.refetch()} />
      ) : (
        <div className="space-y-4">
          <div
            className={`px-4 py-3 rounded-md text-sm font-medium ${
              doctor.data?.all_checks_passed === true
                ? 'bg-green-50 border border-green-200 text-green-800'
                : doctor.data?.all_checks_passed === false
                  ? 'bg-red-50 border border-red-200 text-red-800'
                  : 'bg-gray-50 border border-gray-200 text-gray-700'
            }`}
          >
            {doctor.data?.all_checks_passed === true
              ? 'All checks passed.'
              : doctor.data?.all_checks_passed === false
                ? 'Some checks failed — see below.'
                : 'Check status unknown.'}
          </div>

          <DataTable<DoctorCheck>
            data={checks}
            rowKey={(row, i) => row.name ?? i}
            emptyLabel="No checks reported"
            columns={[
              { key: 'name', label: 'Check', render: (r) => fmtUnknown(r.name) },
              { key: 'status', label: 'Status', render: (r) => <PassFailBadge passed={r.passed} /> },
              {
                key: 'detail',
                label: 'Detail',
                className: 'max-w-md',
                render: (r) => (
                  <span className={`break-words ${r.passed === false ? 'text-red-700' : 'text-gray-700'}`}>
                    {fmt(r.error ?? r.detail)}
                  </span>
                ),
              },
              {
                key: 'remediation',
                label: 'Remediation',
                className: 'max-w-md',
                render: (r) =>
                  r.passed === false && r.remediation ? (
                    <span className="break-words text-gray-700">{r.remediation}</span>
                  ) : (
                    <span className="text-gray-400">—</span>
                  ),
              },
              {
                key: 'duration',
                label: 'Duration',
                render: (r) =>
                  typeof r.duration_ms === 'number' ? `${r.duration_ms.toLocaleString()} ms` : '—',
              },
            ]}
          />

          {anyFailed && (
            <p className="text-xs text-gray-500">
              Repair runs via CLI — <code className="font-mono bg-gray-100 px-1 py-0.5 rounded">agentalloy doctor --repair</code>{' '}
              (the service can't repair itself).
            </p>
          )}
        </div>
      )}
    </Card>
  );
}

// --- Corpus / Reembed --------------------------------------------------------------

function ReembedSection() {
  const status = useReembedStatus();
  const reembed = useReembed();
  const [mode, setMode] = useState<'dry' | 'real' | null>(null);

  const unembedded = status.data?.unembedded ?? 0;
  const lastRun = reembed.data && reembed.data.dry_run === false ? reembed.data : null;
  const dedupHard = Array.isArray(lastRun?.dedup_hard) ? lastRun.dedup_hard : [];
  const dedupSoft = Array.isArray(lastRun?.dedup_soft) ? lastRun.dedup_soft : [];

  const runDry = () => {
    setMode('dry');
    reembed.mutate(true, { onSettled: () => setMode(null) });
  };
  const runReal = () => {
    if (
      !window.confirm(
        'Run reembed now? It runs in the service process and may take minutes. The service stays busy until it finishes.',
      )
    ) {
      return;
    }
    setMode('real');
    reembed.mutate(false, { onSettled: () => setMode(null) });
  };

  return (
    <Card>
      <h2 className="text-lg font-semibold mb-4">Corpus / Reembed</h2>

      {status.isLoading ? (
        <Skeleton className="h-24" />
      ) : status.error ? (
        <ErrorState message={status.error.message} onRetry={() => status.refetch()} />
      ) : (
        <div className="grid grid-cols-2 gap-4 max-w-md">
          <StatCard label="Embedded" value={fmt(status.data?.embedded_total, 'unknown')} />
          <StatCard
            label="Unembedded"
            value={
              unembedded > 0 ? (
                <span className="text-amber-600">{unembedded.toLocaleString()}</span>
              ) : (
                fmt(status.data?.unembedded, 'unknown')
              )
            }
            sub={
              unembedded > 0 ? (
                <span className="text-amber-700 font-medium">reembed needed</span>
              ) : undefined
            }
          />
        </div>
      )}

      <div className="mt-4 flex items-center gap-2">
        <button
          onClick={runDry}
          disabled={reembed.isPending}
          className="px-4 py-2 bg-gray-100 text-gray-700 rounded-md text-sm hover:bg-gray-200 disabled:opacity-50"
        >
          {reembed.isPending && mode === 'dry' ? 'Checking…' : 'Dry run'}
        </button>
        <button
          onClick={runReal}
          disabled={reembed.isPending}
          className="px-4 py-2 bg-brand text-white rounded-md text-sm hover:bg-brand-dark disabled:opacity-50"
        >
          {reembed.isPending && mode === 'real' ? 'Reembedding…' : 'Run reembed'}
        </button>
        {reembed.isPending && mode === 'real' && (
          <span className="text-xs text-gray-500">Running in the service process — may take minutes.</span>
        )}
      </div>

      {lastRun && (
        <div className="mt-4 space-y-2">
          <div className="flex items-center gap-2">
            <span className="text-sm text-gray-700">Last run exit code:</span>
            <span
              className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-medium ${
                lastRun.exit_code === 0 ? 'bg-green-100 text-green-800' : 'bg-red-100 text-red-800'
              }`}
            >
              {fmtUnknown(lastRun.exit_code)}
            </span>
            {dedupHard.length > 0 && <Chip tone="red">dedup hard ×{dedupHard.length}</Chip>}
            {dedupSoft.length > 0 && <Chip tone="amber">dedup soft ×{dedupSoft.length}</Chip>}
          </div>
          {(dedupHard.length > 0 || dedupSoft.length > 0) && (
            <details className="text-sm text-gray-600">
              <summary className="cursor-pointer">Dedup entries</summary>
              <pre className="mt-1 text-xs bg-gray-50 border border-gray-200 rounded p-3 whitespace-pre-wrap break-words max-h-72 overflow-y-auto">
                {JSON.stringify({ dedup_hard: dedupHard, dedup_soft: dedupSoft }, null, 2)}
              </pre>
            </details>
          )}
        </div>
      )}
    </Card>
  );
}

// --- Packs ----------------------------------------------------------------------

function packInstallState(pack: PackEntry): { label: string; style: string } {
  if (pack.skill_count > 0 && pack.installed_count >= pack.skill_count) {
    return { label: 'full', style: 'bg-green-100 text-green-800' };
  }
  if (pack.installed_count > 0) {
    return { label: 'partial', style: 'bg-amber-100 text-amber-800' };
  }
  return { label: 'none', style: 'bg-gray-100 text-gray-800' };
}

function PacksSection() {
  const packs = usePacks();

  return (
    <Card>
      <h2 className="text-lg font-semibold mb-4">
        Packs{' '}
        <span className="text-sm font-normal text-gray-500">({packs.data?.total ?? 0})</span>
      </h2>
      {packs.isLoading ? (
        <TableSkeleton rows={3} />
      ) : packs.error ? (
        <ErrorState message={packs.error.message} onRetry={() => packs.refetch()} />
      ) : (
        <DataTable<PackEntry>
          data={packs.data?.packs ?? []}
          rowKey={(row) => row.name}
          emptyLabel="No packs"
          columns={[
            {
              key: 'name',
              label: 'Pack',
              render: (r) => <span className="font-medium">{r.name}</span>,
            },
            { key: 'version', label: 'Version', render: (r) => fmt(r.version) },
            { key: 'tier', label: 'Tier', render: (r) => fmt(r.tier) },
            {
              key: 'description',
              label: 'Description',
              className: 'max-w-md',
              render: (r) => (
                <span title={r.description ?? undefined}>{truncate(r.description, 80)}</span>
              ),
            },
            {
              key: 'installed',
              label: 'Installed',
              render: (r) => {
                const state = packInstallState(r);
                return (
                  <span className="flex items-center gap-2">
                    <span className="tabular-nums">
                      {r.installed_count} of {r.skill_count}
                    </span>
                    <span
                      className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-medium ${state.style}`}
                    >
                      {state.label}
                    </span>
                  </span>
                );
              },
            },
          ]}
        />
      )}
    </Card>
  );
}

// --- Profiles --------------------------------------------------------------------

function ProfilesSection() {
  const profiles = useProfiles();
  const resolve = useResolveProfile();
  const [repo, setRepo] = useState('');

  const repoValid = repo.trim() !== '';
  const doResolve = () => resolve.mutate(repo.trim());

  return (
    <Card>
      <h2 className="text-lg font-semibold mb-4">
        Profiles{' '}
        <span className="text-sm font-normal text-gray-500">({profiles.data?.total ?? 0})</span>
      </h2>
      {profiles.isLoading ? (
        <TableSkeleton rows={3} />
      ) : profiles.error ? (
        <ErrorState message={profiles.error.message} onRetry={() => profiles.refetch()} />
      ) : (
        <DataTable<ProfileEntry>
          data={profiles.data?.profiles ?? []}
          rowKey={(row) => row.name}
          emptyLabel="No profiles"
          columns={[
            {
              key: 'name',
              label: 'Profile',
              render: (r) => (
                <span className="flex items-center gap-2">
                  <span className="font-medium">{r.name}</span>
                  {r.is_default && <Chip tone="blue">default</Chip>}
                  {r.active_for_cwd && (
                    <span className="text-xs text-green-700">active for service cwd</span>
                  )}
                </span>
              ),
            },
            {
              key: 'match_remote',
              label: 'Match Remote',
              render: (r) => <ChipRow items={r.match_remote} tone="gray" />,
            },
            {
              key: 'match_path',
              label: 'Match Path',
              render: (r) => <ChipRow items={r.match_path} tone="gray" />,
            },
            {
              key: 'has_overrides',
              label: 'Overrides',
              render: (r) =>
                r.has_overrides ? (
                  <span className="flex items-center gap-1.5 text-gray-700">
                    <span className="w-2 h-2 rounded-full bg-amber-500" aria-hidden />
                    yes
                  </span>
                ) : (
                  <span className="text-gray-400">—</span>
                ),
            },
          ]}
        />
      )}

      <div className="mt-6 border-t border-gray-100 pt-4">
        <h3 className="text-sm font-semibold text-gray-700 mb-2">Which profile?</h3>
        <div className="flex items-start gap-2 max-w-2xl">
          <div className="flex-1">
            <FormField label="Repo path">
              <input
                value={repo}
                onChange={(e) => setRepo(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && repoValid && !resolve.isPending) doResolve();
                }}
                placeholder="/absolute/path/to/repo"
                className={inputClass}
              />
            </FormField>
          </div>
          <button
            onClick={doResolve}
            disabled={!repoValid || resolve.isPending}
            className="mt-6 px-4 py-2 bg-brand text-white rounded-md text-sm hover:bg-brand-dark disabled:opacity-50"
          >
            {resolve.isPending ? 'Resolving…' : 'Resolve'}
          </button>
        </div>
        {resolve.data && (
          <p className="text-sm text-gray-700">
            <span className="break-all font-mono text-xs text-gray-500">{resolve.data.repo}</span>{' '}
            resolves to <span className="font-medium">{resolve.data.profile}</span>
            {resolve.data.is_default && (
              <span className="ml-1">
                <Chip tone="blue">default</Chip>
              </span>
            )}
          </p>
        )}
        {resolve.error && <p className="text-sm text-red-600">{resolve.error.message}</p>}
      </div>
    </Card>
  );
}

// --- Page ------------------------------------------------------------------------

export function OpsPage() {
  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold">Ops</h1>
      <DoctorSection />
      <ReembedSection />
      <PacksSection />
      <ProfilesSection />
    </div>
  );
}
