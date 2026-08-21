import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { getUpstream, updateUpstream } from '../lib/api';
import type { UpstreamConfig, UpstreamUpdate } from '../lib/types';
import { showToast } from '../components/Toast';

export interface UpdateUpstreamVars {
  repoRoot: string;
  body: UpstreamUpdate;
}

/**
 * A repo's active chat upstream. Keyed per repo so switching the dropdown
 * refetches; `enabled=false` keeps it idle until a repo is selected.
 */
export function useUpstream(repoRoot: string | undefined, enabled = true) {
  return useQuery<UpstreamConfig>({
    queryKey: ['upstream', repoRoot],
    queryFn: () => getUpstream(repoRoot),
    enabled,
    staleTime: 10_000,
  });
}

export function useUpdateUpstream() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ repoRoot, body }: UpdateUpstreamVars) => updateUpstream(repoRoot, body),
    onSuccess: (_result, { repoRoot }) => {
      queryClient.invalidateQueries({ queryKey: ['upstream', repoRoot] });
      showToast('Upstream saved', 'success');
    },
  });
}
