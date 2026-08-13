import { useSyncExternalStore } from 'react';
import { CheckCircle, XCircle, Info } from 'lucide-react';

export type ToastType = 'success' | 'error' | 'info';

interface ToastItem {
  id: number;
  message: string;
  type: ToastType;
}

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
  if (items.some((t) => t.message === message && t.type === type)) return;
  const id = nextId++;
  items = [...items, { id, message, type }];
  emit();
  setTimeout(() => {
    items = items.filter((t) => t.id !== id);
    emit();
  }, duration);
}

const typeConfig: Record<ToastType, { icon: typeof CheckCircle; color: string }> = {
  success: { icon: CheckCircle, color: 'border-success/30 bg-success/10 text-success' },
  error: { icon: XCircle, color: 'border-error/30 bg-error/10 text-error' },
  info: { icon: Info, color: 'border-brand-500/30 bg-brand-500/10 text-brand-500' },
};

export function ToastContainer() {
  const queue = useSyncExternalStore(subscribe, getSnapshot);
  return (
    <div className="fixed bottom-4 right-4 z-50 space-y-2">
      {queue.map((toast) => {
        const config = typeConfig[toast.type];
        const Icon = config.icon;
        return (
          <div
            key={toast.id}
            className={`flex items-center gap-2 max-w-sm px-4 py-3 rounded-lg border shadow-lg text-sm animate-slide-up ${config.color}`}
          >
            <Icon className="h-4 w-4 shrink-0" />
            <span>{toast.message}</span>
          </div>
        );
      })}
    </div>
  );
}
