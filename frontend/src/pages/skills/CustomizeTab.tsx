import { useEffect, useMemo, useState } from 'react';
import { Card, ChipInput, EmptyState, ErrorState, PageSkeleton } from '../../components';
import { useDeleteOverride, useSaveOverride, useSkillOverride } from '../../hooks/useSkills';
import { ApiError, extractValidationErrors } from '../../lib/api';
import { diffLines } from '../../lib/diff';
import type { SkillOverride } from '../../lib/types';

interface FormState {
  prose: string;
  tags: string[];
}

function seedForm(override: SkillOverride): FormState {
  return {
    prose: override.raw_prose ?? override.shipped_raw_prose ?? '',
    tags: [...override.domain_tags],
  };
}

function InvariantChecklist({ invariants, prose }: { invariants: string[]; prose: string }) {
  if (invariants.length === 0) {
    return <p className="text-sm text-gray-500">No prose invariants for this skill.</p>;
  }
  return (
    <ul className="space-y-1.5">
      {invariants.map((invariant) => {
        const present = prose.includes(invariant);
        return (
          <li key={invariant} className="flex items-start gap-2">
            <span
              className={`mt-0.5 text-sm font-bold ${present ? 'text-green-600' : 'text-red-600'}`}
              title={present ? 'present in prose' : 'missing from prose'}
            >
              {present ? '✓' : '✗'}
            </span>
            <code className="text-xs text-gray-800 whitespace-pre-wrap break-words">
              {invariant}
            </code>
          </li>
        );
      })}
    </ul>
  );
}

const diffLineStyles = {
  same: 'text-gray-600',
  add: 'bg-green-50 text-green-800',
  del: 'bg-red-50 text-red-800',
} as const;

const diffPrefix = { same: ' ', add: '+', del: '-' } as const;

function DiffView({ shipped, current }: { shipped: string; current: string }) {
  const lines = useMemo(() => diffLines(shipped, current), [shipped, current]);
  const changed = lines.some((l) => l.type !== 'same');
  if (!changed) {
    return <p className="text-sm text-gray-500">No differences from the shipped default.</p>;
  }
  return (
    <pre className="text-xs font-mono border border-gray-200 rounded max-h-96 overflow-y-auto">
      {lines.map((l, i) => (
        <div key={i} className={`px-3 whitespace-pre-wrap break-words ${diffLineStyles[l.type]}`}>
          {diffPrefix[l.type]} {l.line}
        </div>
      ))}
    </pre>
  );
}

export function CustomizeTab({ skillId }: { skillId: string }) {
  const overrideQuery = useSkillOverride(skillId);
  const save = useSaveOverride(skillId);
  const remove = useDeleteOverride(skillId);

  const [form, setForm] = useState<FormState | null>(null);
  const [validationErrors, setValidationErrors] = useState<string[]>([]);

  const override = overrideQuery.data;

  // Seed the editor once when the override first loads; never clobber
  // in-progress edits on background refetches.
  useEffect(() => {
    if (override && form === null) setForm(seedForm(override));
  }, [override, form]);

  if (overrideQuery.isLoading) return <PageSkeleton />;
  if (overrideQuery.error) {
    if (overrideQuery.error instanceof ApiError && overrideQuery.error.status === 404) {
      return (
        <EmptyState
          title="This skill is not overridable"
          hint="The backend exposes no override surface for it."
          icon="🔒"
        />
      );
    }
    return (
      <ErrorState message={overrideQuery.error.message} onRetry={() => overrideQuery.refetch()} />
    );
  }
  if (!override || !form) return null;

  const isOverridden = override.active_layer !== 'default';
  const activePath = override.paths[override.active_layer];

  const handleSave = async () => {
    setValidationErrors([]);
    try {
      await save.mutateAsync({
        layer: 'profile',
        raw_prose: form.prose,
        domain_tags: form.tags,
      });
    } catch (err: unknown) {
      const errors = extractValidationErrors(err);
      if (errors) setValidationErrors(errors);
      // non-validation failures are toasted by the global mutation cache
    }
  };

  const handleReset = async () => {
    const ok = window.confirm(
      `Remove the ${override.active_layer} override for "${skillId}" and return to the shipped default?`,
    );
    if (!ok) return;
    try {
      await remove.mutateAsync('profile');
      const fresh = await overrideQuery.refetch();
      if (fresh.data) setForm(seedForm(fresh.data));
      setValidationErrors([]);
    } catch {
      // toasted by the global mutation cache
    }
  };

  return (
    <div className="space-y-6">
      <div
        className={`px-4 py-3 rounded-md text-sm border ${
          isOverridden
            ? 'bg-amber-50 border-amber-200 text-amber-800'
            : 'bg-gray-50 border-gray-200 text-gray-700'
        }`}
      >
        Active layer: <span className="font-semibold">{override.active_layer}</span>
        {' · '}profile: <span className="font-semibold">{override.active_profile}</span>
        {activePath && <span className="block mt-1 font-mono text-xs">{activePath}</span>}
      </div>

      <Card>
        <div className="flex justify-between items-center mb-4">
          <h2 className="text-lg font-semibold">Prose</h2>
          <div className="flex gap-2">
            {isOverridden && (
              <button
                onClick={handleReset}
                disabled={remove.isPending}
                className="px-4 py-2 bg-gray-100 text-gray-700 rounded-md text-sm hover:bg-gray-200 disabled:opacity-50"
              >
                {remove.isPending ? 'Resetting…' : 'Reset to default'}
              </button>
            )}
            <button
              onClick={handleSave}
              disabled={save.isPending}
              className="px-4 py-2 bg-brand text-white rounded-md text-sm hover:bg-brand-dark disabled:opacity-50"
            >
              {save.isPending ? 'Saving…' : 'Save'}
            </button>
          </div>
        </div>
        <textarea
          value={form.prose}
          onChange={(e) => setForm((prev) => (prev ? { ...prev, prose: e.target.value } : prev))}
          rows={18}
          spellCheck={false}
          className="w-full font-mono text-xs px-3 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-1 focus:ring-brand"
        />
        {validationErrors.length > 0 && (
          <div className="mt-2 bg-red-50 border border-red-200 rounded-md px-4 py-3">
            <p className="text-sm font-medium text-red-700 mb-1">Validation failed:</p>
            <ul className="list-disc list-inside space-y-0.5">
              {validationErrors.map((err) => (
                <li key={err} className="text-sm text-red-700">
                  {err}
                </li>
              ))}
            </ul>
          </div>
        )}
        <div className="mt-4">
          <h3 className="text-sm font-semibold text-gray-700 mb-2">Domain Tags</h3>
          <ChipInput
            values={form.tags}
            onChange={(tags) => setForm((prev) => (prev ? { ...prev, tags } : prev))}
          />
        </div>
      </Card>

      <Card>
        <h2 className="text-lg font-semibold mb-1">Prose Invariants</h2>
        <p className="text-xs text-gray-500 mb-3">
          Each invariant must appear verbatim in the prose — missing ones fail validation on save.
        </p>
        <InvariantChecklist invariants={override.prose_invariants} prose={form.prose} />
      </Card>

      <Card>
        <h2 className="text-lg font-semibold mb-1">Locked Fields</h2>
        <p className="text-xs text-gray-500 mb-3">🔒 product-owned — not customizable</p>
        {Object.keys(override.locked_fields).length === 0 ? (
          <p className="text-sm text-gray-500">No locked fields.</p>
        ) : (
          <pre className="text-xs text-gray-600 bg-gray-50 border border-gray-200 rounded p-3 whitespace-pre-wrap break-words max-h-64 overflow-y-auto select-text cursor-not-allowed">
            {JSON.stringify(override.locked_fields, null, 2)}
          </pre>
        )}
      </Card>

      <Card>
        <h2 className="text-lg font-semibold mb-1">Diff vs Shipped Default</h2>
        {override.shipped_raw_prose === null ? (
          <p className="text-sm text-gray-500">No shipped default prose to diff against.</p>
        ) : (
          <DiffView shipped={override.shipped_raw_prose} current={form.prose} />
        )}
      </Card>
    </div>
  );
}
