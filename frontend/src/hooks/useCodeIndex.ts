import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  getCodeRepos,
  getCodeJobs,
  getCodeRepoStats,
  postCodeReindex,
  postCodeWatch,
  deleteCodeRepo,
  postCodeJobCancel,
} from '../lib/codeIndexApi';
import type { CodeIndexRepo, CodeIndexJob, CodeIndexRepoStats } from '../lib/types';
import { showToast } from '../components/Toast';

export function useCodeRepos() {
  return useQuery<CodeIndexRepo[]>({
    queryKey: ['code', 'repos'],
    queryFn: getCodeRepos,
    staleTime: 10_000,
  });
}

export function useCodeJobs(slug?: string, limit = 50) {
  return useQuery<CodeIndexJob[]>({
    queryKey: ['code', 'jobs', slug, limit],
    queryFn: () => getCodeJobs(slug, limit),
    staleTime: 5_000,
    refetchInterval: (query) => {
      const jobs = query.state.data;
      if (!jobs) return false;
      return jobs.some((j) => j.state === 'running' || j.state === 'queued') ? 3_000 : false;
    },
  });
}

export function useCodeRepoStats(slug: string | null) {
  return useQuery<CodeIndexRepoStats>({
    queryKey: ['code', 'repos', slug, 'stats'],
    queryFn: () => getCodeRepoStats(slug!),
    enabled: !!slug,
  });
}

export function useCodeReindex() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (slug: string) => postCodeReindex(slug),
    onSuccess: (_, slug) => {
      showToast(`Reindex started for ${slug}`, 'success');
      qc.invalidateQueries({ queryKey: ['code'] });
    },
    onError: (err: Error) => {
      showToast(`Reindex failed: ${err.message}`, 'error');
    },
  });
}

export function useCodeWatchToggle() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ slug, enabled }: { slug: string; enabled: boolean }) =>
      postCodeWatch(slug, enabled),
    onSuccess: (_, { slug, enabled }) => {
      showToast(`${enabled ? 'Watching' : 'Unwatched'} ${slug}`, 'info');
      qc.invalidateQueries({ queryKey: ['code', 'repos'] });
    },
  });
}

export function useCodeRepoRemove() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (slug: string) => deleteCodeRepo(slug),
    onSuccess: (_, slug) => {
      showToast(`Removed ${slug}`, 'success');
      qc.invalidateQueries({ queryKey: ['code'] });
    },
    onError: (err: Error) => {
      showToast(`Remove failed: ${err.message}`, 'error');
    },
  });
}

export function useCodeJobCancel() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (jobId: string) => postCodeJobCancel(jobId),
    onSuccess: () => {
      showToast('Cancel requested', 'info');
      qc.invalidateQueries({ queryKey: ['code', 'jobs'] });
    },
  });
}
