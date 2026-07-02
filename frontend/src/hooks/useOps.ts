import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { getDoctor, getPacks, getProfiles, getReembedStatus, postReembed, resolveProfile } from '../lib/api';
import type {
  DoctorResponse,
  PacksResponse,
  ProfilesResponse,
  ReembedStatus,
} from '../lib/types';
import { showToast } from '../components/Toast';

/**
 * Doctor runs its checks server-side on every GET — fetch on visit, but do not
 * poll every 30s like passive queries. Re-runs go through refetch().
 */
export function useDoctor() {
  return useQuery<DoctorResponse>({
    queryKey: ['doctor'],
    queryFn: getDoctor,
    staleTime: 0,
    refetchInterval: false,
    refetchOnWindowFocus: false,
  });
}

export function usePacks() {
  return useQuery<PacksResponse>({
    queryKey: ['packs'],
    queryFn: getPacks,
  });
}

export function useReembedStatus() {
  return useQuery<ReembedStatus>({
    queryKey: ['reembed', 'status'],
    queryFn: getReembedStatus,
  });
}

export function useReembed() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (dryRun: boolean) => postReembed(dryRun),
    onSuccess: (result) => {
      if (result.dry_run) {
        showToast(`Dry run — would embed ${result.would_embed ?? 'unknown'} fragments`, 'info');
        return;
      }
      queryClient.invalidateQueries({ queryKey: ['reembed', 'status'] });
      queryClient.invalidateQueries({ queryKey: ['diagnostics', 'corpus'] });
      const ok = result.exit_code === 0;
      showToast(
        ok ? 'Reembed finished (exit code 0)' : `Reembed finished with exit code ${result.exit_code ?? 'unknown'}`,
        ok ? 'success' : 'error',
      );
    },
  });
}

export function useProfiles() {
  return useQuery<ProfilesResponse>({
    queryKey: ['profiles'],
    queryFn: getProfiles,
  });
}

export function useResolveProfile() {
  return useMutation({
    mutationFn: (repo: string) => resolveProfile(repo),
  });
}
