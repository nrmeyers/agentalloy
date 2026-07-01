import { useMutation } from '@tanstack/react-query';
import { evaluateSignal, postCompose, postRetrieve } from '../lib/api';
import type { RetrieveRequest, SignalEvaluateRequest } from '../lib/types';

export function useRetrieve() {
  return useMutation({
    mutationFn: (body: RetrieveRequest) => postRetrieve(body),
  });
}

export function useCompose() {
  return useMutation({
    mutationFn: (body: RetrieveRequest) => postCompose(body),
  });
}

export function useEvaluateSignal() {
  return useMutation({
    mutationFn: (body: SignalEvaluateRequest) => evaluateSignal(body),
  });
}
