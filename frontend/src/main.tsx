import React from 'react';
import ReactDOM from 'react-dom/client';
import { MutationCache, QueryCache, QueryClient, QueryClientProvider } from '@tanstack/react-query';
import App from './App';
import { showToast } from './components/Toast';
import { ApiError } from './lib/api';
import './index.css';

const queryClient = new QueryClient({
  queryCache: new QueryCache({
    onError: (error, query) => {
      // Queries can opt out of toasting expected statuses (e.g. override 404
      // means "not overridable" and is rendered inline, not an error).
      const expected = query.meta?.expectedStatuses;
      if (
        Array.isArray(expected) &&
        error instanceof ApiError &&
        expected.includes(error.status)
      ) {
        return;
      }
      showToast(error instanceof Error ? error.message : 'Request failed', 'error');
    },
  }),
  mutationCache: new MutationCache({
    onError: (error) =>
      showToast(error instanceof Error ? error.message : 'Request failed', 'error'),
  }),
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      refetchInterval: 30_000, // polling — no websockets
      refetchOnWindowFocus: true,
      retry: 1,
    },
  },
});

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>
  </React.StrictMode>,
);
