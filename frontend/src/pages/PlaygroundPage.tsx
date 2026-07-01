import { useMemo, useState } from 'react';
import { Card, ChipInput, EmptyState, ErrorState, FormField, inputClass } from '../components';
import { useCompose, useEvaluateSignal, useRetrieve } from '../hooks/usePlayground';
import { useSkillsList } from '../hooks/useSkills';
import { fmt } from '../lib/format';
import type { ComposeResponse, RetrieveRequest, RetrieveResponse, SignalVerdict } from '../lib/types';
import { Chip } from './skills/shared';

const KNOWN_COMPOSE_KEYS = new Set([
  'status',
  'result_type',
  'output',
  'source_skills',
  'latency_ms',
  'dense_leg_degraded',
]);

// --- Retrieval -----------------------------------------------------------------

function RetrievalResults({ data }: { data: RetrieveResponse }) {
  const [expanded, setExpanded] = useState<string | null>(null);
  if (!Array.isArray(data.results) || data.results.length === 0) {
    return <EmptyState title="No results" hint="Nothing matched this task." icon="🔎" />;
  }
  return (
    <div className="divide-y divide-gray-200 mt-4">
      {data.results.map((result, i) => {
        const isOpen = expanded === result.version_id;
        const pct = Math.max(0, Math.min(1, result.score)) * 100;
        return (
          <div key={result.version_id} className="py-2">
            <button
              type="button"
              onClick={() => setExpanded(isOpen ? null : result.version_id)}
              className="w-full flex items-center gap-3 text-left hover:bg-gray-50 rounded px-2 py-1"
            >
              <span className="text-sm font-semibold text-gray-400 w-6">#{i + 1}</span>
              <span className="text-sm font-medium text-gray-900 w-56 truncate">
                {result.canonical_name}
              </span>
              <span className="font-mono text-xs text-gray-500 w-48 truncate">
                {result.skill_id}
              </span>
              <span className="flex-1 h-2 bg-gray-100 rounded overflow-hidden">
                <span className="block h-full bg-brand" style={{ width: `${pct}%` }} />
              </span>
              <span className="text-xs tabular-nums text-gray-600 w-12 text-right">
                {result.score.toFixed(3)}
              </span>
            </button>
            {isOpen && (
              <pre className="mt-2 ml-10 text-xs text-gray-800 bg-gray-50 border border-gray-200 rounded p-3 whitespace-pre-wrap break-words max-h-72 overflow-y-auto">
                {result.raw_prose}
              </pre>
            )}
          </div>
        );
      })}
    </div>
  );
}

// --- Compose ---------------------------------------------------------------------

function ComposeResult({ data }: { data: ComposeResponse }) {
  const latency = data.latency_ms && typeof data.latency_ms === 'object' ? data.latency_ms : null;
  const extras = Object.fromEntries(
    Object.entries(data).filter(([key]) => !KNOWN_COMPOSE_KEYS.has(key)),
  );
  return (
    <div className="space-y-4 mt-4">
      <div className="flex flex-wrap items-center gap-2">
        {typeof data.status === 'string' && <Chip tone="gray">status: {data.status}</Chip>}
        {typeof data.result_type === 'string' && (
          <Chip tone="blue">result_type: {data.result_type}</Chip>
        )}
        {latency && (
          <>
            {latency.retrieval_ms !== undefined && (
              <Chip tone="gray">retrieval {fmt(latency.retrieval_ms)} ms</Chip>
            )}
            {latency.assembly_ms !== undefined && (
              <Chip tone="gray">assembly {fmt(latency.assembly_ms)} ms</Chip>
            )}
            {latency.total_ms !== undefined && (
              <Chip tone="gray">total {fmt(latency.total_ms)} ms</Chip>
            )}
          </>
        )}
      </div>

      {data.dense_leg_degraded === true && (
        <div className="bg-amber-50 border border-amber-200 text-amber-800 px-4 py-2 rounded-md text-sm">
          ⚠ Dense retrieval leg degraded — results came from a reduced retrieval path.
        </div>
      )}

      {typeof data.output === 'string' ? (
        <div>
          <div className="text-xs font-medium text-gray-500 uppercase mb-1">Injected block</div>
          <pre className="text-xs text-gray-800 bg-gray-50 border-2 border-gray-300 rounded p-3 whitespace-pre-wrap break-words max-h-96 overflow-y-auto">
            {data.output}
          </pre>
        </div>
      ) : (
        <p className="text-sm text-gray-500">No output block in the response.</p>
      )}

      {Array.isArray(data.source_skills) && data.source_skills.length > 0 && (
        <div>
          <div className="text-xs font-medium text-gray-500 uppercase mb-1">Source skills</div>
          <div className="flex flex-wrap gap-1">
            {data.source_skills.map((id) => (
              <Chip key={String(id)} tone="blue">
                {String(id)}
              </Chip>
            ))}
          </div>
        </div>
      )}

      {Object.keys(extras).length > 0 && (
        <details className="text-sm text-gray-600">
          <summary className="cursor-pointer">Other response fields</summary>
          <pre className="mt-1 text-xs bg-gray-50 border border-gray-200 rounded p-3 whitespace-pre-wrap break-words max-h-64 overflow-y-auto">
            {JSON.stringify(extras, null, 2)}
          </pre>
        </details>
      )}
    </div>
  );
}

// --- Signal simulator ---------------------------------------------------------------

function VerdictPanel({ verdict }: { verdict: SignalVerdict }) {
  return (
    <div className="space-y-4 mt-4">
      <div className="flex items-center gap-3">
        <span
          className={`inline-flex items-center px-3 py-1.5 rounded-md text-sm font-bold ${
            verdict.should_compose ? 'bg-green-100 text-green-800' : 'bg-gray-200 text-gray-700'
          }`}
        >
          {verdict.should_compose ? 'WOULD COMPOSE' : 'PASSTHROUGH'}
        </span>
        {verdict.would_announce && <Chip tone="blue">would announce</Chip>}
        {verdict.phase_gate_embed_failed && <Chip tone="red">phase-gate embed failed</Chip>}
      </div>

      <dl className="grid grid-cols-2 lg:grid-cols-4 gap-x-4 gap-y-2">
        <div>
          <dt className="text-xs font-medium text-gray-500 uppercase">Phase</dt>
          <dd className="text-sm text-gray-900">{fmt(verdict.phase)}</dd>
        </div>
        <div>
          <dt className="text-xs font-medium text-gray-500 uppercase">Pre-filter matched</dt>
          <dd className="text-sm text-gray-900">{fmt(verdict.pre_filter_matched)}</dd>
        </div>
        <div>
          <dt className="text-xs font-medium text-gray-500 uppercase">Qwen calls</dt>
          <dd className="text-sm text-gray-900 tabular-nums">{fmt(verdict.qwen_calls)}</dd>
        </div>
        <div>
          <dt className="text-xs font-medium text-gray-500 uppercase">Workflow skill</dt>
          <dd className="text-sm text-gray-900 break-all">{fmt(verdict.workflow_skill_id)}</dd>
        </div>
        <div className="col-span-2">
          <dt className="text-xs font-medium text-gray-500 uppercase">Task</dt>
          <dd className="text-sm text-gray-900 break-words">{fmt(verdict.task)}</dd>
        </div>
        <div className="col-span-2">
          <dt className="text-xs font-medium text-gray-500 uppercase">Domain tags</dt>
          <dd className="text-sm text-gray-900">{fmt(verdict.domain_tags)}</dd>
        </div>
      </dl>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div>
          <div className="text-xs font-medium text-gray-500 uppercase mb-1">Gates met</div>
          <div className="flex flex-wrap gap-1">
            {verdict.gates_met.length === 0 ? (
              <span className="text-sm text-gray-400">—</span>
            ) : (
              verdict.gates_met.map((g) => (
                <Chip key={g} tone="green">
                  {g}
                </Chip>
              ))
            )}
          </div>
        </div>
        <div>
          <div className="text-xs font-medium text-gray-500 uppercase mb-1">Gates unmet</div>
          <div className="flex flex-wrap gap-1">
            {verdict.gates_unmet.length === 0 ? (
              <span className="text-sm text-gray-400">—</span>
            ) : (
              verdict.gates_unmet.map((g) => (
                <Chip key={g} tone="red">
                  {g}
                </Chip>
              ))
            )}
          </div>
        </div>
      </div>

      {verdict.advisories.length > 0 && (
        <div>
          <div className="text-xs font-medium text-gray-500 uppercase mb-1">Advisories</div>
          <ul className="list-disc list-inside space-y-0.5">
            {verdict.advisories.map((a) => (
              <li key={a} className="text-sm text-gray-700">
                {a}
              </li>
            ))}
          </ul>
        </div>
      )}

      {verdict.banner && (
        <div className="bg-blue-50 border border-blue-200 text-blue-800 px-4 py-2 rounded-md text-sm whitespace-pre-wrap">
          {verdict.banner}
        </div>
      )}
    </div>
  );
}

// --- Page ---------------------------------------------------------------------------

export function PlaygroundPage() {
  // Shared between the Retrieval and Compose sections.
  const [task, setTask] = useState('');
  const [phase, setPhase] = useState('');
  const [tags, setTags] = useState<string[]>([]);
  const [k, setK] = useState('4');

  const [repo, setRepo] = useState('');
  const [prompt, setPrompt] = useState('');

  const retrieve = useRetrieve();
  const compose = useCompose();
  const signal = useEvaluateSignal();

  // Phase suggestions derived from live skills data — never hardcoded.
  const { data: skillsData } = useSkillsList({});
  const phaseOptions = useMemo(() => {
    const set = new Set<string>();
    skillsData?.skills?.forEach((s) => s.phase_scope?.forEach((p) => p && set.add(p)));
    return [...set].sort();
  }, [skillsData]);

  const buildRequest = (): RetrieveRequest => {
    const kNum = Number(k);
    return {
      task: task.trim(),
      phase: phase.trim() || undefined,
      domain_tags: tags.length > 0 ? tags : undefined,
      k: Number.isInteger(kNum) && kNum >= 1 && kNum <= 8 ? kNum : undefined,
    };
  };

  const taskValid = task.trim() !== '';
  const signalValid = repo.trim() !== '' && prompt.trim() !== '';

  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold">Playground</h1>

      <Card>
        <h2 className="text-lg font-semibold mb-4">Retrieval</h2>
        <FormField label="Task">
          <textarea
            value={task}
            onChange={(e) => setTask(e.target.value)}
            rows={3}
            placeholder="Describe the task to retrieve skills for…"
            className={inputClass}
          />
        </FormField>
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
          <FormField label="Phase" hint="Optional — suggestions from skill phase scopes">
            <input
              value={phase}
              onChange={(e) => setPhase(e.target.value)}
              list="playground-phases"
              placeholder="e.g. implement"
              className={inputClass}
            />
            <datalist id="playground-phases">
              {phaseOptions.map((p) => (
                <option key={p} value={p} />
              ))}
            </datalist>
          </FormField>
          <FormField label="Domain Tags" hint="Optional">
            <ChipInput values={tags} onChange={setTags} />
          </FormField>
          <FormField label="k" hint="1–8">
            <input
              type="number"
              min={1}
              max={8}
              value={k}
              onChange={(e) => setK(e.target.value)}
              className={inputClass}
            />
          </FormField>
        </div>
        <button
          onClick={() => retrieve.mutate(buildRequest())}
          disabled={!taskValid || retrieve.isPending}
          className="px-4 py-2 bg-brand text-white rounded-md text-sm hover:bg-brand-dark disabled:opacity-50"
        >
          {retrieve.isPending ? 'Retrieving…' : 'Retrieve'}
        </button>
        {retrieve.error && (
          <ErrorState
            message={retrieve.error.message}
            onRetry={() => retrieve.mutate(buildRequest())}
          />
        )}
        {retrieve.data && <RetrievalResults data={retrieve.data} />}
      </Card>

      <Card>
        <h2 className="text-lg font-semibold mb-1">Compose Preview</h2>
        <p className="text-xs text-gray-500 mb-3">
          Uses the same task / phase / tags / k inputs as Retrieval above.
        </p>
        <button
          onClick={() => compose.mutate(buildRequest())}
          disabled={!taskValid || compose.isPending}
          className="px-4 py-2 bg-brand text-white rounded-md text-sm hover:bg-brand-dark disabled:opacity-50"
        >
          {compose.isPending ? 'Composing…' : 'Compose'}
        </button>
        {compose.error && (
          <ErrorState
            message={compose.error.message}
            onRetry={() => compose.mutate(buildRequest())}
          />
        )}
        {compose.data && <ComposeResult data={compose.data} />}
      </Card>

      <Card>
        <h2 className="text-lg font-semibold mb-4">Signal Simulator</h2>
        <FormField label="Repo Path">
          <input
            value={repo}
            onChange={(e) => setRepo(e.target.value)}
            placeholder="/absolute/path/to/repo"
            className={inputClass}
          />
        </FormField>
        <FormField label="Prompt">
          <textarea
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            rows={4}
            placeholder="The user prompt to evaluate…"
            className={inputClass}
          />
        </FormField>
        <button
          onClick={() => signal.mutate({ repo: repo.trim(), prompt })}
          disabled={!signalValid || signal.isPending}
          className="px-4 py-2 bg-brand text-white rounded-md text-sm hover:bg-brand-dark disabled:opacity-50"
        >
          {signal.isPending ? 'Evaluating…' : 'Evaluate'}
        </button>
        <p className="mt-2 text-xs text-gray-500">read-only — never advances repo state</p>
        {signal.error && (
          <ErrorState
            message={signal.error.message}
            onRetry={() => signal.mutate({ repo: repo.trim(), prompt })}
          />
        )}
        {signal.data && <VerdictPanel verdict={signal.data} />}
      </Card>
    </div>
  );
}
