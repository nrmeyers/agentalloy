import { Card } from './Card';

export function ErrorState({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <Card>
      <div className="text-center py-6">
        <p className="text-sm text-red-600">Failed to load: {message}</p>
        {onRetry && (
          <button
            onClick={onRetry}
            className="mt-3 px-4 py-2 bg-gray-100 text-gray-700 rounded-md text-sm hover:bg-gray-200"
          >
            Retry
          </button>
        )}
      </div>
    </Card>
  );
}
