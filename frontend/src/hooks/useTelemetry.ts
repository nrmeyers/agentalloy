import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { getCoverage, getSavings, getTraces } from '../lib/api';
import type { CoverageResponse, SavingsResponse, TracesParams, TracesResponse } from '../lib/types';

export function useTraces(params: TracesParams) {
  return useQuery<TracesResponse>({
    queryKey: ['traces', params],
    queryFn: () => getTraces(params),
    staleTime: 10_000,
    placeholderData: keepPreviousData,
  });
}

export function useSavings(repo?: string) {
  return useQuery<SavingsResponse>({
    queryKey: ['savings', repo ?? ''],
    queryFn: () => getSavings(repo),
  });
}

export function useCoverage(repo?: string) {
  return useQuery<CoverageResponse>({
    queryKey: ['coverage', repo ?? ''],
    queryFn: () => getCoverage(repo),
  });
}
