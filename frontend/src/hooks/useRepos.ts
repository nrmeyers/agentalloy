import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { getApprovals, getRepoGates, getRepos, postApprove } from '../lib/api';
import type { ApprovalsResponse, ApproveRequest, RepoGates, ReposResponse } from '../lib/types';
import { showToast } from '../components/Toast';

export function useRepos() {
  return useQuery<ReposResponse>({
    queryKey: ['repos'],
    queryFn: getRepos,
    staleTime: 10_000,
  });
}

/** Lazy gate status — only fetched once the "Gate status" section is expanded. */
export function useRepoGates(repo: string, enabled = true) {
  return useQuery<RepoGates>({
    queryKey: ['repos', 'gates', repo],
    queryFn: () => getRepoGates(repo),
    enabled,
    staleTime: 10_000,
  });
}

/** Shared by the Approvals page and the sidebar badge — polls at the app default (30s). */
export function useApprovals() {
  return useQuery<ApprovalsResponse>({
    queryKey: ['approvals'],
    queryFn: getApprovals,
  });
}

export function useApprove() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: ApproveRequest) => postApprove(body),
    onSuccess: (result) => {
      // Approval auto-advances the repo phase — refresh everything phase-shaped.
      queryClient.invalidateQueries({ queryKey: ['approvals'] });
      queryClient.invalidateQueries({ queryKey: ['repos'] });
      const advancedPhase =
        result.advanced && typeof result.advanced === 'object' && typeof result.advanced.phase === 'string'
          ? result.advanced.phase
          : null;
      showToast(
        advancedPhase ? `Approved — phase advanced to ${advancedPhase}` : 'Approved — phase advanced',
        'success',
      );
    },
    // 409 approve_refused is toasted by the global MutationCache handler,
    // which already renders the parsed FastAPI detail.
  });
}
