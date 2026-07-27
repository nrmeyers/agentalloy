import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  archiveContract,
  getContract,
  getContracts,
  patchContract,
} from '../lib/api';
import type {
  Contract,
  ContractPatchRequest,
  ContractsListParams,
  ContractsListResponse,
} from '../lib/types';
import { showToast } from '../components/Toast';

export function useContractsList(params: ContractsListParams = {}) {
  return useQuery<ContractsListResponse>({
    queryKey: ['contracts', params],
    queryFn: () => getContracts(params),
    staleTime: 10_000,
    placeholderData: keepPreviousData,
  });
}

export function useContract(contractId: string, enabled = true) {
  return useQuery<Contract>({
    queryKey: ['contract', contractId],
    queryFn: () => getContract(contractId),
    enabled,
  });
}

export function usePatchContract(contractId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: ContractPatchRequest) => patchContract(contractId, body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['contract', contractId] });
      queryClient.invalidateQueries({ queryKey: ['contracts'] });
      showToast('Contract updated', 'success');
    },
  });
}

export function useArchiveContract(contractId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => archiveContract(contractId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['contract', contractId] });
      queryClient.invalidateQueries({ queryKey: ['contracts'] });
      showToast('Contract archived', 'success');
    },
  });
}
