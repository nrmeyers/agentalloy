import type { ReactNode } from 'react';

export function FormField({
  label,
  error,
  hint,
  children,
}: {
  label: string;
  error?: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <div className="mb-4">
      <label className="block text-sm font-medium text-[var(--text-primary)] mb-1">
        {label}
      </label>
      {children}
      {hint && (
        <p className="mt-0.5 text-xs text-[var(--text-tertiary)]">{hint}</p>
      )}
      {error && <p className="mt-1 text-sm text-error">{error}</p>}
    </div>
  );
}

export const inputClass =
  'w-full px-3 py-2 border border-[var(--border-primary)] rounded-md text-sm bg-[var(--bg-primary)] text-[var(--text-primary)] placeholder:text-[var(--text-tertiary)] focus:outline-none focus:ring-2 focus:ring-brand-500 focus:border-transparent transition-colors';

export const readOnlyInputClass =
  'w-full px-3 py-2 border border-[var(--border-primary)] rounded-md text-sm bg-[var(--bg-tertiary)] text-[var(--text-secondary)]';
