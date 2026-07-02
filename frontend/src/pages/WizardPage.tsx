import { useEffect, useState } from 'react';
import {
  Card,
  EmptyState,
  ErrorState,
  FormField,
  TableSkeleton,
  inputClass,
} from '../components';
import { useRepos } from '../hooks/useRepos';
import {
  useWizardInstall,
  useWizardPack,
  useWizardSaveFile,
  useWizardScaffold,
  useWizardValidate,
} from '../hooks/useWizard';
import { ApiError } from '../lib/api';
import { fmt } from '../lib/format';
import type { WizardValidateResult } from '../lib/types';
import { Chip, ChipRow } from './skills/shared';

// --- Helpers ---------------------------------------------------------------------

const CUSTOM_REPO = '__custom__';

/** Pull the machine-readable error code out of a nested FastAPI 4xx detail. */
function errorCode(err: unknown): string | null {
  if (!(err instanceof ApiError)) return null;
  if (!err.body || typeof err.body !== 'object') return null;
  const detail = (err.body as Record<string, unknown>).detail;
  if (!detail || typeof detail !== 'object') return null;
  const code = (detail as Record<string, unknown>).error;
  return typeof code === 'string' ? code : null;
}

/**
 * validate-pack's shape is open — derive pass/fail from whatever error-ish
 * array exists, plus any explicit ok/valid boolean.
 */
function deriveValidation(result: WizardValidateResult): {
  passed: boolean;
  errors: string[];
  warnings: string[];
} {
  let errors: unknown[] = [];
  if (Array.isArray(result.errors)) {
    errors = result.errors;
  } else {
    for (const [key, value] of Object.entries(result)) {
      if (Array.isArray(value) && /error/i.test(key)) {
        errors = value;
        break;
      }
    }
  }
  const warnings = Array.isArray(result.warnings) ? result.warnings.map((w) => fmt(w)) : [];
  const okFlag =
    typeof result.ok === 'boolean' ? result.ok : typeof result.valid === 'boolean' ? result.valid : null;
  return { passed: errors.length === 0 && okFlag !== false, errors: errors.map((e) => fmt(e)), warnings };
}

// --- Stepper ---------------------------------------------------------------------

const STEP_LABELS = ['Scaffold', 'Draft', 'Validate', 'Approve + Install'];

function Stepper({
  step,
  maxStep,
  onSelect,
}: {
  step: number;
  maxStep: number;
  onSelect: (n: number) => void;
}) {
  return (
    <ol className="flex flex-wrap items-center gap-2">
      {STEP_LABELS.map((label, i) => {
        const n = i + 1;
        const reachable = n <= maxStep;
        const active = n === step;
        return (
          <li key={label} className="flex items-center gap-2">
            {i > 0 && <span className="text-gray-300">→</span>}
            <button
              onClick={() => onSelect(n)}
              disabled={!reachable}
              className={`flex items-center gap-2 px-3 py-1.5 rounded-md text-sm ${
                active
                  ? 'bg-brand text-white font-medium'
                  : reachable
                    ? 'bg-white border border-gray-300 text-gray-700 hover:bg-gray-100'
                    : 'bg-gray-50 border border-gray-200 text-gray-400 cursor-not-allowed'
              }`}
            >
              <span
                className={`inline-flex items-center justify-center w-5 h-5 rounded-full text-xs font-semibold ${
                  active ? 'bg-white/20' : reachable ? 'bg-gray-100' : 'bg-gray-100 text-gray-400'
                }`}
              >
                {n}
              </span>
              {label}
            </button>
          </li>
        );
      })}
    </ol>
  );
}

// --- R1–R9 self-check (advisory UI state only — the real gate is validate) --------

interface SelfCheckItem {
  id: string;
  label: string;
  hint: string;
  naHint?: string;
}

const SELF_CHECK_ITEMS: SelfCheckItem[] = [
  {
    id: 'R1',
    label: 'Tiered sourcing',
    hint: 'External claims name their source tier.',
    naHint: 'N/A if not derived from external docs',
  },
  {
    id: 'R2',
    label: 'Imperative, actionable prose',
    hint: 'Fragments tell the agent what to do, not what exists.',
  },
  {
    id: 'R3',
    label: 'Self-contained fragments',
    hint: 'Each fragment stands alone without its neighbours.',
  },
  {
    id: 'R4',
    label: 'Correct fragment taxonomy',
    hint: 'At minimum: execution, verification and rationale fragments.',
  },
  {
    id: 'R5',
    label: 'Date-stamped claims',
    hint: 'Version-dependent claims carry a date.',
    naHint: 'N/A if no version dependency',
  },
  { id: 'R6', label: 'Tag discipline', hint: '2–5 domain tags — no fewer, no more.' },
  { id: 'R7', label: 'No duplication', hint: 'Does not restate an existing corpus skill.' },
  { id: 'R8', label: 'Sizing', hint: '80–800 words per fragment.' },
  { id: 'R9', label: 'Canonical naming', hint: 'canonical_name follows the corpus naming convention.' },
];

type SelfCheckMark = 'open' | 'done' | 'na';
type SelfCheckState = Record<string, SelfCheckMark>;

function SelfCheckPanel({
  state,
  onChange,
}: {
  state: SelfCheckState;
  onChange: (id: string, mark: SelfCheckMark) => void;
}) {
  const addressed = SELF_CHECK_ITEMS.filter((i) => (state[i.id] ?? 'open') !== 'open').length;
  return (
    <Card>
      <h3 className="text-sm font-semibold text-gray-700">Self-check (R1–R9)</h3>
      <p className="text-xs text-gray-500 mb-3">
        Advisory only — tick as you review your draft. The real gate is the validate step.{' '}
        <span className="font-medium">{addressed} of {SELF_CHECK_ITEMS.length} addressed.</span>
      </p>
      <ul className="space-y-2.5">
        {SELF_CHECK_ITEMS.map((item) => {
          const mark = state[item.id] ?? 'open';
          return (
            <li key={item.id} className="flex items-start gap-2">
              <input
                type="checkbox"
                checked={mark === 'done'}
                disabled={mark === 'na'}
                onChange={() => onChange(item.id, mark === 'done' ? 'open' : 'done')}
                className="mt-0.5 accent-brand"
              />
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span
                    className={`text-sm font-medium ${
                      mark === 'na' ? 'text-gray-400 line-through' : 'text-gray-800'
                    }`}
                  >
                    {item.id} · {item.label}
                  </span>
                  <button
                    onClick={() => onChange(item.id, mark === 'na' ? 'open' : 'na')}
                    title={item.naHint ?? 'Mark not applicable'}
                    className={`text-[10px] px-1.5 py-0.5 rounded border ${
                      mark === 'na'
                        ? 'bg-gray-200 border-gray-300 text-gray-700 font-semibold'
                        : 'border-gray-200 text-gray-400 hover:bg-gray-100'
                    }`}
                  >
                    N/A
                  </button>
                </div>
                <p className="text-xs text-gray-500">
                  {item.hint}
                  {item.naHint && <span className="text-gray-400"> ({item.naHint}.)</span>}
                </p>
              </div>
            </li>
          );
        })}
      </ul>
    </Card>
  );
}

// --- Page ------------------------------------------------------------------------

export function WizardPage() {
  const repos = useRepos();
  const scaffold = useWizardScaffold();
  const saveFile = useWizardSaveFile();
  const validate = useWizardValidate();
  const install = useWizardInstall();

  const [step, setStep] = useState(1);
  const [maxStep, setMaxStep] = useState(1);

  // Step 1 form.
  const [repoChoice, setRepoChoice] = useState('');
  const [repoCustom, setRepoCustom] = useState('');
  const [pack, setPack] = useState('');
  const [skillId, setSkillId] = useState('');
  const [skillClass, setSkillClass] = useState('domain');
  const [canonicalName, setCanonicalName] = useState('');
  const repo = repoChoice === CUSTOM_REPO ? repoCustom.trim() : repoChoice;

  // The committed draft identity — set on scaffold success or "Resume draft".
  const [target, setTarget] = useState<{ repo: string; pack: string } | null>(null);

  // Step 2 editor state (per file name; server content is the fallback).
  const [selectedFile, setSelectedFile] = useState<string | null>(null);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [saveError, setSaveError] = useState<string | null>(null);
  const [selfCheck, setSelfCheck] = useState<SelfCheckState>({});

  // Step 3 gate: a passing validation in THIS session (reset on every save).
  const [passedThisSession, setPassedThisSession] = useState(false);

  // Step 4 form.
  const [approver, setApprover] = useState('');
  const [allowDuplicates, setAllowDuplicates] = useState(false);

  // Debounced existence probe for "Resume draft" on the scaffold step.
  const [probe, setProbe] = useState({ repo: '', pack: '' });
  useEffect(() => {
    const t = setTimeout(() => setProbe({ repo, pack: pack.trim() }), 400);
    return () => clearTimeout(t);
  }, [repo, pack]);
  const existing = useWizardPack(probe.repo, probe.pack, step === 1);

  // Draft contents once a target is committed.
  const packQuery = useWizardPack(target?.repo ?? '', target?.pack ?? '', target !== null);
  const files = packQuery.data?.files ?? [];

  // Keep a valid tab selected; prefer the skill yaml over pack.yaml.
  useEffect(() => {
    if (files.length > 0 && (selectedFile === null || !files.some((f) => f.name === selectedFile))) {
      setSelectedFile(files.find((f) => f.name !== 'pack.yaml')?.name ?? files[0].name);
    }
  }, [files, selectedFile]);

  const goTo = (n: number) => {
    if (n <= maxStep) setStep(n);
  };
  const advanceTo = (n: number) => {
    setStep(n);
    setMaxStep((m) => Math.max(m, n));
  };

  /** Commit a repo+pack as the wizard's draft and reset downstream state. */
  const adoptTarget = (repoVal: string, packVal: string) => {
    setTarget({ repo: repoVal, pack: packVal });
    setDrafts({});
    setSelectedFile(null);
    setSaveError(null);
    setPassedThisSession(false);
    validate.reset();
    install.reset();
    advanceTo(2);
  };

  // --- Step 1: scaffold ------------------------------------------------------------

  const repoOptions = repos.data?.repos.map((r) => r.repo_root) ?? [];
  const scaffoldReady = repo !== '' && pack.trim() !== '' && skillId.trim() !== '';

  const handleScaffold = () => {
    scaffold.mutate(
      {
        repo,
        pack: pack.trim(),
        skill_id: skillId.trim(),
        skill_class: skillClass,
        canonical_name: canonicalName.trim() || undefined,
      },
      { onSuccess: () => adoptTarget(repo, pack.trim()) },
    );
  };

  const renderScaffold = () => (
    <Card className="max-w-2xl">
      <h2 className="text-lg font-semibold mb-1">Scaffold a new pack</h2>
      <p className="text-sm text-gray-600 mb-4">
        Creates <code className="font-mono text-xs bg-gray-100 px-1 py-0.5 rounded">
          &lt;repo&gt;/.agentalloy/custom-skills/&lt;pack&gt;
        </code>{' '}
        with a pack.yaml and a starter skill YAML.
      </p>

      <FormField label="Repo" hint="Where the custom pack lives — pick a known repo or type a path.">
        <div className="space-y-2">
          <select
            value={repoChoice}
            onChange={(e) => setRepoChoice(e.target.value)}
            className={`${inputClass} bg-white`}
          >
            <option value="">Select a repo…</option>
            {repoOptions.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
            <option value={CUSTOM_REPO}>Other — type a path…</option>
          </select>
          {repoChoice === CUSTOM_REPO && (
            <input
              value={repoCustom}
              onChange={(e) => setRepoCustom(e.target.value)}
              placeholder="/absolute/path/to/repo"
              className={inputClass}
            />
          )}
        </div>
      </FormField>

      <FormField label="Pack name" hint="Letters, digits, - and _ (max 64 chars).">
        <input
          value={pack}
          onChange={(e) => setPack(e.target.value)}
          placeholder="my-team-conventions"
          className={inputClass}
        />
      </FormField>

      <FormField label="Skill ID" hint="Becomes <skill_id>.yaml inside the pack.">
        <input
          value={skillId}
          onChange={(e) => setSkillId(e.target.value)}
          placeholder="deploy-checklist"
          className={inputClass}
        />
      </FormField>

      <FormField label="Skill class" hint="domain is right for almost everything.">
        <select
          value={skillClass}
          onChange={(e) => setSkillClass(e.target.value)}
          className={`${inputClass} bg-white`}
        >
          <option value="domain">domain (default)</option>
          <option value="system">system — advanced, rare</option>
          <option value="workflow">workflow — advanced, rare</option>
        </select>
      </FormField>

      <FormField label="Canonical name" hint="Optional — human-readable display name.">
        <input
          value={canonicalName}
          onChange={(e) => setCanonicalName(e.target.value)}
          placeholder="Deploy Checklist"
          className={inputClass}
        />
      </FormField>

      {existing.data?.exists && (
        <div className="mb-4 bg-amber-50 border border-amber-200 rounded-md px-4 py-3 flex items-center justify-between gap-4">
          <div className="text-sm text-amber-800 min-w-0">
            A draft pack already exists at{' '}
            <code className="font-mono text-xs break-all">{existing.data.pack_dir}</code>
          </div>
          <button
            onClick={() => adoptTarget(probe.repo, probe.pack)}
            className="shrink-0 px-4 py-2 bg-amber-600 text-white rounded-md text-sm hover:bg-amber-700"
          >
            Resume draft
          </button>
        </div>
      )}

      <button
        onClick={handleScaffold}
        disabled={!scaffoldReady || scaffold.isPending}
        className="px-4 py-2 bg-brand text-white rounded-md text-sm hover:bg-brand-dark disabled:opacity-50"
      >
        {scaffold.isPending ? 'Scaffolding…' : 'Scaffold'}
      </button>
    </Card>
  );

  // --- Step 2: draft ---------------------------------------------------------------

  const serverContent = files.find((f) => f.name === selectedFile)?.content;
  const currentContent = (selectedFile !== null ? drafts[selectedFile] : undefined) ?? serverContent ?? '';
  const isDirty = (name: string) => {
    const d = drafts[name];
    if (d === undefined) return false;
    return d !== files.find((f) => f.name === name)?.content;
  };
  const dirtyFiles = files.filter((f) => isDirty(f.name)).map((f) => f.name);

  const handleSave = async () => {
    if (!target || selectedFile === null) return;
    setSaveError(null);
    try {
      await saveFile.mutateAsync({
        repo: target.repo,
        pack: target.pack,
        file: selectedFile,
        content: currentContent,
      });
      // Edits after a green run must be re-validated before install.
      setPassedThisSession(false);
      validate.reset();
    } catch (err: unknown) {
      if (errorCode(err) === 'invalid_yaml' && err instanceof ApiError) {
        setSaveError(err.message);
      }
      // other failures are toasted by the global mutation cache
    }
  };

  const renderDraft = () => {
    if (packQuery.isLoading) return <TableSkeleton rows={4} />;
    if (packQuery.error) {
      return <ErrorState message={packQuery.error.message} onRetry={() => packQuery.refetch()} />;
    }
    if (!packQuery.data?.exists || files.length === 0) {
      return (
        <EmptyState
          title="Pack not found on disk."
          hint="Go back to step 1 and scaffold it first."
          icon="📦"
        />
      );
    }
    return (
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6 items-start">
        <Card className="lg:col-span-2">
          <div className="flex items-center justify-between gap-4 mb-3">
            <div className="flex flex-wrap gap-1">
              {files.map((f) => (
                <button
                  key={f.name}
                  onClick={() => {
                    setSelectedFile(f.name);
                    setSaveError(null);
                  }}
                  className={`px-3 py-1.5 rounded-md text-sm font-mono ${
                    f.name === selectedFile
                      ? 'bg-blue-50 text-blue-700 font-medium'
                      : 'text-gray-600 hover:bg-gray-100'
                  }`}
                >
                  {f.name}
                  {isDirty(f.name) && (
                    <span className="ml-1 text-amber-600" title="unsaved changes">
                      ●
                    </span>
                  )}
                </button>
              ))}
            </div>
            <button
              onClick={handleSave}
              disabled={saveFile.isPending || selectedFile === null}
              className="shrink-0 px-4 py-2 bg-brand text-white rounded-md text-sm hover:bg-brand-dark disabled:opacity-50"
            >
              {saveFile.isPending ? 'Saving…' : 'Save'}
            </button>
          </div>
          <textarea
            value={currentContent}
            onChange={(e) => {
              if (selectedFile !== null) {
                const value = e.target.value;
                setDrafts((prev) => ({ ...prev, [selectedFile]: value }));
              }
            }}
            rows={24}
            spellCheck={false}
            className="w-full font-mono text-xs px-3 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-1 focus:ring-brand"
          />
          {saveError && (
            <div className="mt-2 bg-red-50 border border-red-200 rounded-md px-4 py-3">
              <p className="text-sm font-medium text-red-700">Invalid YAML — not saved:</p>
              <p className="text-sm text-red-700 whitespace-pre-wrap break-words">{saveError}</p>
            </div>
          )}
          <p className="mt-2 text-xs text-gray-500 font-mono break-all">{packQuery.data.pack_dir}</p>
        </Card>
        <SelfCheckPanel
          state={selfCheck}
          onChange={(id, mark) => setSelfCheck((prev) => ({ ...prev, [id]: mark }))}
        />
        <div className="lg:col-span-3 flex items-center gap-3">
          <button
            onClick={() => advanceTo(3)}
            className="px-4 py-2 bg-brand text-white rounded-md text-sm hover:bg-brand-dark"
          >
            Continue to validate
          </button>
          {dirtyFiles.length > 0 && (
            <span className="text-sm text-amber-700">
              Unsaved changes in {dirtyFiles.join(', ')} — validate runs against saved files.
            </span>
          )}
        </div>
      </div>
    );
  };

  // --- Step 3: validate ------------------------------------------------------------

  const validation = validate.data ? deriveValidation(validate.data) : null;

  const runValidate = () => {
    if (!target) return;
    validate.mutate(
      { repo: target.repo, pack: target.pack },
      {
        onSuccess: (result) => {
          if (deriveValidation(result).passed) {
            setPassedThisSession(true);
            setMaxStep((m) => Math.max(m, 4));
          }
        },
      },
    );
  };

  const renderValidate = () => (
    <Card className="max-w-3xl">
      <h2 className="text-lg font-semibold mb-1">Validate</h2>
      <p className="text-sm text-gray-600 mb-4">
        Strict schema + lint dry-run over the saved pack files. Nothing is installed yet.
      </p>

      <div className="flex items-center gap-3 mb-4">
        <button
          onClick={runValidate}
          disabled={validate.isPending}
          className="px-4 py-2 bg-brand text-white rounded-md text-sm hover:bg-brand-dark disabled:opacity-50"
        >
          {validate.isPending ? 'Validating…' : 'Run validate-pack (strict)'}
        </button>
        <button onClick={() => setStep(2)} className="text-sm text-brand hover:underline">
          ← Back to draft
        </button>
      </div>

      {validation && (
        <div className="space-y-3">
          <div
            className={`px-4 py-3 rounded-md text-sm font-medium border ${
              validation.passed
                ? 'bg-green-50 border-green-200 text-green-800'
                : 'bg-red-50 border-red-200 text-red-800'
            }`}
          >
            {validation.passed
              ? '0 errors — ready for approval'
              : `${validation.errors.length} error${validation.errors.length === 1 ? '' : 's'} — fix the draft and re-run`}
          </div>

          {validation.errors.length > 0 && (
            <ul className="list-disc list-inside space-y-0.5">
              {validation.errors.map((err, i) => (
                <li key={i} className="text-sm text-red-700 break-words">
                  {err}
                </li>
              ))}
            </ul>
          )}

          {validation.warnings.length > 0 && (
            <div className="bg-amber-50 border border-amber-200 rounded-md px-4 py-3">
              <p className="text-sm font-medium text-amber-800 mb-1">
                {validation.warnings.length} warning{validation.warnings.length === 1 ? '' : 's'}:
              </p>
              <ul className="list-disc list-inside space-y-0.5">
                {validation.warnings.map((w, i) => (
                  <li key={i} className="text-sm text-amber-800 break-words">
                    {w}
                  </li>
                ))}
              </ul>
            </div>
          )}

          <details className="text-sm text-gray-600">
            <summary className="cursor-pointer">Raw validate-pack result</summary>
            <pre className="mt-1 text-xs bg-gray-50 border border-gray-200 rounded p-3 whitespace-pre-wrap break-words max-h-72 overflow-y-auto">
              {JSON.stringify(validate.data, null, 2)}
            </pre>
          </details>
        </div>
      )}

      <div className="mt-4">
        <button
          onClick={() => advanceTo(4)}
          disabled={!passedThisSession}
          title={passedThisSession ? undefined : 'Run a passing validation first'}
          className="px-4 py-2 bg-brand text-white rounded-md text-sm hover:bg-brand-dark disabled:opacity-50"
        >
          Continue to approve
        </button>
      </div>
    </Card>
  );

  // --- Step 4: approve + install -----------------------------------------------------

  const repoEntry = repos.data?.repos.find((r) => r.repo_root === target?.repo);
  const inAddSkillLane = repoEntry?.phase === 'add-skill';

  const handleInstall = () => {
    if (!target) return;
    const ok = window.confirm(
      `Approve and install pack "${target.pack}" into ${target.repo}?\n\nThis changes what gets composed into every future session in this repo.`,
    );
    if (!ok) return;
    install.mutate({
      repo: target.repo,
      pack: target.pack,
      approver: approver.trim() || undefined,
      allow_duplicates: allowDuplicates,
    });
  };

  const renderInstallResult = () => {
    const result = install.data;
    if (!result) return null;
    const approval = result.approval;
    const installOutcome = result.install ?? {};
    const dedupHard = Array.isArray(installOutcome.dedup_hard) ? installOutcome.dedup_hard : [];
    const dedupSoft = Array.isArray(installOutcome.dedup_soft) ? installOutcome.dedup_soft : [];
    const advancedPhase =
      approval?.advanced && typeof approval.advanced === 'object' && typeof approval.advanced.phase === 'string'
        ? approval.advanced.phase
        : null;
    return (
      <div className="space-y-3 mt-4">
        {approval && (
          <div
            className={`px-4 py-3 rounded-md text-sm border ${
              approval.ok === false
                ? 'bg-red-50 border-red-200 text-red-800'
                : 'bg-green-50 border-green-200 text-green-800'
            }`}
          >
            <p className="font-medium">
              Approval recorded for phase {fmt(approval.phase)}
              {approval.approver && <> by {approval.approver}</>}
              {advancedPhase && <> — phase advanced to {advancedPhase}</>}
            </p>
            {approval.marker && (
              <p className="mt-1 font-mono text-xs break-all">marker: {fmt(approval.marker)}</p>
            )}
          </div>
        )}

        <div className="bg-green-50 border border-green-200 rounded-md px-4 py-3 text-sm text-green-800">
          <p className="font-medium">
            Installed{installOutcome.action !== undefined && <> — action: {fmt(installOutcome.action)}</>}
            {typeof installOutcome.skills_ingested === 'number' && (
              <> · {installOutcome.skills_ingested} skill{installOutcome.skills_ingested === 1 ? '' : 's'} ingested</>
            )}
          </p>
        </div>

        {dedupHard.length > 0 && (
          <div className="bg-red-50 border border-red-200 rounded-md px-4 py-3">
            <p className="text-sm font-medium text-red-800 mb-1">
              Hard dedup matches ×{dedupHard.length} — near-duplicates of existing corpus skills
            </p>
            <pre className="text-xs text-red-800 whitespace-pre-wrap break-words max-h-48 overflow-y-auto">
              {JSON.stringify(dedupHard, null, 2)}
            </pre>
          </div>
        )}
        {dedupSoft.length > 0 && (
          <div className="bg-amber-50 border border-amber-200 rounded-md px-4 py-3">
            <p className="text-sm font-medium text-amber-800 mb-1">
              Soft dedup matches ×{dedupSoft.length} — similar to existing corpus skills
            </p>
            <pre className="text-xs text-amber-800 whitespace-pre-wrap break-words max-h-48 overflow-y-auto">
              {JSON.stringify(dedupSoft, null, 2)}
            </pre>
          </div>
        )}

        <details className="text-sm text-gray-600">
          <summary className="cursor-pointer">Raw install result</summary>
          <pre className="mt-1 text-xs bg-gray-50 border border-gray-200 rounded p-3 whitespace-pre-wrap break-words max-h-72 overflow-y-auto">
            {JSON.stringify(result, null, 2)}
          </pre>
        </details>
      </div>
    );
  };

  const renderApprove = () => (
    <Card className="max-w-3xl">
      <h2 className="text-lg font-semibold mb-4">Approve + install</h2>

      <div className="space-y-2 mb-4 text-sm">
        <div className="flex gap-2">
          <span className="w-20 shrink-0 text-gray-500">Repo</span>
          <span className="font-mono text-xs text-gray-800 break-all">{target?.repo ?? '—'}</span>
        </div>
        <div className="flex gap-2">
          <span className="w-20 shrink-0 text-gray-500">Pack</span>
          <span className="font-medium text-gray-800">{target?.pack ?? '—'}</span>
        </div>
        <div className="flex gap-2 items-start">
          <span className="w-20 shrink-0 text-gray-500">Files</span>
          <ChipRow items={files.map((f) => f.name)} tone="blue" max={6} />
        </div>
        {passedThisSession && (
          <div className="flex gap-2">
            <span className="w-20 shrink-0 text-gray-500">Validate</span>
            <Chip tone="green">0 errors</Chip>
          </div>
        )}
      </div>

      <div className="mb-4 bg-blue-50 border border-blue-200 rounded-md px-4 py-3 text-sm text-blue-800">
        Installing changes what gets composed into{' '}
        <span className="font-medium">every future session in this repo</span>.
        {inAddSkillLane && (
          <p className="mt-1">
            This repo is in the <span className="font-medium">add-skill lane</span> — installing
            records your approval marker and advances the phase back to intake.
          </p>
        )}
      </div>

      <div className="max-w-sm">
        <FormField
          label="Approver"
          hint="Optional — recorded on the sign-off marker; defaults to the service user."
        >
          <input
            value={approver}
            onChange={(e) => setApprover(e.target.value)}
            placeholder="$USER"
            className={inputClass}
          />
        </FormField>
      </div>

      <label className="flex items-start gap-2 mb-4 text-sm text-gray-700">
        <input
          type="checkbox"
          checked={allowDuplicates}
          onChange={(e) => setAllowDuplicates(e.target.checked)}
          className="mt-0.5 accent-brand"
        />
        <span>
          Allow duplicates{' '}
          <span className="text-xs text-gray-500">
            (downgrade a hard dedup match — not recommended)
          </span>
        </span>
      </label>

      {!passedThisSession && (
        <div className="mb-4 bg-amber-50 border border-amber-200 rounded-md px-4 py-3 text-sm text-amber-800">
          The pack changed since the last passing validation —{' '}
          <button onClick={() => setStep(3)} className="font-medium underline">
            re-run validate
          </button>{' '}
          before installing.
        </div>
      )}

      <button
        onClick={handleInstall}
        disabled={install.isPending || !target || !passedThisSession}
        title={passedThisSession ? undefined : 'Requires a passing validation'}
        className="px-6 py-3 bg-brand text-white rounded-md text-base font-medium hover:bg-brand-dark disabled:opacity-50"
      >
        {install.isPending ? 'Installing…' : 'Approve & install'}
      </button>

      {renderInstallResult()}
    </Card>
  );

  // --- Layout ----------------------------------------------------------------------

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold">New Skill</h1>
      <p className="text-sm text-gray-600">
        Create a custom skill pack on the same rails as the add-skill lane: scaffold → draft →
        validate → approve + install.
      </p>
      <Stepper step={step} maxStep={maxStep} onSelect={goTo} />
      {step === 1 && renderScaffold()}
      {step === 2 && renderDraft()}
      {step === 3 && renderValidate()}
      {step === 4 && renderApprove()}
    </div>
  );
}
