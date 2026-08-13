import {
  PageHeader,
  Card,
  StatusBadge,
  Button,
  PageSkeleton,
} from '../components';
import { useRepos, useApprovals, useApprove } from '../hooks/useRepos';
import {
  usePhase,
  usePhaseAdvance,
  usePhaseClear,
  useArtifacts,
  useResume,
} from '../hooks/useLifecycle';
import {
  GitBranch,
  CheckSquare,
  ArrowRight,
  Trash2,
  FileText,
  AlertTriangle,
} from 'lucide-react';

const PHASES = ['intake', 'spec', 'design', 'plan', 'build', 'qa', 'ship'];

function getNextPhase(current: string): string | null {
  const idx = PHASES.indexOf(current);
  if (idx < 0 || idx >= PHASES.length - 1) return null;
  return PHASES[idx + 1];
}

// --- Repo Phase Card ---------------------------------------------------------

function RepoPhaseCard({ repoRoot }: { repoRoot: string }) {
  const shortName = repoRoot.split('/').pop() ?? repoRoot;
  const { data: phase } = usePhase(repoRoot);
  const { data: artifacts } = useArtifacts(repoRoot);
  const { data: resume } = useResume(repoRoot);
  const advance = usePhaseAdvance();
  const clear = usePhaseClear();

  const currentPhase = phase?.value ?? 'none';
  const currentIndex = PHASES.indexOf(currentPhase);
  const nextPhase = getNextPhase(currentPhase);
  const isPaused = phase?.mode === 'paused';

  return (
    <Card>
      <div className="flex items-start justify-between mb-3">
        <div>
          <p className="text-sm font-medium text-[var(--text-primary)]">{shortName}</p>
          <p className="text-xs text-[var(--text-tertiary)]">{repoRoot}</p>
        </div>
        <div className="flex items-center gap-1.5">
          {isPaused && (
            <StatusBadge status="warning" label="Paused" />
          )}
          <Button
            variant="ghost"
            size="xs"
            icon={Trash2}
            onClick={() => clear.mutate(repoRoot)}
            loading={clear.isPending}
          >
            Clear
          </Button>
          {nextPhase && (
            <Button
              variant="primary"
              size="xs"
              icon={ArrowRight}
              onClick={() =>
                advance.mutate({
                  repo: repoRoot,
                  data: { value: nextPhase },
                })
              }
              loading={advance.isPending}
            >
              → {nextPhase}
            </Button>
          )}
        </div>
      </div>

      {/* Phase stepper */}
      <div className="flex items-center gap-1 mb-2">
        {PHASES.map((p, i) => {
          const isCurrent = p === currentPhase;
          const isPast = currentIndex >= 0 && i < currentIndex;
          return (
            <div
              key={p}
              className={`flex-1 h-2 rounded-full transition-colors ${
                isCurrent
                  ? 'bg-brand-500'
                  : isPast
                    ? 'bg-brand-500/30'
                    : 'bg-[var(--bg-tertiary)]'
              }`}
              title={p}
            />
          );
        })}
      </div>
      <div className="flex items-center justify-between mb-3">
        <span className="text-xs text-[var(--text-secondary)]">
          {currentPhase}
          {phase?.transitioned_by && (
            <span className="text-[var(--text-tertiary)]">
              {' '}· by {phase.transitioned_by}
            </span>
          )}
        </span>
        {phase?.last_updated && (
          <span className="text-[10px] text-[var(--text-tertiary)]">
            {new Date(phase.last_updated).toLocaleString()}
          </span>
        )}
      </div>

      {/* Artifacts */}
      {artifacts && artifacts.length > 0 && (
        <div className="border-t border-[var(--border-primary)] pt-3 mt-3">
          <p className="text-[10px] font-medium text-[var(--text-tertiary)] uppercase tracking-wider mb-2">
            Artifacts
          </p>
          <div className="flex flex-wrap gap-1.5">
            {artifacts.map((a) => (
              <span
                key={`${a.phase}-${a.slug}-${a.name}`}
                className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs bg-[var(--bg-tertiary)] text-[var(--text-secondary)]"
              >
                <FileText className="h-3 w-3" />
                {a.name}
              </span>
            ))}
          </div>
        </div>
      )}

      {/* Owed artifacts from resume */}
      {resume && resume.owed_artifacts && resume.owed_artifacts.length > 0 && (
        <div className="border-t border-[var(--border-primary)] pt-3 mt-3">
          <p className="text-[10px] font-medium text-warning uppercase tracking-wider mb-2 flex items-center gap-1">
            <AlertTriangle className="h-3 w-3" />
            Owed Artifacts
          </p>
          <div className="flex flex-wrap gap-1.5">
            {resume.owed_artifacts.map((a) => (
              <span
                key={`${a.phase}-${a.slug}-${a.name}`}
                className="inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs bg-amber-500/10 text-amber-600 dark:text-amber-400"
              >
                {a.phase}/{a.name}
              </span>
            ))}
          </div>
        </div>
      )}
    </Card>
  );
}

// --- Approvals Section -------------------------------------------------------

function ApprovalsSection() {
  const { data: approvals } = useApprovals();
  const approve = useApprove();

  if (!approvals || approvals.total === 0) return null;

  return (
    <div className="mt-6">
      <h2 className="text-sm font-medium text-[var(--text-secondary)] uppercase tracking-wider mb-3 flex items-center gap-2">
        <CheckSquare className="h-4 w-4" />
        Pending Approvals
        <span className="inline-flex items-center justify-center min-w-[1.125rem] h-[1.125rem] px-1 rounded-full bg-amber-500/15 text-amber-600 dark:text-amber-400 text-[10px] font-semibold">
          {approvals.total}
        </span>
      </h2>
      <div className="space-y-2">
        {approvals.pending.map((a) => (
          <Card key={`${a.repo}-${a.phase}`}>
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-3">
                <div>
                  <p className="text-sm font-medium text-[var(--text-primary)]">{a.repo}</p>
                  <p className="text-xs text-[var(--text-tertiary)]">
                    {a.phase} → {a.next_phase ?? '?'}
                  </p>
                </div>
                {a.stale && (
                  <StatusBadge status="stale" label="Stale" />
                )}
              </div>
              <div className="flex items-center gap-2">
                {a.artifacts.length > 0 && (
                  <div className="flex gap-1">
                    {a.artifacts.map((art) => (
                      <span
                        key={art}
                        className="text-[10px] px-1.5 py-0.5 rounded bg-[var(--bg-tertiary)] text-[var(--text-tertiary)]"
                      >
                        {art}
                      </span>
                    ))}
                  </div>
                )}
                <Button
                  variant="primary"
                  size="sm"
                  icon={CheckSquare}
                  onClick={() =>
                    approve.mutate({
                      repo: a.repo,
                      phase: a.phase,
                    })
                  }
                  loading={approve.isPending}
                >
                  Approve
                </Button>
              </div>
            </div>
          </Card>
        ))}
      </div>
    </div>
  );
}

// --- Main Page ---------------------------------------------------------------

export function LifecyclePage() {
  const { data: repos, isLoading: reposLoading } = useRepos();

  if (reposLoading) return <PageSkeleton />;

  const activeRepos = repos?.repos.filter((r) => r.exists) ?? [];

  return (
    <div>
      <PageHeader
        title="Lifecycle"
        description="Phase progression, approvals, and workflow state"
      />

      {/* Phase legend */}
      <div className="flex items-center gap-1 mb-6 overflow-x-auto pb-1">
        {PHASES.map((phase, i) => (
          <div key={phase} className="flex items-center shrink-0">
            <span className="text-[10px] font-medium text-[var(--text-tertiary)] uppercase tracking-wider px-2 py-1 rounded bg-[var(--bg-tertiary)]">
              {phase}
            </span>
            {i < PHASES.length - 1 && (
              <span className="text-[var(--text-tertiary)] mx-0.5">→</span>
            )}
          </div>
        ))}
      </div>

      {/* Per-repo phase cards */}
      {activeRepos.length === 0 ? (
        <Card>
          <div className="text-center py-8">
            <GitBranch className="h-8 w-8 text-[var(--text-tertiary)] mx-auto mb-2" />
            <p className="text-sm text-[var(--text-secondary)]">No repos configured</p>
          </div>
        </Card>
      ) : (
        <div className="space-y-4">
          {activeRepos.map((repo) => (
            <RepoPhaseCard key={repo.repo_root} repoRoot={repo.repo_root} />
          ))}
        </div>
      )}

      {/* Pending approvals */}
      <ApprovalsSection />
    </div>
  );
}
