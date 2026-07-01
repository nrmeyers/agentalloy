import { useQuery } from '@tanstack/react-query';
import { getCorpusDiagnostics, getHealth, getReadiness, getRuntimeDiagnostics } from '../lib/api';
import type {
  CorpusDiagnostics,
  HealthResponse,
  ReadinessResponse,
  RuntimeDiagnostics,
} from '../lib/types';

export function useRuntimeDiagnostics() {
  return useQuery<RuntimeDiagnostics>({
    queryKey: ['diagnostics', 'runtime'],
    queryFn: getRuntimeDiagnostics,
  });
}

export function useCorpus() {
  return useQuery<CorpusDiagnostics>({
    queryKey: ['diagnostics', 'corpus'],
    queryFn: getCorpusDiagnostics,
  });
}

export function useHealth() {
  return useQuery<HealthResponse>({
    queryKey: ['health'],
    queryFn: getHealth,
    staleTime: 5_000,
  });
}

export function useReadiness() {
  return useQuery<ReadinessResponse>({
    queryKey: ['readiness'],
    queryFn: getReadiness,
    staleTime: 5_000,
  });
}
