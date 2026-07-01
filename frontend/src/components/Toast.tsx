import { useSyncExternalStore } from 'react';

export type ToastType = 'success' | 'error' | 'info';

interface ToastItem {
  id: number;
  message: string;
  type: ToastType;
}

// Module-level store so non-React code (React Query caches, api layer)
// can raise toasts without a context handle.
let nextId = 1;
let items: readonly ToastItem[] = [];
const listeners = new Set<() => void>();

function emit(): void {
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

function getSnapshot(): readonly ToastItem[] {
  return items;
}

export function showToast(message: string, type: ToastType = 'info', duration = 4000): void {
  // Dedupe identical visible toasts (30s polling can re-fire errors).
  if (items.some((t) => t.message === message && t.type === type)) return;
  const id = nextId++;
  items = [...items, { id, message, type }];
  emit();
  setTimeout(() => {
    items = items.filter((t) => t.id !== id);
    emit();
  }, duration);
}

const typeStyles: Record<ToastType, string> = {
  success: 'bg-green-600',
  error: 'bg-red-600',
  info: 'bg-blue-600',
};

export function ToastContainer() {
  const queue = useSyncExternalStore(subscribe, getSnapshot);
  return (
    <div className="fixed top-4 right-4 z-50 space-y-2">
      {queue.map((toast) => (
        <div
          key={toast.id}
          className={`max-w-sm px-4 py-2 rounded-md shadow text-sm text-white ${typeStyles[toast.type]}`}
        >
          {toast.message}
        </div>
      ))}
    </div>
  );
}
