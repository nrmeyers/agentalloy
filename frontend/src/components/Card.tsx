import type { ReactNode } from 'react';

interface CardProps {
  children: ReactNode;
  className?: string;
  padding?: boolean;
  hover?: boolean;
  onClick?: () => void;
}

export function Card({
  children,
  className = '',
  padding = true,
  hover = false,
  onClick,
}: CardProps) {
  return (
    <div
      role={onClick ? 'button' : undefined}
      tabIndex={onClick ? 0 : undefined}
      onClick={onClick}
      onKeyDown={
        onClick
          ? (e) => {
              if (e.key === 'Enter' || e.key === ' ') {
                e.preventDefault();
                onClick();
              }
            }
          : undefined
      }
      className={`rounded-lg border border-[var(--border-primary)] bg-[var(--bg-elevated)]
        ${padding ? 'p-4' : ''}
        ${hover ? 'transition-colors hover:border-[var(--text-tertiary)] cursor-pointer' : ''}
        ${onClick ? 'cursor-pointer' : ''}
        ${className}`}
    >
      {children}
    </div>
  );
}

interface StatCardProps {
  label: string;
  value: ReactNode;
  description?: string;
  trend?: 'up' | 'down' | 'neutral';
  icon?: ReactNode;
}

export function StatCard({ label, value, description, trend, icon }: StatCardProps) {
  const trendColor =
    trend === 'up'
      ? 'text-success'
      : trend === 'down'
        ? 'text-error'
        : 'text-[var(--text-tertiary)]';

  return (
    <Card>
      <div className="flex items-start justify-between">
        <div className="min-w-0">
          <p className="text-xs font-medium text-[var(--text-secondary)] uppercase tracking-wider">
            {label}
          </p>
          <p className="mt-1 text-2xl font-semibold text-[var(--text-primary)] tabular-nums tracking-tight">
            {value}
          </p>
          {description && (
            <p className={`mt-1 text-xs ${trendColor}`}>{description}</p>
          )}
        </div>
        {icon && (
          <div className="text-[var(--text-tertiary)] shrink-0">{icon}</div>
        )}
      </div>
    </Card>
  );
}
