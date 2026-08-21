import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterEach, vi } from 'vitest';

// Unmount after each test so component state never leaks between cases.
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});
