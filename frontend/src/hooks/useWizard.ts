import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  ApiError,
  getWizardPack,
  postWizardInstall,
  postWizardScaffold,
  postWizardValidate,
  putWizardFile,
} from '../lib/api';
import type {
  WizardFileWriteRequest,
  WizardInstallRequest,
  WizardPackContents,
  WizardScaffoldRequest,
} from '../lib/types';
import { showToast } from '../components/Toast';

/**
 * Pack contents for a repo+pack pair. exists:false is a normal answer (used by
 * the scaffold step to offer "Resume draft"), so it never polls and never errors
 * on absence.
 */
export function useWizardPack(repo: string, pack: string, enabled = true) {
  return useQuery<WizardPackContents>({
    queryKey: ['wizard', 'pack', repo, pack],
    queryFn: () => getWizardPack(repo, pack),
    enabled: enabled && repo.trim() !== '' && pack.trim() !== '',
    refetchInterval: false,
    staleTime: 10_000,
    retry: (failureCount, error) => {
      if (error instanceof ApiError && (error.status === 400 || error.status === 404)) return false;
      return failureCount < 1;
    },
    // 400 = half-typed repo path / pack name during the scaffold step — an
    // expected answer while probing for "Resume draft", not a toastable error.
    meta: { expectedStatuses: [400, 404] },
  });
}

export function useWizardScaffold() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: WizardScaffoldRequest) => postWizardScaffold(body),
    onSuccess: (_result, vars) => {
      queryClient.invalidateQueries({ queryKey: ['wizard', 'pack', vars.repo, vars.pack] });
      showToast(`Scaffolded pack "${vars.pack}"`, 'success');
    },
    // 400 already-exists / invalid-name is toasted by the global MutationCache.
  });
}

export function useWizardSaveFile() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: WizardFileWriteRequest) => putWizardFile(body),
    onSuccess: (_result, vars) => {
      queryClient.invalidateQueries({ queryKey: ['wizard', 'pack', vars.repo, vars.pack] });
      showToast(`Saved ${vars.file}`, 'success');
    },
    // invalid_yaml 400s are rendered inline by the draft editor (and toasted globally).
  });
}

export function useWizardValidate() {
  return useMutation({
    mutationFn: ({ repo, pack }: { repo: string; pack: string }) =>
      postWizardValidate(repo, pack),
    // No success toast — the page renders the pass/fail banner.
  });
}

export function useWizardInstall() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: WizardInstallRequest) => postWizardInstall(body),
    onSuccess: (result) => {
      // A new skill is live: refresh everything that displays corpus/pack/phase state.
      queryClient.invalidateQueries({ queryKey: ['skills'] });
      queryClient.invalidateQueries({ queryKey: ['packs'] });
      queryClient.invalidateQueries({ queryKey: ['repos'] });
      queryClient.invalidateQueries({ queryKey: ['approvals'] });
      const ingested = result.install?.skills_ingested;
      showToast(
        typeof ingested === 'number'
          ? `Installed — ${ingested} skill${ingested === 1 ? '' : 's'} ingested`
          : 'Pack installed',
        'success',
      );
    },
    // 409 approve_refused carries the parsed FastAPI detail via the global toast.
  });
}
