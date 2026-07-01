import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { getConfig, reloadConfig, updateConfig } from '../lib/api';
import type { ConfigData } from '../lib/types';
import { showToast } from '../components/Toast';

const CONFIG_KEY = ['config'];

export function useConfig() {
  return useQuery<ConfigData>({
    queryKey: CONFIG_KEY,
    queryFn: getConfig,
    staleTime: 60_000,
  });
}

export function useUpdateConfig() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: updateConfig,
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: CONFIG_KEY });
      showToast(result.message || 'Configuration saved', 'success');
    },
  });
}

export function useReloadConfig() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: reloadConfig,
    onSuccess: (result) => {
      queryClient.invalidateQueries();
      showToast(result.message || 'Configuration reloaded', 'success');
    },
  });
}
