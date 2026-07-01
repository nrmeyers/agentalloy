import { Fragment, useState } from 'react';
import type { ReactNode } from 'react';
import type { UseQueryResult } from '@tanstack/react-query';
import { Card, EmptyState, ErrorState, StatusBadge, TableSkeleton } from '../../components';
import { fmt, fmtTs, truncate } from '../../lib/format';
import type { TraceRecord, TracesResponse } from '../../lib/types';

const COLS = 7;

function DetailItem({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div>
      <dt className="text-xs font-medium text-gray-500 uppercase">{label}</dt>
      <dd className="text-sm text-gray-900 break-all">{value}</dd>
    </div>
  );
}

function ChipList({ items, tone }: { items: string[] | null; tone: 'green' | 'red' | 'gray' }) {
  if (!items || items.length === 0) return <span className="text-sm text-gray-400">—</span>;
  const styles = {
    green: 'bg-green-100 text-green-800',
    red: 'bg-red-100 text-red-800',
    gray: 'bg-gray-100 text-gray-700',
  };
  return (
    <span className="flex flex-wrap gap-1">
      {items.map((item) => (
        <span key={item} className={`px-1.5 py-0.5 rounded text-xs ${styles[tone]}`}>
          {item}
        </span>
      ))}
    </span>
  );
}

function DetailSection({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div>
      <h3 className="text-sm font-semibold text-gray-700 mb-2">{title}</h3>
      <dl className="grid grid-cols-2 lg:grid-cols-4 gap-x-4 gap-y-2">{children}</dl>
    </div>
  );
}

function TraceDetail({ trace }: { trace: TraceRecord }) {
  return (
    <div className="space-y-4 bg-gray-50 p-4 rounded-md">
      <DetailSection title="Signal">
        <DetailItem label="Event Type" value={fmt(trace.event_type)} />
        <DetailItem label="Pre-filter Matched" value={fmt(trace.pre_filter_matched)} />
        <DetailItem label="Gates Met" value={<ChipList items={trace.gates_met} tone="green" />} />
        <DetailItem label="Gates Unmet" value={<ChipList items={trace.gates_unmet} tone="red" />} />
        <DetailItem label="Qwen Calls" value={fmt(trace.qwen_calls)} />
        <DetailItem label="Phase-gate Embed Failed" value={fmt(trace.phase_gate_embed_failed)} />
      </DetailSection>

      <DetailSection title="Retrieval">
        <DetailItem label="BM25 Source" value={fmt(trace.bm25_source)} />
        <DetailItem label="Reranked" value={fmt(trace.reranked)} />
        <DetailItem label="Dense Leg Degraded" value={fmt(trace.dense_leg_degraded)} />
        <DetailItem
          label="Selected Fragments"
          value={<ChipList items={trace.selected_fragment_ids} tone="gray" />}
        />
        <DetailItem
          label="Source Skills"
          value={<ChipList items={trace.source_skill_ids} tone="gray" />}
        />
        <DetailItem
          label="System Skills"
          value={<ChipList items={trace.system_skill_ids} tone="gray" />}
        />
        <DetailItem
          label="Workflow Skills"
          value={<ChipList items={trace.workflow_skill_ids} tone="gray" />}
        />
      </DetailSection>

      <DetailSection title="LM Assist">
        <DetailItem label="Outcome" value={fmt(trace.lm_assist_outcome)} />
        <DetailItem label="Model" value={fmt(trace.lm_assist_model)} />
        <DetailItem
          label="Kept IDs"
          value={<ChipList items={trace.lm_assist_kept_ids} tone="green" />}
        />
        <DetailItem
          label="Dropped IDs"
          value={<ChipList items={trace.lm_assist_dropped_ids} tone="red" />}
        />
        <DetailItem label="Scores" value={fmt(trace.lm_assist_scores)} />
      </DetailSection>

      <DetailSection title="Assembly">
        <DetailItem label="Tier" value={fmt(trace.assembly_tier)} />
        <DetailItem label="Model" value={fmt(trace.assembly_model)} />
        <DetailItem label="Retrieval Latency (ms)" value={fmt(trace.retrieval_latency_ms)} />
        <DetailItem label="Assembly Latency (ms)" value={fmt(trace.assembly_latency_ms)} />
        <DetailItem label="Total Latency (ms)" value={fmt(trace.total_latency_ms)} />
        <DetailItem label="Response Size (chars)" value={fmt(trace.response_size_chars)} />
        <DetailItem label="Tokens Returned" value={fmt(trace.tokens_returned)} />
        <DetailItem label="Tokens Flat Equivalent" value={fmt(trace.tokens_flat_equivalent)} />
        <DetailItem label="Prompt Version" value={fmt(trace.prompt_version)} />
      </DetailSection>

      <DetailSection title="Context">
        <DetailItem label="Repo" value={fmt(trace.repo)} />
        <DetailItem label="Session Key" value={fmt(trace.session_key)} />
        <DetailItem label="Session Source" value={fmt(trace.session_source)} />
        <DetailItem label="Contract Path" value={fmt(trace.contract_path)} />
        <DetailItem
          label="Contract Tags"
          value={<ChipList items={trace.contract_tags} tone="gray" />}
        />
        <DetailItem label="Category" value={fmt(trace.category)} />
        <DetailItem label="Correlation ID" value={fmt(trace.correlation_id)} />
        <DetailItem label="Error Code" value={fmt(trace.error_code)} />
      </DetailSection>

      {trace.task_prompt && (
        <div>
          <h3 className="text-sm font-semibold text-gray-700 mb-2">Task Prompt</h3>
          <pre className="text-xs text-gray-800 bg-white border border-gray-200 rounded p-3 whitespace-pre-wrap break-words max-h-64 overflow-y-auto">
            {trace.task_prompt}
          </pre>
        </div>
      )}
    </div>
  );
}

export function TracesTab({
  query,
  offset,
  limit,
  onPage,
}: {
  query: UseQueryResult<TracesResponse, Error>;
  offset: number;
  limit: number;
  onPage: (offset: number) => void;
}) {
  const { data, isLoading, error, refetch } = query;
  const [expanded, setExpanded] = useState<string | null>(null);

  if (isLoading) return <TableSkeleton />;
  if (error) return <ErrorState message={error.message} onRetry={() => refetch()} />;
  if (!data || data.traces.length === 0) {
    return (
      <EmptyState
        title="No traces recorded"
        hint="Traces will appear here once compositions have been recorded."
      />
    );
  }

  const from = offset + 1;
  const to = Math.min(offset + data.traces.length, data.total);

  return (
    <Card>
      <div className="overflow-x-auto">
        <table className="min-w-full divide-y divide-gray-200">
          <thead>
            <tr>
              {['Time', 'Phase', 'Status', 'Event', 'Prompt', 'Latency (ms)', 'Repo'].map((h) => (
                <th
                  key={h}
                  className="px-4 py-2 text-left text-xs font-medium text-gray-500 uppercase tracking-wide"
                >
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-200">
            {data.traces.map((trace) => (
              <Fragment key={trace.trace_id}>
                <tr
                  onClick={() => setExpanded(expanded === trace.trace_id ? null : trace.trace_id)}
                  className="cursor-pointer hover:bg-gray-50"
                >
                  <td className="px-4 py-2 text-sm text-gray-900 whitespace-nowrap">
                    {fmtTs(trace.request_ts)}
                  </td>
                  <td className="px-4 py-2 text-sm text-gray-900">{fmt(trace.phase)}</td>
                  <td className="px-4 py-2 text-sm">
                    <StatusBadge status={trace.status} />
                  </td>
                  <td className="px-4 py-2 text-sm text-gray-900">{fmt(trace.event_type)}</td>
                  <td className="px-4 py-2 text-sm text-gray-900">
                    {truncate(trace.task_prompt, 60)}
                  </td>
                  <td className="px-4 py-2 text-sm text-gray-900 text-right tabular-nums">
                    {fmt(trace.total_latency_ms)}
                  </td>
                  <td className="px-4 py-2 text-sm text-gray-900">{fmt(trace.repo)}</td>
                </tr>
                {expanded === trace.trace_id && (
                  <tr>
                    <td colSpan={COLS} className="px-4 py-3">
                      <TraceDetail trace={trace} />
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
          </tbody>
        </table>
      </div>

      <div className="flex items-center justify-between mt-4 text-sm text-gray-600">
        <span>
          Showing {from}–{to} of {data.total}
        </span>
        <div className="flex gap-2">
          <button
            onClick={() => onPage(Math.max(0, offset - limit))}
            disabled={offset === 0}
            className="px-3 py-1.5 bg-gray-100 rounded-md hover:bg-gray-200 disabled:opacity-50"
          >
            Previous
          </button>
          <button
            onClick={() => onPage(offset + limit)}
            disabled={offset + limit >= data.total}
            className="px-3 py-1.5 bg-gray-100 rounded-md hover:bg-gray-200 disabled:opacity-50"
          >
            Next
          </button>
        </div>
      </div>
    </Card>
  );
}
