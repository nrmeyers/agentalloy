import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  getPhase,
  postPhaseAdvance,
  deletePhase,
  getArtifacts,
  putArtifact,
  getResume,
  deleteRepoState,
} from '../lib/stateApi';
import type {
  PhaseReadResponse,
  PhaseAdvanceRequest,
  Artifact,
  ResumeData,
} from '../lib/stateApi';
import { showToast } from '../components/Toast';

export function usePhase(repo: string | null) {
  return useQuery<PhaseReadResponse>({
    queryKey: ['state', 'phase', repo],
    queryFn: () => getPhase(repo!),
    enabled: !!repo,
    staleTime: 5_000,
  });
}

export function usePhaseAdvance() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ repo, data }: { repo: string; data: PhaseAdvanceRequest }) =>
      postPhaseAdvance(repo, data),
    onSuccess: (_, { repo }) => {
      showToast('Phase advanced', 'success');
      qc.invalidateQueries({ queryKey: ['state', 'phase', repo] });
      qc.invalidateQueries({ queryKey: ['repos'] });
    },
    onError: (err: Error) => {
      showToast(`Phase advance failed: ${err.message}`, 'error');
    },
  });
}

export function usePhaseClear() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (repo: string) => deletePhase(repo),
    onSuccess: (_, repo) => {
      showToast('Phase cleared', 'info');
      qc.invalidateQueries({ queryKey: ['state', 'phase', repo] });
      qc.invalidateQueries({ queryKey: ['repos'] });
    },
    onError: (err: Error) => {
      showToast(`Phase clear failed: ${err.message}`, 'error');
    },
  });
}

export function useArtifacts(repo: string | null) {
  return useQuery<Artifact[]>({
    queryKey: ['state', 'artifacts', repo],
    queryFn: () => getArtifacts(repo!),
    enabled: !!repo,
  });
}

export function useArtifactSave() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({
      repo,
      phase,
      slug,
      name,
      body,
    }: {
      repo: string;
      phase: string;
      slug: string;
      name: string;
      body: string;
    }) => putArtifact(repo, phase, slug, name, body),
    onSuccess: (_, { repo }) => {
      showToast('Artifact saved', 'success');
      qc.invalidateQueries({ queryKey: ['state', 'artifacts', repo] });
    },
    onError: (err: Error) => {
      showToast(`Save failed: ${err.message}`, 'error');
    },
  });
}

export function useResume(repo: string | null) {
  return useQuery<ResumeData>({
    queryKey: ['state', 'resume', repo],
    queryFn: () => getResume(repo!),
    enabled: !!repo,
  });
}

export function useRepoStateReset() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (repo: string) => deleteRepoState(repo),
    onSuccess: (_, repo) => {
      showToast('Repo state reset', 'info');
      qc.invalidateQueries({ queryKey: ['state', 'phase', repo] });
      qc.invalidateQueries({ queryKey: ['state', 'artifacts', repo] });
      qc.invalidateQueries({ queryKey: ['repos'] });
    },
    onError: (err: Error) => {
      showToast(`Reset failed: ${err.message}`, 'error');
    },
  });
}
