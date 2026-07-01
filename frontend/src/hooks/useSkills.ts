import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  ApiError,
  deleteSkillOverride,
  getSkillDetail,
  getSkillOverride,
  getSkills,
  getSkillVersions,
  putSkillOverride,
} from '../lib/api';
import type {
  OverrideUpdate,
  SkillDetail,
  SkillOverride,
  SkillsListParams,
  SkillsListResponse,
  SkillVersionsResponse,
} from '../lib/types';
import { showToast } from '../components/Toast';

export function useSkillsList(params: SkillsListParams) {
  return useQuery<SkillsListResponse>({
    queryKey: ['skills', params],
    queryFn: () => getSkills(params),
    staleTime: 10_000,
    placeholderData: keepPreviousData,
  });
}

export function useSkillDetail(skillId: string) {
  return useQuery<SkillDetail>({
    queryKey: ['skill', skillId],
    queryFn: () => getSkillDetail(skillId),
  });
}

export function useSkillVersions(skillId: string, enabled = true) {
  return useQuery<SkillVersionsResponse>({
    queryKey: ['skill', skillId, 'versions'],
    queryFn: () => getSkillVersions(skillId),
    enabled,
  });
}

/** 404 means "not overridable" — surface it to the caller, never retry or toast it. */
export function useSkillOverride(skillId: string, enabled = true) {
  return useQuery<SkillOverride, Error>({
    queryKey: ['skill', skillId, 'override'],
    queryFn: () => getSkillOverride(skillId),
    enabled,
    retry: (failureCount, error) => {
      if (error instanceof ApiError && error.status === 404) return false;
      return failureCount < 1;
    },
    refetchInterval: false,
    // 404 = "not overridable" — rendered inline, so don't toast it.
    meta: { expectedStatuses: [404] },
  });
}

export function useSaveOverride(skillId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: OverrideUpdate) => putSkillOverride(skillId, body),
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: ['skill', skillId] });
      queryClient.invalidateQueries({ queryKey: ['skills'] });
      showToast(result.message || 'Override saved', 'success');
    },
  });
}

export function useDeleteOverride(skillId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (layer: string) => deleteSkillOverride(skillId, layer),
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: ['skill', skillId] });
      queryClient.invalidateQueries({ queryKey: ['skills'] });
      showToast(result.message || 'Override removed — back to shipped default', 'success');
    },
  });
}
