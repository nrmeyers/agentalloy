import { useEffect, useState } from 'react';
import { useConfig, useReloadConfig, useUpdateConfig } from '../hooks/useConfig';
import { useRepos } from '../hooks/useRepos';
import { useUpstream, useUpdateUpstream } from '../hooks/useUpstream';
import {
  Card,
  ErrorState,
  FormField,
  PageSkeleton,
  Slider,
  inputClass,
  readOnlyInputClass,
} from '../components';
import type { ConfigData, ConfigUpdate } from '../lib/types';

const LOG_LEVELS = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'];

interface FormState {
  upstream_url: string;
  upstream_model: string;
  upstream_api_key: string; // replacement value only; empty = keep current
  anthropic_upstream_url: string;
  runtime_embed_base_url: string;
  runtime_embedding_model: string;
  embedding_provider: string;
  log_level: string;
  dedup_hard_threshold: number;
  dedup_soft_threshold: number;
  sdd_fast_require_approval: boolean;
  profile_root: string;
  forced_profile: string;
}

function seedForm(config: ConfigData): FormState {
  return {
    upstream_url: config.upstream_url ?? '',
    upstream_model: config.upstream_model ?? '',
    upstream_api_key: '',
    anthropic_upstream_url: config.anthropic_upstream_url,
    runtime_embed_base_url: config.runtime_embed_base_url,
    runtime_embedding_model: config.runtime_embedding_model,
    embedding_provider: config.embedding_provider,
    log_level: config.log_level,
    dedup_hard_threshold: config.dedup_hard_threshold,
    dedup_soft_threshold: config.dedup_soft_threshold,
    sdd_fast_require_approval: config.sdd_fast_require_approval,
    profile_root: config.profile_root,
    forced_profile: config.forced_profile ?? '',
  };
}

/** Empty string means "unset" for nullable fields. */
function nullable(value: string): string | null {
  const trimmed = value.trim();
  return trimmed === '' ? null : trimmed;
}

function buildPartial(config: ConfigData, form: FormState): ConfigUpdate {
  const partial: ConfigUpdate = {};
  if (nullable(form.upstream_url) !== config.upstream_url) {
    partial.upstream_url = nullable(form.upstream_url);
  }
  if (nullable(form.upstream_model) !== config.upstream_model) {
    partial.upstream_model = nullable(form.upstream_model);
  }
  if (form.upstream_api_key.trim() !== '') {
    partial.upstream_api_key = form.upstream_api_key.trim();
  }
  if (form.anthropic_upstream_url !== config.anthropic_upstream_url) {
    partial.anthropic_upstream_url = form.anthropic_upstream_url;
  }
  if (form.runtime_embed_base_url !== config.runtime_embed_base_url) {
    partial.runtime_embed_base_url = form.runtime_embed_base_url;
  }
  if (form.runtime_embedding_model !== config.runtime_embedding_model) {
    partial.runtime_embedding_model = form.runtime_embedding_model;
  }
  if (form.embedding_provider !== config.embedding_provider) {
    partial.embedding_provider = form.embedding_provider;
  }
  if (form.log_level !== config.log_level) {
    partial.log_level = form.log_level;
  }
  if (form.dedup_hard_threshold !== config.dedup_hard_threshold) {
    partial.dedup_hard_threshold = form.dedup_hard_threshold;
  }
  if (form.dedup_soft_threshold !== config.dedup_soft_threshold) {
    partial.dedup_soft_threshold = form.dedup_soft_threshold;
  }
  if (form.sdd_fast_require_approval !== config.sdd_fast_require_approval) {
    partial.sdd_fast_require_approval = form.sdd_fast_require_approval;
  }
  if (form.profile_root !== config.profile_root) {
    partial.profile_root = form.profile_root;
  }
  if (nullable(form.forced_profile) !== config.forced_profile) {
    partial.forced_profile = nullable(form.forced_profile);
  }
  return partial;
}

function TextInput({
  value,
  onChange,
  type = 'text',
  placeholder,
}: {
  value: string;
  onChange: (value: string) => void;
  type?: string;
  placeholder?: string;
}) {
  return (
    <input
      type={type}
      value={value}
      placeholder={placeholder}
      onChange={(e) => onChange(e.target.value)}
      className={inputClass}
    />
  );
}

function ReadOnlyField({ label, value }: { label: string; value: string }) {
  return (
    <FormField label={label}>
      <input value={value} readOnly className={readOnlyInputClass} />
    </FormField>
  );
}

/**
 * Per-repo chat upstream editor (the active entry of <repo>/.agentalloy/upstream).
 * Distinct from the global upstream fields above, which edit the user-scoped .env.
 * Repo selection is owned by the parent so the card can be reset on switch.
 */
function UpstreamCard({
  selectedRepo,
  onSelectRepo,
}: {
  selectedRepo: string | undefined;
  onSelectRepo: (repo: string | undefined) => void;
}) {
  const { data: repos } = useRepos();
  const { data: upstream } = useUpstream(selectedRepo, selectedRepo !== undefined);
  const update = useUpdateUpstream();

  const [form, setForm] = useState({ url: '', model: '', key_env: '' });
  const [error, setError] = useState<string | null>(null);

  // Re-seed the form whenever a different repo is selected or its upstream
  // (re)loads; never clobber in-progress edits on a background refetch.
  useEffect(() => {
    if (upstream) {
      setForm({
        url: upstream.url ?? '',
        model: upstream.model ?? '',
        key_env: upstream.key_env ?? '',
      });
      setError(null);
    }
  }, [selectedRepo, upstream]);

  const set = (key: keyof typeof form, value: string) => {
    setForm((prev) => ({ ...prev, [key]: value }));
    setError(null);
  };

  const handleSave = async () => {
    if (!selectedRepo) return;
    try {
      await update.mutateAsync({
        repoRoot: selectedRepo,
        body: { url: form.url.trim(), model: form.model.trim(), key_env: form.key_env.trim() || null },
      });
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Save failed');
    }
  };

  return (
    <Card>
      <h2 className="text-lg font-semibold mb-4">Per-Repo Upstream</h2>
      <FormField label="Repo" hint="The .agentalloy/upstream file is read from this repo's root">
        <select
          value={selectedRepo ?? ''}
          onChange={(e) => onSelectRepo(e.target.value || undefined)}
          className={inputClass}
        >
          <option value="">Select a repo…</option>
          {(repos?.repos ?? []).map((r) => (
            <option key={r.repo_root} value={r.repo_root}>
              {r.repo_root}
            </option>
          ))}
        </select>
      </FormField>

      {!selectedRepo ? (
        <p className="text-sm text-[var(--text-tertiary)]">
          Pick a repo above to view and edit its active chat upstream.
        </p>
      ) : upstream?.exists === false ? (
        <p className="text-sm text-[var(--text-tertiary)]">
          This repo has no .agentalloy/upstream file yet, so there is no active chat entry to edit.
        </p>
      ) : upstream?.detail ? (
        <div className="bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded-md text-sm">
          {upstream.detail}
        </div>
      ) : (
        <>
          {upstream?.harness && (
            <div className="mb-4 text-sm text-[var(--text-secondary)]">
              Active harness: <span className="font-mono">{upstream.harness}</span>
            </div>
          )}
          <FormField label="URL">
            <TextInput value={form.url} onChange={(v) => set('url', v)} />
          </FormField>
          <FormField label="Model">
            <TextInput value={form.model} onChange={(v) => set('model', v)} />
          </FormField>
          <FormField label="Key Env Var" hint="Name of the env var holding the API key">
            <TextInput value={form.key_env} onChange={(v) => set('key_env', v)} />
          </FormField>
          {error && (
            <div className="bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded-md text-sm">
              {error}
            </div>
          )}
          <div className="flex justify-end">
            <button
              onClick={handleSave}
              disabled={update.isPending}
              className="px-4 py-2 bg-brand text-white rounded-md text-sm hover:bg-brand-dark disabled:opacity-50"
            >
              {update.isPending ? 'Saving…' : 'Save Upstream'}
            </button>
          </div>
        </>
      )}
    </Card>
  );
}

export function ConfigPage() {
  const { data: config, isLoading, error, refetch } = useConfig();
  const update = useUpdateConfig();
  const reload = useReloadConfig();

  const [form, setForm] = useState<FormState | null>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [showKey, setShowKey] = useState(false);
  const [selectedRepo, setSelectedRepo] = useState<string | undefined>(undefined);

  // Seed the form once when config first loads; never clobber in-progress edits
  // on background refetches.
  useEffect(() => {
    if (config && form === null) {
      setForm(seedForm(config));
    }
  }, [config, form]);

  if (isLoading) return <PageSkeleton />;
  if (error) return <ErrorState message={error.message} onRetry={() => refetch()} />;
  if (!config || !form) return null;

  const set = <K extends keyof FormState>(key: K, value: FormState[K]) => {
    setForm((prev) => (prev ? { ...prev, [key]: value } : prev));
    setErrors((prev) => {
      const next = { ...prev };
      delete next[key];
      delete next._form;
      return next;
    });
  };

  const validate = (): Record<string, string> => {
    const errs: Record<string, string> = {};
    if (form.dedup_hard_threshold < 0.5 || form.dedup_hard_threshold > 1.0) {
      errs.dedup_hard_threshold = 'Must be between 0.50 and 1.00';
    }
    if (form.dedup_soft_threshold < 0.3 || form.dedup_soft_threshold > 0.9) {
      errs.dedup_soft_threshold = 'Must be between 0.30 and 0.90';
    }
    return errs;
  };

  const handleSave = async () => {
    const errs = validate();
    if (Object.keys(errs).length > 0) {
      setErrors(errs);
      return;
    }
    const partial = buildPartial(config, form);
    if (Object.keys(partial).length === 0) {
      setErrors({ _form: 'No changes to save.' });
      return;
    }
    try {
      await update.mutateAsync(partial);
      setErrors({});
      setForm((prev) => (prev ? { ...prev, upstream_api_key: '' } : prev));
    } catch (err: unknown) {
      setErrors({ _form: err instanceof Error ? err.message : 'Save failed' });
    }
  };

  const keySet = config.upstream_api_key === '***';

  return (
    <div className="space-y-6 max-w-3xl">
      <div className="flex justify-between items-center">
        <h1 className="text-2xl font-bold">Configuration</h1>
        <div className="flex gap-2">
          <button
            onClick={() => reload.mutate()}
            disabled={reload.isPending}
            className="px-4 py-2 bg-[var(--bg-tertiary)] text-[var(--text-secondary)] rounded-md text-sm hover:bg-[var(--border-primary)] disabled:opacity-50"
          >
            {reload.isPending ? 'Reloading…' : 'Reload'}
          </button>
          <button
            onClick={handleSave}
            disabled={update.isPending}
            className="px-4 py-2 bg-brand text-white rounded-md text-sm hover:bg-brand-dark disabled:opacity-50"
          >
            {update.isPending ? 'Saving…' : 'Save'}
          </button>
        </div>
      </div>

      {errors._form && (
        <div className="bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded-md text-sm">
          {errors._form}
        </div>
      )}

      <Card>
        <h2 className="text-lg font-semibold mb-4">Upstream LLM</h2>
        <FormField label="Upstream URL" hint="Empty = unset">
          <TextInput value={form.upstream_url} onChange={(v) => set('upstream_url', v)} />
        </FormField>
        <FormField label="Upstream Model" hint="Empty = unset">
          <TextInput value={form.upstream_model} onChange={(v) => set('upstream_model', v)} />
        </FormField>
        <FormField
          label="Upstream API Key"
          hint={keySet ? 'A key is currently set. Enter a new value to replace it.' : 'No key set.'}
        >
          <div className="flex gap-2">
            <input
              type={showKey ? 'text' : 'password'}
              value={form.upstream_api_key}
              placeholder={keySet ? '***' : 'not set'}
              onChange={(e) => set('upstream_api_key', e.target.value)}
              autoComplete="new-password"
              className={inputClass}
            />
            <button
              type="button"
              onClick={() => setShowKey((s) => !s)}
              className="px-3 py-2 bg-[var(--bg-tertiary)] text-[var(--text-secondary)] rounded-md text-sm hover:bg-[var(--border-primary)]"
            >
              {showKey ? 'Hide' : 'Show'}
            </button>
          </div>
        </FormField>
        <FormField label="Anthropic Upstream URL" hint="For native Anthropic passthrough">
          <TextInput
            value={form.anthropic_upstream_url}
            onChange={(v) => set('anthropic_upstream_url', v)}
          />
        </FormField>
      </Card>

      <UpstreamCard key={selectedRepo ?? 'none'} selectedRepo={selectedRepo} onSelectRepo={setSelectedRepo} />

      <Card>
        <h2 className="text-lg font-semibold mb-4">Embedding</h2>
        <FormField label="Embed Base URL">
          <TextInput
            value={form.runtime_embed_base_url}
            onChange={(v) => set('runtime_embed_base_url', v)}
          />
        </FormField>
        <FormField label="Embedding Model">
          <TextInput
            value={form.runtime_embedding_model}
            onChange={(v) => set('runtime_embedding_model', v)}
          />
        </FormField>
        <FormField label="Embedding Provider">
          <TextInput
            value={form.embedding_provider}
            onChange={(v) => set('embedding_provider', v)}
          />
        </FormField>
      </Card>

      <Card>
        <h2 className="text-lg font-semibold mb-4">Runtime</h2>
        <FormField label="Log Level" error={errors.log_level}>
          <select
            value={form.log_level}
            onChange={(e) => set('log_level', e.target.value)}
            className={inputClass}
          >
            {!LOG_LEVELS.includes(form.log_level) && (
              <option value={form.log_level}>{form.log_level}</option>
            )}
            {LOG_LEVELS.map((lvl) => (
              <option key={lvl} value={lvl}>
                {lvl}
              </option>
            ))}
          </select>
        </FormField>
        <FormField
          label="Dedup Hard Threshold"
          hint="Range: 0.50–1.00"
          error={errors.dedup_hard_threshold}
        >
          <Slider
            value={form.dedup_hard_threshold}
            min={0.5}
            max={1.0}
            step={0.01}
            onChange={(v) => set('dedup_hard_threshold', v)}
          />
        </FormField>
        <FormField
          label="Dedup Soft Threshold"
          hint="Range: 0.30–0.90"
          error={errors.dedup_soft_threshold}
        >
          <Slider
            value={form.dedup_soft_threshold}
            min={0.3}
            max={0.9}
            step={0.01}
            onChange={(v) => set('dedup_soft_threshold', v)}
          />
        </FormField>
        <FormField label="SDD Fast Require Approval">
          <label className="flex items-center gap-2 text-sm text-[var(--text-secondary)]">
            <input
              type="checkbox"
              checked={form.sdd_fast_require_approval}
              onChange={(e) => set('sdd_fast_require_approval', e.target.checked)}
              className="h-4 w-4 accent-brand-500"
            />
            Require approval for the sdd-fast phase
          </label>
        </FormField>
      </Card>

      <Card>
        <h2 className="text-lg font-semibold mb-4">Profile</h2>
        <FormField label="Profile Root">
          <TextInput value={form.profile_root} onChange={(v) => set('profile_root', v)} />
        </FormField>
        <FormField label="Forced Profile" hint="Empty = auto-resolve">
          <TextInput value={form.forced_profile} onChange={(v) => set('forced_profile', v)} />
        </FormField>
      </Card>

      <Card>
        <h2 className="text-lg font-semibold mb-4">Paths (read-only)</h2>
        <ReadOnlyField label="DuckDB Path" value={config.duckdb_path} />
        <ReadOnlyField label="Fragments Lance Path" value={config.fragments_lance_path} />
        <ReadOnlyField label="Telemetry DB Path" value={config.telemetry_db_path} />
        <ReadOnlyField label="Env File Path" value={config.env_file_path} />
      </Card>

      <div className="text-xs text-[var(--text-tertiary)]">
        Changes take effect after clicking "Save". Run "Reload" to apply without restart. Env file:{' '}
        {config.env_file_path}
      </div>
    </div>
  );
}
