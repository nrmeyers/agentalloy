import { AlertTriangle } from 'lucide-react';
import { Card } from './Card';
import { Button } from './Button';

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <Card>
      <div className="text-center py-6">
        <AlertTriangle className="h-6 w-6 text-error mx-auto mb-2" />
        <p className="text-sm text-[var(--text-secondary)]">
          Failed to load: {message}
        </p>
        {onRetry && (
          <Button variant="outline" size="sm" onClick={onRetry} className="mt-3">
            Retry
          </Button>
        )}
      </div>
    </Card>
  );
}
