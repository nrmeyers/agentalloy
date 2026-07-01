import { Card } from './Card';

export function EmptyState({
  title,
  hint,
  icon = '📊',
}: {
  title: string;
  hint?: string;
  icon?: string;
}) {
  return (
    <Card>
      <div className="text-center py-8 text-gray-500">
        <div className="text-2xl mb-2">{icon}</div>
        <p>{title}</p>
        {hint && <p className="text-sm mt-1">{hint}</p>}
      </div>
    </Card>
  );
}
