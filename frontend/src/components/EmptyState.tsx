import type { ReactNode } from 'react';
import { Card } from './Card';

export function EmptyState({
  title,
  hint,
  icon,
}: {
  title: string;
  hint?: string;
  icon?: ReactNode;
}) {
  return (
    <Card>
      <div className="text-center py-8">
        {icon && <div className="text-2xl mb-2">{icon}</div>}
        <p className="text-sm text-[var(--text-secondary)]">{title}</p>
        {hint && <p className="text-xs text-[var(--text-tertiary)] mt-1">{hint}</p>}
      </div>
    </Card>
  );
}
