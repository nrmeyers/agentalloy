import { useEffect, useState } from 'react';
import { useConfig, useReloadConfig, useUpdateConfig } from '../hooks/useConfig';
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
  bounce_budget: string;
  sdd_fast_require_approval: boolean;
  profile_root: string;
  forced_profile: string;
  authoring_model: string;
  authoring_critic_model: string;
  authoring_lm_base_url: string;
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
    bounce_budget: String(config.bounce_budget),
    sdd_fast_require_approval: config.sdd_fast_require_approval,
    profile_root: config.profile_root,
    forced_profile: config.forced_profile ?? '',
    authoring_model: config.authoring_model,
    authoring_critic_model: config.authoring_critic_model,
    authoring_lm_base_url: config.authoring_lm_base_url,
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
  const bounce = Number(form.bounce_budget);
  if (bounce !== config.bounce_budget) {
    partial.bounce_budget = bounce;
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
  if (form.authoring_model !== config.authoring_model) {
    partial.authoring_model = form.authoring_model;
  }
  if (form.authoring_critic_model !== config.authoring_critic_model) {
    partial.authoring_critic_model = form.authoring_critic_model;
  }
  if (form.authoring_lm_base_url !== config.authoring_lm_base_url) {
    partial.authoring_lm_base_url = form.authoring_lm_base_url;
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

export function ConfigPage() {
  const { data: config, isLoading, error, refetch } = useConfig();
  const update = useUpdateConfig();
  const reload = useReloadConfig();

  const [form, setForm] = useState<FormState | null>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [showKey, setShowKey] = useState(false);

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
    const bounce = Number(form.bounce_budget);
    if (!Number.isInteger(bounce) || bounce < 1 || bounce > 10) {
      errs.bounce_budget = 'Must be an integer between 1 and 10';
    }
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
            className="px-4 py-2 bg-gray-100 text-gray-700 rounded-md text-sm hover:bg-gray-200 disabled:opacity-50"
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
              className="px-3 py-2 bg-gray-100 text-gray-600 rounded-md text-sm hover:bg-gray-200"
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
        <FormField label="Bounce Budget" hint="Min: 1, Max: 10" error={errors.bounce_budget}>
          <input
            type="number"
            min={1}
            max={10}
            value={form.bounce_budget}
            onChange={(e) => set('bounce_budget', e.target.value)}
            className={inputClass}
          />
        </FormField>
        <FormField label="SDD Fast Require Approval">
          <label className="flex items-center gap-2 text-sm text-gray-700">
            <input
              type="checkbox"
              checked={form.sdd_fast_require_approval}
              onChange={(e) => set('sdd_fast_require_approval', e.target.checked)}
              className="h-4 w-4 accent-brand"
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
        <h2 className="text-lg font-semibold mb-4">Authoring</h2>
        <FormField label="Model">
          <TextInput value={form.authoring_model} onChange={(v) => set('authoring_model', v)} />
        </FormField>
        <FormField label="Critic Model">
          <TextInput
            value={form.authoring_critic_model}
            onChange={(v) => set('authoring_critic_model', v)}
          />
        </FormField>
        <FormField label="LM Base URL">
          <TextInput
            value={form.authoring_lm_base_url}
            onChange={(v) => set('authoring_lm_base_url', v)}
          />
        </FormField>
      </Card>

      <Card>
        <h2 className="text-lg font-semibold mb-4">Paths (read-only)</h2>
        <ReadOnlyField label="DuckDB Path" value={config.duckdb_path} />
        <ReadOnlyField label="Fragments Lance Path" value={config.fragments_lance_path} />
        <ReadOnlyField label="Telemetry DB Path" value={config.telemetry_db_path} />
        <ReadOnlyField label="Env File Path" value={config.env_file_path} />
      </Card>

      <div className="text-xs text-gray-500">
        Changes take effect after clicking "Save". Run "Reload" to apply without restart. Env file:{' '}
        {config.env_file_path}
      </div>
    </div>
  );
}
